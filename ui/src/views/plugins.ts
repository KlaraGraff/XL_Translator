// 插件管理视图 —— 管理三种可选插件的安装、检测与连接：
// 1. 图纸转换工具（ODA File Converter，翻译 DWG 需要）：一键安装从 ODA 官网下载（先确认 ODA 的
//    使用条款，核对 ODA 的数字签名）；
// 2. 「PDF 组件」大类（maple 2026-11-27）：「全部安装」按顺序装上缺的几项，展开后每项单独
//    安装、重试、卸载（一键装不上时的手动保底）：
//    ① PDF 处理组件（PyMuPDF + pdf2docx，PDF「智能混合」的原位替换、页面判断和转 Word）：
//       一键安装取 PyPI 的固定版本；另外两项要用它里面的程序库；
//    ② 段落重排组件（BabelDOC，可提取文字的页在原 PDF 上按段落重排）：只能一键安装，
//       装好后「可提取文字的页」自动改用它；
//    ③ 文字识别组件（PP-OCR 识别模型，PDF「原位替换文字」需要）：一键安装取 PyPI 的固定版本，
//       缺少「PDF 处理组件」时一键链路会先自动补齐；以前装的 Tesseract 只认西文，显示「更新」。
// 都保留「选择安装位置」，连接用户自行安装的版本（文字识别组件只在不能一键安装或已连接
// 自装的 Tesseract 时显示）。

import { open } from "@tauri-apps/plugin-dialog";
import { invoke } from "@tauri-apps/api/core";
import type { ViewParams } from "../router";
import { setTopbar } from "../shell";
import { ApiClient, type BabeldocPluginStatus, type CadCapabilityStatus, type ComponentInstallProgress, type OcrPluginStatus, type Pdf2DocxPluginStatus, type PdfComponentsItem, type PdfComponentsStatus } from "../api-client";
import { clearElement, createButton, createCard, createHintBadge, createProgressBar, createStatus, openModal, showToast } from "../components";
import { icon } from "../icons";
import { motionAllowed } from "../motion";
import { isChineseLocale, t, tx } from "../i18n";
import "./plugins.css";

const OFFICIAL_OCR_URL = "https://github.com/PaddlePaddle/PaddleOCR";
const OFFICIAL_PDF2DOCX_URL = "https://github.com/ArtifexSoftware/pdf2docx";
const OFFICIAL_BABELDOC_URL = "https://github.com/funstory-ai/BabelDOC";

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

/** 从别的页跳过来时要定位的组件卡片（navigate("plugins", { focus: "pdf" })）：滚到它，
 *  再闪一圈强调色。轮询会整页重绘，所以记住开始时刻，重绘后的新卡片接着同一段淡出。 */
type ComponentKey = "oda" | "ocr" | "pdf" | "babeldoc";
const FOCUS_MS = 2400;
let focusKey: ComponentKey | null = null;
let focusStart = 0;

let wordCapability: Pdf2DocxPluginStatus | null = null;
let wordErrorText = "";
let wordBusy = false;

let babeldocCapability: BabeldocPluginStatus | null = null;
let babeldocErrorText = "";
let babeldocBusy = false;

/** 「PDF 组件」大类：缺哪几项、「全部安装」的进度；展开看每一项。 */
let groupStatus: PdfComponentsStatus | null = null;
let groupBusy = false;
let groupExpanded = false;
const DEFAULT_OFFICIAL_CAD_URL = isChineseLocale()
  ? "https://www.opendesign.com/cn/guestfiles/oda_file_converter"
  : "https://www.opendesign.com/guestfiles/oda_file_converter";

