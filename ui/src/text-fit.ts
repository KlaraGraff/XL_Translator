/**
 * 界面文字放不下时的两种排法（规则见 docs/UI_TEXT_FIT.md）。
 *
 * - 名称（按钮、标签、标题、菜单、下拉选项、提示、报错）一律写全：先给空间，再折行，
 *   再换排法（并排放不下就上下排、四列放不下就两列）。界面不缩字号。
 * - 内容（文件名、路径、任务名、模型名、连接名）可以单行截断：元素加 FIT_CLIP（.fit-clip），
 *   鼠标停上去时这里补上全文提示；没被截断时不出提示，作者自己写的 title 不动。
 *
 * 全软件只有 app.css 的 .fit-clip 一处写 text-overflow: ellipsis
 * （tests/test_ui_text_fit_contract.py 把关；界面上的检查是 scripts/ui_text_fit_check.mjs）。
 */

export const FIT_CLIP = "fit-clip";

/** 由这里补上的提示做个记号：下次悬停时文字没截断了（窗口变宽、内容变短）就撤掉。 */
const AUTO_TITLE = "fitTitle";

function fullText(el: HTMLElement): string {
  if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) return el.value.trim();
  return (el.textContent ?? "").replace(/\s+/g, " ").trim();
}

export function isClipped(el: HTMLElement): boolean {
  return el.scrollWidth > el.clientWidth + 1;
}

/** 悬停在可截断的内容上：真被截断才给全文提示。 */
function syncTitle(el: HTMLElement): void {
  const auto = el.dataset[AUTO_TITLE] === "1";
  if (el.hasAttribute("title") && !auto) return;
  const text = fullText(el);
  if (text && isClipped(el)) {
    if (el.title !== text) el.title = text;
    el.dataset[AUTO_TITLE] = "1";
  } else if (auto) {
    el.removeAttribute("title");
    delete el.dataset[AUTO_TITLE];
  }
}

let installed = false;

export function installTextFit(): void {
  if (installed) return;
  installed = true;
  // 用 mouseover（会冒泡、在系统提示出现之前触发）一处委托，免得每个列表行各挂一个监听。
  document.addEventListener("mouseover", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const clip = target.closest<HTMLElement>(`.${FIT_CLIP}`);
    if (clip) syncTitle(clip);
  }, { passive: true });
}

let ruler: HTMLSpanElement | null = null;

/** 照输入框的字体量一段文字排成一行有多宽。 */
function textWidth(input: HTMLInputElement, text: string): number {
  if (!ruler) {
    ruler = document.createElement("span");
    ruler.setAttribute("aria-hidden", "true");
    ruler.style.cssText = "position:fixed;left:-9999px;top:0;visibility:hidden;white-space:pre;pointer-events:none";
    document.body.append(ruler);
  }
  const style = getComputedStyle(input);
  ruler.style.font = style.font;
  ruler.style.letterSpacing = style.letterSpacing;
  ruler.textContent = text;
  return ruler.getBoundingClientRect().width;
}

/**
 * 输入框的提示文字（placeholder）是名称类，不能截断；可输入框又不能折行。完整说法放得下就照写，
 * 放不下（英文、法文的窄窗口）换成词表约定的短说法，完整说法放进悬停提示。窗口宽窄变了会重新量。
 */
export function fitPlaceholder(input: HTMLInputElement, full: string, short: string): void {
  const apply = () => {
    if (!input.isConnected || !input.clientWidth) return;
    const style = getComputedStyle(input);
    const room = input.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const fits = textWidth(input, full) <= room;
    input.placeholder = fits ? full : short;
    const auto = input.dataset[AUTO_TITLE] === "1";
    if (!fits && (auto || !input.hasAttribute("title"))) {
      input.title = full;
      input.dataset[AUTO_TITLE] = "1";
    } else if (fits && auto) {
      input.removeAttribute("title");
      delete input.dataset[AUTO_TITLE];
    }
  };
  input.placeholder = full;
  new ResizeObserver(apply).observe(input);
}
