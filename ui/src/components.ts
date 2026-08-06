// V9 共享组件工厂 —— 纯函数，返回 DOM 元素，不引框架。
// 类名与结构对应 docs/mockups/2026-08-06_v9_full_redesign.html 的组件样式
// （ui/src/styles/app.css 是该样张 CSS 的应用外壳改造版）。各视图代理直接用这些
// 工厂拼装页面，不必记忆样张里的具体标签结构。

import { icon, type IconName } from "./icons";

/** 清空一个元素的子节点；不用 replaceChildren（Safari 15.1 不支持）。 */
export function clearElement(el: HTMLElement): void {
  while (el.firstChild) {
    el.removeChild(el.firstChild);
  }
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  options: { className?: string; text?: string; attrs?: Record<string, string> } = {},
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (options.className) {
    node.className = options.className;
  }
  if (options.text !== undefined) {
    node.textContent = options.text;
  }
  if (options.attrs) {
    for (const [key, value] of Object.entries(options.attrs)) {
      node.setAttribute(key, value);
    }
  }
  return node;
}

// ---------------------------------------------------------------------------
// card
// ---------------------------------------------------------------------------

/** .card 容器；children 是可选的初始子节点。 */
export function createCard(children: (HTMLElement | string)[] = [], className?: string): HTMLDivElement {
  const card = el("div", { className: className ? `card ${className}` : "card" });
  appendAll(card, children);
  return card;
}