function localizedOfficialCadUrl(value?: string): string {
  if (!value) return DEFAULT_OFFICIAL_CAD_URL;
  try {
    const url = new URL(value);
    // 中文界面才换成官网中文页；英法界面用默认的英文页。
    if (isChineseLocale() && url.hostname === "www.opendesign.com" && url.pathname === "/guestfiles/oda_file_converter") {
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
  const focus = params.focus;
  focusKey = focus === "oda" || focus === "ocr" || focus === "pdf" || focus === "babeldoc" ? focus : null;
  focusStart = performance.now();
  // 从别处点「安装 ↗ 组件」跳到某一项时把大类展开，那一项才看得见。
  if (focusKey === "ocr" || focusKey === "babeldoc") groupExpanded = true;
  render();
  const loads = Promise.allSettled([refresh(), refreshOcr(), refreshWord(), refreshBabeldoc(), refreshGroup()]);
  const key = focusKey;
  if (!key) return;
  // 三次检测回来各整页重绘一次，卡片节点会换掉、高度也会变：每次都按 key 重新找，
  // 先滚一次让它尽快露出来，检测全部回来、版面定下之后再对准一次。
  const scrollToFocus = (): void => {
    if (!mounted || rootEl !== container) return;
    container.querySelector<HTMLElement>(`[data-component="${key}"]`)
      ?.scrollIntoView({ block: "center", behavior: motionAllowed() ? "smooth" : "auto" });
  };
  requestAnimationFrame(scrollToFocus);
  void loads.then(() => requestAnimationFrame(scrollToFocus));
}

export function unmount(): void {
  mounted = false;
  rootEl = null;
  stopAutoDetect();
  stopCadPolling();
  stopWordPolling();
  stopOcrPolling();
  stopBabeldocPolling();
  stopGroupPolling();
}

function markComponentCard(card: HTMLElement, key: ComponentKey): HTMLElement {
  card.dataset.component = key;
  const elapsed = performance.now() - focusStart;
  // 大类卡片代表 PDF 处理组件；跳到其中另两项时，那一项另有 data-component，强调色描在大类上。
  const focused = focusKey === key || (key === "pdf" && (focusKey === "ocr" || focusKey === "babeldoc"));
  if (focused && elapsed < FOCUS_MS) {
    card.classList.add("plugins-focus");
    card.style.animationDelay = `${-Math.round(elapsed)}ms`;
  }
  return card;
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
  if (ocrCapability.state === "connected") return ocrCapability.engine === "ppocr" && ocrCapability.needs_pdf_component ? "warn" : "ok";
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
      // OCR 的一键链路可能刚刚自动装好了 PDF 处理组件；同步刷新那一项和大类，
      // 不要让它继续显示「未安装」直到用户手动刷新。
      wordCapability = await client.getPdf2DocxCapability();
      groupStatus = await client.getPdfComponentsStatus();
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
    groupStatus = await client.getPdfComponentsStatus();
    ocrErrorText = "";
    showToast({ message: managed ? t("已卸载文字识别组件。") : t("已断开文字识别组件。") });
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : t("无法卸载文字识别组件。"), error: true });
  } finally {
    ocrBusy = false;
    if (mounted) render();
  }
}

/** 连着的是只认西文的 Tesseract（以前一键安装的或用户自己装的）：可以更新成识别模型。 */
function ocrCanUpdate(): boolean {
  return ocrCapability?.engine === "tesseract" && ocrCanOneClick();
}

function ocrCanOneClick(): boolean {
  if (!ocrCapability || ocrCapability.one_click_supported === false) return false;
  return !ocrCapability.needs_pdf_component || ocrCapability.dependency_one_click_supported !== false;
}

