// V9 图标库 —— 从 docs/mockups/2026-08-06_v9_full_redesign.html 的 <symbol> 定义原样迁移。
// 用法：应用启动时调用一次 injectIconSprite() 把 <symbol> 表注入文档，
// 之后各处用 icon("excel") 取一个可直接 append 的 <svg><use> 元素。

export const ICON_NAMES = [
  "excel",
  "word",
  "ppt",
  "subtitle",
  "pdf",
  "cad",
  "convert",
  "ocr",
  "tasks",
  "book",
  "gear",
  "help",
  "folder",
  "search",
  "play",
  "stop",
  "pause",
  "check",
  "warn",
  "chev",
  "plus",
  "pin",
  "edit",
  "trash",
  "ext",
  "doc-file",
  "down",
  "upload",
  "restart",
  "spark",
  "wand",
  "close",
  "message",
  "lock",
  "blocks",
  "link",
  "trans",
] as const;

export type IconName = (typeof ICON_NAMES)[number];

const SPRITE_ID = "v9-icon-sprite";

// 与样张 <defs> 内容逐一对应，viewBox 与路径数据未作修改。
const SYMBOLS: Record<IconName, { viewBox: string; body: string }> = {
  excel: {
    viewBox: "0 0 24 24",
    body: '<rect x="3.5" y="4" width="17" height="16" rx="2.5"/><path d="M3.5 9.5h17M3.5 14.5h17M9 4.5V19.5M15 4.5V19.5"/>',
  },
  word: {
    viewBox: "0 0 24 24",
    body: '<path d="M6 3.5h8.5L19.5 8.5V18a2.5 2.5 0 0 1-2.5 2.5H6A2.5 2.5 0 0 1 3.5 18V6A2.5 2.5 0 0 1 6 3.5Z"/><path d="M14 3.5V9h5.5M7.5 13h7M7.5 16.5h5"/>',
  },
  ppt: {
    viewBox: "0 0 24 24",
    // A projection board on a stand with three bars: a slide deck, distinct from the folded-sheet documents.
    body: '<rect x="3.5" y="4" width="17" height="12" rx="2.5"/><path d="M12 16v4M8.5 20h7M8 12.5V10.5M12 12.5V8M16 12.5V9.5"/>',
  },
  subtitle: {
    viewBox: "0 0 24 24",
    // A screen with two caption bars along its lower edge: subtitles, distinct from the slide-deck stand.
    body: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M7 13.5h6M15 13.5h2M7 16.5h3M12 16.5h5"/>',
  },
  pdf: {
    viewBox: "0 0 24 24",
    body: '<path d="M6 3.5h8.5L19.5 8.5V18a2.5 2.5 0 0 1-2.5 2.5H6A2.5 2.5 0 0 1 3.5 18V6A2.5 2.5 0 0 1 6 3.5Z"/><circle cx="9" cy="13" r="1.6"/><path d="m16.5 17-2.8-3.6-2.2 2.6-1.3-1.4L7 18"/>',
  },
  cad: {
    viewBox: "0 0 24 24",
    // Technical drawing sheet, matching the PDF icon's folded-document
    // silhouette while using drafting lines instead of a photo glyph.
    body: '<path d="M6 3.5h8.5L19.5 8.5V18a2.5 2.5 0 0 1-2.5 2.5H6A2.5 2.5 0 0 1 3.5 18V6A2.5 2.5 0 0 1 6 3.5Z"/><path d="M14 3.5V9h5.5M7.5 12h4M7.5 15h9M7.5 18h6"/>',
  },
  convert: {
    viewBox: "0 0 24 24",
    // Two opposing arrows communicate DWG ↔ DXF conversion without another file glyph.
    body: '<path d="M4 7h14l-3-3M20 17H6l3 3"/>',
  },
  ocr: {
    viewBox: "0 0 24 24",
    // Corner scan brackets around a text line communicate "read text out of a picture".
    body: '<path d="M4 8V5.5A1.5 1.5 0 0 1 5.5 4H8M16 4h2.5A1.5 1.5 0 0 1 20 5.5V8M20 16v2.5a1.5 1.5 0 0 1-1.5 1.5H16M8 20H5.5A1.5 1.5 0 0 1 4 18.5V16"/><path d="M7.5 10.5h9M7.5 13.5h6"/>',
  },
  tasks: {
    viewBox: "0 0 24 24",
    body: '<rect x="3.5" y="4.5" width="17" height="6.5" rx="2"/><rect x="3.5" y="14" width="17" height="6.5" rx="2"/><path d="m6.5 7.7 1.2 1.2 2-2.2"/>',
  },
  book: {
    viewBox: "0 0 24 24",
    body: '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15.5H6.75A2.75 2.75 0 0 0 4 21.25Z"/><path d="M4 18.5V21M8.5 7.5h7"/>',
  },
  gear: {
    viewBox: "0 0 24 24",
    body: '<circle cx="12" cy="12" r="3.2"/><path d="M12 2.8v2.4M12 18.8v2.4M21.2 12h-2.4M5.2 12H2.8M18.5 5.5l-1.7 1.7M7.2 16.8l-1.7 1.7M18.5 18.5l-1.7-1.7M7.2 7.2 5.5 5.5"/>',
  },
  help: {
    viewBox: "0 0 24 24",
    body: '<circle cx="12" cy="12" r="8.5"/><path d="M9.6 9.4a2.5 2.5 0 1 1 3.4 2.9c-.8.4-1 1-1 1.8"/><path d="M12 17h.01"/>',
  },
  folder: {
    viewBox: "0 0 24 24",
    body: '<path d="M3.5 7A2.5 2.5 0 0 1 6 4.5h3.5l2 2.5H18A2.5 2.5 0 0 1 20.5 9.5V17A2.5 2.5 0 0 1 18 19.5H6A2.5 2.5 0 0 1 3.5 17Z"/>',
  },
  search: {
    viewBox: "0 0 24 24",
    body: '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4-4"/>',
  },
  play: {
    viewBox: "0 0 24 24",
    body: '<path d="M7.5 5.5 18 12 7.5 18.5Z"/>',
  },
  stop: {
    viewBox: "0 0 24 24",
    body: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/>',
  },
  pause: {
    viewBox: "0 0 24 24",
    body: '<path d="M9 6v12M15 6v12"/>',
  },
  check: {
    viewBox: "0 0 24 24",
    body: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  },
  warn: {
    viewBox: "0 0 24 24",
    body: '<path d="M12 4 21 19.5H3Z"/><path d="M12 10v4.2M12 17h.01"/>',
  },
  chev: {
    viewBox: "0 0 24 24",
    body: '<path d="m7 10 5 5 5-5"/>',
  },
  plus: {
    viewBox: "0 0 24 24",
    body: '<path d="M12 5v14M5 12h14"/>',
  },
  pin: {
    viewBox: "0 0 24 24",
    body: '<path d="M9 4.5h6l-.7 6.2 2.7 3.3H7l2.7-3.3ZM12 14v5.5"/>',
  },
  edit: {
    viewBox: "0 0 24 24",
    body: '<path d="M14.5 5.5 18.5 9.5 8.5 19.5H4.5V15.5Z"/><path d="m12.5 7.5 4 4"/>',
  },
  trash: {
    viewBox: "0 0 24 24",
    body: '<path d="M4.5 6.5h15M9.5 6V4.5h5V6M6.5 6.5l1 13h9l1-13M10 10.5v5.5M14 10.5v5.5"/>',
  },
  ext: {
    viewBox: "0 0 24 24",
    body: '<path d="M13.5 5.5H18.5V10.5M18.5 5.5 11 13M9 6.5H6A1.5 1.5 0 0 0 4.5 8v10A1.5 1.5 0 0 0 6 19.5h10a1.5 1.5 0 0 0 1.5-1.5v-3"/>',
  },
  "doc-file": {
    viewBox: "0 0 24 24",
    body: '<path d="M6 3.5h8.5L19.5 8.5V18a2.5 2.5 0 0 1-2.5 2.5H6A2.5 2.5 0 0 1 3.5 18V6A2.5 2.5 0 0 1 6 3.5Z"/><path d="M14 3.5V9h5.5"/>',
  },
  // 更新流程用的四个（对照 docs/mockups/2026-08-11_in-app-update.html 的 <symbol>，
  // 笔画粗细与既有图标统一为 1.6px 线框风格）。
  down: {
    viewBox: "0 0 24 24",
    body: '<path d="M12 4v11"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/><path d="M4.5 20h15"/>',
  },
  upload: {
    viewBox: "0 0 24 24",
    body: '<path d="M12 15.5V4"/><path d="m7.5 8.5 4.5-4.5 4.5 4.5"/><path d="M4.5 14.5v3A2.5 2.5 0 0 0 7 20h10a2.5 2.5 0 0 0 2.5-2.5v-3"/>',
  },
  restart: {
    viewBox: "0 0 24 24",
    body: '<path d="M20.5 12a8.5 8.5 0 1 1-2.8-6.3"/><path d="M20.5 4v5h-5"/>',
  },
  spark: {
    viewBox: "0 0 24 24",
    body: '<path d="M12 3.5v3.6M12 16.9v3.6M3.5 12h3.6M16.9 12h3.6M6 6l2.5 2.5M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18"/>',
  },
  // Lantern 的 CustomActionEditor 使用 lucide Wand2；这里保留同样的「斜杖 + 四角星」轮廓，
  // 让翻译设置里的整理按钮和 Lantern 视觉一致。
  wand: {
    viewBox: "0 0 24 24",
    body: '<path d="m21.64 3.64-1.28-1.28a1.21 1.21 0 0 0-1.72 0L2.36 18.64a1.21 1.21 0 0 0 0 1.72l1.28 1.28a1.2 1.2 0 0 0 1.72 0L21.64 5.36a1.2 1.2 0 0 0 0-1.72"/><path d="m14 7 3 3M5 6v4M19 14v4M10 2v2M7 8H3M21 16h-4M11 3H9"/>',
  },
  close: {
    viewBox: "0 0 24 24",
    body: '<path d="M18 6 6 18M6 6l12 12"/>',
  },
  message: {
    viewBox: "0 0 24 24",
    // 对话气泡：侧栏「反馈」。
    body: '<path d="M5 5h14a1.5 1.5 0 0 1 1.5 1.5v8.5a1.5 1.5 0 0 1-1.5 1.5h-8.5L6 20v-3.5H5a1.5 1.5 0 0 1-1.5-1.5V6.5A1.5 1.5 0 0 1 5 5Z"/><path d="M8 9.5h8M8 12.5h5"/>',
  },  lock: {
    viewBox: "0 0 24 24",
    // 反馈页勾选原文件、译文时的隐私说明。
    body: '<rect x="5" y="10.5" width="14" height="9.5" rx="2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
  },
  blocks: {
    viewBox: "0 0 24 24",
    // 三块加一个加号：侧栏「扩展」（翻译类型）。与「插件」的 spark 区分开。
    body: '<rect x="4" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.5"/><path d="M16.75 4v6.5M13.5 7.25H20"/>',
  },
  link: {
    viewBox: "0 0 24 24",
    // 两个环扣在一起：侧栏「MCP」（让智能体接入）。
    body: '<path d="M10 13.5a4 4 0 0 0 6 .4l2.6-2.6a4 4 0 0 0-5.7-5.7l-1.4 1.4"/><path d="M14 10.5a4 4 0 0 0-6-.4l-2.6 2.6a4 4 0 0 0 5.7 5.7l1.4-1.4"/>',
  },
  trans: {
    viewBox: "0 0 24 24",
    // 「文」加「A」：侧栏「翻译」分组的父项（流光外壳）。
    body: '<path d="M4 6h8M8 4v2M6.5 6c0 4 2.3 7 5.5 9M11.5 6c-.6 3.5-3 6.5-6.5 8.5"/><path d="m12.5 20 4-9 4 9M14 17h5"/>',
  },
};

