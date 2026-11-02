// node tests/appearance_theme_dom.mjs —— 外观验收项「深浅主题切换 + 跟随系统；配置面板折叠
// 状态记忆」（docs/refactor/2026-07-16_route_b_tauri_implementation.md:172）的 DOM 级断言。
//
// 以前这条只在 Python 里测 schema 迁移和源码文本，没有任何一处真的把「点一下主题、系统换主题、
// 后端值回填、折叠块重画后还记得」跑一遍。这里用最小 DOM 垫片执行 ui/src 里的真实源码
// （和 tests/test_update_activity.cjs、agent/testing/settings-data-health-cold-start.cjs 同一路数：
// 从真实 .ts 里切出真函数，只替掉它周围的模块依赖），断言的是 document.documentElement.dataset
// 和元素树的状态，不是源码里有没有某个字符串。
//
// 覆盖：
//   1. app.ts initTheme：localStorage 里的偏好决定首屏主题；「跟随系统」时系统换主题要实时跟上；
//      系统变了但用户选的是固定浅色/深色时不许跟着变。
//   2. app.ts syncAppearanceFromBackend：后端 settings.appearance.theme 是权威值，本地缓存与
//      界面都要被它回填（换 WebView 数据目录、重置设置后两者会不一致）。
//   3. settings.ts 的主题三个函数：点选只切可见主题（不写 localStorage），落盘成功后才写缓存。
//   4. settings.ts settingsFold + components.ts createFold：折叠块展开后整页重画，同名 key 仍然
//      是展开的；没碰过的 key 保持默认。

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import vm from "node:vm";

const ROOT = new URL("../", import.meta.url);
const APP_TS = readFileSync(new URL("ui/src/app.ts", ROOT), "utf8");
const SETTINGS_TS = readFileSync(new URL("ui/src/views/settings.ts", ROOT), "utf8");
const COMPONENTS_TS = readFileSync(new URL("ui/src/components.ts", ROOT), "utf8");

/** 从真实源码里切一段：起点含、终点不含。起点找不到就直接报错，避免悄悄切空。 */
function slice(source, startMarker, endMarker, label) {
  const start = source.indexOf(startMarker);
  assert.ok(start >= 0, `${label}: 找不到起点 ${JSON.stringify(startMarker)}`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.ok(end > start, `${label}: 找不到终点 ${JSON.stringify(endMarker)}`);
  return source.slice(start, end);
}

// ── 最小 DOM 垫片 ─────────────────────────────────────────────────────────────
// 只实现这几个函数真正用到的东西：createElement / createTextNode / className / textContent /
// style / dataset / setAttribute / classList.toggle / append / addEventListener + 触发。

function makeElement(tag) {
  const listeners = new Map();
  const classes = new Set();
  const node = {
    tagName: String(tag || "div").toUpperCase(),
    className: "",
    textContent: "",
    style: {},
    dataset: {},
    attributes: {},
    childNodes: [],
    parentNode: null,
    classList: {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name),
      toggle: (name, force) => {
        const next = force === undefined ? !classes.has(name) : Boolean(force);
        if (next) classes.add(name);
        else classes.delete(name);
        return next;
      },
    },
    setAttribute(name, value) {
      this.attributes[name] = String(value);
    },
    getAttribute(name) {
      return name in this.attributes ? this.attributes[name] : null;
    },
    append(...children) {
      for (const child of children) {
        if (typeof child === "string") {
          this.childNodes.push(makeTextNode(child));
        } else {
          child.parentNode = this;
          this.childNodes.push(child);
        }
      }
    },
    appendChild(child) {
      this.append(child);
      return child;
    },
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(handler);
    },
    dispatch(type, event = {}) {
      for (const handler of listeners.get(type) ?? []) handler({ type, preventDefault() {}, ...event });
    },
    /** 测试用：按 class 找后代（这几位函数只往两层里塞，够用）。 */
    findAll(predicate, found = []) {
      for (const child of this.childNodes) {
        if (child instanceof Object && typeof child.findAll === "function") {
          if (predicate(child)) found.push(child);
          child.findAll(predicate, found);
        }
      }
      return found;
    },
    find(predicate) {
      return this.findAll(predicate)[0] ?? null;
    },
  };
  // className 与 classList 必须同源，否则 toggle 之后 className 看不到变化。
  Object.defineProperty(node, "className", {
    get() {
      return [...classes].join(" ");
    },
    set(value) {
      classes.clear();
      for (const name of String(value || "").split(/\s+/)) if (name) classes.add(name);
    },
  });
  return node;
}