function ocrActionButtons(): HTMLElement {
  const actions = document.createElement("div");
  actions.className = "field-row plugins-actions";
  const connected = ocrCapability?.state === "connected";
  if (ocrInstalling()) {
    actions.append(createButton({ label: t("取消安装"), onClick: () => void cancelOcrInstall() }));
    return actions;
  }
  const oneClick = ocrCanOneClick();
  if (oneClick && (!connected || ocrCanUpdate())) {
    actions.append(createButton({
      label: ocrCanUpdate() ? t("更新") : t("一键安装"),
      variant: "primary",
      // 缺少 PDF 处理组件时，后端会在同一个可取消的安装链路里先补齐它。
      disabled: ocrBusy || !ocrCapability || groupInstalling(),
      onClick: () => void startOcrInstall(),
    }));
  }
  if (!oneClick || ocrCapability?.source === "manual") {
    actions.append(createButton({ label: t("选择安装位置"), onClick: () => void chooseTesseract() }));
  }
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

function renderOcrItem(index: number): HTMLElement {
  const { body, titleRow, copy, title } = itemShell(index, "ocr");
  title.textContent = t("文字识别组件");
  const betaTag = document.createElement("span");
  betaTag.className = "plugins-beta-tag";
  betaTag.textContent = t("Beta");
  title.append(betaTag);
  const sizeMb = megabytes(ocrCapability?.download_bytes ?? 0);
  title.append(createHintBadge(t("识别模型：PP-OCRv6（PaddleOCR，开源，Apache-2.0），由 ONNX Runtime（开源，MIT）在本机运行，可识别中文、日文和英文、法文等西方文字，缺少时一键安装会先自动安装「PDF 处理组件」。一键安装：下载固定版本的识别模型和运行库（约 {0} MB），核对文件后安装到本软件的数据目录；官方源无法连接时改用国内镜像。未安装时：「原位替换」只处理图纸里本来就能提取的文字，画成线条的字保留原样。项目主页：{1}", sizeMb, OFFICIAL_OCR_URL)));
  const desc = document.createElement("p");
  desc.textContent = t("识别 PDF 图纸里画成线条的文字，供「原位替换文字」使用。只在本机运行，不上传图纸。");
  copy.append(title, desc);
  const groupItem = groupItemState("ocr");
  if (groupItem) titleRow.append(createStatus(groupItem));
  else if (ocrCapability) titleRow.append(createStatus({ label: ocrStatusLabel(), tone: ocrStatusTone() }));
  titleRow.append(ocrActionButtons());

  const installNotice = componentInstallNotice(ocrCapability?.install, ocrCapability?.state === "connected",
    (error) => t("{0}可重试一键安装。", error));
  const groupError = groupItemError("ocr", ocrCapability?.state === "connected" && !ocrCanUpdate());
  if (ocrErrorText) {
    const error = document.createElement("div");
    error.className = "plugins-notice danger";
    error.textContent = ocrErrorText;
    body.append(error);
  } else if (installNotice) {
    body.append(installNotice);
  } else if (groupError) {
    body.append(groupError);
  } else if (ocrCanUpdate()) {
    const warn = document.createElement("div");
    warn.className = "plugins-notice warn";
    warn.textContent = ocrCapability?.needs_pdf_component
      ? t("当前的文字识别组件只能识别英文、法文等西方文字。新版还可识别中文和日文：一键安装会先补齐上面的「PDF 处理组件」，再点「更新」。")
      : t("当前的文字识别组件只能识别英文、法文等西方文字。点「更新」换成新版后，还可识别中文和日文（约 {0} MB）。", sizeMb);
    body.append(warn);
  } else if (ocrCapability?.state === "connected") {
    const ok = document.createElement("div");
    ok.className = "plugins-notice ok";
    const languageCount = String(ocrCapability.languages.length);
    if (ocrCapability.engine === "ppocr" && ocrCapability.needs_pdf_component) {
      // 识别模型装着，但「PDF 处理组件」后来被卸载了：读不了线条字
      ok.className = "plugins-notice warn";
      ok.textContent = t("文字识别组件要用「PDF 处理组件」才能运行，请安装上面的「PDF 处理组件」。");
    } else if (ocrCapability.engine === "ppocr") {
      ok.textContent = t("文字识别组件已安装，可识别中文、日文和英文、法文等文字。");
    } else if (ocrCapability.source === "managed") {
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
    if (ocrCapability.one_click_supported === false) {
      const reason = ocrCapability.one_click_reason || ocrCapability.dependency_one_click_reason || "";
      const lead = ocrCapability.needs_pdf_component && ocrCapability.dependency_one_click_supported === false
        ? t("尚未检测到文字识别组件。当前系统不能自动安装 PDF 处理组件，请自行安装后选择它的安装位置。")
        : t("尚未检测到文字识别组件。当前系统不支持一键安装，可自行安装后选择它的安装位置。");
      warn.textContent = reason ? `${lead} ${tx(reason)}` : lead;
    } else if (ocrCapability.needs_pdf_component) {
      warn.textContent = t("尚未检测到文字识别组件。一键安装会先自动安装上面的「PDF 处理组件」，然后安装识别模型。");
    } else {
      warn.textContent = t("尚未检测到文字识别组件，可一键安装。");
    }
    body.append(warn);
  }
  return body;
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
      groupStatus = await client.getPdfComponentsStatus();
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
    // 另外两项要用它里面的程序库：一起刷新，让它们显示「需要 PDF 处理组件」。
    [groupStatus, babeldocCapability, ocrCapability] = await Promise.all([
      client.getPdfComponentsStatus(), client.getBabeldocCapability(), client.getOcrCapability(),
    ]);
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
      disabled: wordBusy || !wordCapability || groupInstalling(),
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
  /** 先补 PDF 处理组件、再装哪一项（文字识别组件或段落重排组件）。 */
  dependent: "ocr" | "babeldoc" = "ocr",
): HTMLElement | null {
  if (!job) return null;
  if (job.running) {
    const box = document.createElement("div");
    box.className = "plugins-notice plugins-progress";
    const text = document.createElement("div");
    let percent = 0;
    if (job.phase === "dependency_download" && job.total <= 0) {
      text.textContent = t("正在准备 PDF 处理组件…");
    } else if (job.phase === "dependency_download") {
      percent = (job.done / job.total) * 90;
      text.textContent = t("正在准备 PDF 处理组件：{0} / {1} MB", megabytes(job.done), megabytes(job.total));
    } else if (job.phase === "dependency_extract") {
      percent = Math.max(70, (job.done / Math.max(1, job.total)) * 90);
      text.textContent = dependent === "babeldoc"
        ? t("正在安装 PDF 处理组件，完成后继续安装段落重排组件…")
        : t("正在安装 PDF 处理组件，完成后继续安装文字识别组件…");
    } else if (job.phase === "dependency_verify") {
      percent = Math.max(75, (job.done / Math.max(1, job.total)) * 90);
      text.textContent = t("正在检查 PDF 处理组件能否在本机运行…");
    } else if (job.phase === "download" && job.total <= 0) {
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

function renderWordItem(index: number): HTMLElement {
  const { body, titleRow, copy, title } = itemShell(index, "pdf-processing");
  title.textContent = t("PDF 处理组件");
  const sizeMb = megabytes(wordCapability?.download_bytes ?? 0);
  title.append(createHintBadge(t("包含开源软件 PyMuPDF（AGPL-3.0）和 pdf2docx（MIT）。一键安装：从 PyPI 下载固定版本（约 {0} MB），核对文件后安装到本软件的数据目录；官方源无法连接时改用国内镜像。也可以自行安装 pdf2docx（会一并装上 PyMuPDF），再选择它的安装位置。未安装时：整页图像翻译不受影响；「智能混合」照常按页分类，原位替换和转成 Word 的页改用整页图像翻译（没有图像模型时原样保留）。项目主页：{1}", sizeMb, OFFICIAL_PDF2DOCX_URL)));
  const desc = document.createElement("p");
  desc.textContent = t("判断图纸页、原位替换文字，把可提取文字的页转成 Word 翻译。另外两项也要用它。不安装也能翻译，这些页会改用保底方式。");
  copy.append(title, desc);
  const groupItem = groupItemState("pdf");
  if (groupItem) titleRow.append(createStatus(groupItem));
  else if (wordCapability) titleRow.append(createStatus({ label: wordStatusLabel(), tone: wordStatusTone() }));
  titleRow.append(wordActionButtons());

  const installNotice = componentInstallNotice(wordCapability?.install, wordCapability?.state === "connected",
    (error) => t("{0}可重试一键安装，或使用「选择安装位置」连接自行安装的 pdf2docx。", error));
  const groupError = groupItemError("pdf", wordCapability?.state === "connected");
  if (wordErrorText) {
    const error = document.createElement("div");
    error.className = "plugins-notice danger";
    error.textContent = wordErrorText;
    body.append(error);
  } else if (installNotice) {
    body.append(installNotice);
  } else if (groupError) {
    body.append(groupError);
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
  return body;
}

// ── 「PDF 组件」大类 ────────────────────────────────────────────────────────

const GROUP_ITEMS: PdfComponentsItem[] = ["pdf", "babeldoc", "ocr"];

function groupItemName(item: PdfComponentsItem): string {
  if (item === "pdf") return t("PDF 处理组件");
  if (item === "babeldoc") return t("段落重排组件");
  return t("文字识别组件");
}

/** 大类里的一项：序号代替大图标，标题小一号；data-component 供「安装 ↗ 组件」定位。 */
function itemShell(index: number, key: string): { body: HTMLElement; titleRow: HTMLElement; copy: HTMLElement; title: HTMLElement } {
  const body = document.createElement("div");
  body.className = "plugins-item";
  body.dataset.component = key;
  const titleRow = document.createElement("div");
  titleRow.className = "plugins-title-row";
  const num = document.createElement("span");
  num.className = "plugins-item-num";
  num.textContent = String(index);
  const copy = document.createElement("div");
  copy.className = "plugins-copy";
  const title = document.createElement("h3");
  titleRow.append(num, copy);
  body.append(titleRow);
  return { body, titleRow, copy, title };
}

function groupInstalling(): boolean {
  return Boolean(groupStatus?.install?.running);
}

/** 「全部安装」进行中时这一项的状态（等待安装 / 正在安装 / 安装失败）；不在这一轮里时 null。 */
function groupItemState(item: PdfComponentsItem): { label: string; tone: "idle" | "ok" | "warn" | "danger" } | null {
  const result = groupStatus?.install?.items?.[item];
  if (!result) return null;
  if (groupInstalling()) {
    if (result.status === "running") return { label: t("正在安装"), tone: "idle" };
    if (result.status === "pending") return { label: t("等待安装"), tone: "idle" };
    return null;
  }
  if (result.status === "failed" && groupStatus?.missing.includes(item)) return { label: t("安装失败"), tone: "danger" };
  return null;
}

/** 这一轮「全部安装」里这一项没装好的原因；装好了（或没轮到）时 null。 */
function groupItemError(item: PdfComponentsItem, installed: boolean): HTMLElement | null {
  const result = groupStatus?.install?.items?.[item];
  if (!result || groupInstalling() || installed || result.status !== "failed" || !result.error) return null;
  const box = document.createElement("div");
  box.className = "plugins-notice danger";
  box.textContent = item === "babeldoc"
    ? t("{0}已下载的文件会保留，点「重试」接着下载。没装好不影响翻译：可提取文字的页照旧转成 Word。", tx(result.error))
    : t("{0}可重试一键安装。", tx(result.error));
  return box;
}

function babeldocInstalling(): boolean {
  return Boolean(babeldocCapability?.install?.running);
}

function babeldocReady(): boolean {
  return Boolean(babeldocCapability?.installed) && !babeldocCapability?.needs_pdf_component;
}

function babeldocStatusLabel(): string {
  if (!babeldocCapability) return t("正在检测");
  if (babeldocInstalling()) return t("正在安装");
  if (babeldocCapability.installed) return babeldocCapability.needs_pdf_component ? t("需要 PDF 处理组件") : t("已安装");
  return t("未安装");
}

function babeldocStatusTone(): "idle" | "ok" | "warn" | "danger" {
  if (!babeldocCapability || babeldocInstalling()) return "idle";
  return babeldocReady() ? "ok" : "warn";
}

let babeldocPollTimer: number | null = null;

function stopBabeldocPolling(): void {
  if (babeldocPollTimer !== null) {
    window.clearTimeout(babeldocPollTimer);
    babeldocPollTimer = null;
  }
}

function pollBabeldocInstall(): void {
  stopBabeldocPolling();
  babeldocPollTimer = window.setTimeout(async () => {
    babeldocPollTimer = null;
    try {
      const client = new ApiClient();
      await client.connect();
      const progress = await client.babeldocInstallProgress();
      if (babeldocCapability) babeldocCapability = { ...babeldocCapability, install: progress };
      if (progress.running) {
        if (mounted) render();
        pollBabeldocInstall();
        return;
      }
      // 一键链路可能顺带装好了 PDF 处理组件：几项一起刷新。
      [babeldocCapability, wordCapability, groupStatus] = await Promise.all([
        client.getBabeldocCapability(), client.getPdf2DocxCapability(), client.getPdfComponentsStatus(),
      ]);
      babeldocErrorText = "";
      if (progress.phase === "done") {
        showToast({ message: t("段落重排组件已安装。PDF 智能混合的文字页会先用段落重排。") });
      } else if (progress.error) {
        showToast({ message: tx(progress.error), error: !progress.cancelled });
      }
    } catch (error) {
      babeldocErrorText = error instanceof Error ? error.message : t("插件状态读取失败。");
    }
    if (mounted) render();
  }, 600);
}

async function refreshBabeldoc(): Promise<void> {
  try {
    const client = new ApiClient();
    await client.connect();
    babeldocCapability = await client.getBabeldocCapability();
    babeldocErrorText = "";
    if (babeldocInstalling()) pollBabeldocInstall();
  } catch (error) {
    babeldocErrorText = error instanceof Error ? error.message : t("插件状态读取失败。");
  }
  if (mounted) render();
}

async function startBabeldocInstall(): Promise<void> {
  if (babeldocBusy || babeldocInstalling()) return;
  babeldocBusy = true;
  babeldocErrorText = "";
  render();
  try {
    const client = new ApiClient();
    await client.connect();
    const progress = await client.startBabeldocInstall();
    babeldocCapability = { ...(babeldocCapability ?? { state: "missing", installed: false }), install: progress };
    pollBabeldocInstall();
  } catch (error) {
    babeldocErrorText = error instanceof Error ? error.message : t("无法开始安装。");
    showToast({ message: babeldocErrorText, error: true });
  } finally {
    babeldocBusy = false;
    if (mounted) render();
  }
}

async function cancelBabeldocInstall(): Promise<void> {
  try {
    const client = new ApiClient();
    await client.connect();
    const progress = await client.cancelBabeldocInstall();
    if (babeldocCapability) babeldocCapability = { ...babeldocCapability, install: progress };
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : t("无法取消安装。"), error: true });
  }
  if (mounted) render();
}

async function openBabeldocProjectPage(): Promise<void> {
  try {
    const client = new ApiClient();
    await client.connect();
    const download = await client.officialBabeldocDownload();
    await openExternalUrl(download.url || download.authorization_url || OFFICIAL_BABELDOC_URL);
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : t("无法打开项目主页。"), error: true });
  }
}

async function removeBabeldocComponent(): Promise<void> {
  if (babeldocBusy) return;
  if (!window.confirm(t("卸载将删除已下载的段落重排组件，再次使用需重新下载。确定要卸载吗？"))) return;
  babeldocBusy = true;
  render();
  try {
    const client = new ApiClient();
    await client.connect();
    babeldocCapability = await client.uninstallBabeldocPlugin();
    groupStatus = await client.getPdfComponentsStatus();
    babeldocErrorText = "";
    showToast({ message: t("已卸载段落重排组件。可提取文字的页会照旧转成 Word 翻译。") });
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : t("无法卸载段落重排组件。"), error: true });
  } finally {
    babeldocBusy = false;
    if (mounted) render();
  }
}