function appendAll(node: HTMLElement, children: (HTMLElement | string)[]): void {
  for (const child of children) {
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
}

// ---------------------------------------------------------------------------
// chip / status / pill
// ---------------------------------------------------------------------------

export type ChipTone = "ok" | "warn" | "tint" | "mute" | "dgr";

export interface ChipOptions {
  label: string;
  tone?: ChipTone;
  icon?: IconName;
  className?: string;
}

/** .chip 小圆角徽标（结果态、格式标签等）。 */
export function createChip(options: ChipOptions): HTMLSpanElement {
  const classes = ["chip"];
  if (options.tone) classes.push(options.tone);
  if (options.className) classes.push(options.className);
  const chip = el("span", { className: classes.join(" ") });
  if (options.icon) {
    chip.append(icon(options.icon, { size: "sm" }));
  }
  chip.append(document.createTextNode(options.label));
  return chip;
}

export type StatusTone = "idle" | "run" | "ok" | "warn" | "pause";

export interface StatusOptions {
  label: string;
  tone: StatusTone;
}

/** .status 顶栏状态徽章（带小圆点），例如「待选择来源」「翻译中」。 */
export function createStatus(options: StatusOptions): HTMLSpanElement {
  const status = el("span", { className: `status ${options.tone}` });
  status.append(el("span", { className: "led" }));
  status.append(document.createTextNode(options.label));
  return status;
}

export interface PillOptions {
  label: string;
  live?: boolean;
  dotColor?: string;
  onClick?: () => void;
}

/** .pill 顶栏右侧药丸（任务中心 / 当前模型）。 */
export function createPill(options: PillOptions): HTMLSpanElement {
  const classes = ["pill"];
  if (options.live) classes.push("live");
  const pill = el("span", { className: classes.join(" ") });
  const dot = el("span", { className: "dot" });
  if (options.dotColor) {
    dot.style.background = options.dotColor;
  }
  pill.append(dot, document.createTextNode(options.label));
  if (options.onClick) {
    pill.style.cursor = "pointer";
    pill.addEventListener("click", options.onClick);
  }
  return pill;
}

// ---------------------------------------------------------------------------
// button
// ---------------------------------------------------------------------------

export type ButtonVariant = "default" | "primary" | "danger" | "danger-solid";
export type ButtonSize = "default" | "big" | "mini";

export interface ButtonOptions {
  label?: string;
  icon?: IconName;
  variant?: ButtonVariant;
  size?: ButtonSize;
  disabled?: boolean;
  title?: string;
  onClick?: (event: MouseEvent) => void;
}

const BUTTON_VARIANT_CLASS: Record<ButtonVariant, string> = {
  default: "",
  primary: "pri",
  danger: "dgr",
  "danger-solid": "dgr-solid",
};

const BUTTON_SIZE_CLASS: Record<ButtonSize, string> = {
  default: "",
  big: "big",
  mini: "mini",
};

/** .btn 按钮（含 .pri / .dgr / .dgr-solid / .big / .mini 变体）。 */
export function createButton(options: ButtonOptions): HTMLButtonElement {
  const classes = ["btn"];
  const variantClass = BUTTON_VARIANT_CLASS[options.variant ?? "default"];
  if (variantClass) classes.push(variantClass);
  const sizeClass = BUTTON_SIZE_CLASS[options.size ?? "default"];
  if (sizeClass) classes.push(sizeClass);
  const button = el("button", { className: classes.join(" ") });
  button.type = "button";
  if (options.icon) {
    button.append(icon(options.icon, { size: "sm" }));
  }
  if (options.label) {
    button.append(document.createTextNode(options.label));
  }
  if (options.title) {
    button.title = options.title;
  }
  button.disabled = Boolean(options.disabled);
  if (options.onClick) {
    button.addEventListener("click", options.onClick);
  }
  return button;
}

// ---------------------------------------------------------------------------
// switch / switch row
// ---------------------------------------------------------------------------

export interface SwitchOptions {
  checked?: boolean;
  disabled?: boolean;
  onChange?: (checked: boolean) => void;
}

/** .sw 开关（一个 role="switch" 的 button，不是原生 checkbox）。 */
export function createSwitch(options: SwitchOptions = {}): HTMLButtonElement {
  let checked = Boolean(options.checked);
  const button = el("button", { className: checked ? "sw on" : "sw" });
  button.type = "button";
  button.setAttribute("role", "switch");
  button.setAttribute("aria-checked", String(checked));
  button.disabled = Boolean(options.disabled);
  button.addEventListener("click", () => {
    checked = !checked;
    button.classList.toggle("on", checked);
    button.setAttribute("aria-checked", String(checked));
    options.onChange?.(checked);
  });
  return button;
}

export interface SwitchRowOptions extends SwitchOptions {
  label: string;
  hint?: string;
}

/** .swrow：一行「文字 + 可选 ? 提示 + 开关」，右栏本类型选项的标准写法。 */
export function createSwitchRow(options: SwitchRowOptions): HTMLDivElement {
  const row = el("div", { className: "swrow" });
  row.append(document.createTextNode(options.label));
  if (options.hint) {
    row.append(createHintBadge(options.hint));
  }
  row.append(createSwitch(options));
  return row;
}

// ---------------------------------------------------------------------------
// hint badge + tooltip
//
// 原来直接挂 title 属性：WKWebView 的原生 tooltip 要悬停一两秒才出、位置不可控，
// 而且父级一旦 pointer-events:none（运行中锁定的右栏）就彻底不触发——用户实测「怎么
// 做都不显示」。这里换成自绘浮层：悬停即出、点击可钉住、Esc / 点别处 / 滚动即关。
// 浮层挂在 document.body 上（fixed 定位），不受任何祖先 overflow 裁剪。
// ---------------------------------------------------------------------------

interface ActiveTip {
  element: HTMLElement;
  anchor: HTMLElement;
  /** true = 点击钉住，移开鼠标不关；false = 悬停临时显示。 */
  pinned: boolean;
}

let activeTip: ActiveTip | null = null;
let hoverTimer = 0;
let tipListenersInstalled = false;

/** 关闭当前提示浮层；没有打开时是空操作。视图重建前也可以主动调用。 */
export function hideHint(): void {
  window.clearTimeout(hoverTimer);
  if (!activeTip) return;
  activeTip.element.remove();
  activeTip = null;
}

function installTipListeners(): void {
  if (tipListenersInstalled) return;
  tipListenersInstalled = true;
  // 捕获阶段：任何祖先容器滚动都要跟着关，否则浮层会停在原地。
  window.addEventListener("scroll", () => hideHint(), true);
  window.addEventListener("resize", () => hideHint());
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hideHint();
  });
  document.addEventListener("pointerdown", (event) => {
    if (!activeTip) return;
    const target = event.target as Node | null;
    if (target && activeTip.anchor.contains(target)) return;
    hideHint();
  }, true);
}

function placeTip(tip: HTMLElement, anchor: HTMLElement): void {
  const gap = 8;
  const margin = 8;
  const rect = anchor.getBoundingClientRect();
  const width = tip.offsetWidth;
  const height = tip.offsetHeight;

  let left = rect.left + rect.width / 2 - width / 2;
  left = Math.max(margin, Math.min(left, window.innerWidth - width - margin));

  let top = rect.top - height - gap;
  const below = top < margin;
  if (below) top = rect.bottom + gap;
  tip.classList.toggle("below", below);

  tip.style.left = `${Math.round(left)}px`;
  tip.style.top = `${Math.round(top)}px`;
  // 箭头跟着锚点走，浮层被视口夹住时也不会指偏。
  const arrowX = Math.max(12, Math.min(rect.left + rect.width / 2 - left, width - 12));
  tip.style.setProperty("--tip-arrow", `${Math.round(arrowX)}px`);
}

function showHint(anchor: HTMLElement, message: string, pinned: boolean): void {
  hideHint();
  installTipListeners();
  const tip = el("div", { className: "tip", text: message });
  tip.setAttribute("role", "tooltip");
  document.body.append(tip);
  activeTip = { element: tip, anchor, pinned };
  placeTip(tip, anchor);
}

/**
 * 「?」提示角标。className 用来复用两套等价皮肤：右栏开关行用 "hint"，
 * 设置页 label 里用 "swrow-hint"。
 */
