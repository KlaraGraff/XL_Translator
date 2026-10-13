// 反馈中转站（feedback-relay/src/index.js）的行为测试：用内存 KV 和假的 GitHub API 跑一遍。
// 运行：node tests/feedback_relay.test.mjs
import assert from "node:assert/strict";
import worker, { deleteResolvedPackages, issueBody, issueTitle, wakeRoutine } from "../feedback-relay/src/index.js";

function memoryKv() {
  const store = new Map();
  const meta = new Map();
  return {
    store,
    async getWithMetadata(key, options) {
      const value = await this.get(key, options);
      return { value, metadata: value === null ? null : meta.get(key) ?? null };
    },
    async get(key, options) {
      if (!store.has(key)) return null;
      const value = store.get(key);
      if (options && options.type === "arrayBuffer") return value;
      return typeof value === "string" ? value : new TextDecoder().decode(value);
    },
    async put(key, value, options) {
      store.set(key, value);
      if (options && options.metadata) meta.set(key, options.metadata);
    },
    async delete(key) { store.delete(key); meta.delete(key); },
  };
}

function makeEnv() {
  return { FEEDBACK: memoryKv(), FEEDBACK_REPO: "owner/feedback", GITHUB_TOKEN: "ghp_test", LINK_SECRET: "secret" };
}

const issues = [];
let githubStatus = 201;
globalThis.fetch = async (url, init) => {
  assert.equal(url, "https://api.github.com/repos/owner/feedback/issues");
  assert.equal(init.headers.Authorization, "Bearer ghp_test");
  issues.push(JSON.parse(init.body));
  return new Response("{}", { status: githubStatus });
};

function feedbackRequest(meta, pkg = new Uint8Array([80, 75, 3, 4]), ip = "1.2.3.4") {
  const form = new FormData();
  form.set("meta", JSON.stringify(meta));
  if (pkg) form.set("package", new Blob([pkg], { type: "application/zip" }), "feedback.zip");
  return new Request("https://relay.example/v1/feedback", { method: "POST", body: form, headers: { "CF-Connecting-IP": ip } });
}

const META = {
  category: "problem",
  description: "导出时报错 @someone\n第二行 #12",
  contact: "a@b.c",
  app_version: "10.0.0",
  platform: "macOS-15",
  ui_language: "zh",
  includes: { system: true, task_logs: 1, files: ["样例.xlsx"] },
};

// 正常提交：存包、开 issue、返回编号。
{
  const env = makeEnv();
  const response = await worker.fetch(feedbackRequest(META), env);
  assert.equal(response.status, 200);
  const { id } = await response.json();
  assert.match(id, /^FB-\d{8}-[A-Z2-9]{6}$/);
  assert.ok(env.FEEDBACK.store.has(`pkg:${id}`));
  const issue = issues.at(-1);
  assert.ok(issue.title.startsWith("[问题] 导出时报错"));
  assert.ok(issue.title.endsWith(id));
  assert.ok(issue.body.includes("@​someone"), "mentions are neutralized");
  assert.ok(issue.body.includes("#​12"), "issue references are neutralized");
  assert.ok(issue.body.includes("样例.xlsx"));
  assert.ok(issue.body.includes("**联系方式**：`a@b.c`"), "contact stays copyable");

  // 下载链接：签名正确能下，签名错了 403。
  const link = issue.body.match(/\((https:\/\/relay\.example\/v1\/files\/[^)]+)\)/)[1];
  const download = await worker.fetch(new Request(link), env);
  assert.equal(download.status, 200);
  assert.deepEqual(new Uint8Array(await download.arrayBuffer()), new Uint8Array([80, 75, 3, 4]));
  const forged = await worker.fetch(new Request(link.replace(/sig=[0-9a-f]+/, "sig=" + "0".repeat(64))), env);
  assert.equal(forged.status, 403);
}