function babeldocActionButtons(): HTMLElement {
  const actions = document.createElement("div");
  actions.className = "field-row plugins-actions";
  if (babeldocInstalling()) {
    actions.append(createButton({ label: t("取消安装"), onClick: () => void cancelBabeldocInstall() }));
    return actions;
  }
  const installed = Boolean(babeldocCapability?.installed);
  const failedInGroup = groupStatus?.install?.items?.babeldoc?.status === "failed";
  if (babeldocCapability?.one_click_supported !== false && (!installed || babeldocCapability?.outdated)) {
    actions.append(createButton({
      label: installed ? t("更新") : failedInGroup || babeldocCapability?.install?.phase === "failed" ? t("重试") : t("一键安装"),
      variant: "primary",
      disabled: babeldocBusy || !babeldocCapability || groupInstalling(),
      onClick: () => void startBabeldocInstall(),
    }));
  }
  if (!installed) actions.append(createButton({ label: t("项目主页"), onClick: () => void openBabeldocProjectPage() }));
  actions.append(createButton({ label: t("重新检测"), onClick: () => void refreshBabeldoc() }));
  if (installed) {
    actions.append(createButton({ label: t("卸载"), disabled: babeldocBusy, onClick: () => void removeBabeldocComponent() }));
  }
  return actions;
}

