// 插件管理视图 —— CAD Support 与 ODA 的安装、检测和连接统一在这里完成。
// CAD 工作台只负责翻译任务；能力状态和外部依赖不再塞进「设置 → 更新与关于」。

import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import type { ViewParams } from "../router";
import { setTopbar } from "../shell";
import { ApiClient, type CadCapabilityStatus } from "../api-client";
import { createButton, createCard, createChip, createStatus, openModal, showToast } from "../components";
import "./plugins.css";

let mounted = false;
let rootEl: HTMLElement | null = null;
let capability: CadCapabilityStatus | null = null;
let errorText = "";
let busy = false;

function statusLabel(): string {
  if (!capability) return "正在检测";
  if (!capability.platform_supported) return "当前平台不支持";
  if (capability.plugin !== "enabled") return "CAD 插件未安装";
  if (capability.oda === "connected") return "CAD 能力已就绪";
  return capability.oda === "incompatible" ? "ODA 不可用" : "需要连接 ODA";
}

function statusTone(): "idle" | "ok" | "warn" | "danger" {
  if (!capability) return "idle";
  if (!capability.platform_supported || capability.plugin === "error" || capability.oda === "incompatible") return "danger";
  if (capability.plugin === "enabled" && capability.oda === "connected") return "ok";
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

async function installAndDetect(): Promise<void> {
  if (busy) return;
  busy = true;
  render();
  try {
    const client = new ApiClient();
    await client.connect();
    capability = await client.installCadPlugin();
    errorText = "";
    showToast({ message: capability.oda === "connected" ? "CAD Support 已安装，ODA 已自动连接。" : "CAD Support 已安装，请连接 ODA。" });
  } catch (error) {
    errorText = error instanceof Error ? error.message : "CAD Support 安装失败。";
    showToast({ message: errorText, error: true });
  } finally {
    busy = false;
    if (mounted) render();
  }
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
    showToast({ message: capability.oda === "connected" ? "ODA 已连接。" : "选择的位置不是可用的 ODA 安装。", error: capability.oda !== "connected" });
    if (mounted) render();
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : "无法连接 ODA。", error: true });
  }
}

async function openOfficialPage(): Promise<void> {
  try {
    const client = new ApiClient();
    await client.connect();
    const result = await client.officialCadDownload();
    const url = result.authorization_url || result.url || "";
    if (!url) throw new Error("官方 ODA 页面地址暂不可用。 ");
    await invoke("open_external_url", { url });
  } catch (error) {
    errorText = error instanceof Error ? error.message : "无法打开官方 ODA 页面。";
    showToast({ message: errorText, error: true });
    if (mounted) render();
  }
}

function actionButtons(): HTMLElement {
  const actions = document.createElement("div");
  actions.className = "field-row plugins-actions";
  if (capability?.plugin !== "enabled") {
    actions.append(createButton({ label: busy ? "安装中…" : "安装 / 检测 CAD 能力", variant: "primary", disabled: busy, onClick: () => void installAndDetect() }));
  } else if (capability.oda !== "connected") {
    actions.append(createButton({ label: "选择 ODA 安装位置", variant: "primary", onClick: () => void chooseOda() }));
  }
  actions.append(createButton({ label: "刷新状态", onClick: () => void refresh() }));
  actions.append(createButton({ label: "打开 ODA 官方页面", icon: "ext", onClick: () => void openOfficialPage() }));
  if (capability?.plugin === "enabled") {
    actions.append(createButton({ label: "卸载插件", variant: "danger-solid", onClick: () => confirmUninstall() }));
  }
  return actions;
}

function confirmUninstall(): void {
  openModal({
    tone: "warn",
    icon: "stop",
    title: "卸载 CAD Support？",
    body: ["CAD 翻译能力将被移除。已有任务结果、ODA 安装和其他文档翻译功能不受影响。"],
    actions: [
      { label: "取消" },
      { label: "卸载", variant: "danger-solid", onClick: () => void uninstall() },
    ],
  });
}