export function createHintBadge(message: string, className = "hint"): HTMLSpanElement {
  const badge = el("span", { className, text: "?" });
  badge.tabIndex = 0;
  badge.setAttribute("role", "button");
  badge.setAttribute("aria-label", `说明：${message}`);

  const isOpen = () => activeTip?.anchor === badge;
  const toggle = () => {
    if (isOpen()) hideHint();
    else showHint(badge, message, true);
  };

  badge.addEventListener("mouseenter", () => {
    if (isOpen()) return;
    window.clearTimeout(hoverTimer);
    hoverTimer = window.setTimeout(() => showHint(badge, message, false), 120);
  });
  badge.addEventListener("mouseleave", () => {
    window.clearTimeout(hoverTimer);
    if (isOpen() && !activeTip?.pinned) hideHint();
  });
  // pointerdown 先于 click，用它记录「按下前是否已展开」，避免悬停已打开时点一下反而闪一下又开。
  let openBeforePress = false;
  badge.addEventListener("pointerdown", () => {
    openBeforePress = isOpen() && Boolean(activeTip?.pinned);
  });
  badge.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (openBeforePress) hideHint();
    else showHint(badge, message, true);
  });
  badge.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      toggle();
    }
  });
  badge.addEventListener("blur", () => {
    if (isOpen()) hideHint();
  });
  return badge;
}

// ---------------------------------------------------------------------------
// 锚定菜单（「浏览」按钮的「文件夹 / 文件」二选一）
// ---------------------------------------------------------------------------

export interface MenuItem {
  label: string;
  description?: string;
  onSelect: () => void;
}

/** 同一时刻只允许一个锚定菜单展开；写法与下面语言浮层的 closeActiveLanguagePopover 一致。 */
let closeActiveMenu: (() => void) | null = null;

/**
 * 关闭当前展开的锚定菜单；没有展开时是空操作。
 * 菜单挂在 document.body 上，不随触发它的视图 container 一起被清掉；视图被程序化切走时
 * （不经用户点击，不会触发菜单自己的 pointerdown 关闭逻辑）需要主动调用，否则会变成孤儿面板。
 */
export function closeMenu(): void {
  closeActiveMenu?.();
}

/** 在 anchor 正下方弹出一个小菜单；点外面、Esc、滚动都会关掉。 */
export function openMenu(anchor: HTMLElement, items: MenuItem[]): void {
  hideHint();
  // 连点两次触发按钮之前必须先关掉上一个，否则会在 body 下叠出两份重合的菜单，
  // 各自带一套全局监听，要等下一次外部 pointerdown 才一起清掉。
  closeActiveMenu?.();
  const menu = el("div", { className: "menu" });
  menu.setAttribute("role", "menu");

  const close = () => {
    menu.remove();
    document.removeEventListener("pointerdown", onOutside, true);
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("scroll", close, true);
    window.removeEventListener("resize", close);
    if (closeActiveMenu === close) closeActiveMenu = null;
  };
  const onOutside = (event: PointerEvent) => {
    const target = event.target as Node | null;
    if (target && (menu.contains(target) || anchor.contains(target))) return;
    close();
  };
  const onKey = (event: KeyboardEvent) => {
    if (event.key === "Escape") close();
  };

  for (const item of items) {
    const button = el("button", { className: "menu-item" });
    button.type = "button";
    button.setAttribute("role", "menuitem");
    button.append(el("b", { text: item.label }));
    if (item.description) {
      button.append(el("span", { text: item.description }));
    }
    button.addEventListener("click", () => {
      close();
      item.onSelect();
    });
    menu.append(button);
  }

  document.body.append(menu);
  const margin = 8;
  const rect = anchor.getBoundingClientRect();
  let left = rect.left;
  left = Math.max(margin, Math.min(left, window.innerWidth - menu.offsetWidth - margin));
  let top = rect.bottom + 6;
  if (top + menu.offsetHeight > window.innerHeight - margin) {
    top = Math.max(margin, rect.top - menu.offsetHeight - 6);
  }
  menu.style.left = `${Math.round(left)}px`;
  menu.style.top = `${Math.round(top)}px`;

  document.addEventListener("pointerdown", onOutside, true);
  document.addEventListener("keydown", onKey);
  window.addEventListener("scroll", close, true);
  window.addEventListener("resize", close);
  closeActiveMenu = close;
}

// ---------------------------------------------------------------------------
// field / text field / select field
// ---------------------------------------------------------------------------

/** .field：任意控件外面套一个带 label 的包装。 */
export function createField(label: string, control: HTMLElement): HTMLDivElement {
  const field = el("div", { className: "field" });
  field.append(el("label", { text: label }));
  field.append(control);
  return field;
}

export interface TextFieldOptions {
  label: string;
  value?: string;
  placeholder?: string;
  readonly?: boolean;
  disabled?: boolean;
  onInput?: (value: string) => void;
}