function renderBabeldocItem(index: number): HTMLElement {
  const { body, titleRow, copy, title } = itemShell(index, "babeldoc");
  title.textContent = t("段落重排组件");
  const betaTag = document.createElement("span");
  betaTag.className = "plugins-beta-tag";
  betaTag.textContent = t("Beta");
  title.append(betaTag);
  const sizeMb = megabytes(babeldocCapability?.download_bytes ?? 0);
  title.append(createHintBadge(t("在原 PDF 上按段落重排译文，由开源软件 BabelDOC（AGPL-3.0，未改动源码）在本机运行：公式、图和表格线原样保留，页数不变。每页译完会核对数字、字号、列表，看有没有漏翻、文字叠在一起，没通过的页改为转成 Word。译文由你设置的文本翻译模型给出，记忆库里「始终采用」的词条照样生效。一键安装：从 PyPI 下载固定版本的程序库，再下载它用的字体和版面识别模型（共约 {0} MB），逐个核对后安装到本软件的数据目录；缺少「PDF 处理组件」时先自动安装它。装好后 PDF「智能混合」的文字页先用它。项目主页：{1}", sizeMb, OFFICIAL_BABELDOC_URL)));
  const desc = document.createElement("p");
  desc.textContent = t("在原 PDF 上按段落重排译文，公式、图和表格线原样保留。装好后智能混合的文字页先用它，排得不好的页改为转成 Word。");
  copy.append(title, desc);
  const groupItem = groupItemState("babeldoc");
  if (groupItem) titleRow.append(createStatus(groupItem));
  else if (babeldocCapability) titleRow.append(createStatus({ label: babeldocStatusLabel(), tone: babeldocStatusTone() }));
  titleRow.append(babeldocActionButtons());

  const installNotice = componentInstallNotice(babeldocCapability?.install, Boolean(babeldocCapability?.installed),
    (error) => t("{0}已下载的文件会保留，点「重试」接着下载。没装好不影响翻译：可提取文字的页照旧转成 Word。", error),
    "babeldoc");
  const groupError = groupItemError("babeldoc", Boolean(babeldocCapability?.installed));
  const notice = document.createElement("div");
  if (babeldocErrorText) {
    notice.className = "plugins-notice danger";
    notice.textContent = babeldocErrorText;
  } else if (installNotice) {
    body.append(installNotice);
    return body;
  } else if (groupError) {
    body.append(groupError);
    return body;
  } else if (babeldocCapability?.installed && babeldocCapability.needs_pdf_component) {
    notice.className = "plugins-notice warn";
    notice.textContent = t("段落重排组件要用「PDF 处理组件」才能运行，请安装上面的「PDF 处理组件」。");
  } else if (babeldocCapability?.installed) {
    notice.className = "plugins-notice ok";
    notice.textContent = babeldocCapability.version
      ? t("段落重排组件已安装（BabelDOC {0}）。", babeldocCapability.version)
      : t("段落重排组件已安装。");
  } else if (babeldocCapability?.one_click_supported === false) {
    notice.className = "plugins-notice warn";
    notice.textContent = t("当前系统不能一键安装段落重排组件。{0}可提取文字的页照旧转成 Word 翻译。", tx(babeldocCapability.one_click_reason ?? ""));
  } else if (babeldocCapability) {
    notice.className = "plugins-notice warn";
    notice.textContent = babeldocCapability.needs_pdf_component
      ? t("尚未安装段落重排组件。一键安装会先自动安装上面的「PDF 处理组件」（共约 {0} MB）。没装时可提取文字的页照旧转成 Word。",
        megabytes((babeldocCapability.download_bytes ?? 0) + (babeldocCapability.dependency_bytes ?? 0)))
      : t("尚未安装段落重排组件，可一键安装（约 {0} MB）。没装时可提取文字的页照旧转成 Word。", sizeMb);
  } else {
    return body;
  }
  body.append(notice);
  return body;
}

