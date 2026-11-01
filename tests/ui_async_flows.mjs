// 界面异步流程的行为测试（CODE_AUDIT_2026-08-31.md 第 1323-1326 行「预算内没查完的」三项）。
//
// 这三条以前只有静态读码结论，没有任何可执行的证据：
//   1. ui/src/api-client.ts saveBinaryDownload：保存框默认名直接用服务端
//      Content-Disposition 里的 filename，拿不到才退回调用方给的兜底名。
//   2. ui/src/views/workspace-pdf-pages.ts runPdfBatchRerun：单页 rerunPdfPage 抛错时
//      只弹 toast，随后跳过 waitForRerunSlot、batch.done 照常 +1，继续发下一页。
//   3. ui/src/views/tasks.ts watchTask：streamTask 内部 7 次退避后抛出，catch 里
//      getTask 成功且非终态就 setTimeout(0) 再开一轮 watchTask——跨轮没有上限。
//
// 这里不启浏览器、不连后端、不碰 ui/node_modules：用 node 自带的类型擦除直接加载
// ui/src 的 .ts（加载钩子见下），给 Tauri 接口 / 原生保存框 / fetch / 定时器全部打桩，
// 然后驱动真实函数，断言它们实际发出的请求顺序和实际传给保存框的文件名。
//
// 运行：node tests/ui_async_flows.mjs [场景名]（不带参数跑全部场景）
import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { registerHooks, stripTypeScriptTypes } from "node:module";
import { dirname, resolve as resolvePath } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const UI = new URL("../ui/src/", import.meta.url).pathname;

// ---------------------------------------------------------------------------
// 加载钩子：把 ui/src 的 .ts 当成 ES 模块加载
//   - 相对导入不带扩展名（仓库用 moduleResolution=bundler，node 不会自己猜），补 .ts / index.ts；
//   - import.meta.glob("./catalog/*.json") 是 Vite 专有语法，展开成显式 import；
//   - import.meta.env 只在 Vite 里有，换成一个最小的常量对象；
//   - @tauri-apps/* 与 opencc-js 用桩代替（真正的 Tauri IPC 由 __TAURI_INTERNALS__ 兜住）；
//   - .css 一律当空模块（样式与这里的断言无关）。
// ---------------------------------------------------------------------------
const STUBBED = /\.css$|^@tauri-apps\/|^opencc-js/;
const STUB_SOURCE = `const noop = () => {};
export const invoke = async (cmd, args) => globalThis.__TAURI_INTERNALS__.invoke(cmd, args);
export const isTauri = () => true;
export const save = async (options) => globalThis.__TAURI_INTERNALS__.invoke("plugin:dialog|save", { options });
export const open = async (options) => globalThis.__TAURI_INTERNALS__.invoke("plugin:dialog|open", { options });
export const check = async () => null;
export const relaunch = async () => undefined;
export default noop;`;

