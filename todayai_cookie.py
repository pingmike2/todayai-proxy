#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
todayai_cookie.py — 提取 today.ai 账号 session cookie（手动挡）
=================================================================
定位：给人手动用。交互式提取 cookie 值（打印 + 可选存文件），
供你手动填 Workers 变量或任何其他地方。

要自动化/无人值守（IMAP 抓码 + 自动写 Worker secret + JSON 输出），
请用 update_worker_cookie.py（自动挡）。

流程: 发送 OTP 验证码 → 输入/自动抓取验证码 → 登录 → 提取 session cookie 值。

默认只提取 cookie 值（打印 + 可选存文件），供你手动填 Workers 变量、配 direct 模式或任何其他地方用。

可选扩展：
  --update-worker  提取后自动写入 CF Worker secret TODAY_SESSION_COOKIE
  --verify         提取后调用网关发条消息验证 cookie 可用

用法示例：
  # 1) 最常用：交互式输入验证码，提取 cookie 并保存到文件
  python3 todayai_cookie.py --email YOUR_EMAIL@example.com --save .env

  # 2) 验证码由参数给出（可用于脚本/CI）
  python3 todayai_cookie.py --email YOUR_EMAIL@example.com --otp 123456

  # 3) 自动从 Gmail/IMAP 抓验证码（需应用专用密码）
  python3 todayai_cookie.py --email YOUR_EMAIL@example.com \
      --imap-host imap.gmail.com --imap-user YOUR_EMAIL@example.com --imap-pass xxxx

  # 4) 提取后顺便更新 Worker 并验证
  python3 todayai_cookie.py --email YOUR_EMAIL@example.com --otp 123456 \
      --update-worker --verify

环境变量（均可被 --xxx 覆盖）:
  TODAYAI_EMAIL        账号邮箱
  CLOUDFLARE_API_TOKEN CF API Token（--update-worker 时需要）
  CLOUDFLARE_ACCOUNT_ID CF Account ID（--update-worker 时需要）
  GATEWAY_API_KEY      网关密钥（--verify 时需要）
  TODAYAI_GATEWAY_URL  网关地址（--verify 时需要）
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
OTP_RE = re.compile(r"\b(\d{6})\b")
COOKIE_PREFIX = "__Secure-better-auth.session_token="


# ---------- HTTP ----------

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


# ---------- 1) OTP 登录 ----------

def send_otp(email: str) -> None:
    code, data = api(f"{BASE}/api/auth/email-otp/send-verification-otp",
                     "POST", {"email": email, "type": "sign-in"})
    if code != 200 or not (data or {}).get("success"):
        raise RuntimeError(f"发送验证码失败: HTTP {code} {data}")
    print(f"📧 验证码已发送到 {email}，请查收")


def fetch_otp_imap(host: str, user: str, password: str, timeout_s: int = 120) -> str:
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
            print(f"  ⏳ IMAP 读取中... ({e})", file=sys.stderr)
        time.sleep(5)
    raise RuntimeError(f"等待验证码超时（{timeout_s}s），请换 --otp 手动输入")


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
        session_token = data["token"]

    return {"name": (data.get("user") or {}).get("name", "?"),
            "token": session_token,
            "cookie": COOKIE_PREFIX + session_token}


# ---------- 2) 可选: 写入 CF Worker secret ----------

def update_worker_secret(cf_token: str, account_id: str, worker: str, cookie: str) -> None:
    url = (f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
           f"/workers/scripts/{worker}/secrets")
    payload = {"name": "TODAY_SESSION_COOKIE", "text": cookie, "type": "secret_text"}
    code, data = api(url, "PUT", payload, headers={"Authorization": f"Bearer {cf_token}"})
    if code not in (200, 201):
        raise RuntimeError(f"写入 Worker secret 失败: HTTP {code} {data}")
    name = (data or {}).get("result", {}).get("name", "TODAY_SESSION_COOKIE")
    print(f"✅ Worker secret 更新成功: {name}（已生效，无需重新部署）")


# ---------- 3) 可选: 验证 ----------

def verify_gateway(gateway_url: str, api_key: str) -> None:
    url = f"{gateway_url}/chat/completions"
    body = {"model": "today-fast", "messages": [{"role": "user", "content": "ping，只回 pong"}], "stream": False}
    code, data = api(url, "POST", body, headers={"Authorization": f"Bearer {api_key}"}, timeout=120)
    if code != 200:
        raise RuntimeError(f"验证失败: HTTP {code} {data}")
    reply = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    print(f"✅ 网关验证通过: {reply}")


