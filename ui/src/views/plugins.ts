// 插件管理视图 —— 管理三种可选插件的检测与连接：
// 1. 图纸转换工具（ODA File Converter，CAD 翻译需要）；
// 2. 文字识别组件（Tesseract OCR，PDF「原位替换文字」需要）；
// 3. PDF 处理组件（PyMuPDF + pdf2docx，PDF「自动组合」的原位替换、页面判断和转 Word）：
//    可一键安装（从 PyPI 下载固定版本、核对后装进应用数据目录），也可选择自行安装的位置。
// 前两者只检测本机是否已装好这些第三方软件、以及让用户手动指定安装位置。

import { open } from "@tauri-apps/plugin-dialog";
import { invoke } from "@tauri-apps/api/core";
import type { ViewParams } from "../router";
import { setTopbar } from "../shell";
import { ApiClient, type CadCapabilityStatus, type OcrPluginStatus, type Pdf2DocxPluginStatus } from "../api-client";
import { createButton, createCard, createHintBadge, createProgressBar, createStatus, showToast } from "../components";
import { icon } from "../icons";
import { currentLocale, t, tx } from "../i18n";
import "./plugins.css";

const OFFICIAL_OCR_URL = "https://github.com/tesseract-ocr/tesseract";
const OFFICIAL_PDF2DOCX_URL = "https://github.com/ArtifexSoftware/pdf2docx";

let mounted = false;
let rootEl: HTMLElement | null = null;
let capability: CadCapabilityStatus | null = null;
let errorText = "";
let busy = false;
let detectTimer: number | null = null;
let detectDeadline = 0;

let ocrCapability: OcrPluginStatus | null = null;
let ocrErrorText = "";
let ocrBusy = false;

let wordCapability: Pdf2DocxPluginStatus | null = null;
let wordErrorText = "";
let wordBusy = false;
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
  void refreshOcr();
  void refreshWord();
}

export function unmount(): void {
  mounted = false;
  rootEl = null;
  stopAutoDetect();
  stopWordPolling();
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

function ocrStatusLabel(): string {
  if (!ocrCapability) return t("正在检测");
  if (ocrCapability.state === "connected") return t("已连接");
  return ocrCapability.state === "invalid" ? t("不可用") : t("未找到");
}

function ocrStatusTone(): "idle" | "ok" | "warn" | "danger" {
  if (!ocrCapability) return "idle";
  if (ocrCapability.state === "connected") return "ok";
  if (ocrCapability.state === "invalid") return "danger";
  return "warn";
}

async function refreshOcr(): Promise<void> {
  try {
    const client = new ApiClient();
    await client.connect();
    ocrCapability = await client.getOcrCapability();
    ocrErrorText = "";
  } catch (error) {
    ocrErrorText = error instanceof Error ? error.message : t("插件状态读取失败。");
  }
  if (mounted) render();
}

async function openOcrOfficialUrl(): Promise<void> {
  if (ocrBusy) return;
  ocrBusy = true;
  ocrErrorText = "";
  render();
  try {
    const client = new ApiClient();
    await client.connect();
    const download = await client.officialOcrDownload();
    const url = download.url || download.authorization_url || OFFICIAL_OCR_URL;
    await openExternalUrl(url);
  } catch (error) {
    ocrErrorText = error instanceof Error ? error.message : t("无法打开项目主页。");
    showToast({ message: ocrErrorText, error: true });
  } finally {
    ocrBusy = false;
    if (mounted) render();
  }
}

async function chooseTesseract(): Promise<void> {
  try {
    // 选择整个安装位置：选中它的安装目录即可，内部会自动找到可执行文件。
    const picked = await open({ multiple: false, directory: true });
    const path = Array.isArray(picked) ? picked[0] : picked;
    if (typeof path !== "string" || !path) return;
    const client = new ApiClient();
    await client.connect();
    ocrCapability = await client.connectOcrTesseract(path);
    ocrErrorText = "";
    showToast({
      message: ocrCapability.state === "connected" ? t("文字识别组件已连接。") : t("选择的位置不是可用的文字识别组件。"),
      error: ocrCapability.state !== "connected",
    });
    if (mounted) render();
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : t("无法连接文字识别组件。"), error: true });
  }
}

function ocrActionButtons(): HTMLElement {
  const actions = document.createElement("div");
  actions.className = "field-row plugins-actions";
  if (ocrCapability?.state !== "connected") {
    actions.append(createButton({ label: ocrBusy ? t("打开中…") : t("项目主页"), variant: "primary", disabled: ocrBusy, onClick: () => void openOcrOfficialUrl() }));
  }
  actions.append(createButton({ label: t("选择安装位置"), onClick: () => void chooseTesseract() }));
  actions.append(createButton({ label: t("重新检测"), onClick: () => void refreshOcr() }));
  return actions;
}