/** 把 import.meta.glob("./catalog/*.json", { eager: true, import: "default" }) 展开成显式默认导入。 */
function expandGlob(source, path) {
  return source.replace(
    /import\.meta\.glob(<[^>]*>)?\((["'])([^"']+)\2(?:,\s*\{[^}]*\})?\s*\)/g,
    (_match, _generic, _quote, pattern) => {
      const folder = resolvePath(dirname(path), dirname(pattern));
      const suffix = pattern.slice(dirname(pattern).length + 1).replace(/^\*/, "");
      const entries = readdirSync(folder)
        .filter((name) => name.endsWith(suffix))
        .map((name) => {
          const url = JSON.stringify(pathToFileURL(resolvePath(folder, name)).href);
          const attribute = name.endsWith(".json") ? ', { with: { type: "json" } }' : "";
          return `["${pattern.replace("*", name)}", (await import(${url}${attribute})).default]`;
        });
      return `Object.fromEntries([${entries.join(", ")}])`;
    },
  );
}

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (STUBBED.test(specifier)) return { url: `xlt-stub:${specifier}`, shortCircuit: true, format: "module" };
    if (specifier.startsWith("xlt-stub:")) return { url: specifier, shortCircuit: true, format: "module" };
    if ((specifier.startsWith("./") || specifier.startsWith("../")) && context.parentURL?.startsWith("file:")) {
      const base = new URL(specifier, context.parentURL);
      const candidates = [];
      if (/\.(ts|mts|js|mjs|json)$/.test(base.pathname)) candidates.push(base.href);
      candidates.push(`${base.href}.ts`, `${base.href.replace(/\/$/, "")}/index.ts`);
      for (const candidate of candidates) {
        const path = fileURLToPath(candidate);
        if (!existsSync(path) || !statSync(path).isFile()) continue;
        // 只给解析结果，格式交给 node 的默认处理（.ts 的类型擦除、.json 的 JSON 模块），
        // 免得手写 "module-typescript" 这种内部格式名，换 node 版本就碎。
        return { url: candidate, shortCircuit: true };
      }
    }
    return nextResolve(specifier, context);
  },
  load(url, context, nextLoad) {
    if (url.startsWith("xlt-stub:")) return { format: "module", shortCircuit: true, source: STUB_SOURCE };
    if (url.startsWith("file:") && url.endsWith(".json")) {
      // package.json 的 version 在两个视图里是命名导入，其余 json 只要默认导出。
      const data = readFileSync(fileURLToPath(url), "utf8");
      return {
        format: "module",
        shortCircuit: true,
        source: `export const version = ${JSON.stringify(JSON.parse(data).version ?? "")};\nexport default ${data};`,
      };
    }
    if (url.startsWith("file:") && url.endsWith(".ts")) {
      const path = fileURLToPath(url);
      let source = readFileSync(path, "utf8");
      source = expandGlob(source, path);
      source = source.replace(/import\.meta\.env(\.[A-Za-z_]+)?/g, "({ DEV: false, PROD: true, MODE: 'production' })");
      return { format: "module", shortCircuit: true, source: stripTypeScriptTypes(source, { mode: "strip" }) };
    }
    return nextLoad(url, context);
  },
});

// ---------------------------------------------------------------------------
// 假 DOM：只实现界面代码真正用到的节点操作（append / classList / 事件 / 文本）。
// 不引 jsdom：仓库没有前端测试运行时，为一个文件装一套 DOM 实现不值得。
// ---------------------------------------------------------------------------
const noop = () => {};
const realSetTimeout = globalThis.setTimeout;
const sleep = (ms) => new Promise((resolve) => realSetTimeout(resolve, ms));

class FakeNode {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.style = { setProperty: noop, removeProperty: noop, cssText: "" };
    this.classList = { add: noop, remove: noop, toggle: noop, contains: () => false };
    this.dataset = {};
    this.attributes = {};
    this.textContent = "";
    this.className = "";
    this._handlers = {};
    this.disabled = false;
    this.colSpan = 0;
    this.type = "";
    this.checked = false;
  }
  append(...nodes) {
    for (const node of nodes) this.children.push(node);
  }
  appendChild(node) {
    this.children.push(node);
    return node;
  }
  insertBefore(node) {
    this.children.push(node);
    return node;
  }
  replaceChildren(...nodes) {
    this.children = nodes;
  }
  remove() {}
  addEventListener(type, handler) {
    (this._handlers[type] ??= []).push(handler);
  }
  removeEventListener() {}
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  getAttribute(name) {
    return this.attributes[name] ?? null;
  }
  removeAttribute(name) {
    delete this.attributes[name];
  }
  querySelector() {
    return null;
  }
  querySelectorAll() {
    return [];
  }
  scrollIntoView() {}
  focus() {}
  setSelectionRange() {}
  click() {
    for (const handler of this._handlers.click ?? []) {
      handler({ currentTarget: this, preventDefault: noop, stopPropagation: noop });
    }
  }
  getBoundingClientRect() {
    return { top: 0, left: 0, width: 0, height: 0, bottom: 0, right: 0 };
  }
  get firstChild() {
    return this.children[0] ?? null;
  }
  get lastChild() {
    return this.children.at(-1) ?? null;
  }
}