let groupPollTimer: number | null = null;

function stopGroupPolling(): void {
  if (groupPollTimer !== null) {
    window.clearTimeout(groupPollTimer);
    groupPollTimer = null;
  }
}

async function refreshGroup(): Promise<void> {
  try {
    const client = new ApiClient();
    await client.connect();
    groupStatus = await client.getPdfComponentsStatus();
    if (groupInstalling()) pollGroupInstall();
  } catch (error) {
    console.debug("[PDF 组件] 读取状态失败", error); // i18n-ignore 只进控制台
  }
  if (mounted) render();
}

function pollGroupInstall(): void {
  stopGroupPolling();
  groupPollTimer = window.setTimeout(async () => {
    groupPollTimer = null;
    try {
      const client = new ApiClient();
      await client.connect();
      const progress = await client.pdfComponentsInstallProgress();
      if (groupStatus) groupStatus = { ...groupStatus, install: progress };
      if (progress.running) {
        if (mounted) render();
        pollGroupInstall();
        return;
      }
      [groupStatus, wordCapability, babeldocCapability, ocrCapability] = await Promise.all([
        client.getPdfComponentsStatus(), client.getPdf2DocxCapability(), client.getBabeldocCapability(),
        client.getOcrCapability(),
      ]);
      if (progress.phase === "done") {
        showToast({ message: t("PDF 组件已全部安装。") });
      } else if (progress.error) {
        // 有一项没装好：展开，把那一项和它的「重试」摆出来。
        if (!progress.cancelled) groupExpanded = true;
        showToast({ message: tx(progress.error), error: !progress.cancelled });
      }
    } catch (error) {
      showToast({ message: error instanceof Error ? error.message : t("插件状态读取失败。"), error: true });
    }
    if (mounted) render();
  }, 600);
}

