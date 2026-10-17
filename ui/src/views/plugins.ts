// 插件管理视图 —— 管理三种可选插件的安装、检测与连接：
// 1. 图纸转换工具（ODA File Converter，翻译 DWG 需要）：一键安装从 ODA 官网下载（先确认 ODA 的
//    使用条款，核对 ODA 的数字签名）；
// 2. 文字识别组件（Tesseract OCR，PDF「原位替换文字」需要）：一键安装取 conda-forge 的固定版本；
// 3. PDF 处理组件（PyMuPDF + pdf2docx，PDF「自动组合」的原位替换、页面判断和转 Word）：
//    一键安装取 PyPI 的固定版本。
// 三者都保留「选择安装位置」，连接用户自行安装的版本。

import { open } from "@tauri-apps/plugin-dialog";
import { invoke } from "@tauri-apps/api/core";
import type { ViewParams } from "../router";
import { setTopbar } from "../shell";
import { ApiClient, type CadCapabilityStatus, type ComponentInstallProgress, type OcrPluginStatus, type Pdf2DocxPluginStatus } from "../api-client";
import { clearElement, createButton, createCard, createHintBadge, createProgressBar, createStatus, openModal, showToast } from "../components";
import { icon } from "../icons";
import { currentLocale, t, tx } from "../i18n";
import "./plugins.css";

const OFFICIAL_OCR_URL = "https://github.com/tesseract-ocr/tesseract";
const OFFICIAL_PDF2DOCX_URL = "https://github.com/ArtifexSoftware/pdf2docx";

let mounted = false;
/** 嵌在「扩展」页的「组件」页签里：顶栏由扩展页管，这里不设。 */
let embedded = false;
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

function cadInstalling(): boolean {
  return Boolean(capability?.install?.running);
}

function statusLabel(): string {
  if (!capability) return t("正在检测");
  if (!capability.platform_supported) return t("当前平台不支持");
  if (cadInstalling()) return t("正在安装");
  if (capability.oda === "connected") return capability.source === "managed" ? t("已安装") : t("已连接");
  return capability.oda === "incompatible" ? t("转换工具不可用") : t("需要连接图纸转换工具");
}

function statusTone(): "idle" | "ok" | "warn" | "danger" {
  if (!capability) return "idle";
  if (cadInstalling()) return "idle";
  if (!capability.platform_supported || capability.oda === "incompatible") return "danger";
  if (capability.oda === "connected") return "ok";
  return "warn";
}

export function mount(container: HTMLElement, params: ViewParams): void {
  mounted = true;
  embedded = params.embedded === true;
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
  stopCadPolling();
  stopWordPolling();
  stopOcrPolling();
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
    if (cadInstalling()) pollCadInstall();
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

let cadPollTimer: number | null = null;

function stopCadPolling(): void {
  if (cadPollTimer !== null) {
    window.clearTimeout(cadPollTimer);
    cadPollTimer = null;
  }
}

/** 一键安装在后台进行：每 0.6 秒取一次进度，结束后重新读一遍完整状态。 */
function pollCadInstall(): void {
  stopCadPolling();
  cadPollTimer = window.setTimeout(async () => {
    cadPollTimer = null;
    try {
      const client = new ApiClient();
      await client.connect();
      const progress = await client.cadConverterInstallProgress();
      if (capability) capability = { ...capability, install: progress };
      if (progress.running) {
        if (mounted) render();
        pollCadInstall();
        return;
      }
      capability = await client.getCadCapability();
      errorText = "";
      if (progress.phase === "done") {
        showToast({ message: t("图纸转换工具已安装。") });
      } else if (progress.error) {
        showToast({ message: tx(progress.error), error: !progress.cancelled });
      }
    } catch (error) {
      errorText = error instanceof Error ? error.message : t("插件状态读取失败。");
    }
    if (mounted) render();
  }, 600);
}

/** ODA 官网上说明 File Converter 使用条件的页面（官网没有单独的条款页）。 */
const ODA_TERMS_URL = "https://www.opendesign.com/faq/question/what-are-oda-viewer-and-oda-file-converter";

/** ODA File Converter 不是开源软件：下载前先让用户看到它的来源和条款，同意了才装。 */
function confirmOdaTerms(): Promise<boolean> {
  return new Promise((resolve) => {
    const link = (label: string, url: string): HTMLButtonElement => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "linklike";
      button.append(label, icon("ext"));
      button.addEventListener("click", () => {
        openExternalUrl(url).catch((error) => {
          showToast({ message: error instanceof Error ? error.message : t("无法打开网页。"), error: true });
        });
      });
      return button;
    };
    const modal = openModal({
      tone: "tint",
      icon: "down",
      sourceLabel: t("插件 · 图纸转换工具"),
      title: t("安装图纸转换工具"),
      body: [
        t("图纸转换工具（ODA File Converter）由 Open Design Alliance 免费提供，不是开源软件，使用需遵守 ODA 的条款。ODA 官网说明：非 ODA 会员仅可用于非商业用途。"),
        t("一键安装将从 ODA 官网下载安装包，核对 ODA 的数字签名后安装到本软件的数据目录。"),
      ],
      actions: [
        { label: t("取消"), onClick: () => resolve(false) },
        { label: t("同意并安装"), variant: "primary", onClick: () => resolve(true) },
      ],
    });
    // 两个链接做成正文下方的一行文字链接，不跟「取消」「同意并安装」抢视线；
    // 不塞进按钮行，英法文较长时按钮才不会被挤得折行。
    const links = document.createElement("div");
    links.className = "oda-terms-links";
    links.append(link(t("查看 ODA 条款"), ODA_TERMS_URL), link(t("打开官网"), DEFAULT_OFFICIAL_CAD_URL));
    modal.element.insertBefore(links, modal.element.querySelector(".macts"));
  });
}