// 超过 KV 单值上限的包拆段存，下载时拼回原样。
{
  const env = makeEnv();
  const big = new Uint8Array(21 * 1024 * 1024);
  big[0] = 7;
  big[big.length - 1] = 9;
  const response = await worker.fetch(feedbackRequest(META, big, "5.5.5.5"), env);
  assert.equal(response.status, 200);
  const { id } = await response.json();
  assert.ok(env.FEEDBACK.store.has(`pkg:${id}:1`), "second chunk stored");
  const link = issues.at(-1).body.match(/\((https:\/\/relay\.example\/v1\/files\/[^)]+)\)/)[1];
  const download = new Uint8Array(await (await worker.fetch(new Request(link), env)).arrayBuffer());
  assert.equal(download.length, big.length);
  assert.equal(download[0], 7);
  assert.equal(download[download.length - 1], 9);
}

// 没有附件包也能提交（只有文字），issue 里不出现下载链接。
{
  const env = makeEnv();
  const response = await worker.fetch(feedbackRequest({ ...META, includes: {} }, null), env);
  assert.equal(response.status, 200);
  assert.ok(!issues.at(-1).body.includes("下载反馈包"));
}

// 输入不合法：400。
{
  const env = makeEnv();
  assert.equal((await worker.fetch(feedbackRequest({ ...META, category: "spam" }), env)).status, 400);
  assert.equal((await worker.fetch(feedbackRequest({ ...META, description: "  " }), env)).status, 400);
}

// GitHub 失败：502，并且不留下孤立的附件包。
{
  const env = makeEnv();
  githubStatus = 500;
  const response = await worker.fetch(feedbackRequest(META), env);
  githubStatus = 201;
  assert.equal(response.status, 502);
  assert.equal([...env.FEEDBACK.store.keys()].filter((key) => key.startsWith("pkg:")).length, 0);
}

// 同一 IP 一小时内最多 10 条；KV 里不出现明文 IP。
{
  const env = makeEnv();
  for (let i = 0; i < 10; i += 1) {
    assert.equal((await worker.fetch(feedbackRequest(META, null, "9.9.9.9"), env)).status, 200);
  }
  assert.equal((await worker.fetch(feedbackRequest(META, null, "9.9.9.9"), env)).status, 429);
  assert.equal((await worker.fetch(feedbackRequest(META, null, "8.8.8.8"), env)).status, 200);
  assert.ok([...env.FEEDBACK.store.keys()].every((key) => !key.includes("9.9.9.9")));
}

// 问题处理完毕（已关闭，或带「待通知」）的反馈包每天删掉，正文的下载链接换成说明；还在处理的不动。
{
  const env = makeEnv();
  const ids = [];
  for (const ip of ["7.0.0.1", "7.0.0.2", "7.0.0.3"]) {
    ids.push((await (await worker.fetch(feedbackRequest(META, undefined, ip), env)).json()).id);
  }
  const bodies = issues.slice(-3).map((issue) => issue.body);
  const github = [
    { number: 1, state: "closed", labels: [], body: bodies[0] },
    { number: 2, state: "open", labels: ["待通知"], body: bodies[1] },
    { number: 3, state: "open", labels: [], body: bodies[2] },
    { number: 4, state: "closed", labels: [], body: "没有附件", pull_request: undefined },
  ];
  const patches = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    assert.equal(init.headers.Authorization, "Bearer ghp_test");
    const parsed = new URL(url);
    if ((init.method || "GET") === "GET") {
      const state = parsed.searchParams.get("state");
      const label = parsed.searchParams.get("labels");
      const list = github.filter((issue) => issue.state === state && (!label || issue.labels.includes(label)));
      return new Response(JSON.stringify(list), { status: 200 });
    }
    assert.equal(init.method, "PATCH");
    const number = Number(parsed.pathname.split("/").at(-1));
    const body = JSON.parse(init.body).body;
    patches.push(number);
    github.find((issue) => issue.number === number).body = body;
    return new Response("{}", { status: 200 });
  };
  try {
    const deleted = await deleteResolvedPackages(env, new Date("2026-10-13T03:00:00Z"));
    assert.deepEqual(deleted, [ids[0], ids[1]]);
    assert.deepEqual(patches, [1, 2]);
    assert.ok(!env.FEEDBACK.store.has(`pkg:${ids[0]}`) && !env.FEEDBACK.store.has(`pkg:${ids[1]}`));
    assert.ok(env.FEEDBACK.store.has(`pkg:${ids[2]}`), "feedback still being handled keeps its package");
    assert.ok(github[0].body.includes(`反馈包 ${ids[0]}.zip 已在问题处理完毕后删除（2026-10-13）。`));
    assert.ok(!github[0].body.includes("/v1/files/") && github[0].body.includes("### 描述"));
    // 第二次运行不再重复处理。
    assert.deepEqual(await deleteResolvedPackages(env), []);
  } finally {
    globalThis.fetch = realFetch;
  }
}