const documentStub = {
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (_namespace, tag) => new FakeNode(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: text, _text: text }),
  addEventListener: noop,
  removeEventListener: noop,
  querySelector: () => null,
  querySelectorAll: () => [],
  getElementById: () => null,
  body: new FakeNode("body"),
  documentElement: new FakeNode("html"),
  head: new FakeNode("head"),
  activeElement: null,
};
globalThis.document = documentStub;
globalThis.window = globalThis;
window.addEventListener = noop;
window.removeEventListener = noop;
window.matchMedia = () => ({
  matches: false,
  addEventListener: noop,
  removeEventListener: noop,
  addListener: noop,
  removeListener: noop,
});
globalThis.localStorage = { getItem: () => null, setItem: noop, removeItem: noop };

// 可控定时器：界面代码用 window.setTimeout 退避重连、等重跑槽位，真等 2 秒一轮太慢；
// 而且「无限重连」这类缺口本来就是「跑不完」才暴露的，所以改成手动逐个执行。
const timers = [];
let timerSeq = 0;
window.setTimeout = (fn) => {
  timers.push({ id: ++timerSeq, fn });
  return timerSeq;
};
window.clearTimeout = (id) => {
  const index = timers.findIndex((timer) => timer.id === id);
  if (index >= 0) timers.splice(index, 1);
};
window.setInterval = () => 0;
window.clearInterval = noop;

/** 执行队首定时器（按注册顺序，忽略延迟），返回是否真的执行了一个。 */
async function runTimer() {
  const timer = timers.shift();
  if (!timer) return false;
  timer.fn();
  await sleep(0);
  return true;
}

/** 连跑至多 n 个定时器，返回实际执行个数。 */
async function runTimers(count) {
  let ran = 0;
  while (ran < count && (await runTimer())) ran += 1;
  return ran;
}

// Tauri IPC 桩：sidecar_info 给固定端口；原生保存框记下默认文件名后返回「用户取消」。
const dialogs = [];
window.__TAURI_INTERNALS__ = {
  invoke: async (command, args) => {
    if (command === "sidecar_info") return { port: 1, token: "test-token" };
    if (command === "plugin:dialog|save") {
      dialogs.push(args?.options ?? {});
      return null;
    }
    return null;
  },
  transformCallback: () => 0,
  unregisterCallback: noop,
};

const okResponse = (payload, headers = {}) => ({
  ok: true,
  status: 200,
  statusText: "OK",
  headers: { get: (name) => headers[String(name).toLowerCase()] ?? null },
  json: async () => payload,
  arrayBuffer: async () => new ArrayBuffer(0),
});
const failedResponse = (status, detail) => ({
  ok: false,
  status,
  statusText: "ERR",
  headers: { get: () => null },
  json: async () => ({ detail }),
  arrayBuffer: async () => new ArrayBuffer(0),
});

const findAll = (node, predicate, found = []) => {
  if (!node || typeof node !== "object") return found;
  if (predicate(node)) found.push(node);
  for (const child of node.children ?? []) findAll(child, predicate, found);
  return found;
};
const textOf = (node) =>
  (typeof node?.textContent === "string" ? node.textContent : node?._text ?? "") +
  (node?.children ?? []).map(textOf).join("");
const buttonsIn = (node) => findAll(node, (candidate) => candidate.tagName === "BUTTON");
const toasts = () =>
  findAll(documentStub.body, (node) => typeof node.className === "string" && node.className.includes("toast")).map(textOf);