export interface TextFieldHandle {
  root: HTMLDivElement;
  input: HTMLInputElement;
}

/** .field > label + input[type=text]。 */
export function createTextField(options: TextFieldOptions): TextFieldHandle {
  const input = el("input", { attrs: { type: "text" } });
  input.value = options.value ?? "";
  if (options.placeholder) input.placeholder = options.placeholder;
  input.readOnly = Boolean(options.readonly);
  input.disabled = Boolean(options.disabled);
  if (options.onInput) {
    input.addEventListener("input", () => options.onInput?.(input.value));
  }
  const root = createField(options.label, input);
  return { root, input };
}

export interface SelectFieldOption {
  value: string;
  label: string;
}

export interface SelectFieldOptions {
  label: string;
  options: SelectFieldOption[];
  value?: string;
  disabled?: boolean;
  onChange?: (value: string) => void;
}

export interface SelectFieldHandle {
  root: HTMLDivElement;
  select: HTMLSelectElement;
}

/** .field > label + select。 */
export function createSelectField(options: SelectFieldOptions): SelectFieldHandle {
  const select = el("select");
  for (const opt of options.options) {
    const optionEl = el("option", { text: opt.label, attrs: { value: opt.value } });
    select.append(optionEl);
  }
  if (options.value !== undefined) {
    select.value = options.value;
  }
  select.disabled = Boolean(options.disabled);
  if (options.onChange) {
    select.addEventListener("change", () => options.onChange?.(select.value));
  }
  const root = createField(options.label, select);
  return { root, select };
}

// ---------------------------------------------------------------------------
// 可搜索语言选择器
//
// 替代原生 <select>：59 种语言用滚动条找太慢，而且窄容器里 select 会把长语言名截成
// 一条缝（用户实测记忆库工具条「语言名显示不全」）。这里按样张
// docs/mockups/2026-08-06_lang-picker-focus-excel-notice.html 第 ② 节实现：按钮 +
// 浮层，浮层顶部一个搜索框，中文名 / 英文名 / 语言代码三者任一命中即保留。
//
// 浮层挂在 document.body 上、用 position:fixed 定位，不是样张里的 absolute 子节点——
// 右栏 .rp-scroll 是滚动容器，absolute 浮层会被 overflow 切掉，理由详见 app.css 里
// .lang-pop 上方的注释。定位/关闭的写法与本文件的 openMenu 保持一致。
// ---------------------------------------------------------------------------

/**
 * 后端 /api/languages 与 /api/tm/language-pairs 返回的语言条目。
 * 三处视图（工作区右栏、记忆库、设置页自定义语言）共用这一份定义。
 */
export interface LanguageOption {
  code: string;
  display_name: string;
  /**
   * 后端直接给出的搜索别名，已包含中文别名（语/文互换）、英文名与语言代码。
   * 搜索匹配主要靠它，前端不再自己维护一张英文名表。
   */
  aliases?: string[];
  description?: string;
  builtin?: boolean;
  can_source?: boolean;
  can_target?: boolean;
}

/** 源语言里的「自动识别」：既不配语言代码角标，也不算自定义语言。 */
const AUTO_LANG_CODE = "auto";

/** 「最近使用」只落 localStorage：这是纯 UI 偏好，不值得为它开一个后端接口。
 *  键名带前缀分组，主题偏好（theme）已经在用同一个 localStorage 通道。 */
const LANG_RECENT_PREFIX = "xl.lang-recent.";
/** 与样张一致，最近使用最多三项——再多就把「全部语言」挤到需要滚动才看得见。 */
const LANG_RECENT_MAX = 3;

function readRecentLangs(key: string): string[] {
  try {
    const raw = window.localStorage.getItem(LANG_RECENT_PREFIX + key);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string") : [];
  } catch {
    // 隐私模式下 localStorage 可能直接抛异常；最近使用没了不影响主流程。
    return [];
  }
}

function pushRecentLang(key: string, code: string): void {
  const next = [code, ...readRecentLangs(key).filter((item) => item !== code)].slice(0, LANG_RECENT_MAX);
  try {
    window.localStorage.setItem(LANG_RECENT_PREFIX + key, JSON.stringify(next));
  } catch {
    /* 同上，写不进去就算了。 */
  }
}

/** 中文名 / 英文名（在 aliases 里）/ 语言代码，命中任一即算匹配。 */
function languageMatches(option: LanguageOption, query: string): boolean {
  if (!query) return true;
  const needle = query.toLowerCase();
  if (option.display_name.toLowerCase().includes(needle)) return true;
  if (option.code.toLowerCase().includes(needle)) return true;
  return (option.aliases ?? []).some((alias) => alias.toLowerCase().includes(needle));
}

