// PDF / CAD 首次使用图文指引（maple 2026-10-27 拍板，方案见项目文档「PDF 与 CAD 首次使用指引：推荐方案」）。
//
// 一屏看完的居中弹窗：PDF 讲三种翻译方式和「自动组合」，CAD 讲三项最常被误解的设置、
// DWG 需要的转换工具和译后改译文。卡片底部的本机状态是实时查的（和页面里的缺组件提示
// 同一个接口），缺什么当场给入口。弹窗只讲解、给入口，不改任何设置。
//
// 什么时候弹：第一次进入 PDF 页或 CAD 页时各自动弹一次（workspace.ts 挂载完调用
// maybeAutoShowGuide）。「快速开始」还开着时等它关掉；该页有任务在跑时这次不弹，等下次进入。
// 「看过」存在后端设置 onboarding.pdf_guide_seen / cad_guide_seen，读写走快速开始同一个
// 偏好接口；旧设置文件没有这两项，升级上来的老用户也会弹一次。
//
// 再次打开：顶栏「使用说明」（workspace.ts 通过 shell.setGuideEntry 挂上）、帮助页两条、
// PDF「翻译方式」问号说明末尾的「查看图文说明」。
//
// 插图是代码画的示意图，内容全部自造；图里的示例文字始终是「中文 → 法文」，不随界面语言变。

import { navigate, currentView } from "./router";
import { icon } from "./icons";
import { t } from "./i18n";
import { connectedClient, isQuickStartOpen, onQuickStartClosed, readPreferences, updatePreferences } from "./quickstart";
import "./guide.css";

export type GuideKind = "pdf" | "cad";

const SEEN_KEY: Record<GuideKind, string> = { pdf: "pdf_guide_seen", cad: "cad_guide_seen" };

/** 本次运行里已知看过的指引。写后端失败也记在这里，这次运行不会再弹第二次。 */
const seenLocally = new Set<GuideKind>();
let remoteSeen: Record<GuideKind, boolean> | null = null;
let activeClose: (() => void) | null = null;
let waitingForQuickStart: (() => void) | null = null;

// ---------------------------------------------------------------------------
// 自动弹出
// ---------------------------------------------------------------------------

async function alreadySeen(kind: GuideKind): Promise<boolean> {
  if (seenLocally.has(kind)) return true;
  if (!remoteSeen) {
    const prefs = await readPreferences();
    remoteSeen = { pdf: Boolean(prefs.pdf_guide_seen), cad: Boolean(prefs.cad_guide_seen) };
  }
  return remoteSeen[kind];
}

function markSeen(kind: GuideKind): void {
  seenLocally.add(kind);
  if (remoteSeen) remoteSeen[kind] = true;
  void updatePreferences({ [SEEN_KEY[kind]]: true }).catch(() => undefined);
}

/**
 * 进入 PDF / CAD 页、任务状态已知之后调用。busy 为 true（该页有任务在跑）时这次不弹；
 * 快速开始开着时等它关掉，届时人还在这一页、页上也没任务才弹。
 */
export async function maybeAutoShowGuide(kind: GuideKind, busy: () => boolean): Promise<void> {
  if (activeClose || busy()) return;
  let seen: boolean;
  try {
    seen = await alreadySeen(kind);
  } catch {
    // 读不到设置时不弹：宁可漏弹一次，也不要每次进来都弹。
    return;
  }
  if (seen || activeClose || busy() || currentView()?.id !== kind) return;
  if (isQuickStartOpen()) {
    waitingForQuickStart?.();
    const off = onQuickStartClosed(() => {
      off();
      waitingForQuickStart = null;
      // 快速开始里点了「PDF / 图片」「CAD 图纸」会紧接着进那一页，由那一页的挂载再来调；
      // 这里只处理关掉快速开始时人本来就在这一页的情形。
      window.setTimeout(() => {
        if (currentView()?.id === kind) void maybeAutoShowGuide(kind, busy);
      }, 0);
    });
    waitingForQuickStart = off;
    return;
  }
  openGuide(kind);
}

// ---------------------------------------------------------------------------
// 弹窗外壳
// ---------------------------------------------------------------------------