// ---------------------------------------------------------------------------
// 场景 1：saveBinaryDownload 的默认文件名
// ---------------------------------------------------------------------------
async function save_binary_download_prefers_server_filename() {
  globalThis.fetch = async (url) => {
    const path = String(url);
    if (path.endsWith("/with-name")) {
      return okResponse({}, { "content-disposition": 'attachment; filename="translator-diagnostic-2026.zip"' });
    }
    if (path.endsWith("/with-path")) {
      return okResponse({}, { "content-disposition": 'attachment; filename="../../escape.zip"' });
    }
    return okResponse({});
  };
  const { ApiClient } = await import(pathToFileURL(resolvePath(UI, "api-client.ts")).href);
  const client = new ApiClient();
  await client.connect();

  await client.saveBinaryDownload("/with-name", "fallback.zip");
  assert.equal(
    dialogs.at(-1)?.defaultPath,
    "translator-diagnostic-2026.zip",
    "服务端给了 filename 时，保存框默认名必须用它——诊断包名字里的时间戳和场景是后端算的",
  );

  await client.saveBinaryDownload("/without-name", "fallback.zip");
  assert.equal(dialogs.at(-1)?.defaultPath, "fallback.zip", "没有 Content-Disposition 时必须退回调用方给的兜底名");

  // 钉住「原样使用」这件事本身：审计已判定本地 sidecar 可信、不构成可上报缺陷。
  // 将来若允许接远端服务，这条会先红，提醒先做文件名清洗，而不是悄悄带着路径分隔符进保存框。
  await client.saveBinaryDownload("/with-path", "fallback.zip");
  assert.equal(dialogs.at(-1)?.defaultPath, "../../escape.zip", "当前实现原样采用服务端文件名（本地 sidecar 可信）");
}

// ---------------------------------------------------------------------------
// 场景 2：runPdfBatchRerun 单页失败后跳过 waitForRerunSlot，直接发下一页
// ---------------------------------------------------------------------------
async function batch_rerun_skips_the_slot_wait_after_a_failure() {
  const requests = [];
  globalThis.fetch = async (url, init) => {
    const path = String(url);
    const method = init?.method ?? "GET";
    requests.push({ method, path, body: init?.body });
    if (path.endsWith("/health") || path.endsWith("/api/tasks")) return okResponse({});
    if (path.endsWith("/pdf-pages") && method === "GET") {
      return okResponse({
        task_id: "task-1",
        state: "done",
        terminal: true,
        actionable: false,
        review_enabled: false,
        rerun_actionable: true,
        rerun: { active: false, relative_path: "", page_number: 0, error: "" },
        files: [],
      });
    }
    if (path.endsWith("/pdf-pages/rerun")) {
      const page = JSON.parse(init?.body ?? "{}").page;
      // 第一页失败、第二页成功：失败那一页要是不等槽位就往下发，第二页会紧跟在后面。
      return page === 1 ? failedResponse(500, "rerun failed") : okResponse({});
    }
    return okResponse({});
  };

  const page = (number) => ({
    page_number: number,
    status: "failed",
    review_status: "skipped",
    attempts: 1,
    placeholder: false,
    error: "",
    review_summary: "",
    pending_action: "",
    user_skipped: false,
    skipped_oversize: false,
    quality_flagged: false,
  });
  const snapshot = {
    task_id: "task-1",
    state: "done",
    terminal: true,
    actionable: false,
    review_enabled: false,
    rerun_actionable: true,
    rerun: { active: false, relative_path: "", page_number: 0, error: "" },
    files: [
      {
        relative_path: "a.pdf",
        name: "a.pdf",
        page_count: 2,
        status: "completed",
        error: "",
        pages: [page(1), page(2)],
      },
    ],
  };

  const { buildPdfReviewCard } = await import(pathToFileURL(resolvePath(UI, "views/workspace-pdf-pages.ts")).href);
  const { stateOf } = await import(pathToFileURL(resolvePath(UI, "views/workspace.ts")).href);
  stateOf("pdf").task = {
    task: { task_id: "task-1", surface: "pdf", state: "done", terminal: true },
    logs: [],
    phaseName: "",
    stageName: "",
    percent: 100,
    phaseIndex: 0,
    phaseTotal: 0,
    stageNames: new Map(),
    streamState: "connected",
    watcherActive: false,
    lastEventAt: 0,
    fileProgress: new Map(),
    pdfPagesSnapshot: snapshot,
  };

  const card = buildPdfReviewCard("pdf", stateOf("pdf").task);
  assert.ok(card, "终态任务应当渲染出逐页卡片");
  const batchButton = buttonsIn(card).find((button) => textOf(button).includes("重新生成"));
  assert.ok(batchButton, "卡片上应当有「仅重新生成这 N 页」按钮");
  batchButton.click();
  await sleep(0);
  const confirm = buttonsIn(documentStub.body).find((button) => textOf(button) === "重新生成这 2 页");
  assert.ok(confirm, "确认弹窗里应当有「重新生成这 2 页」按钮");
  confirm.click();
  await sleep(20);

  const rerunPosts = requests.filter((request) => request.path.endsWith("/pdf-pages/rerun"));
  assert.deepEqual(
    rerunPosts.map((request) => JSON.parse(request.body).page),
    [1, 2],
    "第一页失败后仍然发第二页——这是当前实现的缺口行为，测试钉住它，改行为时先改这里",
  );
  const firstSnapshot = requests.findIndex((request) => request.path.endsWith("/pdf-pages") && request.method === "GET");
  const secondPost = requests.findIndex(
    (request) => request.path.endsWith("/pdf-pages/rerun") && JSON.parse(request.body).page === 2,
  );
  assert.ok(firstSnapshot === -1 || firstSnapshot > secondPost, "两页 POST 之间没有快照请求：失败分支跳过了 waitForRerunSlot");
  assert.ok(
    toasts().some((message) => message.includes("rerun failed")),
    "失败那一页要弹出错误 toast（详情来自后端 detail）",
  );

  // 剩下第二页的等待槽位定时器跑完，整批应当收尾，不会自己卡住。
  await runTimers(8);
  assert.equal(
    requests.filter((request) => request.path.endsWith("/pdf-pages/rerun")).length,
    2,
    "整批只有这两页，收尾后不该再发请求",
  );
}