function makeTextNode(text) {
  // 文本节点也要有 className（空串），否则遍历时 `node.className.split` 会炸。
  return { nodeType: 3, className: "", textContent: text, childNodes: [], findAll: () => [] };
}

function makeDocument() {
  const documentElement = makeElement("html");
  return {
    documentElement,
    createElement: makeElement,
    createTextNode: makeTextNode,
  };
}

/** 一份可复位的运行环境：document / window.matchMedia / localStorage 都换成新的。 */
function makeEnv({ prefersDark = false, stored = {} } = {}) {
  const document = makeDocument();
  const store = new Map(Object.entries(stored));
  const listeners = [];
  const media = {
    matches: prefersDark,
    addEventListener: (_type, handler) => listeners.push(handler),
  };
  const window = {
    localStorage: {
      getItem: (key) => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => store.set(key, String(value)),
      removeItem: (key) => store.delete(key),
    },
    matchMedia: () => media,
    setTimeout: () => 0,
  };
  return {
    document,
    window,
    store,
    /** 模拟系统在「浅色 / 深色」之间切换，并通知所有 change 监听器。 */
    setSystemDark(next) {
      media.matches = next;
      for (const handler of listeners) handler({ matches: next, media: "(prefers-color-scheme: dark)" });
    },
    listenerCount: () => listeners.length,
  };
}

/** 用给定源码 + 附加桩函数跑一个独立上下文。 */
function run(source, env, extra = {}) {
  const sandbox = {
    console,
    document: env.document,
    window: env.window,
    localStorage: env.window.localStorage,
    ...extra,
  };
  vm.createContext(sandbox);
  // vm.runInContext 拿到的是脚本而不是模块，不能带 export；切出来的片段里可能夹着
  // `export function record(...)` 这类声明，去掉 `export ` 前缀即可（函数体本身合法）。
  const script = `${stripTypeScriptTypes(source)}\n${WRAPPER}`.replace(/^export\s+/gm, "");
  vm.runInContext(script, sandbox);
  return sandbox;
}

const WRAPPER = `
globalThis.__exports = {
  resolveTheme: typeof resolveTheme === "function" ? resolveTheme : undefined,
  readStoredThemePreference: typeof readStoredThemePreference === "function" ? readStoredThemePreference : undefined,
  applyTheme: typeof applyTheme === "function" ? applyTheme : undefined,
  initTheme: typeof initTheme === "function" ? initTheme : undefined,
  syncAppearanceFromBackend: typeof syncAppearanceFromBackend === "function" ? syncAppearanceFromBackend : undefined,
  applyThemeVisual: typeof applyThemeVisual === "function" ? applyThemeVisual : undefined,
  commitThemeToLocalStorage: typeof commitThemeToLocalStorage === "function" ? commitThemeToLocalStorage : undefined,
  currentThemePreference: typeof currentThemePreference === "function" ? currentThemePreference : undefined,
  text: typeof text === "function" ? text : undefined,
  record: typeof record === "function" ? record : undefined,
  settingsFold: typeof settingsFold === "function" ? settingsFold : undefined,
  createFold: typeof createFold === "function" ? createFold : undefined,
  el: typeof el === "function" ? el : undefined,
};
`;

// app.ts：主题偏好 → 首屏可见主题 → 跟随系统 → 后端回填。
const APP_THEME_SOURCE = slice(
  APP_TS,
  "const THEME_STORAGE_KEY",
  "/**\n * 侧栏「翻译」组",
  "app.ts 主题段",
);

// settings.ts：设置页自己的主题三个函数（可见切换 / 落盘后写缓存 / 读后端值）。
const SETTINGS_THEME_SOURCE = slice(
  SETTINGS_TS,
  'const THEME_STORAGE_KEY = "translator.theme";',
  "function currentPalettePreference",
  "settings.ts 主题段",
);