/** 打开指引。任何一种关闭方式（「知道了」、×、Esc、点弹窗外面、点卡片里的入口）都算看过。 */
export function openGuide(kind: GuideKind): void {
  activeClose?.();
  const overlay = document.createElement("div");
  overlay.className = "overlay g-ov";
  const modal = document.createElement("div");
  modal.className = "modal guide";
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");
  modal.dataset.guide = kind;

  const close = (): void => {
    if (activeClose !== close) return;
    activeClose = null;
    document.removeEventListener("keydown", onKey, true);
    overlay.remove();
    markSeen(kind);
  };
  const onKey = (event: KeyboardEvent): void => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    close();
  };
  /** 卡片里的入口：先关指引，再去对应页面。 */
  const go = (action: () => void) => () => {
    close();
    action();
  };

  const x = document.createElement("button");
  x.type = "button";
  x.className = "g-x";
  x.setAttribute("aria-label", t("关闭"));
  x.append(icon("close", { size: "sm" }));
  x.addEventListener("click", close);

  const label = document.createElement("span");
  label.className = "mlabel";
  const title = document.createElement("h3");
  title.id = `g-title-${kind}`;
  modal.setAttribute("aria-labelledby", title.id);
  const lead = document.createElement("p");
  lead.className = "g-lead";
  const body = document.createElement("div");
  body.className = "g-body";

  if (kind === "pdf") {
    label.textContent = t("PDF 与图片翻译");
    title.textContent = t("PDF 翻译方式一览");
    lead.textContent = t("PDF 中常见三类页面：可以选中文字的页面、工程图纸和扫描件。三类页面适合的翻译方式不同，效果和费用也不同。");
    buildPdfBody(body, go);
  } else {
    label.textContent = t("CAD 图纸翻译");
    title.textContent = t("CAD 图纸翻译一览");
    lead.textContent = t("Translator 直接修改图纸中的文字，线条、尺寸和图层保持不变。下面三项设置的默认值适用于大多数图纸。");
    buildCadBody(body, go);
  }

  const foot = document.createElement("div");
  foot.className = "g-foot";
  const reopen = document.createElement("small");
  reopen.textContent = t("之后可点击页面右上角的「使用说明」再次查看。");
  const ok = document.createElement("button");
  ok.type = "button";
  ok.className = "btn pri";
  ok.textContent = t("知道了");
  ok.addEventListener("click", close);
  foot.append(reopen, ok);

  modal.append(x, label, title, lead, body, foot);
  overlay.append(modal);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });
  document.addEventListener("keydown", onKey, true);
  document.body.append(overlay);
  activeClose = close;
  ok.focus({ preventScroll: true });
}

// ---------------------------------------------------------------------------
// 小部件
// ---------------------------------------------------------------------------

type StatState = "ok" | "warn" | "idle";

