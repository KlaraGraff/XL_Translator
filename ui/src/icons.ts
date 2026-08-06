// V9 图标库 —— 从 docs/mockups/2026-08-06_v9_full_redesign.html 的 <symbol> 定义原样迁移。
// 用法：应用启动时调用一次 injectIconSprite() 把 <symbol> 表注入文档，
// 之后各处用 icon("excel") 取一个可直接 append 的 <svg><use> 元素。

export const ICON_NAMES = [
  "excel",
  "word",
  "pdf",
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
  pdf: {
    viewBox: "0 0 24 24",
    body: '<path d="M6 3.5h8.5L19.5 8.5V18a2.5 2.5 0 0 1-2.5 2.5H6A2.5 2.5 0 0 1 3.5 18V6A2.5 2.5 0 0 1 6 3.5Z"/><circle cx="9" cy="13" r="1.6"/><path d="m16.5 17-2.8-3.6-2.2 2.6-1.3-1.4L7 18"/>',
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