// settings.ts 的 record / text：currentThemePreference 依赖它们，取真身而不是自己写一份。
const SETTINGS_HELPERS_SOURCE = [
  slice(SETTINGS_TS, "export function record(value: unknown)", "export function text(", "record"),
  slice(SETTINGS_TS, "export function text(value: unknown", "export function num(", "text"),
].join("\n");

// settings.ts：折叠块记忆（foldOpen 是模块级状态，整页重画不会丢）。
const SETTINGS_FOLD_SOURCE = slice(
  SETTINGS_TS,
  "const foldOpen: Record<string, boolean> = {};",
  "/**\n * 把「模型名称」输入框包成一个可下拉的组合框",
  "settingsFold",
);

// components.ts：折叠块本身（开合、aria-expanded、.open、body 显隐）。
const COMPONENTS_FOLD_SOURCE = [
  slice(COMPONENTS_TS, "function el<K extends keyof HTMLElementTagNameMap>", "// ---------------------------------------------------------------------------\n// card", "el"),
  slice(COMPONENTS_TS, "export function createFold(options: FoldOptions): FoldHandle {", "// ---------------------------------------------------------------------------\n// segmented control", "createFold"),
].join("\n");

// createFold 只需要一个 chevron 图标节点；icon() 的真实实现是 SVG sprite，与这里无关。
const ICON_STUB = `function icon(name, opts = {}) { const node = document.createElement("span"); node.className = "icon"; node.setAttribute("data-icon", String(name)); return node; }`;

const checks = [];
// 先登记、最后按顺序执行：有几个用例是 async（要 await syncAppearanceFromBackend），
// 立即执行的话失败只会变成一个没人 await 的 Promise，断言等于没跑。
function check(name, fn) {
  checks.push({ name, fn });
}

// ── 1. 首屏主题：localStorage 的偏好决定 data-theme ───────────────────────────
check("首屏按缓存偏好上色（没有缓存时是浅色）", () => {
  const fresh = makeEnv();
  const app = run(APP_THEME_SOURCE, fresh, { isMac: () => true, isWindows: () => false, settingsView: { applyStoredMotionPreference() {} }, initPalettes() {} });
  assert.equal(app.__exports.readStoredThemePreference(), "light", "没有缓存时默认浅色");
  app.__exports.initTheme();
  assert.equal(fresh.document.documentElement.dataset.theme, "light");
  assert.equal(fresh.document.documentElement.dataset.os, "mac", "顺带把平台字号档写进 dataset.os");

  for (const [stored, expected] of [["dark", "dark"], ["light", "light"], ["system", "light"]]) {
    const env = makeEnv({ stored: { "translator.theme": stored } });
    const ctx = run(APP_THEME_SOURCE, env, { isMac: () => true, isWindows: () => false, settingsView: { applyStoredMotionPreference() {} }, initPalettes() {} });
    ctx.__exports.initTheme();
    assert.equal(env.document.documentElement.dataset.theme, expected, `缓存 ${stored} 时应显示 ${expected}`);
  }
});

check("缓存里是坏值时按浅色兜底，不把非法值写进 data-theme", () => {
  const env = makeEnv({ stored: { "translator.theme": "violet" } });
  const app = run(APP_THEME_SOURCE, env, { isMac: () => false, isWindows: () => true, settingsView: { applyStoredMotionPreference() {} }, initPalettes() {} });
  assert.equal(app.__exports.readStoredThemePreference(), "light");
  app.__exports.initTheme();
  assert.equal(env.document.documentElement.dataset.theme, "light");
  assert.equal(env.document.documentElement.dataset.os, "win");
});

// ── 2. 跟随系统：系统换主题要实时跟上，固定主题不许跟 ────────────────────────
check("跟随系统时系统换主题立即生效，切回浅色也立即生效", () => {
  const env = makeEnv({ stored: { "translator.theme": "system" }, prefersDark: false });
  const app = run(APP_THEME_SOURCE, env, { isMac: () => true, isWindows: () => false, settingsView: { applyStoredMotionPreference() {} }, initPalettes() {}, withColorFade: (fn) => fn() });
  app.__exports.initTheme();
  assert.equal(env.document.documentElement.dataset.theme, "light");
  assert.ok(env.listenerCount() >= 1, "必须挂了 matchMedia 的 change 监听，否则跟随系统不会实时生效");

  env.setSystemDark(true);
  assert.equal(env.document.documentElement.dataset.theme, "dark");

  env.setSystemDark(false);
  assert.equal(env.document.documentElement.dataset.theme, "light");
});

