/** Normalize a path pasted from a shell, chat, or a file manager. */
const OUTER_PAIRS: Record<string, string> = {
  "'": "'",
  '"': '"',
  "`": "`",
  "“": "”",
  "‘": "’",
  "「": "」",
  "『": "』",
  "《": "》",
  "〈": "〉",
  "<": ">",
};

export function normalizeUserPath(value: string): string {
  let normalized = value.trim();
  while (normalized.length >= 2) {
    const closing = OUTER_PAIRS[normalized[0]];
    if (!closing || normalized[normalized.length - 1] !== closing) break;
    normalized = normalized.slice(1, -1).trim();
  }
  return normalized;
}

const ELLIPSIS = "…";

/**
 * 放不下的路径省略中间、留两端（与 macOS 访达一致）：开头看得出在哪个盘、哪个目录下，
 * 结尾看得出是哪个文件夹或文件。结尾多留一点，文件名比上层目录更要紧。
 * `measure` 返回一段文字的显示宽度（像素）。
 */
export function middleEllipsis(value: string, maxWidth: number, measure: (text: string) => number): string {
  if (!value || maxWidth <= 0 || measure(value) <= maxWidth) return value;
  const chars = Array.from(value);
  const fits = (keep: number): string | null => {
    const tail = Math.ceil(keep * 0.55);
    const head = keep - tail;
    const candidate = chars.slice(0, head).join("") + ELLIPSIS + chars.slice(chars.length - tail).join("");
    return measure(candidate) <= maxWidth ? candidate : null;
  };
  let low = 0;
  let high = chars.length - 1;
  let best = ELLIPSIS;
  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    const candidate = fits(mid);
    if (candidate !== null) {
      best = candidate;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  return best;
}

let measureCanvas: HTMLCanvasElement | null = null;

/**
 * 输入框不在编辑时显示省略中间的路径，点进去编辑时就是完整路径；鼠标悬停看完整路径。
 *
 * 输入框的值始终是完整路径，省略后的那份画在上面一层（只在放不下、且没在编辑时显示），
 * 所以聚焦、全选、粘贴、拖选都和普通输入框一样。`full` 取当前完整的值；返回的函数在
 * 完整值从别处改了（如「浏览」选了文件夹）之后调用，重排一次。
 */
export function showPathWithMiddleEllipsis(input: HTMLInputElement, full: () => string): () => void {
  const field = document.createElement("span");
  field.className = "path-field";
  const shown = document.createElement("span");
  shown.className = "path-shown";
  shown.setAttribute("aria-hidden", "true");
  input.replaceWith(field);
  field.append(input, shown);
  const layout = (): void => {
    const value = full() || input.value;
    input.title = value;
    const style = getComputedStyle(input);
    const left = parseFloat(style.paddingLeft || "0");
    const right = parseFloat(style.paddingRight || "0");
    const width = input.clientWidth - left - right;
    measureCanvas ??= document.createElement("canvas");
    const context = measureCanvas.getContext("2d");
    if (!value || width <= 0 || !context) {
      field.classList.remove("is-shortened");
      return;
    }
    const font = style.font || `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
    context.font = font;
    const text = middleEllipsis(value, width, (part) => context.measureText(part).width);
    field.classList.toggle("is-shortened", text !== value);
    shown.textContent = text;
    Object.assign(shown.style, {
      font,
      color: style.color,
      letterSpacing: style.letterSpacing,
      left: `${input.offsetLeft + left + parseFloat(style.borderLeftWidth || "0")}px`,
      width: `${width}px`,
    });
  };
  input.addEventListener("blur", layout);
  input.addEventListener("input", layout);
  // 第一次排版（刚挂到页面上）和以后窗口、侧栏宽度变化时都重排。
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(layout).observe(input);
  return layout;
}
