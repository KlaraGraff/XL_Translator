// 更新流程的单一状态源。
//
// 为什么单独一个模块：更新这件事现在有两个出口——启动后浮在顶部的提示卡片
// （update-toast.ts）和设置页的「更新与关于」（views/settings.ts）。两边如果各存
// 各的状态，就会出现「卡片说正在下载 62%，设置页说有可用更新，点一下又从头下一遍」
// 这种自相矛盾。所以状态和动作都收在这里，两个界面只负责画，以及订阅变化重画。
//
// 分工同 update-service.ts 顶部所述：「有没有新版」由后端 /api/updates/check 判定
// （它认识忽略版本、暂停提醒、发布包未就绪这些产品规则），「怎么装」才交给 updater
// 插件。这里不做第二次判断，只是把两者串起来。

import { ApiClient } from "./api-client";
import { openModal, showToast } from "./components";
import { setSettingsAlert } from "./shell";
import {
  resolveUpdate,
  restartApp,
  updaterEnvironment,
  type UpdateHandle,
  type UpdaterEnvironment,
} from "./update-service";

type JsonObject = Record<string, unknown>;

function record(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

// ---------------------------------------------------------------------------
// 状态
// ---------------------------------------------------------------------------

/** 安装流程的进行态。和 result（「服务器怎么说」）分开存：后者会被「重新检查」整个
 *  换掉，而一个已经装好、只差重启的更新不该因为用户又点了一次检查就消失。 */
export type UpdateFlowPhase = "idle" | "downloading" | "installing" | "ready" | "failed";

export interface UpdateFlow {
  phase: UpdateFlowPhase;
  version: string;
  /** 下载百分比；总长度未知时为 null（走不确定进度条）。 */
  percent: number | null;
  received: number;
  total: number | null;
  message: string;
  /** 诊断码：用户看不懂，但截图发过来时它是唯一有用的东西。 */
  code: string;
  /** 失败态的标题。下载断了就说「下载失败」，别一律报「安装失败」误导排查方向。 */
  failureTitle: string;
}

export function idleUpdateFlow(): UpdateFlow {
  return {
    phase: "idle", version: "", percent: null, received: 0, total: null,
    message: "", code: "", failureTitle: "",
  };
}

export interface UpdateSnapshot {
  /** /api/updates/state 的原样返回；null = 还没读过。 */
  prefs: JsonObject | null;
  /** /api/updates/check 的原样返回；null = 这次运行还没有检查结论。 */
  result: JsonObject | null;
  /** 正在检查中。手动检查会把这个态画出来，后台检查不会。 */
  checking: boolean;
  /** 最近一次检查完成的时刻（手动或后台都算）。 */
  checkedAt: string;
  flow: UpdateFlow;
  env: UpdaterEnvironment | null;
  /** 用户对「已装好，等重启」点过「稍后」。收起的是提示，不是事实。 */
  readyCollapsed: boolean;
}

const state: UpdateSnapshot = {
  prefs: null,
  result: null,
  checking: false,
  checkedAt: "",
  flow: idleUpdateFlow(),
  env: null,
  readyCollapsed: false,
};

export function updateSnapshot(): UpdateSnapshot {
  return state;
}

const listeners = new Set<() => void>();

/** 订阅状态变化。返回退订函数。 */
export function subscribeUpdates(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function emit(): void {
  for (const listener of [...listeners]) {
    try {
      listener();
    } catch (error) {
      // 一个订阅者画崩了不能拖累另一个——尤其是下载进度回调里，抛出去会直接中断下载。
      console.error("[update] 订阅者重画失败：", error);
    }
  }
}

// ---------------------------------------------------------------------------
// 读取
// ---------------------------------------------------------------------------

export function notificationsPaused(): boolean {
  return Boolean(record(state.prefs?.preferences).notifications_paused);
}

export function ignoredVersion(): string {
  return text(record(state.prefs?.preferences).ignored_release_version);
}

/**
 * 「现在该不该提示有新版」。返回版本号，空串表示不该提示。
 *
 * 被忽略的版本不算——这是用户明确说过不想再看到的那一个。
 */
export function availableVersion(): string {
  const result = state.result;
  if (!result || text(result.status) !== "available") return "";
  const latest = text(result.latest_version);
  if (!latest || latest === ignoredVersion()) return "";
  return latest;
}

export function canSelfUpdate(): boolean {
  return Boolean(state.env?.canSelfUpdate);
}

const SELF_UPDATE_BLOCKED_COPY: Record<string, string> = {
  running_from_dmg: "Translator 当前是从磁盘映像里运行的，没法自己替换自己。请先把它拖进「应用程序」，或直接下载新版安装包。",
  install_location_read_only: "Translator 所在目录没有写入权限，装不上更新。请把它移到「应用程序」，或直接下载新版安装包。",
  unsupported_architecture: "这台机器的处理器架构没有对应的应用内更新包，请直接下载安装包。",
  unsupported_platform: "当前系统不支持应用内更新，请直接下载安装包。",
  not_a_bundle: "没能定位到 Translator 的安装位置，应用内更新已停用。请直接下载安装包。",
  install_location_unknown: "没能定位到 Translator 的安装位置，应用内更新已停用。请直接下载安装包。",
  dev_build: "开发模式下不提供应用内更新。",
  browser_preview: "浏览器预览模式下不提供应用内更新。",
  environment_probe_failed: "没能确认这台机器是否支持应用内更新，请直接下载安装包。",
};

export function selfUpdateBlockedCopy(reason: string): string {
  return SELF_UPDATE_BLOCKED_COPY[reason] ?? "这台机器暂不支持应用内更新，请直接下载安装包。";
}

// ---------------------------------------------------------------------------
// 后端连接
// ---------------------------------------------------------------------------

const client = new ApiClient();
let connectPromise: Promise<void> | null = null;

async function ensureConnected(): Promise<void> {
  if (!connectPromise) {
    const attempt = client.connect().catch((error) => {
      // 握手失败不缓存，下一次调用重来一次；否则一次开机时序抖动会让更新检查
      // 在整个进程生命周期里永久失效。
      if (connectPromise === attempt) connectPromise = null;
      throw error;
    });
    connectPromise = attempt;
  }
  return connectPromise;
}

/** 读一次更新偏好（暂停提醒 / 忽略版本）。已经读过就不重复读。 */
export async function loadUpdateState(force = false): Promise<void> {
  if (state.prefs && !force) return;
  await ensureConnected();
  state.prefs = await client.request<JsonObject>("/api/updates/state");
  emit();
}

export async function ensureUpdaterEnvironment(): Promise<void> {
  if (state.env) return;
  state.env = await updaterEnvironment();
  emit();
}

// ---------------------------------------------------------------------------
// 检查
// ---------------------------------------------------------------------------

/** 检查结果 → 侧栏红点。忽略过的版本不点亮。 */
function applyAvailability(): boolean {
  const available = Boolean(availableVersion());
  setSettingsAlert(available);
  return available;
}

/**
 * 手动检查（设置页的「检查更新」，以及提示卡片上的「重试」）。
 * 后端对 manual 不做任何拦截：用户明确问了，就一定去问一次。
 */
export async function runUpdateCheck(): Promise<void> {
  state.checking = true;
  emit();
  try {
    await ensureConnected();
    const result = await client.request<JsonObject>("/api/updates/check?mode=manual");
    state.result = result;
    state.checkedAt = new Date().toISOString();
    // 手动检查的结论覆盖上一次安装尝试留下的失败态：用户明确要求重新问一次。
    if (state.flow.phase === "failed") state.flow = idleUpdateFlow();
    applyAvailability();
  } catch (error) {
    showToast({ message: errorMessage(error), error: true });
  } finally {
    state.checking = false;
    emit();
  }
}

/**
 * 启动后的一次后台检查（app.ts 在首屏之后延迟调用）。
 *
 * 每次启动查一次，应用不关就不再查——没有时间节流。曾经有过一个「24 小时内查过就跳过」
 * 的规则，它缓存的其实是「结论」而不是「请求」：一次刚好发生在发版前的检查，会把之后
 * 一整天的启动全部静默跳过，而跳过在界面上完全看不出来。这个工具一天开不了几次，
 * 一个 GET 请求从来不值得省。
 *
 * 该不该「提示」由 notifications_paused / ignored_release_version 决定，后端判。
 */
export async function runBackgroundUpdateCheck(): Promise<void> {
  try {
    await ensureConnected();
    await loadUpdateState();
    const result = await client.request<JsonObject>("/api/updates/check?mode=background");
    // 快速开始向导还没走完 / 用户暂停了提醒：这次不算检查过，界面维持原样。
    if (text(result.status) === "deferred") return;
    state.result = result;
    state.checkedAt = new Date().toISOString();
    applyAvailability();
    emit();
  } catch {
    // 后台检查失败不打扰用户：这一次没查到，下次启动再说。手动检查会把错误报出来。
  }
}

// ---------------------------------------------------------------------------
// 偏好
// ---------------------------------------------------------------------------

async function putPreferences(body: JsonObject): Promise<void> {
  await ensureConnected();
  state.prefs = await client.request<JsonObject>("/api/updates/preferences", {
    method: "PUT",
    body: JSON.stringify(body),
  });
  applyAvailability();
  emit();
}

export async function setNotificationsPaused(paused: boolean): Promise<void> {
  await putPreferences({ notifications_paused: paused });
  showToast({
    message: paused ? "已暂停后台更新提醒；手动检查仍然可用。" : "已恢复后台更新提醒。",
  });
}

export async function ignoreVersion(version: string): Promise<void> {
  await putPreferences({ ignored_release_version: version });
  showToast({ message: `已忽略版本 ${version}；后续版本仍会提示。` });
}

// ---------------------------------------------------------------------------
// 下载 / 安装 / 重启
// ---------------------------------------------------------------------------

/** 「下载并安装」/「立即重启」进行中。两者都会先 await 一次任务数刷新才弹确认框，
 *  那段空档里再点一次就会叠出第二个确认框、第二次下载。 */
let actionBusy = false;

async function activeTaskCount(): Promise<number> {
  try {
    await ensureConnected();
    const list = await client.listTasks();
    return list.active.length;
  } catch {
    return 0;
  }
}

/**
 * 点「更新」。Windows 上安装会直接结束当前进程（NSIS 接手后重开应用），所以必须在
 * 下载之前就把「会打断正在跑的任务」这件事讲清楚；macOS 是就地替换 .app，装完还能
 * 继续用，那道确认留到「立即重启」再问。
 */
export async function requestUpdateInstall(): Promise<void> {
  if (actionBusy) return;
  actionBusy = true;
  try {
    if (state.env?.installBehavior === "installer_restart") {
      const running = await activeTaskCount();
      const body = running > 0
        ? [
          `现在安装会中断这 ${running} 个任务，已经翻好的部分会保留在任务中心，未完成的部分需要重新开始。`,
          "安装程序会在更新完成后自动重新打开 Translator。",
        ]
        : ["安装过程中 Translator 会关闭，安装程序完成后会自动重新打开它。"];
      const confirmed = await confirmModal({
        title: running > 0 ? `还有 ${running} 个任务正在运行` : "安装将关闭 Translator",
        body,
        confirmLabel: "仍然安装",
        cancelLabel: "暂不安装",
      });
      if (!confirmed) return;
    }
    await startInstall();
  } finally {
    actionBusy = false;
  }
}

/** 总长度未知时的重画间隔：够 formatBytes 的显示值真的变一次，又不至于刷爆页面。 */
const INDETERMINATE_REDRAW_BYTES = 512 * 1024;

async function startInstall(): Promise<void> {
  const fallbackVersion = text(state.result?.latest_version);
  state.readyCollapsed = false;
  state.flow = { ...idleUpdateFlow(), phase: "downloading", version: fallbackVersion };
  emit();

  let handle: UpdateHandle | null = null;
  // 出错时用来决定说「取不到更新信息」「下载失败」还是「安装失败」——三者的下一步
  // 动作完全不同：第一种多半是这一版根本没发更新包（重试永远不会成功），第二种重试
  // 或换网络，第三种多半是磁盘/权限/签名，重试没用。
  let stage: "resolve" | "download" | "install" = "resolve";
  try {
    handle = await resolveUpdate();
    if (!handle) {
      // latest.json 说没有可装的东西，但 GitHub Release 明明有新版：多半是这一版
      // 还没上传更新产物。别让用户干等，直接指回手动安装包。
      state.flow = {
        ...idleUpdateFlow(), phase: "failed", version: fallbackVersion,
        failureTitle: "无法自动更新",
        message: "更新服务器上没有这个平台的应用内更新包，请改为下载安装包。",
        code: "updater_payload_missing",
      };
      return;
    }
    stage = "download";
    const version = handle.version;
    // 每个数据块都重画一次会把界面刷爆；整数百分比变了才值得重画。
    let renderedPercent = -1;
    // 服务器没给 Content-Length 时百分比恒为 null，按百分比节流等于永不重画，
    // 「已下载 x MB」会一直停在 0 —— 那种情况下改按下载字节节流。
    let renderedReceived = 0;
    state.flow = { ...state.flow, version };
    await handle.download((percent, received, total) => {
      state.flow = {
        phase: "downloading", version, percent, received, total,
        message: "", code: "", failureTitle: "",
      };
      const step = percent === null ? -1 : Math.floor(percent);
      const changed = percent === null
        ? received - renderedReceived >= INDETERMINATE_REDRAW_BYTES
        : step !== renderedPercent;
      if (changed) {
        renderedPercent = step;
        renderedReceived = received;
        emit();
      }
    });
    stage = "install";
    state.flow = { ...state.flow, phase: "installing", percent: null };
    emit();
    await handle.install();
    state.flow = { ...idleUpdateFlow(), phase: "ready", version };
  } catch (error) {
    state.flow = {
      ...idleUpdateFlow(), phase: "failed",
      version: handle?.version || fallbackVersion,
      failureTitle:
        stage === "resolve" ? "无法自动更新" : stage === "download" ? "下载失败" : "安装失败",
      message:
        // 「取不到更新信息」不能说成「下载失败」：那句话会让人一遍遍重试一件不可能
        // 成功的事——这一版没有提供应用内更新包时，重试到下一个版本发布为止都是失败。
        stage === "resolve"
          ? "没有取到这一版的更新信息：可能是网络不通，也可能是这一版没有提供应用内更新包。可以稍后再试，或改为下载安装包。当前版本没有被改动，可以正常继续使用。"
          : stage === "download"
            ? "安装包没有下载完，当前版本没有被改动，可以正常继续使用。"
            : "更新没有安装成功，当前版本没有被改动，可以正常继续使用。",
      code: updaterDiagnosticCode(error),
    };
  } finally {
    if (handle) {
      // 安装成功后句柄已经被消费掉，close() 报错属于正常路径。
      try { await handle.close(); } catch { /* 忽略 */ }
    }
    emit();
  }
}

/**
 * 把更新器抛出的原始错误压成一个短诊断码。原文往往是一整段英文（有时还带本机路径），
 * 直接摆在界面上既看不懂也可能泄露目录结构；完整信息进控制台。
 */
function updaterDiagnosticCode(error: unknown): string {
  console.error("[updater] 安装失败：", error);
  const raw = errorMessage(error).toLowerCase();
  if (raw.includes("signature")) return "install_signature_rejected";
  if (raw.includes("permission") || raw.includes("denied")) return "install_permission_denied";
  if (raw.includes("space")) return "install_no_disk_space";
  if (raw.includes("network") || raw.includes("timed out") || raw.includes("timeout")) return "download_network_error";
  return "install_failed";
}

/** 「立即重启」：会杀掉正在翻译的任务，必须挡一道，而且默认按钮是「稍后重启」。 */
export async function requestRestart(): Promise<void> {
  if (actionBusy) return;
  actionBusy = true;
  try {
    const running = await activeTaskCount();
    if (running > 0) {
      const confirmed = await confirmModal({
        title: `还有 ${running} 个任务正在运行`,
        body: [
          "现在重启会中断它们，已经翻好的部分会保留在任务中心，未完成的部分需要重新开始。",
          "更新已经装好了，下次正常退出再打开也会生效——不重启不会丢掉这次更新。",
        ],
        confirmLabel: "仍然重启",
        cancelLabel: "稍后重启",
      });
      if (!confirmed) return;
    }
    try {
      await restartApp();
    } catch (error) {
      showToast({ message: `无法自动重启，请手动退出并重新打开 Translator。（${errorMessage(error)}）`, error: true });
    }
  } finally {
    actionBusy = false;
  }
}

/** 「稍后」：收起「已装好」的提示，但更新包已经在磁盘上，重启入口必须继续留着。 */
export function collapseUpdateReady(): void {
  state.readyCollapsed = true;
  setSettingsAlert(false);
  emit();
}

/**
 * openModal 的 Promise 包装。默认按钮（视觉上的主按钮）永远是「不做那件事」的那个：
 * 这些确认框挡的都是会打断用户工作的操作。
 */
function confirmModal(options: {
  title: string;
  body: string[];
  confirmLabel: string;
  cancelLabel: string;
}): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (value: boolean) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    openModal({
      tone: "warn",
      icon: "warn",
      title: options.title,
      body: options.body,
      actions: [
        { label: options.confirmLabel, onClick: () => settle(true) },
        { label: options.cancelLabel, variant: "primary", onClick: () => settle(false) },
      ],
    });
  });
}