function renderOcrCard(): HTMLElement {
  const card = createCard([]);
  const body = document.createElement("div");
  body.className = "plugins-card-body";
  const titleRow = document.createElement("div");
  titleRow.className = "plugins-title-row";
  const iconBox = document.createElement("div");
  iconBox.className = "plugins-icon";
  iconBox.append(icon("ocr", { size: "lg" }));
  const copy = document.createElement("div");
  copy.className = "plugins-copy";
  const title = document.createElement("h2");
  title.textContent = t("文字识别组件");
  const betaTag = document.createElement("span");
  betaTag.className = "plugins-beta-tag";
  betaTag.textContent = t("Beta");
  title.append(betaTag);
  title.append(createHintBadge(t("软件名称：Tesseract OCR（开源）。安装后选择它的安装位置；放在系统默认位置会自动找到。项目主页：{0}", OFFICIAL_OCR_URL)));
  const desc = document.createElement("p");
  desc.textContent = t("识别 PDF 图纸里画成线条的文字，供「原位替换文字」使用。只在本机运行，不上传图纸。");
  copy.append(title, desc);
  titleRow.append(iconBox, copy);
  if (ocrCapability) titleRow.append(createStatus({ label: ocrStatusLabel(), tone: ocrStatusTone() }));
  titleRow.append(ocrActionButtons());
  body.append(titleRow);

  if (ocrErrorText) {
    const error = document.createElement("div");
    error.className = "plugins-notice danger";
    error.textContent = ocrErrorText;
    body.append(error);
  } else if (ocrCapability?.state === "connected") {
    const ok = document.createElement("div");
    ok.className = "plugins-notice ok";
    const languageCount = ocrCapability.languages.length;
    ok.textContent = ocrCapability.version
      ? t("Tesseract OCR 已连接（版本 {0}，已安装 {1} 种语言包）。", ocrCapability.version, String(languageCount))
      : t("Tesseract OCR 已连接（已安装 {0} 种语言包）。", String(languageCount));
    body.append(ok);
  } else if (ocrCapability?.state === "invalid") {
    const warn = document.createElement("div");
    warn.className = "plugins-notice danger";
    warn.textContent = t("之前连接的文字识别组件已不可用，请重新选择安装位置。");
    body.append(warn);
  } else if (ocrCapability) {
    const warn = document.createElement("div");
    warn.className = "plugins-notice warn";
    warn.textContent = t("尚未检测到文字识别组件。可打开项目主页、选择安装位置或重新检测。");
    body.append(warn);
  }
  card.append(body);
  return card;
}

function wordInstalling(): boolean {
  return Boolean(wordCapability?.install?.running);
}

function wordStatusLabel(): string {
  if (!wordCapability) return t("正在检测");
  if (wordInstalling()) return t("正在安装");
  if (wordCapability.state === "connected") return wordCapability.source === "managed" ? t("已安装") : t("已连接");
  return wordCapability.state === "invalid" ? t("不可用") : t("未安装");
}

function wordStatusTone(): "idle" | "ok" | "warn" | "danger" {
  if (!wordCapability) return "idle";
  if (wordInstalling()) return "idle";
  if (wordCapability.state === "connected") return wordCapability.inplace === false ? "warn" : "ok";
  if (wordCapability.state === "invalid") return "danger";
  return "warn";
}

let wordPollTimer: number | null = null;

function stopWordPolling(): void {
  if (wordPollTimer !== null) {
    window.clearTimeout(wordPollTimer);
    wordPollTimer = null;
  }
}

/** 一键安装在后台进行：每 0.6 秒取一次进度，结束后重新读一遍完整状态。 */
function pollWordInstall(): void {
  stopWordPolling();
  wordPollTimer = window.setTimeout(async () => {
    wordPollTimer = null;
    try {
      const client = new ApiClient();
      await client.connect();
      const progress = await client.pdfComponentInstallProgress();
      if (wordCapability) wordCapability = { ...wordCapability, install: progress };
      if (progress.running) {
        if (mounted) render();
        pollWordInstall();
        return;
      }
      wordCapability = await client.getPdf2DocxCapability();
      wordErrorText = "";
      if (progress.phase === "done") {
        showToast({ message: t("PDF 处理组件已安装。") });
      } else if (progress.error) {
        showToast({ message: tx(progress.error), error: !progress.cancelled });
      }
    } catch (error) {
      wordErrorText = error instanceof Error ? error.message : t("插件状态读取失败。");
    }
    if (mounted) render();
  }, 600);
}

async function refreshWord(): Promise<void> {
  try {
    const client = new ApiClient();
    await client.connect();
    wordCapability = await client.getPdf2DocxCapability();
    wordErrorText = "";
    if (wordInstalling()) pollWordInstall();
  } catch (error) {
    wordErrorText = error instanceof Error ? error.message : t("插件状态读取失败。");
  }
  if (mounted) render();
}