/**
 * 把命中片段包成 <b>。只高亮显示名——匹配可能发生在英文别名或代码上，那时显示名里
 * 找不到这段文字，原样输出即可（样张同样处理）。用真实节点而不是 innerHTML，
 * 语言名来自后端，不该走字符串拼 HTML 那条路。
 */
function appendMarkedText(host: HTMLElement, value: string, query: string): void {
  const index = query ? value.toLowerCase().indexOf(query.toLowerCase()) : -1;
  if (index < 0) {
    host.append(document.createTextNode(value));
    return;
  }
  host.append(document.createTextNode(value.slice(0, index)));
  host.append(el("b", { text: value.slice(index, index + query.length) }));
  host.append(document.createTextNode(value.slice(index + query.length)));
}

export interface LanguagePickerOptions {
  options: LanguageOption[];
  value: string;
  disabled?: boolean;
  /**
   * 「最近使用」分组的存储键（例如 "excel-target"、"tm-source"）。
   * 不传就不显示这个分组——一次性的选择器没必要污染最近列表。
   */
  recentKey?: string;
  /** 追加到 .lang 根节点的类名，右栏用 "block" 让按钮铺满一列。 */
  className?: string;
  onChange: (code: string) => void;
  /**
   * 语言目录整个是空的时候，空态里「重试」按钮的动作（通常是重新拉 /api/languages
   * 并重建这一片界面）。不传就只显示说明文字，让用户自己切页面触发重试。
   */
  onReload?: () => void;
}

export interface LanguagePickerHandle {
  root: HTMLDivElement;
  getValue(): string;
  /** 外部改值时同步按钮文案，不回调 onChange。 */
  setValue(code: string): void;
}

/** 同一时刻只允许一个语言浮层展开；换一个按钮点开时先收掉上一个。 */
let closeActiveLanguagePopover: (() => void) | null = null;

/**
 * 关闭当前展开的语言浮层；没有展开时是空操作。
 * 浮层挂在 document.body 上（fixed 定位，避免被祖先 overflow 裁剪），不会随视图卸载自动消失；
 * 视图被程序化切走时（不经过用户点击，不会触发浮层自己的 pointerdown 关闭逻辑）需要主动调用，
 * 否则浮层会变成挂在 body 下的孤儿面板，且 closeActiveLanguagePopover 会一直指向已死的闭包。
 */
export function closeLanguagePopover(): void {
  closeActiveLanguagePopover?.();
}

