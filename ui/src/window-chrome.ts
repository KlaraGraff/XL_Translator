// 窗口顶部：不用系统那条单独的标题栏，让灰色画布一直铺到窗口顶上。
//
// - Mac：tauri.conf.json 里 titleBarStyle 是 Overlay、标题隐藏，红绿灯浮在侧栏左上角
//   （trafficLightPosition 定位），侧栏顶部留出一行给它（app.css 的 [data-chrome="mac"]）。
// - Windows：tauri.windows.conf.json 里 decorations 为 false，没有系统标题栏；这里在右上角
//   自己画「最小化 / 最大化 / 关闭」，顶部一条和画布同色（[data-chrome="win"]）。拖窗口边缘
//   调大小、窗口阴影、Alt+F4 由系统照常处理。
// - 浏览器开发态（不在 Tauri 里）什么都不加，界面和以前一样。
//
// 拖动窗口：Tauri 的 data-tauri-drag-region。不带值只认直接点在这个元素自己身上（点到里面的
// 按钮、文字不算），"deep" 认整棵子树。双击拖动区域按系统习惯放大 / 还原。

import { isTauri } from "@tauri-apps/api/core";
import { onLocaleChange, t } from "./i18n";
import { isMac, isWindows } from "./platform";

export type WindowChrome = "mac" | "win" | "none";

/** 这台机器上用哪种窗口顶部。只有真在 Tauri 窗口里才去掉系统标题栏。 */
export function detectWindowChrome(): WindowChrome {
  if (!isTauri()) return "none";
  if (isMac()) return "mac";
  if (isWindows()) return "win";
  return "none";
}

/** 在 <html> 上写 data-chrome，样式据此给红绿灯 / 窗口按钮留位置。要在挂外壳之前调用。 */
export function applyWindowChromeAttr(chrome: WindowChrome = detectWindowChrome()): WindowChrome {
  if (chrome === "none") delete document.documentElement.dataset.chrome;
  else document.documentElement.dataset.chrome = chrome;
  return chrome;
}

/**
 * 外壳挂好以后调用：标出能拖动窗口的空白处；Windows 再加上右上角三个按钮。
 * shell 是 .app-shell，side 是侧栏，brand 是侧栏顶上的 Logo 行，topbar 是右侧舞台的标题行。
 */
export function installWindowChrome(
  parts: { shell: HTMLElement; side: HTMLElement; brand: HTMLElement; topbar: HTMLElement },
  chrome: WindowChrome = detectWindowChrome(),
): void {
  if (chrome === "none") return;
  // 画布四周的空隙、侧栏和标题行的空白处都能拖；Logo 行整行都能拖（里面没有可点的东西）。
  parts.shell.setAttribute("data-tauri-drag-region", "");
  parts.side.setAttribute("data-tauri-drag-region", "");
  parts.topbar.setAttribute("data-tauri-drag-region", "");
  parts.brand.setAttribute("data-tauri-drag-region", "deep");
  if (chrome === "win") parts.shell.append(buildCaptionButtons());
}

// Windows 自带的图标字体（Win11 是 Segoe Fluent Icons，Win10 是 Segoe MDL2 Assets），
// 和系统标题栏上的按钮是同一套字形。
const GLYPH = {
  minimize: "",
  maximize: "",
  restore: "",
  close: "",
};

function buildCaptionButtons(): HTMLElement {
  const box = document.createElement("div");
  box.className = "win-caption";

  const make = (kind: "min" | "max" | "close"): HTMLButtonElement => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `win-caption-btn ${kind}`;
    // 不进 Tab 顺序：系统标题栏的按钮也不在网页的 Tab 顺序里，键盘用 Alt+空格 / Alt+F4。
    button.tabIndex = -1;
    return button;
  };
  const minBtn = make("min");
  const maxBtn = make("max");
  const closeBtn = make("close");
  box.append(minBtn, maxBtn, closeBtn);

  let maximized = false;
  const paint = (): void => {
    minBtn.textContent = GLYPH.minimize;
    maxBtn.textContent = maximized ? GLYPH.restore : GLYPH.maximize;
    closeBtn.textContent = GLYPH.close;
    const labels: [HTMLButtonElement, string][] = [
      [minBtn, t("最小化")],
      [maxBtn, maximized ? t("向下还原") : t("最大化")],
      [closeBtn, t("关闭")],
    ];
    for (const [button, label] of labels) {
      button.title = label;
      button.setAttribute("aria-label", label);
    }
  };
  paint();

  void (async () => {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    const win = getCurrentWindow();
    const sync = async (): Promise<void> => {
      try {
        maximized = await win.isMaximized();
      } catch {
        return;
      }
      paint();
    };
    minBtn.addEventListener("click", () => void win.minimize());
    // 放大 / 还原后窗口尺寸变化也会触发 onResized，这里再同步一次是保险。
    maxBtn.addEventListener("click", () => void win.toggleMaximize().then(sync));
    closeBtn.addEventListener("click", () => void win.close());
    await sync();
    await win.onResized(() => void sync());
  })();

  // 换界面语言时按钮的悬停提示跟着换。
  onLocaleChange(paint);
  return box;
}