// ---------------------------------------------------------------------------
// 场景 3：watchTask 在事件流持续失败时跨轮无限重连
// ---------------------------------------------------------------------------
async function watch_task_reconnects_across_rounds_without_a_cap() {
  let streamAttempts = 0;
  let taskReads = 0;
  globalThis.fetch = async (url) => {
    const path = String(url);
    if (path.endsWith("/api/tasks")) return okResponse({ active: [], recent: [] });
    if (path.endsWith("/events")) {
      streamAttempts += 1;
      return failedResponse(503, "stream down");
    }
    if (/\/api\/tasks\/[^/]+$/.test(path)) {
      taskReads += 1;
      // 事件流永远坏、任务详情永远「还在跑」——正是审计说的无上限重连场景。
      return okResponse({ task_id: "t-watch", surface: "excel", state: "running", terminal: false });
    }
    return okResponse({});
  };

  const { focusTask } = await import(pathToFileURL(resolvePath(UI, "views/tasks.ts")).href);
  focusTask({ task_id: "t-watch", surface: "excel", state: "running", terminal: false, logs: [] });
  await sleep(20);
  await runTimers(40);

  assert.ok(taskReads >= 2, "每轮耗尽后 catch 里要拉一次任务详情决定是否续连");
  assert.ok(
    streamAttempts >= 14,
    `事件流一直 503 时应当已经跨轮重连（至少 14 次尝试），实际 ${streamAttempts} 次——不足说明没跑满两轮`,
  );
  return `${streamAttempts} 次事件流尝试 / ${taskReads} 次任务详情`;
}

const SCENARIOS = {
  save_binary_download_prefers_server_filename,
  batch_rerun_skips_the_slot_wait_after_a_failure,
  watch_task_reconnects_across_rounds_without_a_cap,
};

const only = process.argv[2];
const selected = only ? [[only, SCENARIOS[only]]] : Object.entries(SCENARIOS);
if (!selected.length || selected.some(([, run]) => typeof run !== "function")) {
  console.error(`未知场景：${only}；可选：${Object.keys(SCENARIOS).join(", ")}`);
  process.exit(2);
}
for (const [name, run] of selected) {
  const detail = await run();
  console.log(`ok - ${name}${detail ? `：${detail}` : ""}`);
}
console.log(`ui async flows: ${selected.length} 个场景通过`);