async function startCadInstall(): Promise<void> {
  if (busy || cadInstalling()) return;
  if (!(await confirmOdaTerms())) return;
  if (busy || cadInstalling()) return;
  busy = true;
  errorText = "";
  stopAutoDetect();
  render();
  try {
    const client = new ApiClient();
    await client.connect();
    const progress = await client.startCadConverterInstall();
    if (capability) capability = { ...capability, install: progress };
    pollCadInstall();
  } catch (error) {
    errorText = error instanceof Error ? error.message : t("无法开始安装。");
    showToast({ message: errorText, error: true });
  } finally {
    busy = false;
    if (mounted) render();
  }
}

async function cancelCadInstall(): Promise<void> {
  try {
    const client = new ApiClient();
    await client.connect();
    const progress = await client.cancelCadConverterInstall();
    if (capability) capability = { ...capability, install: progress };
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : t("无法取消安装。"), error: true });
  }
  if (mounted) render();
}

async function removeCadConverter(): Promise<void> {
  if (busy) return;
  if (!window.confirm(t("卸载将删除已下载的图纸转换工具，再次使用需重新下载。确定要卸载吗？"))) return;
  busy = true;
  render();
  try {
    const client = new ApiClient();
    await client.connect();
    capability = await client.removeCadConverter();
    errorText = "";
    showToast({ message: t("已卸载图纸转换工具。") });
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : t("无法卸载图纸转换工具。"), error: true });
  } finally {
    busy = false;
    if (mounted) render();
  }
}

function ocrInstalling(): boolean {
  return Boolean(ocrCapability?.install?.running);
}

function ocrStatusLabel(): string {
  if (!ocrCapability) return t("正在检测");
  if (ocrInstalling()) return t("正在安装");
  if (ocrCapability.state === "connected") return ocrCapability.source === "managed" ? t("已安装") : t("已连接");
  return ocrCapability.state === "invalid" ? t("不可用") : t("未找到");
}

function ocrStatusTone(): "idle" | "ok" | "warn" | "danger" {
  if (!ocrCapability) return "idle";
  if (ocrInstalling()) return "idle";
  if (ocrCapability.state === "connected") return "ok";
  if (ocrCapability.state === "invalid") return "danger";
  return "warn";
}

let ocrPollTimer: number | null = null;

function stopOcrPolling(): void {
  if (ocrPollTimer !== null) {
    window.clearTimeout(ocrPollTimer);
    ocrPollTimer = null;
  }
}

