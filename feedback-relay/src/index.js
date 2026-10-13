// 应用内「反馈」的中转站，部署在 Cloudflare Workers（部署流程见
// .github/workflows/feedback-relay.yml）。
//
// 应用把用户勾选的内容打成一个 ZIP 发过来；这里做三件事：
//   1. 把 ZIP 存进 KV（FEEDBACK 绑定），最长保存 90 天；
//   2. 在私有仓库（FEEDBACK_REPO）里开一条 issue，正文是用户写的话和一个带签名、
//      90 天内有效的下载链接，GitHub 会按仓库的关注设置发邮件提醒；
//   3. 把反馈编号返回给应用，应用显示给用户。
//
// 另有每天一次的定时任务（wrangler.toml 的 crons）：问题处理完毕（issue 已关闭，或已修好、
// 带「待通知」标签）的反馈包立即删除，不等 90 天到期。应用里对用户就是这样承诺的。
//
// 不记录 IP：限流只用「LINK_SECRET + IP」的哈希做一小时计数，计数一小时后过期。
// GitHub 令牌只存在 Worker 的密钥里，应用里没有任何凭据。

// 应用端附件上限：文件 40 MB + 截图 10 MB，ZIP 头和日志另占一点。KV 单个值最多 25 MiB，所以大包按
// CHUNK_BYTES 拆成几段存，下载时再按顺序拼回去。
const MAX_PACKAGE_BYTES = 54 * 1024 * 1024;
const CHUNK_BYTES = 20 * 1024 * 1024;
const MAX_DESCRIPTION_CHARS = 5000;
const MAX_CONTACT_CHARS = 200;
const FILE_TTL_SECONDS = 90 * 24 * 3600;
const RATE_LIMIT_PER_HOUR = 10;
const CATEGORY_LABELS = { problem: "问题", suggestion: "建议", other: "其他" };

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    try {
      if (request.method === "POST" && url.pathname === "/v1/feedback") return await handleFeedback(request, env, url, ctx);
      if (request.method === "GET" && url.pathname.startsWith("/v1/files/")) return await handleDownload(env, url);
      if (request.method === "GET" && url.pathname === "/health") return json({ ok: true });
      return json({ error: "not_found" }, 404);
    } catch (error) {
      console.error("feedback relay failed", error && error.message);
      return json({ error: "internal" }, 500);
    }
  },

  async scheduled(event, env, ctx) {
    if (event.cron === CLEANUP_CRON) {
      ctx.waitUntil(deleteResolvedPackages(env).catch((error) => console.error("cleanup failed", error && error.message)));
    } else {
      ctx.waitUntil(wakeRoutine(env, { retryOnly: true }).catch((error) => console.error("routine wake failed", error && error.message)));
    }
  },
};

const CLEANUP_CRON = "0 3 * * *";

// 收到反馈后叫醒 Claude 的「每日反馈分拣」定时任务（它的 API 触发地址），不用等到第二天早上。
// 一次运行会处理所有没处理的反馈，所以 30 分钟内最多叫一次；被挡下的记一笔，由每 30 分钟的
// 定时检查补叫，保证新反馈最晚约一小时内有人看。只发固定的一句提醒，不带用户写的任何内容。
// 没配 ROUTINE_FIRE_URL / ROUTINE_FIRE_TOKEN 时什么都不做。
const WAKE_INTERVAL_MS = 30 * 60 * 1000;
const WAKE_TEXT = "反馈中转站：收到新的用户反馈，请按例行规则处理私有仓库里未处理的 issue。";

