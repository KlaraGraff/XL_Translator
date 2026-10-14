// V9 应用入口 —— 注入图标 sprite、挂载外壳、注册顶层视图、初始化主题、默认进入 Excel。

// 必须最先导入：纯浏览器 dev 走查时垫掉 Tauri IPC（生产构建整体剔除）。
import "./dev-tauri-shim";

import "./styles/tokens.css";
import "./styles/palettes.css";
import "./styles/app.css";

import { injectIconSprite } from "./icons";
import { mountShell, relocalizeShell, setExtensionSideItems } from "./shell";
import { mountRouter, navigate, registerView, remountCurrentView } from "./router";
import { checkFirstLaunch } from "./quickstart";
import { refreshModelPill } from "./model-pill";
import { runBackgroundUpdateCheck } from "./update-controller";
import { mountUpdateToast } from "./update-toast";
import { ApiClient } from "./api-client";
import { extSurfaceIcon, extSurfaceLabel, isExtSurface, loadSurfaceDescriptors, pinnedExtDescriptors } from "./surface-descriptors";
import { currentLocale, isLocale, localeTag, onLocaleChange, setLocale, storeLocale } from "./i18n";

import * as excelView from "./views/excel";
import { applyPalettes, initPalettes, palettesFromAppearance, storePalettes } from "./palette";
import * as wordView from "./views/word";
import * as pdfView from "./views/pdf";
import * as cadView from "./views/cad";
import * as tasksView from "./views/tasks";
import * as libraryView from "./views/library";
import * as pluginsView from "./views/plugins";
import * as mcpView from "./views/mcp";
import * as settingsView from "./views/settings";
import * as helpView from "./views/help";
import * as feedbackView from "./views/feedback";
import { extensionView } from "./views/extension";

const THEME_STORAGE_KEY = "translator.theme";
type ThemePreference = "light" | "dark" | "system";

function resolveTheme(preference: ThemePreference): "light" | "dark" {
  if (preference === "system") {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  return preference;
}

function readStoredThemePreference(): ThemePreference {
  const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  return stored === "light" || stored === "dark" || stored === "system" ? stored : "light";
}

function applyTheme(preference: ThemePreference): void {
  document.documentElement.dataset.theme = resolveTheme(preference);
}

/**
 * 主题初始化：读取本地缓存的偏好（浅色 / 深色 / 跟随系统，没有缓存时按默认的浅色）和浅色、深色
 * 各自的配色方案（没有缓存时按默认的素雅灰），并在跟随系统时监听
 * 系统主题变化实时刷新。设置页的「外观与语言」子页后续应在写入后端设置的同时
 * 同步写回 localStorage(THEME_STORAGE_KEY)，保证下次启动前的首屏渲染不用等后端。
 */
function initTheme(): void {
  applyTheme(readStoredThemePreference());
  initPalettes();
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (readStoredThemePreference() === "system") {
      applyTheme("system");
    }
  });
}

/**
 * 启动时按后端设置对齐主题和界面语言。localStorage 只是首屏缓存，后端
 * settings.appearance 才是权威值（重置设置、换了 WebView 数据目录都会让两者不一致）。
 * 语言不一致时原地换语言（setLocale），和在设置页切换走同一条路，不重新加载。
 */
async function syncAppearanceFromBackend(): Promise<void> {
  const client = new ApiClient();
  await client.connect();
  const settings = await client.request<{ appearance?: { theme?: unknown; palette_light?: unknown; palette_dark?: unknown; ui_language?: unknown } }>("/api/settings");
  const appearance = settings.appearance ?? {};
  const theme = appearance.theme;
  if (theme === "light" || theme === "dark" || theme === "system") {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    applyTheme(theme);
  }
  const palettes = palettesFromAppearance(appearance);
  storePalettes(palettes);
  applyPalettes(palettes);
  const language = appearance.ui_language;
  if (isLocale(language) && language !== currentLocale()) {
    storeLocale(language);
    setLocale(language);
  }
}

/**
 * 运行时注册的翻译类型（以后的格式扩展，键为 `ext:<id>`）：描述符到了之后给每个类型注册
 * 一个工作区视图（任务中心「打开工作区」也靠它），设置里没取消固定的放进侧栏「翻译」组。
 * 没有这类类型时什么都不做，也不多发请求。
 */
async function mountExtensionPages(): Promise<void> {
  const extensions = (await loadSurfaceDescriptors()).filter((item) => !item.builtin && isExtSurface(item.key));
  if (!extensions.length) return;
  for (const item of extensions) {
    if (isExtSurface(item.key)) registerView(item.key, extensionView(item.key));
  }
  const client = new ApiClient();
  await client.connect();
  const settings = await client.request<{ extensions?: Record<string, unknown> }>("/api/settings");
  setExtensionSideItems(pinnedExtDescriptors(settings.extensions ?? {}).flatMap((item) => (
    isExtSurface(item.key)
      ? [{ id: item.key, label: () => extSurfaceLabel(item.key), icon: extSurfaceIcon(item.key) }]
      : []
  )));
}

