// 投放区的光（界面焕新阶段 2）：作用在「翻译」入口页的 .hero 和类型页空状态的 .drop。
// 参考 Fluent Reveal / Vercel spotlight / Apple Intelligence 边光，没有实心光斑：
// 指针附近的一段边框亮起，底面一层极淡的大光晕加被同一位置揭开的点阵，图标随指针轻微倾斜；
// 拖着文件经过时边框换成绕圈流动的多色边光，松手即停。样式在 styles/app.css「投放区的光」。
//
// 每帧只写元素上的 CSS 变量（--gx/--gy 位置、--gs 范围、--ang 边光角度、--tx/--ty 倾斜），
// 不碰布局。减少动效时不跑 rAF 循环，直接写目标值，拖入时边光不转。

import { motionAllowed } from "./motion";

const ZONE = ".hero, .drop";

interface GlowState {
  el: HTMLElement | null;
  raf: number;
  on: boolean;
  spin: boolean;
  mx: number;
  my: number;
  gx: number;
  gy: number;
  gs: number;
  ts: number;
  ang: number;
  tang: number;
  tx: number;
  ty: number;
  ptx: number;
  pty: number;
  last: number;
  t0: number;
  w: number;
  h: number;
}

const G: GlowState = {
  el: null, raf: 0, on: false, spin: false, mx: 0, my: 0, gx: 0, gy: 0, gs: 0.7, ts: 1,
  ang: 0, tang: 0, tx: 0, ty: 0, ptx: 0, pty: 0, last: 0, t0: 0, w: 1, h: 1,
};
let dragZone: HTMLElement | null = null;
let installed = false;

const lerp = (a: number, b: number, k: number) => a + (b - a) * k;

function zoneAt(target: EventTarget | Element | null): HTMLElement | null {
  return target instanceof Element ? target.closest<HTMLElement>(ZONE) : null;
}

function setTargets(clientX: number, clientY: number): void {
  if (!G.el) return;
  const r = G.el.getBoundingClientRect();
  G.w = r.width;
  G.h = r.height;
  G.mx = clientX - r.left;
  G.my = clientY - r.top;
  const dx = G.mx - r.width / 2;
  const dy = G.my - r.height / 2;
  // 边光的亮段落在指针那一侧（atan2 + 90），再往回偏一点，让亮段的中心正对指针。
  if (!G.spin) G.tang = ((Math.atan2(dy, dx) * 180) / Math.PI + 90 + 360) % 360 - 62;
  G.ptx = Math.max(-1, Math.min(1, dx / (r.width / 2)));
  G.pty = Math.max(-1, Math.min(1, dy / (r.height / 2)));
  G.last = performance.now();
}

function write(el: HTMLElement): void {
  const st = el.style;
  st.setProperty("--gx", `${G.gx.toFixed(1)}px`);
  st.setProperty("--gy", `${G.gy.toFixed(1)}px`);
  st.setProperty("--gs", G.gs.toFixed(3));
  st.setProperty("--ang", `${G.ang.toFixed(1)}deg`);
  st.setProperty("--tx", G.tx.toFixed(3));
  st.setProperty("--ty", G.ty.toFixed(3));
}

function schedule(): void {
  if (!motionAllowed()) {
    // 减少动效：不跑循环，直接到位；倾斜不做。
    if (!G.el) return;
    G.gx = G.mx;
    G.gy = G.my;
    G.gs = 1;
    G.ang = G.tang;
    G.tx = 0;
    G.ty = 0;
    write(G.el);
    if (!G.on && !G.spin) G.el = null;
    return;
  }
  if (!G.raf) G.raf = requestAnimationFrame(frame);
}

function frame(now: number): void {
  G.raf = 0;
  const z = G.el;
  if (!z || !z.isConnected) {
    G.el = null;
    return;
  }
  const idle = G.on && !G.spin && now - G.last > 900;
  // 指针停住一会儿后范围轻轻呼吸，提示这里还能放东西。
  const breathe = idle ? 1 + Math.sin((now - G.t0) / 700) * 0.04 : 1;
  G.gx = lerp(G.gx, G.mx, 0.16);
  G.gy = lerp(G.gy, G.my, 0.16);
  G.gs = lerp(G.gs, G.ts * breathe, G.on ? 0.12 : 0.08);
  if (G.spin) {
    G.ang = (G.ang + 2.4) % 360;
  } else {
    // 走最短方向；差得很远（> 150°）时慢一点，免得边光甩一大圈。
    const d = (((G.tang - G.ang) % 360) + 540) % 360 - 180;
    G.ang += d * (Math.abs(d) > 150 ? 0.06 : 0.12);
  }
  G.tx = lerp(G.tx, G.ptx, 0.12);
  G.ty = lerp(G.ty, G.pty, 0.12);
  write(z);
  const settled = !G.on && !G.spin
    && Math.abs(G.gs - G.ts) < 0.01 && Math.abs(G.tx) < 0.005 && Math.abs(G.ty) < 0.005
    && Math.abs(G.gx - G.mx) < 0.5 && Math.abs(G.gy - G.my) < 0.5;
  if (settled) {
    G.el = null;
    z.style.removeProperty("--tx");
    z.style.removeProperty("--ty");
    return;
  }
  G.raf = requestAnimationFrame(frame);
}