/** 一键安装在后台进行：每 0.6 秒取一次进度，结束后重新读一遍完整状态。 */
function pollOcrInstall(): void {
  stopOcrPolling();
  ocrPollTimer = window.setTimeout(async () => {
    ocrPollTimer = null;
    try {
      const client = new ApiClient();
      await client.connect();
      const progress = await client.ocrComponentInstallProgress();
      if (ocrCapability) ocrCapability = { ...ocrCapability, install: progress };
      if (progress.running) {
        if (mounted) render();
        pollOcrInstall();
        return;
      }
      ocrCapability = await client.getOcrCapability();
      ocrErrorText = "";
      if (progress.phase === "done") {
        showToast({ message: t("文字识别组件已安装。") });
      } else if (progress.error) {
        showToast({ message: tx(progress.error), error: !progress.cancelled });
      }
    } catch (error) {
      ocrErrorText = error instanceof Error ? error.message : t("插件状态读取失败。");
    }
    if (mounted) render();
  }, 600);
}

async function refreshOcr(): Promise<void> {
  try {
    const client = new ApiClient();
    await client.connect();
    ocrCapability = await client.getOcrCapability();
    ocrErrorText = "";
    if (ocrInstalling()) pollOcrInstall();
  } catch (error) {
    ocrErrorText = error instanceof Error ? error.message : t("插件状态读取失败。");
  }
  if (mounted) render();
}

async function startOcrInstall(): Promise<void> {
  if (ocrBusy || ocrInstalling()) return;
  ocrBusy = true;
  ocrErrorText = "";
  render();
  try {
    const client = new ApiClient();
    await client.connect();
    const progress = await client.startOcrComponentInstall();
    ocrCapability = { ...(ocrCapability ?? { state: "missing", path: "", languages: [], has_latin: false }), install: progress };
    pollOcrInstall();
  } catch (error) {
    ocrErrorText = error instanceof Error ? error.message : t("无法开始安装。");
    showToast({ message: ocrErrorText, error: true });
  } finally {
    ocrBusy = false;
    if (mounted) render();
  }
}

async function cancelOcrInstall(): Promise<void> {
  try {
    const client = new ApiClient();
    await client.connect();
    const progress = await client.cancelOcrComponentInstall();
    if (ocrCapability) ocrCapability = { ...ocrCapability, install: progress };
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : t("无法取消安装。"), error: true });
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

async function removeOcrComponent(): Promise<void> {
  if (ocrBusy) return;
  const managed = ocrCapability?.source === "managed";
  // 卸载会删掉下载的文件，再用要重新下载；断开只是忘掉路径，不用确认。
  if (managed && !window.confirm(t("卸载将删除已下载的文字识别组件，再次使用需重新下载。确定要卸载吗？"))) return;
  ocrBusy = true;
  render();
  try {
    const client = new ApiClient();
    await client.connect();
    ocrCapability = await client.uninstallOcrPlugin(managed ? "managed" : "manual");
    ocrErrorText = "";
    showToast({ message: managed ? t("已卸载文字识别组件。") : t("已断开文字识别组件。") });
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : t("无法卸载文字识别组件。"), error: true });
  } finally {
    ocrBusy = false;
    if (mounted) render();
  }
}

