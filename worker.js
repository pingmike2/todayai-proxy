/**
 * Today AI (today.ai 国际版) -> OpenAI 兼容 API (Cloudflare Workers 版)
 *
 * 已实测协议:
 *   发送: POST https://api.today.ai/v1/messages
 *         Authorization: Bearer <API token>
 *         {"content":"...","role":"user"} -> 202 {threadId, messageId}
 *   拉回复: GET https://api.today.ai/v1/messages?threadId=<id>
 *         -> data[] 里 role=assistant 的 content 就是回复
 *   换token: POST https://today.ai/api/token (带 session cookie)
 *         -> {token, expiresIn:3600}
 *
 * 设计:
 *   - TODAY_SESSION_COOKIE 为长效凭证 (Better Auth session, 用很久)
 *   - Worker 启动后自动用它换短效 API token, 内存缓存 + 临期自动续
 *   - 对外暴露 OpenAI /v1/chat/completions (stream / non-stream)
 *
 * 部署: wrangler deploy
 * Secrets:
 *   TODAY_SESSION_COOKIE  浏览器抓的完整 Cookie (必填, 含 __Secure-better-auth.session_token=...)
 *   GATEWAY_API_KEY       网关自身校验 key (自定义)
 */

const API_URL = "https://api.today.ai";
const FRONT_URL = "https://today.ai";

// API token 内存缓存（Worker 实例内共享）
let apiTokenCache = { token: null, expiresAtMs: 0 };

/** 用 session cookie 换 API token（cached 1h）；也支持直填 TODAY_API_TOKEN */
async function getApiToken(env) {
  const now = Date.now();
  // 直填模式：直接用静态 token（用户手动抓的，有效期约 1h，过期需更新 secret）
  if (env.TODAY_API_TOKEN) {
    return env.TODAY_API_TOKEN;
  }
  if (apiTokenCache.token && apiTokenCache.expiresAtMs - now > 60_000) {
    return apiTokenCache.token;
  }
  if (!env.TODAY_SESSION_COOKIE) {
    throw new Error("未配置 TODAY_SESSION_COOKIE 或 TODAY_API_TOKEN");
  }
  const res = await fetch(`${FRONT_URL}/api/token`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      Cookie: env.TODAY_SESSION_COOKIE,
    },
    body: JSON.stringify({ audience: API_URL, expiresIn: 3600 }),
  });
  if (!res.ok) {
    throw new Error(`换 API token 失败: ${res.status} ${await res.text().catch(() => "")}`);
  }
  const data = await res.json();
  if (!data.token) throw new Error("换 API token 响应缺少 token 字段");
  apiTokenCache = { token: data.token, expiresAtMs: Date.now() + (data.expiresIn || 3600) * 1000 };
  return data.token;
}

/** 发送一条用户消息，返回 threadId */
async function sendMessage(env, content, mode) {
  const token = await getApiToken(env);
  const body = { content, role: "user" };
  // mode.fast/balanced/power 可选的附加参数（若上游不接受可删掉）
  if (mode && mode !== "mode.balanced") {
    body.mode = mode;
  }
  const res = await fetch(`${API_URL}/v1/messages`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(body),
  });
  const raw = await res.text();
  if (res.status === 202) {
    let data;
    try { data = JSON.parse(raw); } catch { data = null; }
    if (data?.threadId) return data.threadId;
  }
  throw new Error(`发送消息失败: ${res.status} ${raw.slice(0, 300)}`);
}