function enter(z: HTMLElement, clientX: number, clientY: number): void {
  if (G.el && G.el !== z) leave();
  G.el = z;
  G.on = true;
  G.t0 = performance.now();
  z.classList.add("lit");
  setTargets(clientX, clientY);
  G.gx = G.mx;
  G.gy = G.my;
  G.gs = 0.7;
  G.ts = 1;
  if (!G.spin) G.ang = G.tang;
  G.tx = 0;
  G.ty = 0;
  schedule();
}

function leave(): void {
  const z = G.el;
  if (!z) return;
  z.classList.remove("lit");
  G.on = false;
  // 收回到中央偏上、范围缩小，再淡出。
  G.mx = G.w / 2;
  G.my = G.h * 0.45;
  G.ts = 0.75;
  G.ptx = 0;
  G.pty = 0;
  schedule();
}

function dragStart(z: HTMLElement, clientX: number, clientY: number): void {
  if (dragZone && dragZone !== z) dragStop();
  dragZone = z;
  G.spin = motionAllowed();
  z.classList.add("dragging");
  if (G.el !== z || !G.on) enter(z, clientX, clientY);
  else {
    setTargets(clientX, clientY);
    schedule();
  }
}

function dragStop(): void {
  if (!dragZone) return;
  dragZone.classList.remove("dragging");
  dragZone = null;
  G.spin = false;
  if (G.on) leave();
}

/** 拖着文件在窗口里移动（Tauri 的 enter/over，坐标是 CSS 像素）；离开所有投放区时收起。 */
export function dragOverAt(clientX: number, clientY: number): void {
  const z = zoneAt(document.elementFromPoint(clientX, clientY));
  if (!z) {
    dragStop();
    return;
  }
  if (dragZone !== z) dragStart(z, clientX, clientY);
  else {
    setTargets(clientX, clientY);
    schedule();
  }
}

/** 拖动结束（松手、离开窗口或取消）：投放区立即归位，不慢慢淡出（.snap 只挂两帧）。 */
export function dragEnded(): void {
  const zone = dragZone;
  if (zone) {
    zone.classList.add("snap");
    requestAnimationFrame(() => requestAnimationFrame(() => zone.classList.remove("snap")));
  }
  dragStop();
}

/** 换页时调用：停掉循环，去掉高亮，别把上一页的状态带过去。 */
export function resetDropGlow(): void {
  if (G.raf) cancelAnimationFrame(G.raf);
  G.raf = 0;
  dragZone?.classList.remove("dragging");
  G.el?.classList.remove("lit");
  dragZone = null;
  G.el = null;
  G.on = false;
  G.spin = false;
}

/** 装一次全局监听（指针移动委托到 document）。入口页和工作区的投放区共用。 */
export function installDropGlow(): void {
  if (installed) return;
  installed = true;
  document.addEventListener("pointermove", (e) => {
    if (G.spin || dragZone) return;
    const z = zoneAt(e.target);
    if (z) {
      if (G.el !== z || !G.on) enter(z, e.clientX, e.clientY);
      else {
        setTargets(e.clientX, e.clientY);
        schedule();
      }
    } else if (G.on) leave();
  }, { passive: true });
  document.documentElement.addEventListener("mouseleave", () => {
    if (G.on && !dragZone) leave();
  });
  // 浏览器开发态的拖放（Tauri 窗口里文件拖放走 onDragDropEvent，这些 DOM 事件不会带文件来）。
  document.addEventListener("dragenter", (e) => {
    const z = zoneAt(e.target);
    if (z) dragStart(z, e.clientX, e.clientY);
  });
  document.addEventListener("dragover", (e) => {
    const z = zoneAt(e.target);
    if (z) {
      if (dragZone !== z) dragStart(z, e.clientX, e.clientY);
      else {
        setTargets(e.clientX, e.clientY);
        schedule();
      }
    } else if (dragZone) dragStop();
  });
  document.addEventListener("dragleave", (e) => {
    if (!e.relatedTarget) dragStop();
  });
  document.addEventListener("drop", () => dragStop());
  window.addEventListener("dragend", () => dragStop());
}