check("固定深色 / 固定浅色时系统换主题不跟着变", () => {
  for (const fixed of ["dark", "light"]) {
    const env = makeEnv({ stored: { "translator.theme": fixed }, prefersDark: fixed === "light" });
    const app = run(APP_THEME_SOURCE, env, { isMac: () => true, isWindows: () => false, settingsView: { applyStoredMotionPreference() {} }, initPalettes() {}, withColorFade: (fn) => fn() });
    app.__exports.initTheme();
    env.setSystemDark(fixed !== "dark");
    assert.equal(env.document.documentElement.dataset.theme, fixed, `固定 ${fixed} 时不允许被系统主题带走`);
  }
});

check("跟随系统时系统已经是深色，首屏就直接是深色", () => {
  const env = makeEnv({ stored: { "translator.theme": "system" }, prefersDark: true });
  const app = run(APP_THEME_SOURCE, env, { isMac: () => true, isWindows: () => false, settingsView: { applyStoredMotionPreference() {} }, initPalettes() {} });
  app.__exports.initTheme();
  assert.equal(env.document.documentElement.dataset.theme, "dark");
});

// ── 3. 后端权威值回填 ─────────────────────────────────────────────────────────
check("后端 appearance.theme 覆盖 localStorage 并立刻改界面", async () => {
  const env = makeEnv({ stored: { "translator.theme": "light" } });
  const applied = [];
  const sandbox = run(APP_THEME_SOURCE, env, {
    isMac: () => true,
    isWindows: () => false,
    settingsView: { applyStoredMotionPreference() {} },
    initPalettes() {},
    ApiClient: class {
      async connect() {}
      async request() {
        return { appearance: { theme: "dark", palette_light: "liuguang", palette_dark: "amber", ui_language: "en" } };
      }
    },
    palettesFromAppearance: (appearance) => ({ light: appearance.palette_light, dark: appearance.palette_dark }),
    storePalettes: (choice) => applied.push(["store", choice]),
    applyPalettes: (choice) => applied.push(["apply", choice]),
    isLocale: () => false,
    currentLocale: () => "zh",
    storeLocale() {},
    setLocale: async () => {},
  });
  await sandbox.__exports.syncAppearanceFromBackend();
  assert.equal(env.store.get("translator.theme"), "dark", "localStorage 要跟着后端改写，否则下次首屏又回到旧主题");
  assert.equal(env.document.documentElement.dataset.theme, "dark");
  assert.deepEqual(applied.at(-1), ["apply", { light: "liuguang", dark: "amber" }]);
});

check("后端给的是非法主题时不动界面，也不改本地缓存", async () => {
  const env = makeEnv({ stored: { "translator.theme": "dark" } });
  const sandbox = run(APP_THEME_SOURCE, env, {
    isMac: () => true,
    isWindows: () => false,
    settingsView: { applyStoredMotionPreference() {} },
    initPalettes() {},
    ApiClient: class {
      async connect() {}
      async request() {
        return { appearance: { theme: "violet" } };
      }
    },
    palettesFromAppearance: () => ({ light: "liuguang", dark: "liuguang" }),
    storePalettes() {},
    applyPalettes() {},
    isLocale: () => false,
    currentLocale: () => "zh",
    storeLocale() {},
    setLocale: async () => {},
  });
  await sandbox.__exports.syncAppearanceFromBackend();
  assert.equal(env.store.get("translator.theme"), "dark");
  assert.equal(env.document.documentElement.dataset.theme, undefined, "没有合法值时不应擅自点亮某个主题");
});