// 收到反馈叫醒「每日反馈检查」：30 分钟内只叫一次，被挡下或失败的由每 30 分钟的定时检查补叫；
// 只发固定提醒，不带用户内容；没配令牌时什么都不做。
{
  const env = { ...makeEnv(), ROUTINE_FIRE_URL: "https://api.example/routines/trig_x/fire", ROUTINE_FIRE_TOKEN: "routine-token" };
  const fires = [];
  let fireStatus = 200;
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    if (url === env.ROUTINE_FIRE_URL) {
      assert.equal(init.headers.Authorization, "Bearer routine-token");
      assert.equal(init.headers["anthropic-beta"], "experimental-cc-routine-2026-04-01");
      fires.push(JSON.parse(init.body).text);
      return new Response("{}", { status: fireStatus });
    }
    return realFetch(url, init);
  };
  try {
    const t0 = Date.UTC(2026, 9, 13, 12, 0);
    assert.equal(await wakeRoutine({ ...env, ROUTINE_FIRE_TOKEN: "" }, { now: t0 }), "disabled");
    assert.equal(await wakeRoutine(env, { retryOnly: true, now: t0 }), "idle");

    // 提交反馈后马上叫醒一次，提醒里没有用户写的内容。
    const response = await worker.fetch(feedbackRequest(META, undefined, "8.0.0.1"), env);
    assert.equal(response.status, 200);
    assert.equal(fires.length, 1);
    assert.ok(!fires[0].includes("导出时报错") && !fires[0].includes("a@b.c"));

    // 30 分钟内再来：不叫，记待办；定时检查在间隔过后补叫一次，之后无事可做。
    const last = Number(await env.FEEDBACK.get("wake:last"));
    assert.equal(await wakeRoutine(env, { now: last + 10 * 60 * 1000 }), "deferred");
    assert.equal(await wakeRoutine(env, { retryOnly: true, now: last + 20 * 60 * 1000 }), "deferred");
    assert.equal(await wakeRoutine(env, { retryOnly: true, now: last + 31 * 60 * 1000 }), "fired");
    assert.equal(fires.length, 2);
    assert.equal(await wakeRoutine(env, { retryOnly: true, now: last + 70 * 60 * 1000 }), "idle");

    // 叫醒失败：留待办、不占间隔，下一轮定时检查再试。
    fireStatus = 500;
    assert.equal(await wakeRoutine(env, { now: last + 80 * 60 * 1000 }), "failed");
    fireStatus = 200;
    assert.equal(await wakeRoutine(env, { retryOnly: true, now: last + 81 * 60 * 1000 }), "fired");
    assert.equal(fires.length, 4);
  } finally {
    globalThis.fetch = realFetch;
  }
}

// 标题截断与空行处理。
assert.equal(issueTitle("FB-1", "suggestion", "\n\n短句"), "[建议] 短句 · FB-1");
assert.ok(issueTitle("FB-1", "other", "长".repeat(80)).includes("…"));
assert.ok(issueBody({ id: "FB-1", category: "other", description: "x", contact: "", meta: {}, link: "", size: 0 }).includes("未填写"));

console.log("feedback relay tests passed");