async function uninstall(): Promise<void> {
  try {
    const client = new ApiClient();
    await client.connect();
    capability = await client.uninstallCadPlugin();
    showToast({ message: "CAD Support 已卸载。" });
    if (mounted) render();
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : "插件卸载失败。", error: true });
  }
}

function render(): void {
  if (!mounted || !rootEl) return;
  setTopbar({ title: "插件管理", status: { label: statusLabel(), tone: statusTone() }, subtitle: "安装、检测和连接 Translator 的可选能力" });
  rootEl.className = "content plugins-content";
  rootEl.replaceChildren();

  const column = document.createElement("div");
  column.className = "plugins-column";

  const intro = createCard([]);
  const introHead = document.createElement("div");
  introHead.className = "tc-head";
  const introTitle = document.createElement("b");
  introTitle.textContent = "可选插件与能力";
  introHead.append(introTitle);
  intro.append(introHead);
  const introBody = document.createElement("div");
  introBody.className = "plugins-intro";
  introBody.append(document.createTextNode("插件只增加 CAD 图纸翻译能力，不会改变 Excel、Word 和 PDF 的现有配置。"));
  intro.append(introBody);
  column.append(intro);

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
  title.textContent = "CAD Support";
  const desc = document.createElement("p");
  desc.textContent = "翻译 DWG / DXF 中的可见文字，原件不覆盖，结果和报告写入输出目录。";
  copy.append(title, desc);
  titleRow.append(icon, copy);
  if (capability) titleRow.append(createStatus({ label: statusLabel(), tone: statusTone() }));
  body.append(titleRow);

  const details = document.createElement("div");
  details.className = "plugins-details";
  const rows: Array<[string, string]> = [
    ["CAD Support", capability?.plugin === "enabled" ? `已启用 · ${capability.version || "当前版本"}` : capability?.plugin === "error" ? "安装状态异常" : "未安装"],
    ["ODA File Converter", capability?.oda === "connected" ? "已连接" : capability?.oda === "incompatible" ? "校验失败" : "未找到"],
    ["本机平台", capability?.platform ? `${capability.platform.system} · ${capability.platform.arch}` : "检测中"],
  ];
  rows.forEach(([label, value]) => {
    const row = document.createElement("div");
    row.className = "plugins-detail-row";
    const labelEl = document.createElement("span");
    labelEl.textContent = label;
    const valueEl = document.createElement("strong");
    valueEl.textContent = value;
    row.append(labelEl, valueEl);
    details.append(row);
  });
  body.append(details);

  if (errorText) {
    const error = document.createElement("div");
    error.className = "plugins-notice danger";
    error.textContent = errorText;
    body.append(error);
  } else if (capability?.plugin === "enabled" && capability.oda === "connected") {
    const ok = document.createElement("div");
    ok.className = "plugins-notice ok";
    ok.textContent = `已连接：${capability.converter || "ODA File Converter"}`;
    body.append(ok);
  } else if (capability?.plugin === "enabled") {
    const warn = document.createElement("div");
    warn.className = "plugins-notice warn";
    warn.textContent = "CAD Support 已安装。点击“选择 ODA 安装位置”可一次选择整个 .app 或安装目录；也可以先打开官方页面下载。";
    body.append(warn);
  }
  body.append(actionButtons());
  card.append(body);
  column.append(card);

  const note = createCard([]);
  const noteBody = document.createElement("div");
  noteBody.className = "plugins-note";
  noteBody.append(createChip({ label: "旧配置已保留", tone: "ok" }));
  const noteText = document.createElement("span");
  noteText.textContent = "CAD 使用“文档翻译（Excel / Word / CAD）”模型角色，当前模型、连接、语言和记忆库设置不会要求重新填写。";
  noteBody.append(noteText);
  note.append(noteBody);
  column.append(note);

  rootEl.append(column);
}

