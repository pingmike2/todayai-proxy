# 参与贡献

欢迎 PR —— 这是一个「两个 Python 脚本 + 一个 Worker」的反代小项目，改动越聚焦越好。本仓库是模板 Fork，改完请自己先跑通再说。

## 先跑起来

```bash
git clone https://github.com/pingmike2/todayai-proxy.git
cd todayai-proxy
python3 -m py_compile todayai_cookie.py update_worker_cookie.py   # 语法闸
```

真实联调需要 today.ai 国际版账号 + Cloudflare Worker secrets；本地没有就不用硬跑，`--help` / `--dry-run` 能走通大部分路径即可。cookie 脚本带 `--dry-run`。

## 硬约束（会直接拒掉 PR 的）

1. **绝不提交任何凭证**。`TODAY_SESSION_COOKIE`、`CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`、邮箱密码都属于 secrets，只作为 Worker Secret / 本机 `.env`（`.env` 已被 `.gitignore` 挡掉）。示例一律用占位符 `YOUR_EMAIL@example.com`、`GATEWAY_API_KEY`。
2. **不带个人部署痕迹进代码**。Worker 子域、账号 ID、别人家的 workers.dev 域名都不要写死在源码或 README 里，用 `<你的子域>` / `YOUR-SUBDOMAIN` 占位。
3. **逆向协议注释要与实测一致**。`worker.js` 顶部和 README「逆向事实存档」里的端点、换票流程（`POST today.ai/api/token` 需 session cookie、`api.today.ai/v1/*`、1h JWT）是踩坑换来的，改协议认知必须同步这两个地方，别只改一头。
4. **README 与脚本的 env 表同源**。变量清单（组 A/B/C）是从代码 `os.environ` / argparse 默认值 grep 核对的，改了脚本参数就同步 README，别凭印象写。
5. **中文文案不要被 shell 弄坏编码**。改 `.py` 文件里的中文字符串用编辑器或 `patch`，别走会做编码往返的管道。

## 加一条行为

- 改 Worker：动 `worker.js`，保持 OpenAI 兼容接口契约（`/v1/chat/completions` 转 `/v1/messages` + 轮询），注释只写代码表达不了的约束。
- 改 cookie 脚本：`todayai_cookie.py` 是手动挡，`update_worker_cookie.py` 是自动/工作流挡（JSON 输出 + 退出码 0/1/2），两处的 `--gateway-url` 默认值都要走 `TODAYAI_GATEWAY_URL` env。
- 改刷新流程：`.github/workflows/refresh-cookie.yml` 是手动触发（填 OTP）或 IMAP 全自动两种模式，别删掉手动挡。

## 提 PR

- 一个 PR 一件事，标题写清动词（`feat:` / `fix:` / `docs:` / `test:`）。
- 描述里说明：为什么改、怎么验证（贴 `py_compile` / `--dry-run` 输出最好）。
- 改了行为就同步 README 的相关小节；改了版本号就同步 `package.json` 与主文件里的 `version`。

## 报 bug

带上这些信息基本能一次定位：

- Worker 是 `worker.js` 还是 cookie 脚本（哪个挡）？
- 报错原文 + 触发命令（含 `--dry-run` / `--json` 与否）
- 若涉及 `--verify` / `update-worker`：`TODAYAI_GATEWAY_URL`、`DEFAULT_WORKER` 当前值是什么（凭证部分打码）
- today.ai 登录/换票是否报 401/404 —— 那是凭证过期或协议变了，不是脚本 bug
