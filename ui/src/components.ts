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
    const hint = el("span", { className: "hint", text: "?" });
    hint.title = options.hint;
    row.append(hint);
  }
  row.append(createSwitch(options));
  return row;
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
