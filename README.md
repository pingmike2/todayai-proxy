# today.ai (国际版) -> OpenAI 兼容反代 (Cloudflare Workers)

把 Today AI（today.ai 国际版）私有协议包装成 OpenAI 标准接口，任何兼容客户端直连。

## ✅ 状态：协议已实测跑通

| 接口 | 实测 |
|---|---|
| `POST /v1/messages` `{"content","role":"user"}` | ✅ 202，返回 threadId |
| `GET /v1/messages?threadId=` | ✅ 轮询拿 assistant 回复 |
| `POST today.ai/api/token`（带 session cookie） | ✅ 换 1h 有效 API JWT |
| 响应示例 | "我是豆皮，nian 的专属助手..." |

## 架构

```
[任意 OpenAI 客户端]
   ↓ /v1/chat/completions (Bearer GATEWAY_API_KEY)
[CF Worker]
   ↓ 自动用 session cookie 换 API token (1h, 内存缓存+临期续期)
   ↓ POST /v1/messages → 轮询 GET /v1/messages?threadId → 转 OpenAI SSE
[api.today.ai]
```

## 变量总览（一共 8 个，分 3 组）

### 组 A：Cloudflare Worker Secrets（云端配置，2 个）

| 变量 | 必填 | 说明 |
|---|---|---|
| `TODAY_SESSION_COOKIE` | ✅ | today.ai 登录 cookie（`__Secure-better-auth.session_token=...`）。由 cookie 脚本自动维护，也可手动填 |
| `GATEWAY_API_KEY` | ✅ | 网关对外 API Key（客户端调用时填的 Bearer Key，自定义） |

配置方式（任选其一）：
```bash
wrangler secret put TODAY_SESSION_COOKIE
wrangler secret put GATEWAY_API_KEY
# 或由脚本自动写入（见 update_worker_cookie.py）
```

### 组 B：脚本环境变量（提取 cookie 必填，3 个）

| 变量 | 说明 |
|---|---|
| `TODAYAI_EMAIL` | today.ai 账号邮箱 |
| `CLOUDFLARE_API_TOKEN` | CF API Token（用于脚本调 CF API 写 secret） |
| `CLOUDFLARE_ACCOUNT_ID` | CF Account ID |

### 组 C：脚本增强选项（可选，3 个）

| 变量 | 说明 |
|---|---|
| `TODAYAI_IMAP_HOST` / `TODAYAI_IMAP_USER` / `TODAYAI_IMAP_PASS` | IMAP 自动抓验证码（无人值守必需） |
| `TODAYAI_GATEWAY_URL` | 网关地址（`--verify` 用，默认 `https://<你的子域>.workers.dev/v1`，请改成你自己的） |
| `GATEWAY_API_KEY` | 网关密钥（`--verify` 用，同组 A 的值） |

> 最小必配：**2 个 Worker Secrets + 3 个脚本环境变量 = 5 个**；
> 加 IMAP 3 个共 8 个即可完全无人值守。

## 部署（2 分钟）

```bash
cd todayai-proxy
wrangler login

# 1. 抓 session cookie（见下，或用 cookie 脚本自动提取）
# 2. 配置 secrets
wrangler secret put TODAY_SESSION_COOKIE   # 粘贴完整 cookie
wrangler secret put GATEWAY_API_KEY        # 自定义网关密钥

wrangler deploy
```

### 抓 session cookie（手动）

1. 浏览器登录 https://today.ai（Google 登录）
2. F12 → Application → Cookies → `today.ai`
3. 复制 `__Secure-better-auth.session_token` 的 **Name=Value**（连同其他 cookie 一起更稳）
   或 F12 → Network → 任意请求 → Request Headers → 整段 `Cookie:` 值

> cookie 是长效凭证（Better Auth session），Worker 每次自动换短效 API token，**不用每小时手动更新**。
> session 过期后（一般 7 天+）重新抓一次即可。

### 客户端接入

```
Base URL : https://todayai-proxy.<你的子域>.workers.dev/v1
API Key  : 你的 GATEWAY_API_KEY
Model    : today-balanced / today-fast / today-power
```

## 脚本：todayai_cookie.py（手动挡）