# ---------- 主流程 ----------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="提取 today.ai 账号 session cookie（可选自动写入 CF Worker）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--email", default=os.environ.get("TODAYAI_EMAIL"), help="today.ai 邮箱")
    p.add_argument("--otp", help="验证码（缺省交互输入；配 --imap-* 可自动抓）")
    p.add_argument("--imap-host", default=os.environ.get("TODAYAI_IMAP_HOST"), help="IMAP 服务器(自动抓码)")
    p.add_argument("--imap-user", default=os.environ.get("TODAYAI_IMAP_USER"), help="IMAP 用户(通常同邮箱)")
    p.add_argument("--imap-pass", default=os.environ.get("TODAYAI_IMAP_PASS"), help="IMAP 密码/应用专用密码")
    p.add_argument("--save", help="把提取的 cookie 以 TODAYAI_SESSION_COOKIE=... 写入文件(如 .env)")
    p.add_argument("--quiet", action="store_true", help="只打印 cookie 值本身（便于管道/复制）")
    # 可选扩展
    p.add_argument("--update-worker", action="store_true", help="提取后自动写入 CF Worker secret")
    p.add_argument("--cf-token", default=os.environ.get("CLOUDFLARE_API_TOKEN"), help="CF API Token(--update-worker)")
    p.add_argument("--cf-account", default=os.environ.get("CLOUDFLARE_ACCOUNT_ID"), help="CF Account ID(--update-worker)")
    p.add_argument("--worker", default=DEFAULT_WORKER, help=f"Worker 名称（默认 {DEFAULT_WORKER}）")
    p.add_argument("--verify", action="store_true", help="提取后调用网关验证")
    p.add_argument("--gateway-url", default=os.environ.get("TODAYAI_GATEWAY_URL", f"https://{DEFAULT_WORKER}.YOUR-SUBDOMAIN.workers.dev/v1"))
    p.add_argument("--api-key", default=os.environ.get("GATEWAY_API_KEY"), help="网关密钥(--verify 用)")
    args = p.parse_args(argv)

    if not args.email:
        print("❌ 需要 --email 或 TODAYAI_EMAIL", file=sys.stderr)
        return 2

    # 1) 发验证码（仅当需要交互输入或 IMAP 自动抓取时才发）
    otp = args.otp
    if not otp:
        if not args.imap_host:
            print("📨 第 1 步：发送 OTP 验证码...")
            send_otp(args.email)
        elif args.imap_host:
            print("📨 第 1 步：发送 OTP 验证码，并将从 IMAP 自动抓取...")
            send_otp(args.email)

    # 2) 拿验证码
    if not otp and args.imap_host:
        print(f"📬 第 2 步：自动从 IMAP({args.imap_host}) 抓验证码...")
        otp = fetch_otp_imap(args.imap_host, args.imap_user or args.email, args.imap_pass)
    elif not otp:
        otp = input("🔢 请输入邮箱收到的验证码: ").strip()
    if not re.fullmatch(r"\d{6}", otp or ""):
        print("❌ 验证码格式应为 6 位数字", file=sys.stderr)
        return 2

    # 3) 登录提取 cookie
    print("🔐 第 3 步：OTP 登录并提取 session cookie...")
    info = sign_in(args.email, otp)
    cookie = info["cookie"]
    session_life = time.strftime("%Y-%m-%d", time.localtime(time.time() + 59 * 86400))
    print(f"👤 登录用户: {info['name']} | session 预计有效期至 {session_life}")

    if args.quiet:
        # 纯值输出：方便复制/管道，不混入日志
        print(cookie)
    else:
        print(f"🍪 Cookie 值（完整）:\n{cookie}")
        print(f"（{len(cookie)} 字符 | 可通过 'python3 todayai_cookie.py --help' 查看使用说明）")

    # 4) 保存到文件（可选）
    if args.save:
        line = f"TODAYAI_SESSION_COOKIE={cookie}\n"
        with open(args.save, "a", encoding="utf-8") as f:
            f.write(line)
        print(f"💾 已追加写入 {args.save}")

    # 5) 自动更新 Worker（可选）
    if args.update_worker:
        if not (args.cf_token and args.cf_account):
            print("❌ --update-worker 需要 --cf-token/--cf-account 或对应环境变量", file=sys.stderr)
            return 2
        print(f"🛠 第 5 步：写入 Worker '{args.worker}' secret TODAY_SESSION_COOKIE...")
        update_worker_secret(args.cf_token, args.cf_account, args.worker, cookie)

    # 6) 验证（可选）
    if args.verify:
        if not args.api_key:
            print("⚠️ 跳过验证：缺 --api-key / GATEWAY_API_KEY", file=sys.stderr)
        else:
            print("🧪 第 6 步：调用网关验证...")
            verify_gateway(args.gateway_url, args.api_key)

    return 0


if __name__ == "__main__":
    sys.exit(main())