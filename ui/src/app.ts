// V9 应用入口 —— 注入图标 sprite、挂载外壳、注册顶层视图、初始化主题、默认进入 Excel。

// 必须最先导入：纯浏览器 dev 走查时垫掉 Tauri IPC（生产构建整体剔除）。
import "./dev-tauri-shim";

import "./styles/tokens.css";
import "./styles/app.css";

import { injectIconSprite } from "./icons";
import { mountShell } from "./shell";
import { mountRouter, navigate, registerView } from "./router";
import { checkFirstLaunch } from "./quickstart";
import { refreshModelPill } from "./model-pill";
import { runBackgroundUpdateCheck } from "./update-controller";
import { mountUpdateToast } from "./update-toast";
import { ApiClient } from "./api-client";
import { currentLocale, isLocale, localeTag, storeLocale } from "./i18n";

import * as excelView from "./views/excel";
import * as wordView from "./views/word";
import * as pdfView from "./views/pdf";
import * as cadView from "./views/cad";
import * as tasksView from "./views/tasks";
import * as libraryView from "./views/library";
import * as pluginsView from "./views/plugins";
import * as settingsView from "./views/settings";
import * as helpView from "./views/help";

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
 * 主题初始化：读取本地缓存的偏好（浅色 / 深色 / 跟随系统，没有缓存时按默认的浅色），并在跟随系统时监听
 * 系统主题变化实时刷新。设置页的「外观与语言」子页后续应在写入后端设置的同时
 * 同步写回 localStorage(THEME_STORAGE_KEY)，保证下次启动前的首屏渲染不用等后端。
 */
function initTheme(): void {
  applyTheme(readStoredThemePreference());
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (readStoredThemePreference() === "system") {
      applyTheme("system");
    }
  });
}

const LOCALE_RELOAD_GUARD_KEY = "translator.locale-reloaded";

/**
 * 启动时按后端设置对齐主题和界面语言。localStorage 只是首屏缓存，后端
 * settings.appearance 才是权威值（重置设置、换了 WebView 数据目录都会让两者不一致）。
 * 语言不一致时只能重载页面：模块级文案在加载时已经按旧语言算好了。
 * 用 sessionStorage 防止「写不进 localStorage → 每次都不一致 → 无限重载」。
 */
async function syncAppearanceFromBackend(): Promise<void> {
  const client = new ApiClient();
  await client.connect();
  const settings = await client.request<{ appearance?: { theme?: unknown; ui_language?: unknown } }>("/api/settings");
  const appearance = settings.appearance ?? {};
  const theme = appearance.theme;
  if (theme === "light" || theme === "dark" || theme === "system") {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    applyTheme(theme);
  }
  const language = appearance.ui_language;
  if (isLocale(language) && language !== currentLocale()) {
    storeLocale(language);
    let reloaded = false;
    try {
      reloaded = window.sessionStorage.getItem(LOCALE_RELOAD_GUARD_KEY) === language;
      window.sessionStorage.setItem(LOCALE_RELOAD_GUARD_KEY, language);
    } catch {
      reloaded = true;
    }
    if (!reloaded) window.location.reload();
  }
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
  registerView("settings", settingsView);
  registerView("help", helpView);

  navigate("excel");

  void syncAppearanceFromBackend().catch(() => undefined);

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