const STAT_ICON: Record<StatState, string> = {
  ok: `<svg viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  warn: `<svg viewBox="0 0 16 16"><path d="M8 3.5v5.5" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/><circle cx="8" cy="12.3" r="1.4" fill="currentColor"/></svg>`,
  idle: `<svg viewBox="0 0 16 16"><path d="M4.5 8h7" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>`,
};

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function statusDot(state: StatState): HTMLSpanElement {
  const dot = el("span", `g-dot ${state}`);
  dot.innerHTML = STAT_ICON[state];
  return dot;
}

function stat(state: StatState, text: string, action?: { label: string; onClick: () => void }): HTMLDivElement {
  const row = el("div", `g-stat ${state}`);
  row.append(statusDot(state), el("span", undefined, text));
  if (action) {
    const link = el("button", "g-link", `${action.label} ↗`);
    link.type = "button";
    link.addEventListener("click", action.onClick);
    row.append(link);
  }
  return row;
}

/** 卡片：插图 + 名称（可带 Beta）+ 若干行「适合 / 效果 / 建议」或一段说明 + 黄色提示 + 本机状态。 */
function card(opts: {
  ill: string;
  name: string;
  beta?: boolean;
  rows?: [string, string][];
  text?: string;
  note?: HTMLElement;
  stats?: HTMLElement;
}): HTMLDivElement {
  const node = el("div", "g-card");
  const ill = el("div");
  ill.style.display = "contents";
  ill.innerHTML = opts.ill;
  const name = el("div", "g-name", opts.name);
  if (opts.beta) name.append(el("span", "g-tag", t("Beta")));
  node.append(ill, name);
  if (opts.rows) {
    const dl = el("dl");
    for (const [key, value] of opts.rows) dl.append(el("dt", undefined, key), el("dd", undefined, value));
    node.append(dl);
  }
  if (opts.text) node.append(el("p", undefined, opts.text));
  if (opts.note) node.append(opts.note);
  if (opts.stats) node.append(opts.stats);
  return node;
}

/** 模型名不在中间断开。 */
function withNowrapModels(text: string): DocumentFragment {
  const frag = document.createDocumentFragment();
  let last = 0;
  for (const match of text.matchAll(/gpt-image-2(?:\.5-(?:flare|sunburst))?|Images 2\.[05]/g)) {
    const index = match.index ?? 0;
    if (index > last) frag.append(text.slice(last, index));
    frag.append(el("span", "g-nw", match[0]));
    last = index + match[0].length;
  }
  if (last < text.length) frag.append(text.slice(last));
  return frag;
}

function escapeAttr(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

// ---------------------------------------------------------------------------
// 插图（viewBox 260×118；示例文字固定为中文 → 法文）
// ---------------------------------------------------------------------------

const VB = "0 0 260 118";
const SCAN_SOURCE = "评审意见：同意"; // i18n-ignore 示意图里的示例文字固定为中文 → 法文
const ROOM_SOURCE = "会议室"; // i18n-ignore 示意图里的示例文字固定为中文 → 法文
const arrow = (x1: number, y: number, x2: number): string =>
  `<path class="gi-arr" d="M${x1} ${y}H${x2 - 5}"/><path class="gi-arrh" d="M${x2} ${y}l-6 -4v8z"/>`;
const bars = (x: number, y: number, ws: number[], gap = 7, h = 3): string =>
  ws.map((w, i) => `<rect class="gi-bar" x="${x}" y="${y + i * gap}" width="${w}" height="${h}" rx="1.5"/>`).join("");
const svg = (label: string, inner: string): string =>
  `<svg class="g-ill" viewBox="${VB}" role="img" aria-label="${escapeAttr(label)}">${inner}</svg>`;

function wordPage(x: number, title: string, cls: string): string {
  return `
    <rect class="gi-paper" x="${x}" y="7" width="84" height="104" rx="3"/>
    <text class="${cls}" x="${x + 9}" y="22" font-size="8.5">${title}</text>
    ${bars(x + 9, 28, [66, 58, 62])}
    <rect class="gi-thin" x="${x + 9}" y="50" width="66" height="27"/>
    <path class="gi-thin" d="M${x + 9} 59H${x + 75}M${x + 9} 68H${x + 75}M${x + 31} 50V77M${x + 53} 50V77"/>
    ${bars(x + 9, 84, [66, 54, 60, 36])}`;
}

function plan(x: number, y: number, a: string, b: string, cls: string, squeeze: boolean): string {
  return `
    <rect class="gi-paper" x="${x}" y="${y}" width="114" height="84" rx="3"/>
    <rect class="gi-wall" x="${x + 7}" y="${y + 7}" width="76" height="58"/>
    <path class="gi-wall" d="M${x + 38} ${y + 7}V${y + 34}M${x + 38} ${y + 48}V${y + 65}"/>
    <path class="gi-l" d="M${x + 38} ${y + 34}a14 14 0 0 1 14 14"/>
    <path class="gi-thin" d="M${x + 7} ${y + 73}H${x + 83}M${x + 7} ${y + 70}v6M${x + 83} ${y + 70}v6M${x + 38} ${y + 70}v6"/>
    <text class="gi-t" x="${x + 16}" y="${y + 81}" font-size="5">3600</text><text class="gi-t" x="${x + 54}" y="${y + 81}" font-size="5">4800</text>
    <rect class="gi-thin" x="${x + 88}" y="${y + 7}" width="19" height="70"/>
    ${bars(x + 91, y + 13, [13, 11, 13, 9, 12, 11, 8, 12], 8, 2.4)}
    <text class="${cls}" x="${x + 22.5}" y="${y + 24}" font-size="7.5" text-anchor="middle">${a}</text>
    <text class="${cls}" x="${x + 60.5}" y="${y + 24}" font-size="7.5" text-anchor="middle" ${squeeze ? `textLength="38" lengthAdjust="spacingAndGlyphs"` : ""}>${b}</text>`;
}

function scanPage(x: number, title: string, cls: string, rot: number): string {
  return `
    <g transform="rotate(${rot} ${x + 42} 59)">
      <rect class="${rot ? "gi-scan" : "gi-paper"}" x="${x}" y="7" width="84" height="104" rx="3"/>
      <text class="${cls}" x="${x + 9}" y="23" font-size="8">${title}</text>
      <path class="gi-l" d="M${x + 9} 34q5 -4 10 0t10 0t10 0t10 0t10 0M${x + 9} 43q5 -4 10 0t10 0t10 0t10 0M${x + 9} 52q5 -4 10 0t10 0t10 0t10 0t10 0"/>
      <circle class="gi-red" cx="${x + 57}" cy="86" r="14" stroke-width="1.6"/>
      <circle class="gi-red" cx="${x + 57}" cy="86" r="10" stroke-width=".7"/>
      <path class="gi-redf" d="M${x + 57} 79.5l2 5h5l-4 3l2 5l-5 -3l-5 3l2 -5l-4 -3h5z"/>
      <path class="gi-l" d="M${x + 10} 94c6 -10 9 6 13 -2s6 -8 9 2s7 -4 9 0"/>
    </g>`;
}

const illWord = (): string =>
  svg(t("示意图：可选中文字的页面翻译前后"), `${wordPage(20, "3. 施工组织", "gi-tb")}${arrow(112, 59, 148)}${wordPage(156, "3. Organisation", "gi-a")}`); // i18n-ignore 示意图里的示例文字固定为中文 → 法文
const illInplace = (): string =>
  svg(t("示意图：图纸原位替换前后"), `${plan(6, 17, "办公室", "会议室", "gi-tb", false)}${arrow(123, 59, 137)}${plan(140, 17, "Bureau", "Salle de réunion", "gi-a", true)}`); // i18n-ignore 示意图里的示例文字固定为中文 → 法文
const illImage = (): string =>
  svg(t("示意图：扫描件整页图像翻译前后"), `
    ${scanPage(20, SCAN_SOURCE, "gi-tb", -3)}${arrow(112, 59, 148)}${scanPage(156, "Avis : approuvé", "gi-a", 0)}
    <g transform="translate(225 99)"><rect class="gi-chip" x="0" y="0" width="22" height="16" rx="4"/><rect class="gi-thin" x="4" y="3.5" width="14" height="9" rx="1.5" style="stroke:var(--ink-3)"/><path class="gi-l" d="M5 11.5l4 -4l3 3l2 -2l3 3" stroke-width=".9"/></g>`);

/** 自动组合条里的四张小页面。 */
function thumb(kind: "text" | "table" | "draw" | "scan"): string {
  const paper = kind === "scan" ? "gi-scan" : "gi-paper";
  let body = "";
  if (kind === "text") body = bars(7, 9, [26, 22, 25, 18, 24, 20], 6, 2.4);
  if (kind === "table") body = `${bars(7, 9, [22, 26], 6, 2.4)}<rect class="gi-thin" x="7" y="22" width="26" height="18"/><path class="gi-thin" d="M7 28H33M7 34H33M16 22V40M25 22V40"/>`;
  if (kind === "draw") body = `<rect class="gi-wall" x="6" y="9" width="28" height="22" style="stroke-width:1.4"/><path class="gi-wall" d="M18 9V31" style="stroke-width:1.4"/><path class="gi-thin" d="M6 37H34"/><rect class="gi-thin" x="22" y="40" width="12" height="8"/>`;
  if (kind === "scan") body = `<path class="gi-l" d="M7 12q3 -2 6 0t6 0t6 0t6 0M7 18q3 -2 6 0t6 0t6 0" stroke-width=".9"/><circle class="gi-red" cx="26" cy="36" r="7" stroke-width="1.2"/><path class="gi-l" d="M8 40c3 -5 5 3 8 -1s3 -4 5 1" stroke-width=".9"/>`;
  return `<svg viewBox="0 0 40 52" aria-hidden="true"><rect class="${paper}" x="1" y="1" width="38" height="50" rx="2.5"/>${body}</svg>`;
}

function room(x: number, lines: { t: string; c: string; s: number }[], cap: string): string {
  const body = lines.map((line, i) =>
    `<text class="${line.c}" x="${x + 40}" y="${(lines.length === 1 ? 51 : 45) + i * 12}" font-size="${line.s}" text-anchor="middle">${line.t}</text>`).join("");
  return `<rect class="gi-paper" x="${x}" y="12" width="80" height="74" rx="3"/><rect class="gi-wall" x="${x + 5}" y="17" width="70" height="64" style="stroke-width:1.5"/>${body}<text class="gi-cap" x="${x + 40}" y="104" text-anchor="middle">${escapeAttr(cap)}</text>`;
}

const illCadDisplay = (): string =>
  svg(t("译文的显示方式"), `
    ${room(5, [{ t: "Salle de réunion", c: "gi-a", s: 8 }], t("仅译文"))}
    ${room(90, [{ t: ROOM_SOURCE, c: "gi-tb", s: 8.5 }, { t: "Salle de réunion", c: "gi-a", s: 7 }], t("原文在上"))}
    ${room(175, [{ t: "Salle de réunion", c: "gi-a", s: 8 }, { t: ROOM_SOURCE, c: "gi-tb", s: 7 }], t("译文在上"))}`);

function fitRow(y: number, squeeze: boolean): string {
  return `
    <rect class="gi-grid" x="58" y="${y}" width="150" height="30"/>
    <path class="gi-grid" d="M104 ${y}V${y + 30}"/>
    <text class="gi-t" x="81" y="${y + 19}" font-size="9" text-anchor="middle">B-204</text>
    ${squeeze
      ? `<text class="gi-a" x="156" y="${y + 19}" font-size="8.5" text-anchor="middle" textLength="94" lengthAdjust="spacingAndGlyphs">Salle de réunion principale</text>`
      : `<text class="gi-a" x="110" y="${y + 19.5}" font-size="10">Salle de réunion principale</text><rect class="gi-bad" x="205" y="${y + 4}" width="50" height="22" rx="2"/>`}`;
}

const illCadFit = (): string =>
  svg(t("译文过长时自动缩小"), `
    <defs><clipPath id="g-clip"><rect x="0" y="0" width="260" height="118" rx="9"/></clipPath></defs>
    <g clip-path="url(#g-clip)">
      <text class="gi-cap" x="10" y="35">${escapeAttr(t("缩小前"))}</text>${fitRow(16, false)}
      <text class="gi-cap" x="10" y="87">${escapeAttr(t("缩小后"))}</text>${fitRow(68, true)}
    </g>`);

const illCadLock = (): string =>
  svg(t("图框等内容保持原样"), `
    <rect class="gi-paper" x="10" y="12" width="240" height="94" rx="3"/>
    <rect class="gi-wall" x="18" y="20" width="118" height="78" style="stroke-width:1.6"/>
    <path class="gi-wall" d="M74 20V98" style="stroke-width:1.6"/>
    <text class="gi-a" x="46" y="62" font-size="8.5" text-anchor="middle">Bureau</text>
    <text class="gi-a" x="105" y="62" font-size="8.5" text-anchor="middle">Archives</text>
    <rect class="gi-grid" x="146" y="20" width="96" height="78"/>
    <path class="gi-grid" d="M146 40H242M146 59H242M146 78H242M178 40V98"/>
    <text class="gi-tb" x="194" y="34" font-size="8.5" text-anchor="middle">某某设计院</text>
    <text class="gi-t" x="162" y="53" font-size="7.5" text-anchor="middle">工程</text><text class="gi-t" x="210" y="53" font-size="7.5" text-anchor="middle">办公楼</text>
    <text class="gi-t" x="162" y="72" font-size="7.5" text-anchor="middle">设计</text><text class="gi-t" x="210" y="72" font-size="7.5" text-anchor="middle">张三</text>
    <text class="gi-t" x="162" y="91" font-size="7.5" text-anchor="middle">审核</text><text class="gi-t" x="210" y="91" font-size="7.5" text-anchor="middle">李四</text>
    <g transform="translate(232 3)"><rect class="gi-lock" x="0" y="8" width="18" height="14" rx="3"/><path class="gi-l" d="M4 8V5a5 5 0 0 1 10 0v3" style="stroke:var(--ink-2);stroke-width:1.6"/></g>`);

// ---------------------------------------------------------------------------
// PDF
// ---------------------------------------------------------------------------

type Go = (action: () => void) => () => void;

function buildPdfBody(body: HTMLElement, go: Go): void {
  const fit = t("适合");
  const eff = t("效果");
  const wordName = t("转成 Word");
  const inplaceName = t("原位替换");
  const imageName = t("整页图像翻译");

  // 本机状态先空着，查到后再填；查不到就一直空着，不猜。
  const wordStats = el("div", "g-stats");
  const inplaceStats = el("div", "g-stats");
  const imageStats = el("div", "g-stats");

  const note = el("div", "g-note");
  note.append(statusDot("warn"));
  const noteText = el("span");
  noteText.append(withNowrapModels(t("已测试多种图像模型，目前仅建议使用 ChatGPT 的 Images 2.5 或 Images 2.0，模型名为 gpt-image-2.5-flare、gpt-image-2.5-sunburst 或 gpt-image-2。")));
  note.append(noteText);

  const cards = el("div", "g-cards");
  cards.append(
    card({
      ill: illWord(),
      name: wordName,
      rows: [
        [fit, t("可以选中文字的页面，如由 Word 导出的报告、合同、说明书。")],
        [eff, t("保留大部分排版，可另存一份 Word；费用较低。由于各语言篇幅不同，译文的页数可能多于或少于原文。")],
      ],
      stats: wordStats,
    }),
    card({
      ill: illInplace(),
      name: inplaceName,
      beta: true,
      rows: [
        [fit, t("工程图纸和 A3 及以上的大幅面页面。")],
        [eff, t("只替换文字，线条和尺寸不变，过长的译文自动缩小。可供阅读，尚不适合正式对外交付。")],
        [t("建议"), t("大型工程图纸如有 DWG 原图，建议在「CAD 图纸」页翻译后重新导出 PDF，效果更好。")],
        // AutoCAD 导出 PDF 时，SHX 线形字体默认写成几何线条（PDFSHX 只另加注释），文字层里没有这些字。
        [t("说明"), t("SHX 字体（如 txt.shx）的文字在 CAD 导出 PDF 时默认变为线条，需由「文字识别组件」识别；TrueType 字体可直接提取。")],
      ],
      stats: inplaceStats,
    }),
    card({
      ill: illImage(),
      name: imageName,
      rows: [
        [fit, t("扫描件、拍照件、签字盖章页。")],
        [eff, t("整页由图像模型重新生成为图片，译文无法编辑；页面越大越模糊，费用较高。")],
      ],
      note,
      stats: imageStats,
    }),
  );

  const band = el("div", "g-band");
  const bandText = el("div");
  bandText.append(
    el("h4", undefined, t("混有多种页面时，选择「自动组合」")),
    el("p", undefined, t("程序逐页判断页面类型，分别使用对应方式翻译，最后按原页序合成一份 PDF。某种方式在本机不可用时，该页改用「保底方式」，完成后单独列出。")),
  );
  const flow = el("div", "g-flow");
  const pages = el("div", "g-pages");
  const pageSpecs: ["text" | "table" | "draw" | "scan", string, string][] = [
    ["text", t("文字页"), wordName],
    ["table", t("表格页"), wordName],
    ["draw", t("图纸"), inplaceName],
    ["scan", t("扫描件"), imageName],
  ];
  for (const [kind, name, route] of pageSpecs) {
    const pg = el("div", "g-pg");
    const pic = el("div");
    pic.style.display = "contents";
    pic.innerHTML = thumb(kind);
    pg.append(pic, el("small", undefined, name), el("span", "g-chip", route));
    pages.append(pg);
  }
  const out = el("div", "g-out");
  const outPic = el("div");
  outPic.style.display = "contents";
  outPic.innerHTML = `<svg viewBox="0 0 40 52" aria-hidden="true"><rect class="gi-paper" x="6" y="1" width="33" height="44" rx="2.5"/><rect class="gi-paper" x="3" y="4" width="33" height="44" rx="2.5"/><rect class="gi-paper" x="0" y="7" width="33" height="44" rx="2.5"/><text class="gi-a" x="16.5" y="33" font-size="8" text-anchor="middle">PDF</text></svg>`;
  out.append(outPic, el("small", undefined, t("合成一份 PDF")));
  const arrowEl = el("span", "g-arrow", "→");
  arrowEl.setAttribute("aria-hidden", "true");
  flow.append(pages, arrowEl, out);
  band.append(bandText, flow);

  body.append(cards, band);
  void fillPdfStats({ wordStats, inplaceStats, imageStats }, go);
}

async function fillPdfStats(
  hosts: { wordStats: HTMLElement; inplaceStats: HTMLElement; imageStats: HTMLElement },
  go: Go,
): Promise<void> {
  let client;
  try {
    client = await connectedClient();
  } catch {
    return;
  }
  const [caps, ocr] = await Promise.all([
    client.pdfRouteCapabilities().catch(() => null),
    client.getOcrCapability().catch(() => null),
  ]);
  if (!hosts.wordStats.isConnected) return;
  const installPdf = { label: t("一键安装"), onClick: go(() => navigate("plugins", { focus: "pdf" })) };
  if (caps) {
    const component = caps.word_component
      ? null
      : stat("warn", t("需先安装「PDF 处理组件」"), installPdf);
    // 转成 Word：组件 + 把译文转回 PDF 的 Word / LibreOffice。
    if (component) hosts.wordStats.append(component);
    else if (!caps.office_engine) hosts.wordStats.append(stat("warn", t("需要电脑上安装 Microsoft Word 或 LibreOffice")));
    else hosts.wordStats.append(stat("ok", t("本机可以使用")));
    // 原位替换：组件里的 PyMuPDF 要能加载。
    if (!caps.word_component) hosts.inplaceStats.append(stat("warn", t("需先安装「PDF 处理组件」"), installPdf));
    else if (caps.inplace_component === false) {
      hosts.inplaceStats.append(stat("warn", t("「PDF 处理组件」无法加载，需重新安装"), { label: t("重新安装"), onClick: installPdf.onClick }));
    } else hosts.inplaceStats.append(stat("ok", t("本机可以使用")));
    hosts.imageStats.append(caps.image_model_ready
      ? stat("ok", t("已配置「图像翻译」模型，可以使用"))
      : stat("warn", t("需先配置「图像翻译」模型"), { label: t("去设置"), onClick: go(() => navigate("settings", { page: "models", role: "image" })) }));
  }
  if (ocr && ocr.state !== "connected") {
    hosts.inplaceStats.append(stat("idle", t("导出为线条的文字另需「文字识别组件」"), { label: t("安装"), onClick: go(() => navigate("plugins", { focus: "ocr" })) }));
  }
}

// ---------------------------------------------------------------------------
// CAD
// ---------------------------------------------------------------------------

function buildCadBody(body: HTMLElement, go: Go): void {
  const cards = el("div", "g-cards");
  cards.append(
    card({ ill: illCadDisplay(), name: t("译文的显示方式"), text: t("默认只保留译文。需要双语图纸时，在右侧「输出」中选择「双语对照」，再选原文在上或译文在上。") }),
    card({ ill: illCadFit(), name: t("译文过长时自动缩小"), text: t("放不下时先压窄字形，再逐步缩小字号，最小为原字号的 40%；仍放不下的位置列入报告，请人工复核。") }),
    card({ ill: illCadLock(), name: t("图框等内容保持原样"), text: t("图框、标题栏、会签栏和公司标志通常无需翻译。在「高级 → 锁定不翻译」中填写图块名或图层名，或点击「从图纸中挑选」直接勾选。") }),
  );

  const strip = el("div", "g-strip");
  const stripText = el("div");
  stripText.append(
    el("h4", undefined, t("DWG 图纸需要「图纸转换工具」")),
    el("p", undefined, t("DXF 图纸可直接翻译。DWG 图纸需通过免费的「图纸转换工具」读写，安装后返回本页重新读取即可。")),
  );
  const stripStat = el("div");
  strip.append(stripText, stripStat);

  const tip = el("div", "g-tip");
  tip.append(icon("spark", { size: "sm" }), el("span", undefined, t("翻译完成后，可在「任务」中打开「查看与修改译文」，逐条修改后重新写回图纸，无需再次调用模型。")));

  body.append(cards, strip, tip);
  void (async () => {
    try {
      const client = await connectedClient();
      const capability = await client.getCadCapability();
      if (!stripStat.isConnected) return;
      stripStat.append(capability.oda === "connected"
        ? stat("ok", t("「图纸转换工具」已就绪"))
        : stat("warn", t("尚未连接「图纸转换工具」"), { label: t("一键安装"), onClick: go(() => navigate("plugins", { focus: "oda" })) }));
    } catch {
      // 查不到时不显示状态。
    }
  })();
}
