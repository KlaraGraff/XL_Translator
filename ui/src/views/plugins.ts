// 插件管理视图 —— 当前只管理“图纸转换工具”这一种插件的检测与连接。
// CAD 翻译能力已内置在 Translator 中；这里不再提供虚假的翻译组件安装/卸载。

import { open } from "@tauri-apps/plugin-dialog";
import { invoke } from "@tauri-apps/api/core";
import type { ViewParams } from "../router";
import { setTopbar } from "../shell";
import { ApiClient, type CadCapabilityStatus } from "../api-client";
import { createButton, createCard, createHintBadge, createStatus, showToast } from "../components";
import "./plugins.css";

let mounted = false;
let rootEl: HTMLElement | null = null;
let capability: CadCapabilityStatus | null = null;
let errorText = "";
let busy = false;
let detectTimer: number | null = null;
let detectDeadline = 0;

function statusLabel(): string {
  if (!capability) return "正在检测";
  if (!capability.platform_supported) return "当前平台不支持";
  if (capability.oda === "connected") return "已连接";
  return capability.oda === "incompatible" ? "转换工具不可用" : "需要连接图纸转换工具";
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
    const url = download.url || download.authorization_url || "";
    if (!url) throw new Error("官方下载地址暂不可用。");
    await openExternalUrl(url);
    showToast({ message: "已打开图纸转换工具官网。安装完成后可刷新检测。" });
    detectDeadline = Date.now() + 3 * 60_000;
    scheduleAutoDetect();
  } catch (error) {
    errorText = error instanceof Error ? error.message : "无法打开官方下载页。";
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
      showToast({ message: "已检测到图纸转换工具，并自动连接。" });
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
    errorText = error instanceof Error ? error.message : "插件状态读取失败。";
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
    showToast({ message: capability.oda === "connected" ? "图纸转换工具已连接。" : "选择的位置不是可用的转换工具。", error: capability.oda !== "connected" });
    if (mounted) render();
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : "无法连接图纸转换工具。", error: true });
  }
}

function actionButtons(): HTMLElement {
  const actions = document.createElement("div");
  actions.className = "field-row plugins-actions";
  if (capability?.oda !== "connected") {
    actions.append(createButton({ label: busy ? "打开中…" : "去官网下载", variant: "primary", disabled: busy, onClick: () => void openOfficialDownloadAndDetect() }));
    actions.append(createButton({ label: "选择已安装位置", onClick: () => void chooseOda() }));
  }
  actions.append(createButton({ label: "刷新状态", onClick: () => void refresh() }));
  return actions;
}

function render(): void {
  if (!mounted || !rootEl) return;
  setTopbar({ title: "插件管理", status: { label: statusLabel(), tone: statusTone() }, subtitle: "管理 Translator 的插件与连接状态" });
  rootEl.className = "content plugins-content";
  rootEl.replaceChildren();

  const column = document.createElement("div");
  column.className = "plugins-column";

  const card = createCard([]);
  const body = document.createElement("div");
  body.className = "plugins-card-body";
  const titleRow = document.createElement("div");
  titleRow.className = "plugins-title-row";
  const icon = document.createElement("div");
  icon.className = "plugins-icon";
  icon.textContent = "▥";
  const copy = document.createElement("div");
  copy.className = "plugins-copy";
  const title = document.createElement("h2");
  title.textContent = "图纸转换工具";
  title.append(createHintBadge("负责把翻译后的内容转换为 DWG 或其他 CAD 图纸格式。"));
  const desc = document.createElement("p");
  desc.textContent = "翻译 DWG / DXF 中的可见文字，原件不覆盖，结果和报告写入输出目录。";
  copy.append(title, desc);
  titleRow.append(icon, copy);
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
    ok.textContent = "转换工具已准备好。";
    body.append(ok);
  } else if (capability) {
    const warn = document.createElement("div");
    warn.className = "plugins-notice warn";
    warn.textContent = "尚未检测到图纸转换工具。可打开官网、选择已安装位置或刷新检测。";
    body.append(warn);
  }
  card.append(body);
  column.append(card);

  rootEl.append(column);
}