/** 光秃秃的选择器（无 label），记忆库工具条那种行内用法用它。 */
export function createLanguagePicker(options: LanguagePickerOptions): LanguagePickerHandle {
  const root = el("div", { className: options.className ? `lang ${options.className}` : "lang" });
  const button = el("button", { className: "lang-btn" });
  button.type = "button";
  button.setAttribute("aria-haspopup", "listbox");
  button.disabled = Boolean(options.disabled);
  const valueLabel = el("span", { className: "val" });
  const codeBadge = el("span", { className: "code" });
  button.append(valueLabel, codeBadge, icon("chev", { className: "chev" }));
  root.append(button);

  let current = options.value;

  const paintButton = () => {
    const found = options.options.find((item) => item.code === current);
    valueLabel.textContent = found?.display_name ?? current;
    // 「自动识别」没有真正的语言代码，挂个 auto 角标只会让人以为那是一门语言。
    const showCode = Boolean(found) && current !== AUTO_LANG_CODE;
    codeBadge.textContent = showCode ? current : "";
    codeBadge.style.display = showCode ? "" : "none";
  };
  paintButton();

  const openPopover = () => {
    closeActiveLanguagePopover?.();

    const pop = el("div", { className: "lang-pop" });
    pop.setAttribute("role", "listbox");

    const searchWrap = el("div", { className: "lang-search" });
    const search = el("input", { attrs: { type: "text" } });
    search.placeholder = "搜索语言、代码…";
    searchWrap.append(search);
    // 目录为空时搜索框只会误导——摆一个搜不出任何东西的输入框，等于暗示问题出在搜索上。
    if (!options.options.length) searchWrap.style.display = "none";

    const list = el("div", { className: "lang-list" });

    const foot = el("div", { className: "lang-foot" });
    const countLabel = el("span");
    const keyHints = el("span");
    keyHints.append(
      el("span", { className: "kbd", text: "↑↓" }),
      document.createTextNode(" 选择 "),
      el("span", { className: "kbd", text: "↵" }),
      document.createTextNode(" 确认 "),
      el("span", { className: "kbd", text: "esc" }),
      document.createTextNode(" 关闭"),
    );
    foot.append(countLabel, keyHints);

    pop.append(searchWrap, list, foot);
    document.body.append(pop);
    root.dataset.open = "true";

    const close = () => {
      pop.remove();
      delete root.dataset.open;
      document.removeEventListener("pointerdown", onOutside, true);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", close);
      if (closeActiveLanguagePopover === close) closeActiveLanguagePopover = null;
    };
    const onOutside = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (target && (pop.contains(target) || root.contains(target))) return;
      close();
    };
    const onScroll = (event: Event) => {
      // 列表自身滚动（含键盘导航触发的 scrollIntoView）不该把浮层关掉，
      // 只有外层容器滚动才会让 fixed 浮层脱离按钮。
      const target = event.target as Node | null;
      if (target && pop.contains(target)) return;
      close();
    };
    closeActiveLanguagePopover = close;

    const place = () => {
      const rect = button.getBoundingClientRect();
      const margin = 8;
      // 浮层固定 288px（样张值），但按钮更宽时跟着长，免得浮层比触发它的按钮还窄。
      pop.style.minWidth = `${Math.round(rect.width)}px`;
      const width = pop.offsetWidth;
      const height = pop.offsetHeight;
      const left = Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin));
      let top = rect.bottom + 6;
      if (top + height > window.innerHeight - margin) {
        const above = rect.top - height - 6;
        top = above >= margin ? above : Math.max(margin, window.innerHeight - height - margin);
      }
      pop.style.left = `${Math.round(left)}px`;
      pop.style.top = `${Math.round(top)}px`;
    };

    const pick = (code: string) => {
      current = code;
      if (options.recentKey) pushRecentLang(options.recentKey, code);
      paintButton();
      // 先收浮层再回调：记忆库的 onChange 会整块重建工具条，锚点在那之后就没了。
      close();
      options.onChange(code);
    };

    const renderList = () => {
      const query = search.value.trim();
      clearElement(list);

      const pool = options.options.filter((item) => languageMatches(item, query));
      // 有查询词时不再分组：用户已经在定向找某一门语言，「最近使用」只会把结果切碎。
      const recentCodes = options.recentKey && !query ? readRecentLangs(options.recentKey) : [];
      const recent = pool.filter((item) => recentCodes.includes(item.code));
      const rest = pool.filter((item) => !recent.includes(item));

      const appendItem = (item: LanguageOption) => {
        const row = el("div", { className: "lang-item" });
        row.setAttribute("role", "option");
        row.dataset.code = item.code;
        row.setAttribute("aria-selected", item.code === current ? "true" : "false");
        const name = el("span", { className: "nm" });
        appendMarkedText(name, item.display_name, query);
        row.append(name);
        // builtin === false 就是设置页加的自定义目标语言；「自动识别」虽然也不是内置
        // 语种，但它是选择器的固有项，不该顶着「自定义」的黄标。
        if (item.builtin === false && item.code !== AUTO_LANG_CODE) {
          row.append(el("span", { className: "tag", text: "自定义" }));
        }
        row.append(el("span", { className: "cd", text: item.code === AUTO_LANG_CODE ? "" : item.code }));
        row.addEventListener("click", () => pick(item.code));
        list.append(row);
      };

      if (!pool.length) {
        const empty = el("div", { className: "lang-empty" });
        // 「一个都没搜到」和「目录压根没加载出来」是两码事。后者要是也说「换个说法试试」，
        // 等于让用户去调整搜索词来修一个后端没返回数据的故障——怎么试都不会有结果。
        if (!options.options.length) {
          empty.append(el("b", { text: "语言列表没能加载出来" }));
          empty.append(
            document.createTextNode("这不是搜索词的问题：后端没有返回语言目录，多半是刚启动还没就绪。"),
          );
          if (options.onReload) {
            const retry = el("button", { className: "lang-retry", text: "重试" });
            retry.type = "button";
            retry.addEventListener("click", () => {
              // 先收浮层：重试会重建整片界面，锚点在那之后就没了（同 pick() 的顺序）。
              close();
              options.onReload?.();
            });
            empty.append(retry);
          }
        } else {
          empty.append(el("b", { text: "没有匹配的语言" }));
          empty.append(document.createTextNode("换个说法试试，中文名、英文名、语言代码都能搜。"));
        }
        list.append(empty);
      } else {
        if (recent.length) {
          list.append(el("div", { className: "lang-group", text: "最近使用" }));
          recent.forEach(appendItem);
          list.append(el("div", { className: "lang-group", text: "全部语言" }));
        }
        rest.forEach(appendItem);
      }

      countLabel.textContent = `共 ${pool.length} 项`;
      // 条目数变了高度就变了，重算一次位置，否则贴着视口底部时会溢出。
      place();
    };

    const items = () => Array.from(list.querySelectorAll<HTMLElement>(".lang-item"));

    search.addEventListener("input", renderList);
    search.addEventListener("keydown", (event) => {
      const rows = items();
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (!rows.length) return;
        let index = rows.findIndex((row) => row.classList.contains("cur"));
        if (index < 0) index = event.key === "ArrowDown" ? -1 : 0;
        index = (index + (event.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length;
        rows.forEach((row) => row.classList.remove("cur"));
        rows[index].classList.add("cur");
        rows[index].scrollIntoView({ block: "nearest" });
      } else if (event.key === "Enter") {
        event.preventDefault();
        // 没按过方向键就直接回车 = 取第一条，等于「搜到什么就要什么」。
        const chosen = rows.find((row) => row.classList.contains("cur")) ?? rows[0];
        if (chosen?.dataset.code) pick(chosen.dataset.code);
      } else if (event.key === "Escape") {
        event.preventDefault();
        close();
        button.focus();
      }
    });

    renderList();
    document.addEventListener("pointerdown", onOutside, true);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", close);
    search.focus();
  };

  button.addEventListener("click", () => {
    if (root.dataset.open === "true") {
      closeActiveLanguagePopover?.();
      return;
    }
    openPopover();
  });

  return {
    root,
    getValue: () => current,
    setValue: (code: string) => {
      current = code;
      paintButton();
    },
  };
}

