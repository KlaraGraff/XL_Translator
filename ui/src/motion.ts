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