async function startGroupInstall(): Promise<void> {
  if (groupBusy || groupInstalling()) return;
  groupBusy = true;
  render();
  try {
    const client = new ApiClient();
    await client.connect();
    const progress = await client.startPdfComponentsInstall();
    groupStatus = { ...(groupStatus ?? { missing: [], download_bytes: 0, one_click_supported: true, unsupported: [] }), install: progress };
    pollGroupInstall();
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : t("无法开始安装。"), error: true });
  } finally {
    groupBusy = false;
    if (mounted) render();
  }
}

async function cancelGroupInstall(): Promise<void> {
  try {
    const client = new ApiClient();
    await client.connect();
    const progress = await client.cancelPdfComponentsInstall();
    if (groupStatus) groupStatus = { ...groupStatus, install: progress };
  } catch (error) {
    showToast({ message: error instanceof Error ? error.message : t("无法取消安装。"), error: true });
  }
  if (mounted) render();
}

/** 哪几项装好了、能用：PDF 处理组件已连接；段落重排装好且有 PDF 处理组件；文字识别是新版识别模型。 */
function groupInstalledItems(): PdfComponentsItem[] {
  const done: PdfComponentsItem[] = [];
  if (wordCapability?.state === "connected") done.push("pdf");
  if (babeldocReady()) done.push("babeldoc");
  if (ocrCapability?.state === "connected" && !ocrCanUpdate() && !ocrCapability.needs_pdf_component) done.push("ocr");
  return done;
}

function anyItemInstalling(): boolean {
  return groupInstalling() || wordInstalling() || babeldocInstalling() || ocrInstalling();
}

function groupStatusChip(): HTMLElement | null {
  if (!wordCapability || !babeldocCapability || !ocrCapability) return null;
  if (anyItemInstalling()) return createStatus({ label: t("正在安装"), tone: "idle" });
  const count = groupInstalledItems().length;
  if (count === GROUP_ITEMS.length) return createStatus({ label: t("已安装"), tone: "ok" });
  if (count === 0) return createStatus({ label: t("未安装"), tone: "warn" });
  return createStatus({ label: t("已装 {0} / {1}", count, GROUP_ITEMS.length), tone: "warn" });
}