async function startWordInstall(): Promise<void> {
  if (wordBusy || wordInstalling()) return;
  wordBusy = true;
  wordErrorText = "";
  render();
  try {
    const client = new ApiClient();
    await client.connect();
    const progress = await client.startPdfComponentInstall();
    wordCapability = { ...(wordCapability ?? { state: "missing", path: "" }), install: progress };
    pollWordInstall();
  } catch (error) {
    wordErrorText = error instanceof Error ? error.message : t("无法开始安装。");
    showToast({ message: wordErrorText, error: true });
  } finally {
    wordBusy = false;
    if (mounted) render();
  }
}

async function cancelWordInstall(): Promise<void> {
  try {
    const client = new ApiClient();
    await client.connect();
    const progress = await client.cancelPdfComponentInstall();
    if (wordCapability) wordCapability = { ...wordCapability, install: progress };
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : t("无法取消安装。"), error: true });
  }
  if (mounted) render();
}

async function openWordProjectPage(): Promise<void> {
  if (wordBusy) return;
  wordBusy = true;
  wordErrorText = "";
  render();
  try {
    const client = new ApiClient();
    await client.connect();
    const download = await client.officialPdf2DocxDownload();
    const url = download.url || download.authorization_url || OFFICIAL_PDF2DOCX_URL;
    await openExternalUrl(url);
  } catch (error) {
    wordErrorText = error instanceof Error ? error.message : t("无法打开项目主页。");
    showToast({ message: wordErrorText, error: true });
  } finally {
    wordBusy = false;
    if (mounted) render();
  }
}

async function choosePdf2Docx(): Promise<void> {
  try {
    // 选择安装位置：选中 pdf2docx 所在的文件夹（或它的虚拟环境目录）即可，内部会自动找到可执行文件。
    const picked = await open({ multiple: false, directory: true });
    const path = Array.isArray(picked) ? picked[0] : picked;
    if (typeof path !== "string" || !path) return;
    const client = new ApiClient();
    await client.connect();
    wordCapability = await client.connectPdf2Docx(path);
    wordErrorText = "";
    showToast({
      message: wordCapability.state === "connected" ? t("PDF 处理组件已连接。") : t("选择的位置不是可用的 PDF 处理组件。"),
      error: wordCapability.state !== "connected",
    });
    if (mounted) render();
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : t("无法连接 PDF 处理组件。"), error: true });
  }
}

async function removePdfComponent(): Promise<void> {
  if (wordBusy) return;
  const managed = wordCapability?.source === "managed";
  wordBusy = true;
  render();
  try {
    const client = new ApiClient();
    await client.connect();
    wordCapability = await client.uninstallPdf2DocxPlugin(managed ? "managed" : "manual");
    wordErrorText = "";
    showToast({
      message: managed
        ? t("已卸载 PDF 处理组件。原位替换和转成 Word 的页会改用保底方式。")
        : t("已断开 PDF 处理组件。原位替换和转成 Word 的页会改用保底方式。"),
    });
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : t("无法卸载 PDF 处理组件。"), error: true });
  } finally {
    wordBusy = false;
    if (mounted) render();
  }
}

function wordActionButtons(): HTMLElement {
  const actions = document.createElement("div");
  actions.className = "field-row plugins-actions";
  const installing = wordInstalling();
  const connected = wordCapability?.state === "connected";
  const canOneClick = wordCapability?.one_click_supported !== false;
  if (installing) {
    actions.append(createButton({ label: t("取消安装"), onClick: () => void cancelWordInstall() }));
    return actions;
  }
  if (canOneClick && wordCapability?.source !== "managed") {
    actions.append(createButton({
      label: t("一键安装"),
      variant: connected ? "default" : "primary",
      disabled: wordBusy || !wordCapability,
      onClick: () => void startWordInstall(),
    }));
  }
  actions.append(createButton({ label: t("选择安装位置"), onClick: () => void choosePdf2Docx() }));
  if (!connected) {
    actions.append(createButton({ label: t("项目主页"), disabled: wordBusy, onClick: () => void openWordProjectPage() }));
  }
  actions.append(createButton({ label: t("重新检测"), onClick: () => void refreshWord() }));
  if (connected || wordCapability?.state === "invalid") {
    actions.append(createButton({
      label: wordCapability?.source === "managed" ? t("卸载") : t("断开"),
      disabled: wordBusy,
      onClick: () => void removePdfComponent(),
    }));
  }
  return actions;
}

function megabytes(bytes: number): string {
  return String(Math.max(1, Math.round(bytes / 1048576)));
}

