// 反馈中转站（feedback-relay/src/index.js）的行为测试：用内存 KV 和假的 GitHub API 跑一遍。
// 运行：node tests/feedback_relay.test.mjs
import assert from "node:assert/strict";
import worker, { issueBody, issueTitle } from "../feedback-relay/src/index.js";

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

// 标题截断与空行处理。
assert.equal(issueTitle("FB-1", "suggestion", "\n\n短句"), "[建议] 短句 · FB-1");
assert.ok(issueTitle("FB-1", "other", "长".repeat(80)).includes("…"));
assert.ok(issueBody({ id: "FB-1", category: "other", description: "x", contact: "", meta: {}, link: "", size: 0 }).includes("未填写"));

console.log("feedback relay tests passed");