/**
 * 把图标 sprite（一组 <symbol>）注入文档，整个应用生命周期只需调用一次。
 * 幂等：重复调用不会重复注入。
 */
export function injectIconSprite(target: HTMLElement = document.body): void {
  if (document.getElementById(SPRITE_ID)) {
    return;
  }
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("id", SPRITE_ID);
  svg.setAttribute("width", "0");
  svg.setAttribute("height", "0");
  svg.setAttribute("style", "position:absolute");
  svg.setAttribute("aria-hidden", "true");
  const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
  for (const name of ICON_NAMES) {
    const def = SYMBOLS[name];
    const symbol = document.createElementNS("http://www.w3.org/2000/svg", "symbol");
    symbol.setAttribute("id", `i-${name}`);
    symbol.setAttribute("viewBox", def.viewBox);
    symbol.innerHTML = def.body;
    defs.appendChild(symbol);
  }
  svg.appendChild(defs);
  target.insertBefore(svg, target.firstChild);
}

export type IconSize = "sm" | "md" | "lg";

export interface IconOptions {
  size?: IconSize;
  className?: string;
}

/**
 * 返回一个可直接 append 的 <svg class="ic"><use href="#i-name"/></svg>。
 * 对应样张里 `<svg class="ic"><use href="#i-xxx"/></svg>` 的用法。
 */
export function icon(name: IconName, options: IconOptions = {}): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const classes = ["ic"];
  if (options.size && options.size !== "md") {
    classes.push(options.size);
  }
  if (options.className) {
    classes.push(options.className);
  }
  svg.setAttribute("class", classes.join(" "));
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.appendChild(use);
  return svg;
}