export interface LanguageFieldOptions extends LanguagePickerOptions {
  label: string;
}

export interface LanguageFieldHandle {
  root: HTMLDivElement;
  picker: LanguagePickerHandle;
}

/** .field > label + .lang.block：右栏「目标语言 / 源语言」那种带标题的一列。 */
export function createLanguageField(options: LanguageFieldOptions): LanguageFieldHandle {
  const picker = createLanguagePicker({
    ...options,
    className: options.className ? `block ${options.className}` : "block",
  });
  const root = createField(options.label, picker.root);
  return { root, picker };
}

// ---------------------------------------------------------------------------
// fold (collapsible group, 「本次任务」折叠组)
// ---------------------------------------------------------------------------

export interface FoldOptions {
  title: string;
  content: HTMLElement;
  open?: boolean;
}

export interface FoldHandle {
  root: HTMLDivElement;
  setOpen(open: boolean): void;
}

/** .fold：带 chevron 的折叠分组，样张里「本次任务」用的样式。 */
export function createFold(options: FoldOptions): FoldHandle {
  const root = el("div", { className: "fold" });
  const header = el("div", { className: "fold-h" });
  header.append(document.createTextNode(options.title));
  const chevron = icon("chev", { size: "sm" });
  header.append(chevron);
  const body = el("div", { className: "fold-b" });
  body.append(options.content);

  let open = Boolean(options.open);
  const applyState = () => {
    header.style.color = open ? "var(--tint-ink)" : "";
    chevron.style.transform = open ? "rotate(180deg)" : "";
    body.style.display = open ? "" : "none";
  };
  applyState();

  header.addEventListener("click", () => {
    open = !open;
    applyState();
  });

  root.append(header, body);
  return { root, setOpen: (next: boolean) => { open = next; applyState(); } };
}

// ---------------------------------------------------------------------------
// progress bar
// ---------------------------------------------------------------------------

export interface ProgressBarOptions {
  percent: number;
  tone?: "accent" | "warn";
}

export interface ProgressBarHandle {
  root: HTMLDivElement;
  setPercent(percent: number): void;
}

/** .bar > i：细进度条，用于运行中卡片与任务中心卡片。 */
export function createProgressBar(options: ProgressBarOptions): ProgressBarHandle {
  const root = el("div", { className: "bar" });
  const fill = el("i");
  if (options.tone === "warn") {
    fill.style.background = "linear-gradient(90deg,var(--warn),#e8a33d)";
  }
  root.append(fill);
  const setPercent = (percent: number) => {
    fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
  };
  setPercent(options.percent);
  return { root, setPercent };
}

// ---------------------------------------------------------------------------
// banner (完成横幅)
// ---------------------------------------------------------------------------

export interface BannerOptions {
  title: string;
  subtitle?: string;
  icon?: IconName;
  actions?: HTMLElement[];
  onClose?: () => void;
}

/** .banner：任务终态摘要横幅（样张屏⑤）。 */
export function createBanner(options: BannerOptions): HTMLDivElement {
  const banner = el("div", { className: "banner" });
  const bi = el("div", { className: "bi" });
  bi.append(icon(options.icon ?? "check"));
  banner.append(bi);

  const copy = el("div");
  copy.append(el("b", { text: options.title }));
  if (options.subtitle) {
    copy.append(el("div", { className: "sub", text: options.subtitle }));
  }
  banner.append(copy);

  const acts = el("div", { className: "acts" });
  for (const action of options.actions ?? []) {
    acts.append(action);
  }
  if (options.onClose) {
    const closeBtn = createButton({ label: "✕", size: "mini" });
    closeBtn.style.border = "0";
    closeBtn.style.color = "var(--ink-3)";
    closeBtn.addEventListener("click", options.onClose);
    acts.append(closeBtn);
  }
  banner.append(acts);
  return banner;
}

// ---------------------------------------------------------------------------
// empty state
// ---------------------------------------------------------------------------

export interface EmptyStateOptions {
  title: string;
  description?: string;
  icon?: IconName;
}

