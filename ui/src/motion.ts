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

/** 同 tokens.css 的 --ease-spring：成功反馈（复制后的对勾）这类「弹出来」的小动作用它。 */
export const EASE_SPRING = "cubic-bezier(.3, 1.25, .5, 1)";
/** 同 tokens.css 的 --ease-snap：吸附到位的位移（分段控件滑块）用它，干脆、不回弹。 */
export const EASE_SNAP = "cubic-bezier(.2, .8, .2, 1)";
/** 同 tokens.css 的 --t-base。 */
export const T_BASE = 200;
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

// ---------------------------------------------------------------------------
// 整块重画时不重播动画
// ---------------------------------------------------------------------------
// 各页刷新（轮询、事件流、改一个选项）大多是清空后整块重建。新节点上的 CSS 动画会从头播：
// 进场浮现再浮一次（任务页统计格每 4 秒闪一下），循环动画（呼吸点、流光条、骨架）
// 每重建一次就跳回第一帧，事件密的时候看着一抽一抽。下面两样分别管这两类。

const entrances = new Map<string, { identity: string; started: number }>();

/**
 * 进场动画只在「换了一样东西」时播。slot 是页面上的一个位置（如任务详情的统计格），identity 是
 * 此刻放在那儿的东西（如任务 id，进页时把挂载序号拼进去就能每次进页重播一次）。
 * 返回动画已经播了多少毫秒：0 表示刚开始；重画时还没播完，新节点用它接着播（负的 animation-delay）；
 * 已经播完返回 null，这次重画不该再播。
 */
export function entranceElapsed(slot: string, identity: string, totalMs: number): number | null {
  const now = performance.now();
  const seen = entrances.get(slot);
  if (!seen || seen.identity !== identity) {
    entrances.set(slot, { identity, started: now });
    return 0;
  }
  const elapsed = now - seen.started;
  return elapsed < totalMs ? elapsed : null;
}

/**
 * 让所有循环 CSS 动画都对齐到同一条时间线（startTime = 0）：重建出来的新节点接着旧节点的相位播，
 * 看不出被换过。各自的 animation-delay（错落）不受影响。MutationObserver 在本轮任务结束、
 * 下一帧绘制之前回调，新节点第一帧就已经对齐。
 * 不支持 getAnimations 的运行时（Safari 13.1 之前）什么都不做，动画照旧从头播。
 */
export function installLoopingAnimationSync(): void {
  if (typeof document.getAnimations !== "function" || typeof MutationObserver !== "function") return;
  new MutationObserver(() => {
    for (const animation of document.getAnimations()) {
      // 只管 CSS 的循环动画；Web Animations 播的过渡（换页、滑块）有自己的起点。
      // 暂停着的（animation-play-state: paused，如换页快照）不碰：设了起点会让它重新跑起来。
      if (!("animationName" in animation) || animation.startTime === 0 || animation.playState === "paused") continue;
      const iterations = animation.effect?.getTiming().iterations;
      if (iterations === Infinity) animation.startTime = 0;
    }
  }).observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });
}