/** 轮询 thread 直到出现 assistant 回复（isFinal），超时抛错 */
async function waitForReply(env, threadId, timeoutMs = 120_000, intervalMs = 1500) {
  const token = await getApiToken(env);
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    const res = await fetch(`${API_URL}/v1/messages?threadId=${encodeURIComponent(threadId)}`, {
      headers: {
        Accept: "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        Authorization: `Bearer ${token}`,
      },
    });
    if (res.ok) {
      const data = await res.json();
      const items = data?.data || [];
      const assistant = [...items]
        .reverse()
        .find((m) => m.role === "assistant" && m.isFinal && m.content);
      if (assistant) return assistant.content;
      lastError = null;
    } else {
      lastError = `轮询失败: ${res.status}`;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error(lastError || "等待回复超时");
}

function buildPrompt(messages) {
  const lines = [];
  for (const m of messages || []) {
    const role = m.role || "user";
    let content = m.content ?? "";
    if (Array.isArray(content)) {
      content = content.filter((c) => c?.type === "text").map((c) => c.text).join(" ");
    }
    if (role === "system") lines.push(`[系统] ${content}`);
    else if (role === "user") lines.push(`[用户] ${content}`);
    else if (role === "assistant") lines.push(`[助手] ${content}`);
    else lines.push(String(content));
  }
  return lines.join("\n").trim();
}

function openaiCompletion(text, model, id) {
  return {
    id: id || `chatcmpl-${crypto.randomUUID().slice(0, 24)}`,
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model,
    choices: [{ index: 0, message: { role: "assistant", content: text }, finish_reason: "stop" }],
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };
}

function* sseChunks(text, model, id) {
  const base = { id, object: "chat.completion.chunk", created: Math.floor(Date.now() / 1000), model };
  yield `data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: { role: "assistant" }, finish_reason: null }] })}\n\n`;
  for (let i = 0; i < text.length; i += 4) {
    const delta = { index: 0, delta: { content: text.slice(i, i + 4) }, finish_reason: null };
    yield `data: ${JSON.stringify({ ...base, choices: [delta] })}\n\n`;
  }
  yield `data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: {}, finish_reason: "stop" }] })}\n\n`;
  yield "data: [DONE]\n\n";
}

const MODE_MAP = {
  "today-fast": "mode.fast",
  "today-balanced": "mode.balanced",
  "today-power": "mode.power",
  "today": "mode.balanced",
};

function checkAuth(request, env) {
  const key = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "").trim();
  if (key !== (env.GATEWAY_API_KEY || "todayai-1234")) {
    return new Response(JSON.stringify({ error: "Invalid API key" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  return null;
}

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
};

async function handleChatCompletions(request, env) {
  const authErr = checkAuth(request, env);
  if (authErr) return authErr;
  if (!env.TODAY_SESSION_COOKIE && !env.TODAY_API_TOKEN) {
    return new Response(
      JSON.stringify({ error: "未配置 TODAY_SESSION_COOKIE 或 TODAY_API_TOKEN" }),
      { status: 500, headers: { "Content-Type": "application/json", ...CORS_HEADERS } }
    );
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
      status: 400,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });
  }

  const model = body.model || "today-balanced";
  const stream = !!body.stream;
  const messages = body.messages || [];
  if (!messages.length) {
    return new Response(JSON.stringify({ error: "messages 不能为空" }), {
      status: 400,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });
  }
  const mode = MODE_MAP[model];
  if (!mode) {
    return new Response(JSON.stringify({ error: `未知模型: ${model}` }), {
      status: 400,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });
  }

  const prompt = buildPrompt(messages);
  let threadId, text;
  try {
    threadId = await sendMessage(env, prompt, mode);
    text = await waitForReply(env, threadId);
  } catch (e) {
    return new Response(JSON.stringify({ error: String(e) }), {
      status: 502,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });
  }

  const id = `chatcmpl-${crypto.randomUUID().slice(0, 24)}`;
  if (stream) {
    const encoder = new TextEncoder();
    const gen = sseChunks(text, model, id);
    const streamBody = new ReadableStream({
      start(controller) {
        const push = () => {
          try {
            const { value, done } = gen.next();
            if (done) return controller.close();
            controller.enqueue(encoder.encode(value));
            setTimeout(push, 30);
          } catch (err) {
            controller.error(err);
          }
        };
        push();
      },
    });
    return new Response(streamBody, {
      headers: { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache", "X-Accel-Buffering": "no", ...CORS_HEADERS },
    });
  }

  return new Response(JSON.stringify(openaiCompletion(text, model, id)), {
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS_HEADERS });

    if (url.pathname === "/v1/chat/completions" && request.method === "POST") {
      return handleChatCompletions(request, env);
    }
    if (url.pathname === "/v1/models") {
      const authErr = checkAuth(request, env);
      if (authErr) return authErr;
      return new Response(
        JSON.stringify({
          object: "list",
          data: [
            { id: "today-fast", object: "model", owned_by: "today.ai" },
            { id: "today-balanced", object: "model", owned_by: "today.ai" },
            { id: "today-power", object: "model", owned_by: "today.ai" },
          ],
        }),
        { headers: { "Content-Type": "application/json", ...CORS_HEADERS } }
      );
    }
    if (url.pathname === "/healthz") {
      return new Response(JSON.stringify({ ok: true }), { headers: { "Content-Type": "application/json", ...CORS_HEADERS } });
    }
    return new Response(JSON.stringify({ error: "Not Found" }), { status: 404, headers: { "Content-Type": "application/json", ...CORS_HEADERS } });
  },
};