// Excel 页 Phase 4 的四个控件（公式回填 / 用 Excel 调行高 / 保持行高 / 仅补译）的界面回归。
//
// docs/validation/2026-07-24_phase-04-excel_acceptance.md:52 记着「未取得手工 UI DOM/截图断言，
// 原因是当前 macOS 锁屏」，之后 2026-10-02 的 workspace-status-regression.mjs 也只跑了
// 扫描 → 开始 → 完成 → 重扫 → 任务中心，一个 Excel 选项都没碰。这里补上那一段：在 Node 里
// 用一套最小 DOM 直接跑 ui/src/views/workspace.ts 与 ui/src/components.ts 的真代码，
// 渲染「高级」折叠块，然后按用户的方式点开关，断言 DOM 状态和落盘补丁。
//
// 断言的是这些：
//   1. 四个控件都真的画出来了，role="switch"，aria-checked 等于默认值，且各带「?」说明；
//   2. 「用 Excel 调整行高」与「保持行高，缩小字号」互斥，而且两边一起落盘（只写一半会
//      在磁盘上留下「两个都开」的脏数据，下次启动水合出来就是设置被默认替代的样子）；
//   3. 磁盘上已经是「两个都开」的老脏数据时，界面按后端的裁决只亮锁定行高那一半；
//   4. 保存失败时界面退回原状并给出提示，不会显示一个其实没存上的开关；
//   5. 「仅补译未翻译内容」与「翻译公式单元格」的开关值真的进请求 / 进落盘补丁；
//   6. 任务运行中四个控件都锁住。
//
// 运行：node tests/excel_controls_ui.mjs（由 tests/test_workspace_ui_coverage.py 在 pytest 里调用）

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import vm from "node:vm";

// 需要 Node ≥ 22.13 的 module.stripTypeScriptTypes（CI 的 UI job 用 Node 24）。
// 更老的 Node 上没有这个 API，tests/test_workspace_ui_coverage.py 会跳过，而不是报一个看不懂的错。

