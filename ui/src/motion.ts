// 动效开关与参数（V10.3「流光」）。时长、缓动与 styles/tokens.css 的 --t-* / --ease-* 同值；
// Web Animations API 拿不到 CSS 变量，所以这里各写一份，改的时候两处一起改。
//
// 系统打开「减弱动态效果」，或 <html data-motion="reduced">（留给设置页的「动效：减少」）时，
// 所有动画都跳过，只保留最终状态；CSS 过渡由 app.css 统一压到 1ms。

export const EASE_OUT = "cubic-bezier(.2, 0, 0, 1)";
export const EASE_IN = "cubic-bezier(.4, 0, 1, 1)";

const reducedQuery = typeof window !== "undefined" && typeof window.matchMedia === "function"
  ? window.matchMedia("(prefers-reduced-motion: reduce)")
  : null;

/** 现在可以做动画吗：没开减少动效，并且这个运行时支持 element.animate（Safari 13.1 起）。 */
export function motionAllowed(): boolean {
  if (document.documentElement.dataset.motion === "reduced") return false;
  if (reducedQuery?.matches) return false;
  return typeof Element.prototype.animate === "function";
}

/** 同 tokens.css 的 --ease-spring：滑块、高亮块这类「滑到位」的位移用它。 */
export const EASE_SPRING = "cubic-bezier(.3, 1.25, .5, 1)";
/** 同 tokens.css 的 --t-page。 */
export const T_PAGE = 260;

/**
 * 页内换内容时新内容的进场：从下方几像素淡入。旧内容已经被同步换掉，这里只管新的。
 * "page" 与 router.ts 换页同参（设置页左栏换子页），"panel" 与「扩展」页换页签同参
 * （页签、用途卡这类页内切换）。减少动效时什么都不做。
 */
export function riseIn(targets: Element | Element[], level: "page" | "panel" = "panel"): Animation[] {
  if (!motionAllowed()) return [];
  const [distance, duration] = level === "page" ? [8, 240] : [6, 160];
  return (Array.isArray(targets) ? targets : [targets]).map((target) => target.animate(
    [{ opacity: 0, transform: `translateY(${distance}px)` }, { opacity: 1, transform: "none" }],
    { duration, easing: EASE_OUT },
  ));
}

let colorFadeTimer = 0;

/**
 * 换主题或配色：颜色在 --t-page 里过渡过去，不是一帧跳变。<html class="color-fade"> 只挂这一小会儿，
 * 期间 app.css 让所有元素只过渡颜色类属性（位移类的动画走 Web Animations，不受影响）。
 */
export function withColorFade(change: () => void): void {
  if (motionAllowed()) {
    const root = document.documentElement;
    root.classList.add("color-fade");
    window.clearTimeout(colorFadeTimer);
    colorFadeTimer = window.setTimeout(() => root.classList.remove("color-fade"), T_PAGE + 60);
  }
  change();
}