function groupActionButtons(): HTMLElement {
  const actions = document.createElement("div");
  actions.className = "field-row plugins-actions";
  if (groupInstalling()) {
    actions.append(createButton({ label: t("取消安装"), onClick: () => void cancelGroupInstall() }));
  } else if (groupStatus && groupStatus.missing.length > 0 && groupStatus.one_click_supported) {
    actions.append(createButton({
      label: t("全部安装（还需约 {0} MB）", megabytes(groupStatus.download_bytes)),
      variant: "primary",
      disabled: groupBusy || anyItemInstalling(),
      onClick: () => void startGroupInstall(),
    }));
  }
  const toggle = createButton({
    label: groupExpanded ? t("收起") : t("展开"),
    onClick: () => {
      groupExpanded = !groupExpanded;
      render();
    },
  });
  toggle.setAttribute("aria-expanded", String(groupExpanded));
  actions.append(toggle);
  return actions;
}

/** 「全部安装」进行中：正在装哪一项（第几项）、一共下载了多少。 */
function groupProgressNotice(): HTMLElement | null {
  const job = groupStatus?.install;
  if (!job?.running) return null;
  const [item, phase] = String(job.phase || "").split(":") as [PdfComponentsItem, string];
  const plan = Object.keys(job.items ?? {}) as PdfComponentsItem[];
  const position = Math.max(1, plan.indexOf(item) + 1);
  const box = document.createElement("div");
  box.className = "plugins-notice plugins-progress";
  const text = document.createElement("div");
  const name = GROUP_ITEMS.includes(item) ? groupItemName(item) : "";
  const percent = job.total > 0 ? Math.min(99, (job.done / job.total) * 100) : 0;
  if (!name) {
    text.textContent = t("正在准备安装…");
  } else if (phase === "download") {
    text.textContent = t("正在安装「{0}」（第 {1} / {2} 项）：已下载 {3} / {4} MB", name, position, plan.length,
      megabytes(job.done), megabytes(job.total));
  } else if (phase === "extract") {
    text.textContent = t("正在安装「{0}」（第 {1} / {2} 项）：正在解压…", name, position, plan.length);
  } else {
    text.textContent = t("正在安装「{0}」（第 {1} / {2} 项）：正在检查能否在本机运行…", name, position, plan.length);
  }
  box.append(text, createProgressBar({ percent }).root);
  return box;
}

function renderPdfGroupCard(): HTMLElement {
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
  title.textContent = t("PDF 组件");
  title.append(createHintBadge(t("PDF 翻译「智能混合」用到的三项组件，都只在本机运行，不上传文件。「全部安装」按顺序装上缺的几项：先装 PDF 处理组件（另外两项要用它里面的程序库），再装段落重排组件和文字识别组件；某一项没装好不影响其它项，展开后可以单独重试。每一项也可以展开后单独安装或卸载。")));
  const desc = document.createElement("p");
  desc.textContent = t("PDF 翻译「智能混合」用到的三项组件。不安装也能翻译，用不了的页由程序换一种方式。只在本机运行，不上传文件。");
  copy.append(title, desc);
  titleRow.append(iconBox, copy);
  const chip = groupStatusChip();
  if (chip) titleRow.append(chip);
  titleRow.append(groupActionButtons());
  body.append(titleRow);

  const progress = groupProgressNotice();
  if (progress) body.append(progress);

  if (!groupExpanded) {
    // 收起时一行看清三项装没装。
    const summary = document.createElement("div");
    summary.className = "plugins-group-summary";
    const installed = groupInstalledItems();
    for (const item of GROUP_ITEMS) {
      const chipBox = document.createElement("div");
      chipBox.className = "plugins-group-chip";
      const state = groupItemState(item);
      chipBox.append(createStatus(state ?? (installed.includes(item)
        ? { label: t("已安装"), tone: "ok" }
        : { label: t("未安装"), tone: "warn" })));
      const name = document.createElement("b");
      name.textContent = groupItemName(item);
      chipBox.append(name);
      if (item !== "pdf") {
        const beta = document.createElement("span");
        beta.className = "plugins-beta-tag";
        beta.textContent = t("Beta");
        chipBox.append(beta);
      }
      summary.append(chipBox);
    }
    body.append(summary);
  } else {
    const items = document.createElement("div");
    items.className = "plugins-items";
    items.append(renderWordItem(1), renderBabeldocItem(2), renderOcrItem(3));
    body.append(items);
  }
  card.append(body);
  return markComponentCard(card, "pdf");
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
  column.append(markComponentCard(card, "oda"));
  column.append(renderPdfGroupCard());

  rootEl.append(column);
}