一次性提取 cookie 值，供你手动填 Workers 变量 / 配 direct 模式：

```bash
# 交互式：发验证码 → 输入 → 打印 cookie（--quiet 只要值，--save 存文件）
python3 todayai_cookie.py --email YOUR_EMAIL@example.com --save .env
python3 todayai_cookie.py --email YOUR_EMAIL@example.com --otp 123456 --quiet
```

## 脚本：update_worker_cookie.py（自动化 / 工作流挡）

提取 cookie + 自动写入 Worker secret，稳定退出码 + JSON 输出，适合 cron / n8n / CI：

```bash
# 全自动（配好 IMAP，cron 每月跑一次）：发码→抓码→登录→写 Worker→验证
python3 update_worker_cookie.py --email YOUR_EMAIL@example.com \
    --imap-host imap.gmail.com --imap-user YOUR_EMAIL@example.com \
    --imap-pass "app_password" --verify

# 半自动（手动给码）
python3 update_worker_cookie.py --email YOUR_EMAIL@example.com --otp 123456

# 工作流调用（JSON + 存 cookie + 验证；退出码 0/1/2）
python3 update_worker_cookie.py --otp 123456 --json --save .env --verify
```

cron 示例（每月 1 号凌晨刷新）：
```cron
0 3 1 * * cd /path/to/todayai-proxy && python3 update_worker_cookie.py --json >> /var/log/todayai-cookie.log 2>&1
```

## GitHub Action 自动刷新(可选)

仓库已带 `.github/workflows/refresh-cookie.yml`，推送后在 **GitHub Actions** 页点 **Run workflow** 即可手动刷新。

两种模式（Action 自动判断）：

| 模式 | 需要 | 说明 |
|---|---|---|
| 手动 OTP | 无 | Run workflow 时在 **otp** 输入框填邮箱收到的验证码 |
| IMAP 全自动 | 仓库配置 `TODAYAI_IMAP_PASS` 等 secrets | 无需人工，适合定时 |

**仓库 Secrets 需配置**（Settings → Secrets and variables → Actions）：

| Secret | 必填 | 说明 |
|---|---|---|
| `TODAYAI_EMAIL` | ✅ | today.ai 账号邮箱 |
| `CLOUDFLARE_API_TOKEN` | ✅ | CF API Token |
| `CLOUDFLARE_ACCOUNT_ID` | ✅ | CF Account ID |
| `GATEWAY_API_KEY` | 建议 | 网关密钥（`--verify` 用） |
| `TODAYAI_IMAP_HOST/USER/PASS` | 仅自动模式 | IMAP 抓验证码（如 imap.gmail.com + 应用专用密码） |

> 没有 IMAP 密码也能用：每次手动触发填 OTP 即可，3 秒搞定。

## curl 测试

```bash
curl -N https://todayai-proxy.<子域>.workers.dev/v1/chat/completions \
  -H "Authorization: Bearer <GATEWAY_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"today-balanced","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

## 逆向事实存档

- 认证: `auth.today.ai` Better Auth（国际版邮箱 OTP 可用；发码 `POST /api/auth/email-otp/send-verification-otp {email,type:"sign-in"}`，验证 `POST /api/auth/sign-in/email-otp {email,otp}`）
- API 换票: `POST today.ai/api/token` `{audience:"https://api.today.ai",expiresIn:3600}`（需 session cookie，Bearer session token 无效）
- API: `api.today.ai/v1/*`，JWT（issuer=auth.today.ai, aud=api.today.ai, 1h）
- 模型选择: 仅 `mode.fast / mode.balanced / mode.power` 三档（底层模型不公开，助手自称"豆皮"）
- 定价: Pro $20/月 · Ultra $200/月
- 其他端点: `/v1/agents/default`、`/v1/automations`、`/v1/memories/overview`、`/v1/skills`

## 文件

| 文件 | 说明 |
|---|---|
| `worker.js` | CF Workers 版（协议已实测校准） |
| `wrangler.toml` | Worker 配置 |
| `todayai_cookie.py` | **手动挡**：一键提取 today.ai session cookie |
| `update_worker_cookie.py` | **自动挡**：提取 cookie + 写 Worker secret + 验证（工作流友好） |
| `README.md` | 本文档 |