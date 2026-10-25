#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_worker_cookie.py — 自动化刷新 today.ai cookie 到 CF Worker（工作流版）
============================================================================
与手动挡 todayai_cookie.py 的区别：
  - 默认全自动：IMAP 抓验证码即可无人值守（适合 cron / n8n / CI）
  - 输出结构化 JSON（--json）供下游工作流解析
  - 稳定退出码：0=成功 1=失败 2=参数错误
  - 核心动作：OTP 登录 → 提取 cookie → 写入 CF Worker secret →（可选）网关验证

用法示例：
  # 全自动（配好 IMAP 后 cron 每月跑一次即可，无需人工）
  python3 update_worker_cookie.py --email YOUR_EMAIL@example.com \
      --imap-host imap.gmail.com --imap-user YOUR_EMAIL@example.com \
      --imap-pass "app_password" --verify

  # 半自动（手动给验证码；--otp 给码后不再发新邮件）
  python3 update_worker_cookie.py --email YOUR_EMAIL@example.com --otp 123456

  # 工作流调用：JSON 输出 + 存 cookie + 验证
  python3 update_worker_cookie.py --email YOUR_EMAIL@example.com \
      --otp 123456 --json --save .env --verify

  # 演练（不实际写远程）
  python3 update_worker_cookie.py --email YOUR_EMAIL@example.com --otp 123456 --dry-run

环境变量（均可被 --xxx 覆盖）:
  TODAYAI_EMAIL            账号邮箱（必填）
  CLOUDFLARE_API_TOKEN     CF API Token（必填，写 Worker secret 用）
  CLOUDFLARE_ACCOUNT_ID    CF Account ID（必填）
  TODAYAI_IMAP_HOST        IMAP 服务器（自动抓码，可选但推荐）
  TODAYAI_IMAP_USER        IMAP 用户（通常同邮箱）
  TODAYAI_IMAP_PASS        IMAP 密码/应用专用密码
  TODAYAI_GATEWAY_URL      网关地址（--verify 用，默认 https://<你的子域>.workers.dev/v1，替换成你自己的）
  GATEWAY_API_KEY          网关密钥（--verify 用）

退出码: 0 成功 / 1 业务失败 / 2 参数错误
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
BASE = "https://today.ai"
DEFAULT_WORKER = "todayai-proxy"
DEFAULT_GATEWAY_URL = os.environ.get("TODAYAI_GATEWAY_URL", f"https://{DEFAULT_WORKER}.YOUR-SUBDOMAIN.workers.dev/v1")
OTP_RE = re.compile(r"\b(\d{6})\b")
COOKIE_PREFIX = "__Secure-better-auth.session_token="

# 退出码
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


# ---------- 公共：HTTP ----------