// ── 4. 设置页点选：可见主题先切，落盘成功后才写缓存 ──────────────────────────
check("点选只切可见主题，不碰 localStorage；落盘成功后才写缓存", () => {
  const env = makeEnv({ stored: { "translator.theme": "light" } });
  const page = run(`${SETTINGS_HELPERS_SOURCE}\n${SETTINGS_THEME_SOURCE}`, env, {
    settings: { appearance: { theme: "system" } },
  });
  const api = page.__exports;

  assert.equal(api.currentThemePreference(), "system", "分段控件的高亮读后端值，不读本地缓存");

  api.applyThemeVisual("dark");
  assert.equal(env.document.documentElement.dataset.theme, "dark", "点了就有反馈");
  assert.equal(env.store.get("translator.theme"), "light", "还没落盘，缓存不许动（中-17）");

  api.commitThemeToLocalStorage("dark");
  assert.equal(env.store.get("translator.theme"), "dark", "落盘成功后写缓存");

  // 跟随系统：可见主题按当前系统取值，缓存里仍然是 "system" 这个选择本身。
  env.setSystemDark(true);
  api.commitThemeToLocalStorage("system");
  api.applyThemeVisual("system");
  assert.equal(env.store.get("translator.theme"), "system");
  assert.equal(env.document.documentElement.dataset.theme, "dark", "系统是深色时「跟随系统」显示深色");
  env.setSystemDark(false);
  api.applyThemeVisual("system");
  assert.equal(env.document.documentElement.dataset.theme, "light");
});

check("后端值是坏值时设置页高亮浅色，不把非法值当主题", () => {
  const env = makeEnv();
  const page = run(`${SETTINGS_HELPERS_SOURCE}\n${SETTINGS_THEME_SOURCE}`, env, {
    settings: { appearance: { theme: "violet" } },
  });
  assert.equal(page.__exports.currentThemePreference(), "light");
});

// ── 5. 配置面板折叠状态记忆 ──────────────────────────────────────────────────
check("折叠块展开后整页重画仍然是展开的，没碰过的 key 保持默认", () => {
  const env = makeEnv();
  const sandbox = run(`${ICON_STUB}\n${COMPONENTS_FOLD_SOURCE}\n${SETTINGS_FOLD_SOURCE}`, env, {
    document: env.document,
  });
  const { settingsFold } = sandbox.__exports;

  const content = () => env.document.createElement("div");
  const headerOf = (root) => root.find((node) => node.className.split(" ").includes("fold-h"));
  const bodyOf = (root) => root.find((node) => node.className.split(" ").includes("fold-b"));

  const first = settingsFold("provider.advanced", { title: "高级", content: content() });
  assert.ok(first, "settingsFold 要返回折叠块根节点");
  assert.ok(headerOf(first), "折叠块必须有 .fold-h 头部");
  assert.equal(headerOf(first).getAttribute("aria-expanded"), "false", "默认收起");
  assert.equal(bodyOf(first).style.display, "none");
  assert.equal(first.classList.contains("open"), false);

  headerOf(first).dispatch("click");
  assert.equal(headerOf(first).getAttribute("aria-expanded"), "true");
  assert.equal(bodyOf(first).style.display, "", "展开后内容可见");
  assert.equal(first.classList.contains("open"), true);

  // 整页重画：同一个 key 重新创建一个折叠块（renderBody() 之后就是这样）。
  const rerendered = settingsFold("provider.advanced", { title: "高级", content: content() });
  assert.equal(headerOf(rerendered).getAttribute("aria-expanded"), "true", "重画后不能自己收起来");
  assert.equal(rerendered.classList.contains("open"), true);

  // 另一个没碰过的 key 仍然按传入的初始值。
  const untouched = settingsFold("provider.builtin", { title: "内置规则", content: content() });
  assert.equal(headerOf(untouched).getAttribute("aria-expanded"), "false");
  const openedByDefault = settingsFold("provider.review", { title: "复核", content: content(), open: true });
  assert.equal(headerOf(openedByDefault).getAttribute("aria-expanded"), "true");

  // 用户手动收起，重画后也要记住「收起」。
  headerOf(rerendered).dispatch("click");
  assert.equal(headerOf(rerendered).getAttribute("aria-expanded"), "false");
  const rerenderedAgain = settingsFold("provider.advanced", { title: "高级", content: content() });
  assert.equal(headerOf(rerenderedAgain).getAttribute("aria-expanded"), "false", "收起同样要记住");
});

for (const { name, fn } of checks) {
  await fn();
  console.log(`  ✓ ${name}`);
}
console.log(`appearance theme / fold memory: ${checks.length} DOM 组断言通过`);
