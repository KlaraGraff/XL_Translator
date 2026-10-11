// 插件管理视图 —— 当前只管理“图纸转换工具”这一种插件的检测与连接。
// CAD 翻译能力已内置在 Translator 中；这里不再提供虚假的翻译组件安装/卸载。

import { open } from "@tauri-apps/plugin-dialog";
import { invoke } from "@tauri-apps/api/core";
import type { ViewParams } from "../router";
import { setTopbar } from "../shell";
import { ApiClient, type CadCapabilityStatus } from "../api-client";
import { createButton, createCard, createHintBadge, createStatus, showToast } from "../components";
import { icon } from "../icons";
import { currentLocale, t } from "../i18n";
import "./plugins.css";

let mounted = false;
let rootEl: HTMLElement | null = null;
let capability: CadCapabilityStatus | null = null;
let errorText = "";
let busy = false;
let detectTimer: number | null = null;
let detectDeadline = 0;
const DEFAULT_OFFICIAL_CAD_URL = currentLocale() === "zh"
  ? "https://www.opendesign.com/cn/guestfiles/oda_file_converter"
  : "https://www.opendesign.com/guestfiles/oda_file_converter";

function localizedOfficialCadUrl(value?: string): string {
  if (!value) return DEFAULT_OFFICIAL_CAD_URL;
  try {
    const url = new URL(value);
    // 中文界面才换成官网中文页；英法界面用默认的英文页。
    if (currentLocale() === "zh" && url.hostname === "www.opendesign.com" && url.pathname === "/guestfiles/oda_file_converter") {
      url.pathname = "/cn/guestfiles/oda_file_converter";
    }
    return url.toString().replace(/\/$/, "");
  } catch {
    return DEFAULT_OFFICIAL_CAD_URL;
  }
}

function statusLabel(): string {
  if (!capability) return t("正在检测");
  if (!capability.platform_supported) return t("当前平台不支持");
  if (capability.oda === "connected") return t("已连接");
  return capability.oda === "incompatible" ? t("转换工具不可用") : t("需要连接图纸转换工具");
}

function statusTone(): "idle" | "ok" | "warn" | "danger" {
  if (!capability) return "idle";
  if (!capability.platform_supported || capability.oda === "incompatible") return "danger";
  if (capability.oda === "connected") return "ok";
  return "warn";
}

export function mount(container: HTMLElement, _params: ViewParams): void {
  mounted = true;
  rootEl = container;
  render();
  void refresh();
}

export function unmount(): void {
  mounted = false;
  rootEl = null;
  stopAutoDetect();
}

function stopAutoDetect(): void {
  if (detectTimer !== null) {
    window.clearTimeout(detectTimer);
    detectTimer = null;
  }
}

async function openExternalUrl(url: string): Promise<void> {
  if (!url) return;
  await invoke("open_external_url", { url });
}

async function openOfficialDownloadAndDetect(allowWhenBusy = false): Promise<void> {
  if (busy && !allowWhenBusy) return;
  busy = true;
  errorText = "";
  stopAutoDetect();
  render();
  try {
    const client = new ApiClient();
    await client.connect();
    const download = await client.officialCadDownload();
    const url = localizedOfficialCadUrl(download.url || download.authorization_url);
    await openExternalUrl(url);
    showToast({ message: t("已打开图纸转换工具官网。安装完成后可刷新检测。") });
    detectDeadline = Date.now() + 3 * 60_000;
    scheduleAutoDetect();
  } catch (error) {
    errorText = error instanceof Error ? error.message : t("无法打开官方下载页。");
    showToast({ message: errorText, error: true });
  } finally {
    busy = false;
    if (mounted) render();
  }
}

function scheduleAutoDetect(): void {
  if (!mounted || Date.now() >= detectDeadline) return;
  detectTimer = window.setTimeout(() => void pollForOda(), 2_500);
}