function wordInstallNotice(): HTMLElement | null {
  const job = wordCapability?.install;
  if (!job) return null;
  if (job.running) {
    const box = document.createElement("div");
    box.className = "plugins-notice plugins-progress";
    const text = document.createElement("div");
    let percent = 0;
    if (job.phase === "download") {
      percent = job.total > 0 ? (job.done / job.total) * 90 : 0;
      text.textContent = t("正在下载组件：{0} / {1} MB", megabytes(job.done), megabytes(job.total));
    } else if (job.phase === "extract") {
      percent = 90 + (job.total > 0 ? (job.done / job.total) * 5 : 0);
      text.textContent = t("正在解压组件…");
    } else {
      percent = 96;
      text.textContent = t("正在检查组件能否在本机运行…");
    }
    box.append(text, createProgressBar({ percent }).root);
    return box;
  }
  if (job.phase === "failed" && job.error && wordCapability?.state !== "connected") {
    const box = document.createElement("div");
    box.className = "plugins-notice danger";
    box.textContent = job.cancelled
      ? tx(job.error)
      : t("{0}可重试一键安装，或使用「选择安装位置」连接自行安装的 pdf2docx。", tx(job.error));
    return box;
  }
  return null;
}

function renderWordCard(): HTMLElement {
  const card = createCard([]);
  const body = document.createElement("div");
  body.className = "plugins-card-body";
  const titleRow = document.createElement("div");
  titleRow.className = "plugins-title-row";
  const iconBox = document.createElement("div");
  iconBox.className = "plugins-icon";
  iconBox.append(icon("word", { size: "lg" }));
  const copy = document.createElement("div");
  copy.className = "plugins-copy";
  const title = document.createElement("h2");
  title.textContent = t("PDF 处理组件");
  const sizeMb = megabytes(wordCapability?.download_bytes ?? 0);
  title.append(createHintBadge(t("包含开源软件 PyMuPDF（AGPL-3.0）和 pdf2docx（MIT）。一键安装：从 PyPI 下载固定版本（约 {0} MB），核对文件后安装到本软件的数据目录；官方源无法连接时改用国内镜像。也可以自行安装 pdf2docx（会一并装上 PyMuPDF），再选择它的安装位置。未安装时：整页图像翻译不受影响；「自动组合」照常按页分类，原位替换和转成 Word 的页改用保底方式。项目主页：{1}", sizeMb, OFFICIAL_PDF2DOCX_URL)));
  const desc = document.createElement("p");
  desc.textContent = t("PDF 翻译「自动组合」用它判断图纸页、原位替换文字，并把可提取文字的页转成 Word 翻译。不安装也能翻译，这些页会改用保底方式。");
  copy.append(title, desc);
  titleRow.append(iconBox, copy);
  if (wordCapability) titleRow.append(createStatus({ label: wordStatusLabel(), tone: wordStatusTone() }));
  titleRow.append(wordActionButtons());
  body.append(titleRow);

  const installNotice = wordInstallNotice();
  if (wordErrorText) {
    const error = document.createElement("div");
    error.className = "plugins-notice danger";
    error.textContent = wordErrorText;
    body.append(error);
  } else if (installNotice) {
    body.append(installNotice);
  } else if (wordCapability?.state === "connected") {
    const ok = document.createElement("div");
    ok.className = "plugins-notice ok";
    ok.textContent = wordCapability.source === "managed"
      ? (wordCapability.pymupdf_version
        ? t("PDF 处理组件已安装（PyMuPDF {0}）。", wordCapability.pymupdf_version)
        : t("PDF 处理组件已安装。"))
      : t("已连接 pdf2docx：{0}", wordCapability.path);
    body.append(ok);
    if (wordCapability.inplace === false) {
      const warn = document.createElement("div");
      warn.className = "plugins-notice warn";
      warn.textContent = t("原位替换暂不可用：{0}。转成 Word 不受影响。", wordCapability.inplace_message || t("组件里的 PyMuPDF 无法加载"));
      body.append(warn);
    }
  } else if (wordCapability?.state === "invalid") {
    const warn = document.createElement("div");
    warn.className = "plugins-notice danger";
    warn.textContent = t("之前连接的 PDF 处理组件已不可用，请重新安装或重新选择安装位置。");
    body.append(warn);
  } else if (wordCapability) {
    const warn = document.createElement("div");
    warn.className = "plugins-notice warn";
    warn.textContent = wordCapability.one_click_supported === false
      ? t("尚未安装 PDF 处理组件。当前系统不支持一键安装，可自行安装 pdf2docx 后选择它的安装位置。")
      : t("尚未安装 PDF 处理组件。可一键安装，或选择自行安装的 pdf2docx 的位置。");
    body.append(warn);
  }
  card.append(body);
  return card;
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
  setTopbar({ title: t("插件"), status: { label: statusLabel(), tone: statusTone() }, subtitle: t("连接翻译时用到的可选组件") });
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
  column.append(renderOcrCard());
  column.append(renderWordCard());

  rootEl.append(column);
}