/** .empty：任务清单 / 工作区的空态占位，也用作视图 stub 的「建设中」内容。 */
export function createEmptyState(options: EmptyStateOptions): HTMLDivElement {
  const empty = el("div", { className: "empty" });
  if (options.icon) {
    const iconEl = icon(options.icon, { size: "lg" });
    iconEl.style.width = "40px";
    iconEl.style.height = "40px";
    iconEl.style.color = "var(--ink-3)";
    empty.append(iconEl);
  }
  empty.append(el("b", { text: options.title }));
  if (options.description) {
    empty.append(el("p", { text: options.description }));
  }
  return empty;
}

// ---------------------------------------------------------------------------
// modal（含危险操作红头样式）
// ---------------------------------------------------------------------------

export type ModalTone = "warn" | "danger";

export interface ModalAction {
  label: string;
  variant?: ButtonVariant;
  /** 点击后是否保留模态打开（例如需要等待异步请求）。默认点击后自动关闭。 */
  keepOpen?: boolean;
  onClick?: () => void | Promise<void>;
}

export interface ModalConfirmInput {
  /** 输入框占位符，通常就是要求输入的确认词，例如 "RESET"。 */
  placeholder: string;
  /** 只有输入值等于这个字符串时，被 gate 的按钮才会启用。 */
  matchValue: string;
}

export interface ModalOptions {
  /** warn = 黄色警示图标；danger = 红色不可逆操作图标。 */
  tone: ModalTone;
  icon: IconName;
  /** 模态上方的小标签，标出触发来源（例如「设置 · 数据与维护 · 完整本地重置」）。 */
  sourceLabel?: string;
  title: string;
  /** 依次渲染为若干个 <p>。 */
  body: (string | HTMLElement)[];
  /**
   * 危险操作的输入确认（对应样张「完整本地重置」的 RESET 输入框）。
   * 提供时，actions 数组里最后一个按钮会被 gate，直到输入匹配。
   */
  confirmInput?: ModalConfirmInput;
  actions: ModalAction[];
}

export interface ModalHandle {
  element: HTMLDivElement;
  close(): void;
}

/** 打开一个 .overlay > .modal，立即 append 到 document.body。 */
export function openModal(options: ModalOptions): ModalHandle {
  const overlay = el("div", { className: "overlay" });
  const modal = el("div", { className: "modal" });

  if (options.sourceLabel) {
    modal.append(el("span", { className: "mlabel", text: options.sourceLabel }));
  }

  const mi = el("div", { className: `mi ${options.tone === "danger" ? "dgr" : "warn"}` });
  mi.append(icon(options.icon));
  modal.append(mi);

  modal.append(el("h3", { text: options.title }));

  for (const item of options.body) {
    const p = el("p");
    if (typeof item === "string") {
      p.textContent = item;
    } else {
      p.append(item);
    }
    modal.append(p);
  }

  const close = () => overlay.remove();

  const actionButtons: HTMLButtonElement[] = [];
  const actsRow = el("div", { className: "macts" });

  let confirmInputEl: HTMLInputElement | null = null;
  if (options.confirmInput) {
    const hint = el("p", { text: "输入以确认：" });
    hint.style.fontSize = "12.5px";
    modal.append(hint);
    confirmInputEl = el("input", { attrs: { type: "text" } });
    confirmInputEl.placeholder = options.confirmInput.placeholder;
    modal.append(confirmInputEl);
  }

  options.actions.forEach((action, index) => {
    const button = createButton({
      label: action.label,
      variant: action.variant,
      onClick: async () => {
        await action.onClick?.();
        if (!action.keepOpen) {
          close();
        }
      },
    });
    const isLast = index === options.actions.length - 1;
    if (isLast && options.confirmInput) {
      button.disabled = true;
    }
    actionButtons.push(button);
    actsRow.append(button);
  });

  if (confirmInputEl && options.confirmInput) {
    const matchValue = options.confirmInput.matchValue;
    const gatedButton = actionButtons[actionButtons.length - 1];
    confirmInputEl.addEventListener("input", () => {
      gatedButton.disabled = confirmInputEl!.value !== matchValue;
    });
  }

  modal.append(actsRow);
  overlay.append(modal);
  document.body.append(overlay);

  return { element: modal, close };
}

// ---------------------------------------------------------------------------
// toast
// ---------------------------------------------------------------------------

export interface ToastOptions {
  message: string;
  error?: boolean;
  /** 毫秒，默认 3200。 */
  duration?: number;
}

/** 顶部居中的一次性提示条，追加到 document.body，超时后自动移除。 */
export function showToast(options: ToastOptions): void {
  const toast = el("div", { className: options.error ? "toast error" : "toast", text: options.message });
  document.body.append(toast);
  window.setTimeout(() => {
    toast.remove();
  }, options.duration ?? 3200);
}