async function pollForOda(): Promise<void> {
  detectTimer = null;
  if (!mounted || Date.now() >= detectDeadline) return;
  try {
    const client = new ApiClient();
    await client.connect();
    capability = await client.refreshCadCapability();
    if (capability.oda === "connected") {
      stopAutoDetect();
      showToast({ message: t("已检测到图纸转换工具，并自动连接。") });
      render();
      return;
    }
    render();
  } catch {
    // 下载期间网络或 sidecar 短暂不可用时继续轮询，不打断用户的安装动作。
  }
  scheduleAutoDetect();
}

async function refresh(): Promise<void> {
  try {
    const client = new ApiClient();
    await client.connect();
    capability = await client.getCadCapability();
    errorText = "";
  } catch (error) {
    errorText = error instanceof Error ? error.message : t("插件状态读取失败。");
  }
  if (mounted) render();
}

async function chooseOda(): Promise<void> {
  try {
    // 选择整个安装位置：macOS 可选 .app 包，Windows 可选安装目录。
    const picked = await open({ multiple: false, directory: true });
    const path = Array.isArray(picked) ? picked[0] : picked;
    if (typeof path !== "string" || !path) return;
    const client = new ApiClient();
    await client.connect();
    capability = await client.connectCadOda(path);
    errorText = "";
    showToast({ message: capability.oda === "connected" ? t("图纸转换工具已连接。") : t("选择的位置不是可用的转换工具。"), error: capability.oda !== "connected" });
    if (mounted) render();
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : t("无法连接图纸转换工具。"), error: true });
  }
}

function actionButtons(): HTMLElement {
  const actions = document.createElement("div");
  actions.className = "field-row plugins-actions";
  if (capability?.oda !== "connected") {
    actions.append(createButton({ label: busy ? t("打开中…") : t("去官网下载"), variant: "primary", disabled: busy, onClick: () => void openOfficialDownloadAndDetect() }));
  }
  // 保留手动兜底：即使自动检测已连接，用户仍可切换到另一套安装位置。
  actions.append(createButton({ label: t("选择安装目录"), onClick: () => void chooseOda() }));
  actions.append(createButton({ label: t("检测安装状态"), onClick: () => void refresh() }));
  return actions;
}

function render(): void {
  if (!mounted || !rootEl) return;
  setTopbar({ title: t("插件"), status: { label: statusLabel(), tone: statusTone() }, subtitle: t("连接翻译 DWG 图纸需要的转换工具") });
  rootEl.className = "content plugins-content";
  rootEl.replaceChildren();

  const column = document.createElement("div");
  column.className = "plugins-column";

  const card = createCard([]);
  const body = document.createElement("div");
  body.className = "plugins-card-body";
  const titleRow = document.createElement("div");
  titleRow.className = "plugins-title-row";
  const iconBox = document.createElement("div");
  iconBox.className = "plugins-icon";
  iconBox.append(icon("convert", { size: "lg" }));
  const copy = document.createElement("div");
  copy.className = "plugins-copy";
  const title = document.createElement("h2");
  title.textContent = t("图纸转换工具");
  const officialUrl = localizedOfficialCadUrl(capability?.official_download_url);
  title.append(createHintBadge(t("使用 ODA File Converter，把翻译后的 DXF 转回 DWG。官方下载：{0}", officialUrl)));
  const desc = document.createElement("p");
  desc.textContent = t("把 DWG 转为可翻译的 DXF，完成翻译后再转回 DWG。");
  copy.append(title, desc);
  titleRow.append(iconBox, copy);
  if (capability) titleRow.append(createStatus({ label: statusLabel(), tone: statusTone() }));
  titleRow.append(actionButtons());
  body.append(titleRow);

  if (errorText) {
    const error = document.createElement("div");
    error.className = "plugins-notice danger";
    error.textContent = errorText;
    body.append(error);
  } else if (capability?.oda === "connected") {
    const ok = document.createElement("div");
    ok.className = "plugins-notice ok";
    ok.textContent = t("ODA File Converter 已连接，可开始选择 DWG / DXF 来源。");
    body.append(ok);
  } else if (capability) {
    const warn = document.createElement("div");
    warn.className = "plugins-notice warn";
    warn.textContent = t("尚未检测到图纸转换工具。可打开官网、选择安装目录或检测安装状态。");
    body.append(warn);
  }
  card.append(body);
  column.append(card);

  rootEl.append(column);
}