function ocrActionButtons(): HTMLElement {
  const actions = document.createElement("div");
  actions.className = "field-row plugins-actions";
  const connected = ocrCapability?.state === "connected";
  if (ocrInstalling()) {
    actions.append(createButton({ label: t("取消安装"), onClick: () => void cancelOcrInstall() }));
    return actions;
  }
  if (ocrCapability?.one_click_supported !== false && ocrCapability?.source !== "managed") {
    actions.append(createButton({
      label: t("一键安装"),
      variant: connected ? "default" : "primary",
      disabled: ocrBusy || !ocrCapability,
      onClick: () => void startOcrInstall(),
    }));
  }
  actions.append(createButton({ label: t("选择安装位置"), onClick: () => void chooseTesseract() }));
  if (!connected) {
    actions.append(createButton({ label: t("项目主页"), disabled: ocrBusy, onClick: () => void openOcrOfficialUrl() }));
  }
  actions.append(createButton({ label: t("重新检测"), onClick: () => void refreshOcr() }));
  // 系统里自动找到的不是我们装的，也没记过路径：没有可卸载或断开的。
  if (ocrCapability?.source === "managed" || ocrCapability?.source === "manual" || ocrCapability?.state === "invalid") {
    actions.append(createButton({
      label: ocrCapability?.source === "managed" ? t("卸载") : t("断开"),
      disabled: ocrBusy,
      onClick: () => void removeOcrComponent(),
    }));
  }
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
  const sizeMb = megabytes(ocrCapability?.download_bytes ?? 0);
  title.append(createHintBadge(t("软件名称：Tesseract OCR（开源，Apache-2.0）。一键安装：从 conda-forge 下载固定版本的程序（含英语、法语、德语、西班牙语、意大利语、葡萄牙语的识别数据，约 {0} MB），核对文件后安装到本软件的数据目录；官方源无法连接时改用国内镜像。也可以自行安装，再选择它的安装位置；装在系统默认位置的会自动找到。未安装时：「原位替换」只处理图纸里本来就能提取的文字，画成线条的字保留原样。项目主页：{1}", sizeMb, OFFICIAL_OCR_URL)));
  const desc = document.createElement("p");
  desc.textContent = t("识别 PDF 图纸里画成线条的文字，供「原位替换文字」使用。只在本机运行，不上传图纸。");
  copy.append(title, desc);
  titleRow.append(iconBox, copy);
  if (ocrCapability) titleRow.append(createStatus({ label: ocrStatusLabel(), tone: ocrStatusTone() }));
  titleRow.append(ocrActionButtons());
  body.append(titleRow);

  const installNotice = componentInstallNotice(ocrCapability?.install, ocrCapability?.state === "connected",
    (error) => t("{0}可重试一键安装，或自行安装 Tesseract OCR 后选择它的安装位置。", error));
  if (ocrErrorText) {
    const error = document.createElement("div");
    error.className = "plugins-notice danger";
    error.textContent = ocrErrorText;
    body.append(error);
  } else if (installNotice) {
    body.append(installNotice);
  } else if (ocrCapability?.state === "connected") {
    const ok = document.createElement("div");
    ok.className = "plugins-notice ok";
    const languageCount = String(ocrCapability.languages.length);
    if (ocrCapability.source === "managed") {
      ok.textContent = ocrCapability.version
        ? t("文字识别组件已安装（Tesseract {0}，{1} 种语言）。", ocrCapability.version, languageCount)
        : t("文字识别组件已安装（{0} 种语言）。", languageCount);
    } else {
      ok.textContent = ocrCapability.version
        ? t("Tesseract OCR 已连接（版本 {0}，已安装 {1} 种语言包）。", ocrCapability.version, languageCount)
        : t("Tesseract OCR 已连接（已安装 {0} 种语言包）。", languageCount);
    }
    body.append(ok);
  } else if (ocrCapability?.state === "invalid") {
    const warn = document.createElement("div");
    warn.className = "plugins-notice danger";
    warn.textContent = t("之前连接的文字识别组件已不可用，请重新安装或重新选择安装位置。");
    body.append(warn);
  } else if (ocrCapability) {
    const warn = document.createElement("div");
    warn.className = "plugins-notice warn";
    warn.textContent = ocrCapability.one_click_supported === false
      ? t("尚未检测到文字识别组件。当前系统不支持一键安装，可自行安装后选择它的安装位置。")
      : t("尚未检测到文字识别组件。可一键安装，或选择自行安装的 Tesseract OCR 的位置。");
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
  if (managed && !window.confirm(t("卸载将删除已下载的 PDF 处理组件，再次使用需重新下载。确定要卸载吗？"))) return;
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

/** 一键安装进行中（进度条）或刚失败（原因 + 改用手动安装的提示）时的提示框；其余情况返回 null。 */
function componentInstallNotice(
  job: ComponentInstallProgress | undefined,
  connected: boolean,
  failedHint: (error: string) => string,
): HTMLElement | null {
  if (!job) return null;
  if (job.running) {
    const box = document.createElement("div");
    box.className = "plugins-notice plugins-progress";
    const text = document.createElement("div");
    let percent = 0;
    if (job.phase === "download" && job.total <= 0) {
      text.textContent = t("正在下载组件…");
    } else if (job.phase === "download") {
      percent = (job.done / job.total) * 90;
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
  if (job.phase === "failed" && job.error && !connected) {
    const box = document.createElement("div");
    box.className = "plugins-notice danger";
    box.textContent = job.cancelled ? tx(job.error) : failedHint(tx(job.error));
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

  const installNotice = componentInstallNotice(wordCapability?.install, wordCapability?.state === "connected",
    (error) => t("{0}可重试一键安装，或使用「选择安装位置」连接自行安装的 pdf2docx。", error));
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
  const connected = capability?.oda === "connected";
  if (cadInstalling()) {
    actions.append(createButton({ label: t("取消安装"), onClick: () => void cancelCadInstall() }));
    return actions;
  }
  const oneClick = capability?.one_click_supported === true && capability.source !== "managed";
  if (oneClick) {
    actions.append(createButton({
      label: t("一键安装"),
      variant: connected ? "default" : "primary",
      disabled: busy,
      onClick: () => void startCadInstall(),
    }));
  }
  // 保留手动兜底：即使自动检测已连接，用户仍可切换到另一套安装位置。
  actions.append(createButton({ label: t("选择安装位置"), onClick: () => void chooseOda() }));
  if (!connected) {
    actions.append(createButton({
      label: busy ? t("打开中…") : t("去官网下载"),
      variant: oneClick ? "default" : "primary",
      disabled: busy,
      onClick: () => void openOfficialDownloadAndDetect(),
    }));
  }
  actions.append(createButton({ label: t("重新检测"), onClick: () => void refresh() }));
  // 只有一键安装的那一份能卸载；用户自己装的从不删除。
  if (capability?.source === "managed") {
    actions.append(createButton({ label: t("卸载"), disabled: busy, onClick: () => void removeCadConverter() }));
  }
  return actions;
}

function render(): void {
  if (!mounted || !rootEl) return;
  if (!embedded) setTopbar({ title: t("插件"), status: { label: statusLabel(), tone: statusTone() }, subtitle: t("连接翻译时用到的可选组件") });
  rootEl.classList.add("plugins-content");
  clearElement(rootEl);

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
  title.append(createHintBadge(t("软件名称：ODA File Converter（Open Design Alliance 免费提供，不是开源软件）。一键安装：确认 ODA 的使用条款后，从 ODA 官网下载安装包，核对 ODA 的数字签名并试转换一张图纸，通过后安装到本软件的数据目录。也可以自行从官网下载安装，再选择它的安装位置；装在系统默认位置的会自动找到。未安装时：DXF 图纸照常翻译，DWG 图纸需要它才能翻译。官方下载：{0}", officialUrl)));
  const desc = document.createElement("p");
  desc.textContent = t("把 DWG 转为可翻译的 DXF，完成翻译后再转回 DWG。");
  copy.append(title, desc);
  titleRow.append(iconBox, copy);
  if (capability) titleRow.append(createStatus({ label: statusLabel(), tone: statusTone() }));
  titleRow.append(actionButtons());
  body.append(titleRow);

  const installNotice = componentInstallNotice(capability?.install, capability?.oda === "connected",
    (error) => t("{0}可重试一键安装，或从 ODA 官网下载安装后选择它的安装位置。", error));
  if (errorText) {
    const error = document.createElement("div");
    error.className = "plugins-notice danger";
    error.textContent = errorText;
    body.append(error);
  } else if (installNotice) {
    body.append(installNotice);
  } else if (capability?.oda === "connected") {
    const ok = document.createElement("div");
    ok.className = "plugins-notice ok";
    ok.textContent = capability.source === "managed"
      ? (capability.converter_version
        ? t("图纸转换工具已安装（ODA File Converter {0}）。", capability.converter_version)
        : t("图纸转换工具已安装。"))
      : t("ODA File Converter 已连接，可开始选择 DWG / DXF 来源。");
    body.append(ok);
  } else if (capability) {
    const warn = document.createElement("div");
    warn.className = "plugins-notice warn";
    warn.textContent = capability.one_click_supported === true
      ? t("尚未检测到图纸转换工具。可一键安装，或从 ODA 官网下载安装后选择它的安装位置。")
      : t("尚未检测到图纸转换工具。可从 ODA 官网下载安装后选择它的安装位置，或重新检测。");
    body.append(warn);
  }
  card.append(body);
  column.append(card);
  column.append(renderOcrCard());
  column.append(renderWordCard());

  rootEl.append(column);
}
