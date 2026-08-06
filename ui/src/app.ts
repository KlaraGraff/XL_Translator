// V9 应用入口 —— 注入图标 sprite、挂载外壳、注册七个视图、初始化主题、默认进入 Excel。
// 旧的 ui/src/main.ts 暂时保留在原位（未接入 index.html），供后续代理迁移逻辑参考；
// 迁移完成后由主会话统一删除。

import "./styles/tokens.css";
import "./styles/app.css";

import { injectIconSprite } from "./icons";
import { mountShell } from "./shell";
import { mountRouter, navigate, registerView } from "./router";

import * as excelView from "./views/excel";
import * as wordView from "./views/word";
import * as pdfView from "./views/pdf";
import * as tasksView from "./views/tasks";
import * as libraryView from "./views/library";
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
  return stored === "light" || stored === "dark" || stored === "system" ? stored : "system";
}

function applyTheme(preference: ThemePreference): void {
  document.documentElement.dataset.theme = resolveTheme(preference);
}

/**
 * 主题初始化：读取本地缓存的偏好（跟随系统 / 浅色 / 深色），并在跟随系统时监听
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

function main(): void {
  initTheme();
  injectIconSprite();

  const root = document.querySelector<HTMLDivElement>("#app");
  if (!root) {
    throw new Error("#app root element not found.");
  }

  const shell = mountShell(root);
  mountRouter(shell.contentContainer);

  // 整模块注册：视图文件导出 mount（必需）与 unmount（可选，清理 SSE/定时器），
  // 这里原样接线，视图新增 unmount 时无需回来改注册代码。
  registerView("excel", excelView);
  registerView("word", wordView);
  registerView("pdf", pdfView);
  registerView("tasks", tasksView);
  registerView("library", libraryView);
  registerView("settings", settingsView);
  registerView("help", helpView);

  navigate("excel");
}

main();