// ── 最小 DOM：只有 workspace.ts / components.ts 真的用到的那些 API ─────────────
class TextNode {
  constructor(text) { this.textContent = String(text); this.parentNode = null; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
}

class Element_ {
  constructor(tag, doc) {
    this.tagName = String(tag || "div").toUpperCase();
    this.doc = doc;
    this.children = [];
    this.parentNode = null;
    this.attrs = new Map();
    this.listeners = new Map();
    this._class = "";
    this._text = "";
    this.dataset = {};
    this.style = { setProperty() {}, removeProperty() {}, getPropertyValue: () => "" };
    this.value = "";
    this.type = "";
    this.disabled = false;
    this.tabIndex = 0;
    this.classList = {
      add: (...names) => { const set = new Set(this._words()); for (const name of names) set.add(name); this._class = [...set].join(" "); },
      remove: (...names) => { const set = new Set(this._words()); for (const name of names) set.delete(name); this._class = [...set].join(" "); },
      toggle: (name, force) => { const set = new Set(this._words()); const want = force === undefined ? !set.has(name) : Boolean(force); if (want) set.add(name); else set.delete(name); this._class = [...set].join(" "); return want; },
      contains: (name) => this._words().includes(name),
    };
  }
  _words() { return this._class.split(/\s+/).filter(Boolean); }
  get className() { return this._class; }
  set className(value) { this._class = String(value || ""); }
  get textContent() { return this.children.length ? this.children.map((child) => child.textContent).join("") : this._text; }
  set textContent(value) { this.children = []; this._text = String(value ?? ""); }
  get firstChild() { return this.children[0] ?? null; }
  get childNodes() { return this.children; }
  get lastChild() { return this.children[this.children.length - 1] ?? null; }
  append(...nodes) {
    for (const node of nodes) {
      const child = typeof node === "string" ? this.doc.createTextNode(node) : node;
      if (child.parentNode) child.remove();
      child.parentNode = this;
      this.children.push(child);
    }
  }
  appendChild(node) { this.append(node); return node; }
  insertBefore(node, reference) {
    const index = reference ? this.children.indexOf(reference) : -1;
    if (index < 0) { this.append(node); return node; }
    if (node.parentNode) node.remove();
    node.parentNode = this;
    this.children.splice(index, 0, node);
    return node;
  }
  before(node) { if (this.parentNode) this.parentNode.insertBefore(node, this); }
  after(node) { if (this.parentNode) this.parentNode.insertBefore(node, this.nextSibling()); }
  nextSibling() {
    if (!this.parentNode) return null;
    const siblings = this.parentNode.children;
    return siblings[siblings.indexOf(this) + 1] ?? null;
  }
  removeChild(node) { this.children = this.children.filter((child) => child !== node); return node; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  setAttribute(name, value) { this.attrs.set(String(name), String(value)); }
  getAttribute(name) { return this.attrs.has(String(name)) ? this.attrs.get(String(name)) : null; }
  removeAttribute(name) { this.attrs.delete(String(name)); }
  hasAttribute(name) { return this.attrs.has(String(name)); }
  addEventListener(type, listener) { if (!this.listeners.has(type)) this.listeners.set(type, []); this.listeners.get(type).push(listener); }
  removeEventListener(type, listener) { const list = this.listeners.get(type); if (list) this.listeners.set(type, list.filter((item) => item !== listener)); }
  dispatch(type, event = {}) { for (const listener of [...(this.listeners.get(type) ?? [])]) listener({ target: this, ...event }); }
  click() { this.dispatch("click"); }
  focus() {}
  blur() {}
  setSelectionRange() {}
  getBoundingClientRect() { return { left: 0, top: 0, width: 120, height: 20, right: 120, bottom: 20 }; }
  matches(selector) { return matchesSelector(this, selector); }
  closest(selector) { let node = this; while (node) { if (node.matches?.(selector)) return node; node = node.parentNode; } return null; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  querySelectorAll(selector) {
    const parts = String(selector).split(",").map((part) => part.trim());
    // workspace.ts 用 `:scope > .swrow, :scope > .field` 数右栏项，别的选择器这里用不到。
    if (parts.every((part) => part.startsWith(":scope >"))) {
      const inner = parts.map((part) => part.slice(":scope >".length).trim());
      return this.children.filter((child) => inner.some((part) => matchesSelector(child, part)));
    }
    if (parts.some((part) => part.startsWith(":scope"))) return [];
    const found = [];
    const walk = (node) => {
      for (const child of node.children ?? []) {
        if (child instanceof TextNode) continue;
        if (matchesSelector(child, selector)) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }
  get outerHTML() { return `<${this.tagName.toLowerCase()}>`; }
  cloneNode() { const copy = new Element_(this.tagName.toLowerCase(), this.doc); copy._class = this._class; copy._text = this._text; return copy; }
}

function matchesSelector(node, selector) {
  if (!node || node instanceof TextNode) return false;
  return String(selector).split(",").some((part) => matchOne(node, part.trim()));
}

function matchOne(node, selector) {
  if (!selector) return false;
  if (selector.startsWith(".")) return node._words().includes(selector.slice(1));
  if (selector.startsWith("[")) {
    const body = selector.slice(1, -1);
    const [name, value] = body.split("=");
    if (value === undefined) return node.hasAttribute(name);
    return node.getAttribute(name) === value.replace(/^["']|["']$/g, "");
  }
  return selector === "*" || node.tagName === selector.toUpperCase();
}

function createDocument() {
  const doc = {
    createElement: (tag) => new Element_(tag, doc),
    createTextNode: (text) => new TextNode(text),
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener() {},
    removeEventListener() {},
    body: null,
  };
  doc.body = new Element_("body", doc);
  return doc;
}

// ── 加载真代码：去掉 import / export 与 TS 类型，放进 vm 里跑 ─────────────────
function loadModule(file) {
  const source = readFileSync(new URL(`../${file}`, import.meta.url), "utf8")
    .replace(/^import\s+[\s\S]*?from\s+["'][^"']+["'];?\s*$/gm, "")
    .replace(/^import\s+["'][^"']+["'];?\s*$/gm, "")
    .replace(/^export\s+/gm, "");
  return stripTypeScriptTypes(source);
}

function createWorkspace() {
  const document = createDocument();
  const state = { patches: [], toasts: [], renders: 0, failNextSave: false };
  const context = vm.createContext({
    console,
    document,
    window: {
      addEventListener() {},
      removeEventListener() {},
      localStorage: { getItem: () => null, setItem() {} },
      clearTimeout() {},
      setTimeout: () => 0,
      matchMedia: () => ({ matches: false }),
    },
    setTimeout: () => 0,
    clearTimeout() {},
    setInterval: () => 0,
    clearInterval: () => {},
    performance: { now: () => 0 },
    MutationObserver: class { observe() {} },
    ResizeObserver: class { observe() {} disconnect() {} },
    ApiClient: class {},
    // i18n 在中文界面下原样返回；本测试只看控件与落盘，不看译文。
    t: (source, ...args) => String(source).replace(/\{(\d+)\}/g, (all, index) => String(args[index] ?? all)),
    tx: (value) => value,
    localized: (build) => build(),
    fmtNum: String,
    isSingularCount: () => false,
    localeTag: () => "zh-CN",
    currentLocale: () => "zh",
    isChineseLocale: () => true,
    icon: () => document.createElement("span"),
    EASE_SNAP: 0,
    T_BASE: 0,
    motionAllowed: () => false,
    morphChildren() {},
    setReusableClick() {},
    syncAttributes() {},
    record: (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {}),
    text: (value, fallback = "") => (typeof value === "string" && value ? value : fallback),
    num: (value, fallback = 0) => (typeof value === "number" ? value : fallback),
    hasOwn: (object, key) => Object.prototype.hasOwnProperty.call(object, key),
    nestedPatch: (path, value) => {
      const parts = path.split(".");
      const last = parts.pop();
      const root = {};
      let cursor = root;
      for (const part of parts) { const next = {}; cursor[part] = next; cursor = next; }
      cursor[last] = value;
      return root;
    },
    el: (tag, className) => { const node = document.createElement(tag); if (className) node.className = className; return node; },
    isExtSurface: (surface) => String(surface).startsWith("ext:"),
    localizedText: (value, fallback = "") => (value && typeof value === "object" ? value.zh || value.en || fallback : value || fallback),
    surfaceDescriptor: () => null,
    extSurfaceLabel: () => "",
    extSurfaceIcon: () => "grid",
    redactedText: (value, fallback = "") => String(value || fallback),
    normalizeUserPath: (value) => String(value || ""),
    fitPlaceholder: () => {},
    FIT_CLIP: "fit-clip",
    showPathWithMiddleEllipsis: () => {},
    formatSizeKb: () => "",
    formatTime: () => "",
  });

  vm.runInContext(loadModule("ui/src/components.ts"), context);
  vm.runInContext(loadModule("ui/src/views/workspace.ts"), context);
  vm.runInContext(`
    globalThis.__state = { patches: [], toasts: [], renders: 0, failNextSave: false };
    persistSettings = async (patch) => {
      if (__state.failNextSave) { __state.failNextSave = false; throw new Error("保存失败（测试）"); }
      __state.patches.push(patch);
    };
    rerender = () => { __state.renders += 1; };
    showToast = (options) => { __state.toasts.push(options); };
    rescanAfterScanInputChange = () => {};
    globalThis.__getState = () => __state;
  `, context);
  return context;
}

// ── 断言 ────────────────────────────────────────────────────────────────────
const checks = [];
/** vm 里的对象原型来自另一个 realm，deepStrictEqual 会比原型；按 JSON 文本比较更稳。 */
const asJson = (value) => JSON.stringify(value);
async function check(name, fn) {
  await fn();
  checks.push(name);
}
/** 开关的 onChange 是 async（先 PUT 再重画），点完要让出事件循环等它落盘。 */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

const PHASE4_CONTROLS = [
  { label: "翻译公式单元格", key: "formulaBackfill", defaultChecked: true },
  { label: "用 Excel 调整行高", key: "excelAutofit", defaultChecked: false },
  { label: "保持行高，缩小字号", key: "lockRowHeight", defaultChecked: false },
  { label: "仅补译未翻译内容", key: "untranslated", defaultChecked: false },
];

function switchRow(fold, label) {
  const row = fold.querySelectorAll(".swrow").find((item) => item.textContent.startsWith(label));
  assert.ok(row, `找不到「${label}」这一行`);
  return row;
}

function switchOf(fold, label) {
  const control = switchRow(fold, label).querySelector(".sw");
  assert.ok(control, `「${label}」没有开关`);
  return control;
}

function renderFold(context) {
  return context.buildAdvancedFold("excel", context.stateOf("excel"), false);
}

function hydratedContext(savedExcelOutput) {
  const context = createWorkspace();
  const payload = JSON.stringify({ excel_output: savedExcelOutput });
  vm.runInContext(`settings = ${payload}; applySettingsToStates();`, context);
  return context;
}

(async () => {
// 1) 四个 Phase 4 控件都在页面上，都是 role="switch"，aria-checked 等于默认值。
{
  const context = createWorkspace();
  const fold = renderFold(context);
  await check("Excel 高级折叠块渲染出 Phase 4 的四个控件", () => {
    assert.ok(fold, "「高级」折叠块没有渲染出来");
    for (const control of PHASE4_CONTROLS) {
      const toggle = switchOf(fold, control.label);
      assert.equal(toggle.getAttribute("role"), "switch", `${control.label}: role`);
      assert.equal(toggle.getAttribute("aria-checked"), String(control.defaultChecked), `${control.label}: aria-checked`);
      assert.equal(toggle.classList.contains("on"), control.defaultChecked, `${control.label}: .on`);
    }
  });
  await check("每个控件都带「?」说明，说明文字就是它那一段完整提示", () => {
    for (const control of PHASE4_CONTROLS) {
      const badge = switchRow(fold, control.label).querySelector(".hint");
      assert.ok(badge, `${control.label}: 缺少提示角标`);
      const label = badge.getAttribute("aria-label") ?? "";
      assert.ok(label.length > 10, `${control.label}: 提示为空`);
    }
  });
  await check("行高两个开关的说明写清了不能同时使用", () => {
    const autofit = switchRow(fold, "用 Excel 调整行高").querySelector(".hint").getAttribute("aria-label");
    const lock = switchRow(fold, "保持行高，缩小字号").querySelector(".hint").getAttribute("aria-label");
    assert.ok(autofit.includes("Excel"), autofit);
    assert.ok(lock.includes("不能与"), lock);
  });
}

// 2) 点「用 Excel 调整行高」：自己亮、锁行高灭，两边一起落盘。
{
  const context = hydratedContext({ enable_excel_autofit: false, lock_row_height: true });
  const state = context.__getState();
  const fold = renderFold(context);
  const autofit = switchOf(fold, "用 Excel 调整行高");
  const lock = switchOf(fold, "保持行高，缩小字号");
  await check("互斥前：锁行高亮着、Excel 行高灭着", () => {
    assert.equal(lock.getAttribute("aria-checked"), "true");
    assert.equal(autofit.getAttribute("aria-checked"), "false");
  });
  autofit.click();
  await settle();
  // 真的应用里 onChange 落盘成功后会 rerender(surface) 重画右栏；这里没有 mounted
  // 容器，按同样的重建方式拿一份新的折叠块来断言（等价于用户看到的那一屏）。
  const after = renderFold(context);
  await check("点完：DOM 上 Excel 行高亮、锁行高灭", () => {
    assert.equal(switchOf(after, "用 Excel 调整行高").getAttribute("aria-checked"), "true");
    assert.equal(switchOf(after, "用 Excel 调整行高").classList.contains("on"), true);
    assert.equal(switchOf(after, "保持行高，缩小字号").getAttribute("aria-checked"), "false");
    assert.equal(switchOf(after, "保持行高，缩小字号").classList.contains("on"), false);
  });
  await check("点完：状态跟着改（不是只改了 ARIA）", () => {
    const state_ = context.stateOf("excel");
    assert.equal(state_.toggles.get("excelAutofit"), true);
    assert.equal(state_.toggles.get("lockRowHeight"), false);
  });
  await check("点完：一次 PUT 把互斥的两半都写上", () => {
    const patch = state.patches.at(-1);
    assert.equal(asJson(patch), asJson({
      excel_output: { enable_excel_autofit: true, lock_row_height: false },
    }));
  });
}

// 3) 反方向：点「保持行高，缩小字号」也把 Excel 行高那一半写 false。
{
  const context = hydratedContext({ enable_excel_autofit: true, lock_row_height: false });
  const state = context.__getState();
  const fold = renderFold(context);
  switchOf(fold, "保持行高，缩小字号").click();
  await settle();
  const after = renderFold(context);
  await check("打开「保持行高」会关掉「用 Excel 调整行高」", () => {
    const state_ = context.stateOf("excel");
    assert.equal(state_.toggles.get("lockRowHeight"), true);
    assert.equal(state_.toggles.get("excelAutofit"), false);
    assert.equal(switchOf(after, "保持行高，缩小字号").getAttribute("aria-checked"), "true");
    assert.equal(switchOf(after, "用 Excel 调整行高").getAttribute("aria-checked"), "false");
    assert.equal(asJson(state.patches.at(-1)), asJson({
      excel_output: { lock_row_height: true, enable_excel_autofit: false },
    }));
  });
}

// 4) 老脏数据（磁盘上两个都开着）：界面按后端裁决只亮锁行高。
{
  const context = hydratedContext({ enable_excel_autofit: true, lock_row_height: true });
  const fold = renderFold(context);
  await check("老脏数据水合后只亮「保持行高，缩小字号」", () => {
    assert.equal(switchOf(fold, "保持行高，缩小字号").getAttribute("aria-checked"), "true");
    assert.equal(switchOf(fold, "用 Excel 调整行高").getAttribute("aria-checked"), "false");
  });
  await check("老脏数据水合不会把两个都画成开着", () => {
    const checked = ["用 Excel 调整行高", "保持行高，缩小字号"]
      .map((label) => switchOf(fold, label).getAttribute("aria-checked"));
    assert.deepEqual(checked, ["false", "true"]);
  });
}

// 5) 保存失败：开关退回原状，并弹出「已恢复为原设置」的提示。
{
  const context = hydratedContext({ enable_excel_autofit: false, lock_row_height: false });
  const state = context.__getState();
  const fold = renderFold(context);
  state.failNextSave = true;
  switchOf(fold, "用 Excel 调整行高").click();
  await settle();
  await settle();
  const after = renderFold(context);
  await check("保存失败后界面退回原状", () => {
    const state_ = context.stateOf("excel");
    assert.equal(state_.toggles.get("excelAutofit"), false, "状态没有退回");
    assert.equal(state_.toggles.get("lockRowHeight"), false);
    assert.equal(switchOf(after, "用 Excel 调整行高").getAttribute("aria-checked"), "false");
  });
  await check("保存失败后有提示，且重画了一次", () => {
    assert.ok(state.toasts.length >= 1, "没有提示");
    assert.ok(String(state.toasts.at(-1).message).includes("保存失败"), String(state.toasts.at(-1).message));
    assert.ok(state.renders >= 1, "没有重画");
  });
}

// 6) 「仅补译未翻译内容」与公式回填：开关状态要真的进请求 / 落盘。
{
  const context = hydratedContext({ formula_display_value_backfill: true });
  const state = context.__getState();
  const st = context.stateOf("excel");
  st.sourcePath = "/tmp/sample.xlsx";
  st.files = [{ path: "/tmp/sample.xlsx", name: "sample", size_kb: 1 }];
  st.selected = new Set(["/tmp/sample.xlsx"]);
  const fold = renderFold(context);
  switchOf(fold, "仅补译未翻译内容").click();
  await settle();
  await check("打开「仅补译未翻译内容」后请求里带 untranslated_only=true", () => {
    assert.equal(context.buildPayload("excel", st).untranslated_only, true);
  });
  await check("「翻译公式单元格」关掉后落盘 excel_output.formula_display_value_backfill=false", async () => {
    switchOf(fold, "翻译公式单元格").click();
    await settle();
    const patch = state.patches.at(-1);
    assert.equal(asJson(patch), asJson({ excel_output: { formula_display_value_backfill: false } }));
  });
}

// 7) 开关在任务运行中锁住，不能点。
{
  const context = createWorkspace();
  const fold = context.buildAdvancedFold("excel", context.stateOf("excel"), true);
  await check("任务运行中四个控件都是禁用的", () => {
    for (const control of PHASE4_CONTROLS) {
      assert.equal(switchOf(fold, control.label).disabled, true, `${control.label} 没有禁用`);
    }
  });
}

console.log(`Excel 控件界面回归：${checks.length} 项断言通过`);
for (const name of checks) console.log(`  ✓ ${name}`);

})().catch((error) => { console.error(error); process.exit(1); });