export async function wakeRoutine(env, { retryOnly = false, now = Date.now() } = {}) {
  if (!env.ROUTINE_FIRE_URL || !env.ROUTINE_FIRE_TOKEN) return "disabled";
  const pending = (await env.FEEDBACK.get("wake:pending")) === "1";
  if (retryOnly && !pending) return "idle";
  const last = Number((await env.FEEDBACK.get("wake:last")) || 0);
  if (now - last < WAKE_INTERVAL_MS) {
    await env.FEEDBACK.put("wake:pending", "1");
    return "deferred";
  }
  await env.FEEDBACK.put("wake:last", String(now));
  let status = 0;
  try {
    const response = await fetch(env.ROUTINE_FIRE_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.ROUTINE_FIRE_TOKEN}`,
        "anthropic-beta": "experimental-cc-routine-2026-04-01",
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ text: WAKE_TEXT }),
    });
    status = response.status;
  } catch (error) {
    console.error("routine wake failed", error && error.message);
  }
  if (status < 200 || status >= 300) {
    // 没叫醒就留着待办，下一轮定时检查再试；每天的例行检查也会兜底。
    await env.FEEDBACK.put("wake:pending", "1");
    await env.FEEDBACK.delete("wake:last");
    if (status) console.error("routine wake failed", status);
    return "failed";
  }
  await env.FEEDBACK.delete("wake:pending");
  return "fired";
}

// 已修好、等发版通知的问题也算处理完毕：用户的文件已经用不上了。
const RESOLVED_OPEN_LABEL = "待通知";
const PACKAGE_LINK_RE = /\/v1\/files\/(FB-\d{8}-[A-Z2-9]{6})\?/;

/** Delete the packages of feedback that has been dealt with; return their ids. */
export async function deleteResolvedPackages(env, now = new Date()) {
  const githubApi = env.GITHUB_API || "https://api.github.com";
  const date = now.toISOString().slice(0, 10);
  const deleted = [];
  for (const query of ["state=closed", `state=open&labels=${encodeURIComponent(RESOLVED_OPEN_LABEL)}`]) {
    for (let page = 1; page <= 20; page += 1) {
      const response = await fetch(`${githubApi}/repos/${env.FEEDBACK_REPO}/issues?${query}&per_page=100&page=${page}`, {
        headers: githubHeaders(env),
      });
      if (!response.ok) throw new Error(`github list failed: ${response.status}`);
      const batch = await response.json();
      for (const issue of batch) {
        const match = PACKAGE_LINK_RE.exec(issue.body || "");
        if (issue.pull_request || !match) continue;
        const id = match[1];
        await deletePackage(env, id);
        // 链接换成一句说明；正文里没有链接了，下次就不会再处理这条。
        const body = issue.body
          .split("\n")
          .map((line) => (PACKAGE_LINK_RE.test(line) ? `反馈包 ${id}.zip 已在问题处理完毕后删除（${date}）。` : line))
          .join("\n");
        const patched = await fetch(`${githubApi}/repos/${env.FEEDBACK_REPO}/issues/${issue.number}`, {
          method: "PATCH",
          headers: { ...githubHeaders(env), "Content-Type": "application/json" },
          body: JSON.stringify({ body }),
        });
        if (!patched.ok) console.error("github issue update failed", issue.number, patched.status);
        deleted.push(id);
      }
      if (batch.length < 100) break;
    }
  }
  return deleted;
}

function githubHeaders(env) {
  return {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "xl-translator-feedback-relay",
  };
}

async function handleFeedback(request, env, url, ctx) {
  const declared = Number(request.headers.get("Content-Length") || 0);
  if (declared > MAX_PACKAGE_BYTES + 1024 * 1024) return json({ error: "too_large" }, 413);

  if (!(await withinRateLimit(request, env))) return json({ error: "rate_limited" }, 429);

  let form;
  try {
    form = await request.formData();
  } catch {
    return json({ error: "bad_request" }, 400);
  }
  let meta;
  try {
    meta = JSON.parse(String(form.get("meta") || ""));
  } catch {
    return json({ error: "bad_request" }, 400);
  }
  const category = Object.hasOwn(CATEGORY_LABELS, meta.category) ? meta.category : "";
  const description = typeof meta.description === "string" ? meta.description.trim() : "";
  const contact = typeof meta.contact === "string" ? meta.contact.trim() : "";
  if (!category || !description || description.length > MAX_DESCRIPTION_CHARS || contact.length > MAX_CONTACT_CHARS) {
    return json({ error: "bad_request" }, 400);
  }

  const pkg = form.get("package");
  const hasPackage = pkg && typeof pkg === "object" && typeof pkg.arrayBuffer === "function" && pkg.size > 0;
  if (hasPackage && pkg.size > MAX_PACKAGE_BYTES) return json({ error: "too_large" }, 413);

  const id = feedbackId(new Date());
  let link = "";
  if (hasPackage) {
    await storePackage(env, id, pkg);
    const expires = Math.floor(Date.now() / 1000) + FILE_TTL_SECONDS;
    const signature = await sign(env.LINK_SECRET, `${id}.${expires}`);
    link = `${url.origin}/v1/files/${id}?exp=${expires}&sig=${signature}`;
  }

  // GITHUB_API 只在本地联调时指向假的 GitHub；线上不设，用官方地址。
  const githubApi = env.GITHUB_API || "https://api.github.com";
  const issue = await fetch(`${githubApi}/repos/${env.FEEDBACK_REPO}/issues`, {
    method: "POST",
    headers: { ...githubHeaders(env), "Content-Type": "application/json" },
    body: JSON.stringify({
      title: issueTitle(id, category, description),
      body: issueBody({ id, category, description, contact, meta, link, size: hasPackage ? pkg.size : 0 }),
    }),
  });
  if (!issue.ok) {
    console.error("github issue failed", issue.status);
    if (hasPackage) await deletePackage(env, id);
    return json({ error: "upstream" }, 502);
  }
  const wake = wakeRoutine(env).catch((error) => console.error("routine wake failed", error && error.message));
  if (ctx) ctx.waitUntil(wake);
  else await wake;
  return json({ ok: true, id });
}

async function handleDownload(env, url) {
  const id = url.pathname.slice("/v1/files/".length);
  const expires = Number(url.searchParams.get("exp") || 0);
  const signature = url.searchParams.get("sig") || "";
  if (!/^FB-\d{8}-[A-Z2-9]{6}$/.test(id) || !expires || expires < Date.now() / 1000) {
    return json({ error: "not_found" }, 404);
  }
  const expected = await sign(env.LINK_SECRET, `${id}.${expires}`);
  if (!timingSafeEqual(signature, expected)) return json({ error: "forbidden" }, 403);
  const head = await env.FEEDBACK.getWithMetadata(`pkg:${id}`, { type: "arrayBuffer" });
  if (!head || !head.value) return json({ error: "not_found" }, 404);
  const chunks = Math.max(1, Number(head.metadata && head.metadata.chunks) || 1);
  const parts = [head.value];
  for (let index = 1; index < chunks; index += 1) {
    const part = await env.FEEDBACK.get(`pkg:${id}:${index}`, { type: "arrayBuffer" });
    if (!part) return json({ error: "not_found" }, 404);
    parts.push(part);
  }
  return new Response(new Blob(parts), {
    headers: {
      "Content-Type": "application/zip",
      "Content-Disposition": `attachment; filename="${id}.zip"`,
      "Cache-Control": "private, no-store",
    },
  });
}

// 第一段存在 pkg:<id>，元数据里记段数；其余段存在 pkg:<id>:1、pkg:<id>:2 …
// 一段一段地读出再写入，Worker 内存里不会同时放两份整包。
async function storePackage(env, id, pkg) {
  const chunks = Math.max(1, Math.ceil(pkg.size / CHUNK_BYTES));
  for (let index = chunks - 1; index >= 0; index -= 1) {
    const part = await pkg.slice(index * CHUNK_BYTES, (index + 1) * CHUNK_BYTES).arrayBuffer();
    const key = index === 0 ? `pkg:${id}` : `pkg:${id}:${index}`;
    const options = { expirationTtl: FILE_TTL_SECONDS };
    if (index === 0) options.metadata = { size: pkg.size, chunks };
    await env.FEEDBACK.put(key, part, options);
  }
}

async function deletePackage(env, id) {
  const chunks = Math.ceil(MAX_PACKAGE_BYTES / CHUNK_BYTES);
  for (let index = 0; index < chunks; index += 1) {
    await env.FEEDBACK.delete(index === 0 ? `pkg:${id}` : `pkg:${id}:${index}`);
  }
}

async function withinRateLimit(request, env) {
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const hour = Math.floor(Date.now() / 3_600_000);
  const key = `rl:${(await sha256Hex(`${env.LINK_SECRET}|${ip}`)).slice(0, 24)}:${hour}`;
  const count = Number((await env.FEEDBACK.get(key)) || 0);
  if (count >= RATE_LIMIT_PER_HOUR) return false;
  // KV 是最终一致的，计数不精确；这里只为挡住成批的刷量，偶尔多放过一两条无妨。
  await env.FEEDBACK.put(key, String(count + 1), { expirationTtl: 3600 });
  return true;
}

// 编号：FB-年月日-6 位（去掉易混的 0/O/1/I）。
const ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";

export function feedbackId(now) {
  const date = now.toISOString().slice(0, 10).replaceAll("-", "");
  const bytes = crypto.getRandomValues(new Uint8Array(6));
  const suffix = Array.from(bytes, (byte) => ID_ALPHABET[byte % ID_ALPHABET.length]).join("");
  return `FB-${date}-${suffix}`;
}

export function issueTitle(id, category, description) {
  const firstLine = description.split(/\r?\n/).find((line) => line.trim()) || "";
  const short = firstLine.trim().slice(0, 60);
  return `[${CATEGORY_LABELS[category]}] ${neutralize(short)}${firstLine.trim().length > 60 ? "…" : ""} · ${id}`;
}

export function issueBody({ id, category, description, contact, meta, link, size }) {
  const includes = meta && typeof meta.includes === "object" && meta.includes ? meta.includes : {};
  const files = Array.isArray(includes.files) ? includes.files.map((name) => neutralize(String(name))).slice(0, 10) : [];
  const parts = [
    `**编号**：${id}`,
    `**类型**：${CATEGORY_LABELS[category]}`,
    `**版本**：${neutralize(String(meta.app_version || "未知"))} · ${neutralize(String(meta.platform || "未知系统"))} · 界面语言 ${neutralize(String(meta.ui_language || "zh"))}`,
    `**联系方式**：${contact ? codeSpan(contact) : "未填写"}`,
    `**附带**：系统信息${includes.system ? "✓" : "✗"} · 截图 ${Number(includes.images) || 0} 张 · 任务日志 ${Number(includes.task_logs) || 0} 次 · 文件 ${files.length} 个${files.length ? `（${files.join("、")}）` : ""}`,
    "",
    "### 描述",
    "",
    description.split(/\r?\n/).map((line) => `> ${neutralize(line)}`).join("\n"),
  ];
  if (link) {
    parts.push("", "### 附件", "", `[下载反馈包 ${id}.zip](${link})（${formatBytes(size)}，问题处理完毕后自动删除，最长保存 90 天）`);
  }
  return parts.join("\n");
}

// 正文是用户写的，不能让它在私有仓库里 @ 到人或者关联到别的 issue。
function neutralize(text) {
  return String(text).replace(/@/g, "@​").replace(/#(\d)/g, "#​$1");
}

// 联系方式要能原样复制（邮箱里的 @ 不能插零宽空格），放进代码样式里 GitHub 就不会当成 @ 提及。
function codeSpan(text) {
  return `\`${String(text).replace(/[`\r\n]/g, " ")}\``;
}

function formatBytes(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

async function sign(secret, message) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return toHex(mac);
}

async function sha256Hex(text) {
  return toHex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
}

function toHex(buffer) {
  return Array.from(new Uint8Array(buffer), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}