def api(url: str, method: str = "GET", body=None, headers=None, timeout: float = 30, want_headers: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            parsed = json.loads(raw) if raw else None
            if want_headers:
                return r.status, parsed, r.headers
            return r.status, parsed
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if want_headers:
            return e.code, {"error": detail}, e.headers
        return e.code, {"error": detail}


# ---------- 1) OTP ----------

def send_otp(email: str) -> None:
    code, data = api(f"{BASE}/api/auth/email-otp/send-verification-otp",
                     "POST", {"email": email, "type": "sign-in"})
    if code != 200 or not (data or {}).get("success"):
        raise RuntimeError(f"发送验证码失败: HTTP {code} {data}")


def fetch_otp_imap(host: str, user: str, password: str, timeout_s: int = 180) -> str:
    """从 IMAP 收件箱自动抓取 Today 验证码（轮询直到超时）。"""
    import imaplib
    import email as emaillib
    from email.header import decode_header

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(host)
            M.login(user, password)
            M.select("INBOX")
            _, nums = M.search(None, "ALL")
            ids = (nums[0] or b"").split()[-10:]
            for num in reversed(ids):
                _, msg_data = M.fetch(num, "(RFC822)")
                msg = emaillib.message_from_bytes(msg_data[0][1])
                subject = str(decode_header(msg.get("Subject", ""))[0][0])
                sender = str(msg.get("From", ""))
                if "today" not in (subject + sender).lower():
                    continue
                parts = []
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            parts.append(part.get_payload(decode=True).decode("utf-8", "replace"))
                else:
                    parts.append(msg.get_payload(decode=True).decode("utf-8", "replace"))
                text = " ".join(parts)
                m = OTP_RE.search(text)
                if m:
                    M.logout()
                    return m.group(1)
            M.logout()
        except Exception as e:  # noqa: BLE001
            # IMAP 网络/登录错误先重试；日志走 stderr 不污染 JSON
            print(f"⚠️ IMAP 读取中... ({e})", file=sys.stderr)
        time.sleep(5)
    raise RuntimeError(f"等待验证码超时（{timeout_s}s）")


def sign_in(email: str, otp: str) -> dict:
    code, data, resp_headers = api(f"{BASE}/api/auth/sign-in/email-otp",
                                   "POST", {"email": email, "otp": otp},
                                   want_headers=True)
    if code != 200:
        raise RuntimeError(f"登录失败: HTTP {code} {data}")
    if not data.get("token"):
        raise RuntimeError(f"登录响应缺少 token: {data}")

    # 关键：body 的 token 是短 ID（无签名），Set-Cookie 里的才是完整可用 cookie 值。
    # 完整格式: __Secure-better-auth.session_token=<id>.<signature>
    session_token = ""
    for sc in (resp_headers.get_all("Set-Cookie") or []):
        if sc.startswith(COOKIE_PREFIX):
            session_token = sc.split(";", 1)[0]
            if session_token.startswith(COOKIE_PREFIX):
                session_token = session_token[len(COOKIE_PREFIX):]
            break
    if not session_token:
        # 兜底：拿不到响应头时退回 body token（可能换票失败，但至少给出值）
        session_token = data["token"]

    return {"name": (data.get("user") or {}).get("name", "?"),
            "token": session_token,
            "cookie": COOKIE_PREFIX + session_token,
            "expires_est": time.strftime("%Y-%m-%d", time.localtime(time.time() + 59 * 86400))}


# ---------- 2) 写入 CF Worker secret ----------

def update_worker_secret(cf_token: str, account_id: str, worker: str, cookie: str, dry_run: bool = False) -> None:
    url = (f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
           f"/workers/scripts/{worker}/secrets")
    payload = {"name": "TODAY_SESSION_COOKIE", "text": cookie, "type": "secret_text"}
    if dry_run:
        return
    code, data = api(url, "PUT", payload, headers={"Authorization": f"Bearer {cf_token}"})
    if code not in (200, 201):
        raise RuntimeError(f"写入 Worker secret 失败: HTTP {code} {data}")


# ---------- 3) 验证 ----------

def verify_gateway(gateway_url: str, api_key: str) -> str:
    url = f"{gateway_url}/chat/completions"
    body = {"model": "today-fast", "messages": [{"role": "user", "content": "ping, 只回 pong"}], "stream": False}
    code, data = api(url, "POST", body, headers={"Authorization": f"Bearer {api_key}"}, timeout=120)
    if code != 200:
        raise RuntimeError(f"网关验证失败: HTTP {code} {data}")
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")


# ---------- 主流程 ----------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="自动化刷新 today.ai cookie 到 CF Worker（工作流版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # 账号与验证码
    p.add_argument("--email", default=os.environ.get("TODAYAI_EMAIL"), help="today.ai 邮箱（必填）")
    p.add_argument("--otp", help="验证码（给码则不重发邮件；缺省时：有 IMAP 自动抓，否则交互输入）")
    p.add_argument("--imap-host", default=os.environ.get("TODAYAI_IMAP_HOST"), help="IMAP 服务器（自动抓码）")
    p.add_argument("--imap-user", default=os.environ.get("TODAYAI_IMAP_USER"), help="IMAP 用户（通常同邮箱）")
    p.add_argument("--imap-pass", default=os.environ.get("TODAYAI_IMAP_PASS"), help="IMAP 密码/应用专用密码")
    # CF
    p.add_argument("--cf-token", default=os.environ.get("CLOUDFLARE_API_TOKEN"), help="CF API Token（必填）")
    p.add_argument("--cf-account", default=os.environ.get("CLOUDFLARE_ACCOUNT_ID"), help="CF Account ID（必填）")
    p.add_argument("--worker", default=DEFAULT_WORKER, help=f"Worker 名称（默认 {DEFAULT_WORKER}）")
    # 行为
    p.add_argument("--non-interactive", action="store_true", help="非交互：无 OTP 且无 IMAP 时直接报错，不等待输入（cron/n8n 用）")
    p.add_argument("--dry-run", action="store_true", help="演练：不实际写入远程")
    p.add_argument("--verify", action="store_true", help="更新后调用网关验证")
    p.add_argument("--save", help="把 cookie 以 TODAYAI_SESSION_COOKIE=... 追加写入文件（如 .env）")
    p.add_argument("--json", action="store_true", help="输出结构化 JSON（工作流友好）")
    p.add_argument("--gateway-url", default=os.environ.get("TODAYAI_GATEWAY_URL", DEFAULT_GATEWAY_URL))
    p.add_argument("--api-key", default=os.environ.get("GATEWAY_API_KEY"), help="网关密钥（--verify 用）")
    p.add_argument("--imap-timeout", type=int, default=180, help="IMAP 抓码超时秒数（默认 180）")
    args = p.parse_args(argv)

    # 结果收集器（--json 时输出）
    result = {"ok": False, "step": "", "error": None, "user": None,
              "cookie_masked": None, "expires_est": None,
              "worker_updated": False, "verify_reply": None, "dry_run": args.dry_run}

    def fail(msg: str, step: str, exit_code: int = EXIT_FAIL) -> int:
        result.update(ok=False, step=step, error=msg)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"❌ [{step}] {msg}", file=sys.stderr)
        return exit_code

    # ---- 参数校验 ----
    if not args.email:
        return fail("需要 --email 或 TODAYAI_EMAIL", "参数", EXIT_USAGE)
    if not args.dry_run and not (args.cf_token and args.cf_account):
        return fail("需要 --cf-token/--cf-account 或对应环境变量（--dry-run 可跳过）", "参数", EXIT_USAGE)
    if args.imap_host and not args.imap_pass:
        return fail("给了 --imap-host 还需要 --imap-pass", "参数", EXIT_USAGE)

    # ---- 第 1 步：拿验证码 ----
    otp = args.otp
    if not otp:
        if args.imap_host:
            result["step"] = "发验证码+IMAP抓码"
            try:
                send_otp(args.email)
                print(f"📨 验证码已发送到 {args.email}，正在从 IMAP({args.imap_host}) 自动抓取..." if not args.json else "", file=sys.stderr)
                otp = fetch_otp_imap(args.imap_host, args.imap_user or args.email, args.imap_pass, args.imap_timeout)
                print(f"🔑 抓到验证码: {otp}" if not args.json else "", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                return fail(str(e), "验证码")
        elif args.non_interactive:
            return fail("非交互模式需要 --otp 或 --imap-*", "参数", EXIT_USAGE)
        else:
            result["step"] = "发验证码"
            try:
                send_otp(args.email)
            except Exception as e:  # noqa: BLE001
                return fail(str(e), "验证码")
            print(f"📨 验证码已发送到 {args.email}", file=sys.stderr)
            otp = input("🔢 请输入邮箱收到的验证码: ").strip()

    if not re.fullmatch(r"\d{6}", otp or ""):
        return fail("验证码格式应为 6 位数字", "验证码", EXIT_USAGE)

    # ---- 第 2 步：登录提取 ----
    result["step"] = "登录"
    try:
        info = sign_in(args.email, otp)
    except Exception as e:  # noqa: BLE001
        return fail(str(e), "登录")
    cookie = info["cookie"]
    result.update(user=info["name"], expires_est=info["expires_est"],
                  cookie_masked=cookie[:40] + "..." + f"({len(cookie)}字符)")
    print(f"👤 登录用户: {info['name']} | session 有效期至 {info['expires_est']}" if not args.json else "", file=sys.stderr)

    # ---- 第 3 步：保存到文件（可选） ----
    if args.save:
        with open(args.save, "a", encoding="utf-8") as f:
            f.write(f"TODAYAI_SESSION_COOKIE={cookie}\n")
        print(f"💾 已追加写入 {args.save}" if not args.json else "", file=sys.stderr)

    # ---- 第 4 步：写入 CF Worker ----
    result["step"] = "写Worker"
    try:
        update_worker_secret(args.cf_token, args.cf_account, args.worker, cookie, dry_run=args.dry_run)
        result["worker_updated"] = True
    except Exception as e:  # noqa: BLE001
        return fail(str(e), "写Worker")
    action = "演练(dry-run)" if args.dry_run else "更新成功"
    print(f"🛠 Worker '{args.worker}' {action}" if not args.json else "", file=sys.stderr)

    # ---- 第 5 步：网关验证（可选） ----
    if args.verify:
        if not args.api_key:
            return fail("--verify 需要 --api-key / GATEWAY_API_KEY", "验证", EXIT_USAGE)
        result["step"] = "验证"
        try:
            reply = verify_gateway(args.gateway_url, args.api_key)
            result["verify_reply"] = reply
            print(f"🧪 网关验证通过: {reply}" if not args.json else "", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            return fail(str(e), "验证")

    # ---- 完成 ----
    result["ok"] = True
    result["step"] = "完成"
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"✅ 完成：cookie 已刷新（有效期至 {info['expires_est']}）")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())