/**
 * 换界面语言后原地重画：侧栏改文字，当前页按新语言重新挂载并停在原处，
 * 滚动位置也还原。不重新加载页面，所以没有白屏闪烁，也不会跳回 Excel 页。
 */
function relocalizeInPlace(content: HTMLElement): void {
  const scrolled = scrollPositions(content);
  relocalizeShell();
  remountCurrentView();
  void refreshModelPill();
  restoreScrollPositions(content, scrolled);
}

// 滚动的往往是页面里面的一块（例如设置页的 .set-body），按「类名 + 第几个」记下来。
function scrollKey(el: Element, content: HTMLElement): string {
  const same = [...content.querySelectorAll(el.tagName)].filter((other) => other.className === el.className);
  return `${el.tagName}.${el.className}#${same.indexOf(el)}`;
}

function scrollPositions(content: HTMLElement): Map<string, number> {
  const positions = new Map<string, number>();
  if (content.scrollTop > 0) positions.set("", content.scrollTop);
  for (const el of content.querySelectorAll("*")) {
    if (el.scrollTop > 0) positions.set(scrollKey(el, content), el.scrollTop);
  }
  return positions;
}

// 有的页面挂载后才异步画出内容（设置页要先读设置），内容够高之前设不上，
// 每帧试一次，最多一秒。
function restoreScrollPositions(content: HTMLElement, positions: Map<string, number>): void {
  if (positions.size === 0) return;
  const pending = new Map(positions);
  const deadline = performance.now() + 1000;
  const attempt = (): void => {
    for (const [key, top] of pending) {
      const el = key === ""
        ? content
        : [...content.querySelectorAll("*")].find((candidate) => scrollKey(candidate, content) === key);
      if (!el) continue;
      el.scrollTop = top;
      if (Math.abs(el.scrollTop - top) < 2) pending.delete(key);
    }
    if (pending.size > 0 && performance.now() < deadline) requestAnimationFrame(attempt);
  };
  requestAnimationFrame(attempt);
}

function main(): void {
  document.documentElement.lang = localeTag();
  initTheme();
  injectIconSprite();

  const root = document.querySelector<HTMLDivElement>("#app");
  if (!root) {
    throw new Error("#app root element not found.");
  }

  const shell = mountShell(root);
  mountRouter(shell.contentContainer);
  onLocaleChange(() => {
    relocalizeInPlace(shell.contentContainer);
  });
  // 顶部的更新提示卡片。挂载时什么都不画，等到有话说（发现新版、或用户手动检查）才出现。
  mountUpdateToast();

  // 整模块注册：视图文件导出 mount（必需）与 unmount（可选，清理 SSE/定时器），
  // 这里原样接线，视图新增 unmount 时无需回来改注册代码。
  registerView("excel", excelView);
  registerView("word", wordView);
  registerView("pdf", pdfView);
  registerView("cad", cadView);
  registerView("tasks", tasksView);
  registerView("library", libraryView);
  registerView("plugins", pluginsView);
  registerView("mcp", mcpView);
  registerView("settings", settingsView);
  registerView("help", helpView);
  registerView("feedback", feedbackView);

  navigate("excel");

  void syncAppearanceFromBackend().catch(() => undefined);

  // 板块类型描述符：拉一次放进内存，运行时注册的页面（以后的格式扩展）靠它取名称和开关，
  // 并据此注册视图、放进侧栏。内置四个页面不依赖它，拉取失败也不影响启动。
  void mountExtensionPages().catch(() => undefined);

  // 顶栏「当前模型」药丸：先按已保存的配置点亮一次，之后由设置页每次保存/测试后刷新。
  void refreshModelPill();

  // 首次启动检查（main.ts:3989 等价逻辑）：不阻塞首屏渲染，读取到
  // quick_start_completed === false 时自动弹出快速开始向导。
  void checkFirstLaunch().catch(() => undefined);

  // 启动后的后台更新检查，每次启动查一次（没有时间节流，理由见 update-controller）。
  // 延后 8 秒是为了让首屏、sidecar 握手和快速开始向导先各就各位——这件事没有任何紧迫性。
  // 查到新版就在顶部弹一张提示卡片并点亮侧栏红点；没查到就一声不吭。
  // 「这次该不该提示」由后端判断（暂停提醒 / 忽略的版本 / 快速开始还没走完）。
  window.setTimeout(() => {
    void runBackgroundUpdateCheck();
  }, 8000);
}

main();
