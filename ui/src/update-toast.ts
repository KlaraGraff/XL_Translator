// 顶部居中的更新提示卡片。
//
// 一张卡片走完整个生命周期——发现新版 → 下载 → 安装 → 等重启——而不是「弹个通知，
// 再去设置页里操作」。同一时刻永远只可能有一个更新在飞，中途也没有任何东西要配置，
// 拆成两个界面只会让人多跑一趟。状态本身在 update-controller.ts 里，和设置页
// 「更新与关于」共用，两边不可能对同一个问题给出不同答案。
//
// 有话说时才出现：启动后的自动检查在真的发现新版之前什么都不显示。手动检查额外会显示
// 「正在检查 / 已是最新」两个瞬态——点一下没有任何反应，读起来就像坏了。
//
// 不占布局高度：卡片浮在内容之上（见 app.css 的 .toast-stack），窗口内容不会被往下挤。
// 这一点和它取代的那条通栏提示条不同——那条会把整个工作区推下去 40 多像素。

import { icon } from "./icons";
import { renderReleaseNotes } from "./markdown";
import { toastStackSlot } from "./shell";
import {
  availableVersion,
  canSelfUpdate,
  collapseUpdateReady,
  ensureUpdaterEnvironment,
  ignoredVersion,
  requestRestart,
  requestUpdateInstall,
  selfUpdateBlockedCopy,
  subscribeUpdates,
  updateSnapshot,
} from "./update-controller";

type JsonObject = Record<string, unknown>;

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" ? value : fallback;
}

function formatBytes(value: unknown): string {
  const bytes = Math.max(0, num(value));
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

/** GitHub 的 published_at 是完整 ISO 时间戳，卡片上只要日期。 */
function formatReleaseDate(iso: string): string {
  const when = new Date(iso);
  if (!iso || Number.isNaN(when.getTime())) return "";
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`;
}

async function openExternalUrl(url: string): Promise<void> {
  if (!url) return;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("open_external_url", { url });
  } catch (error) {
    console.error("[update] 打不开外部链接：", error);
  }
}

// ---------------------------------------------------------------------------
// 卡片自己的状态（只影响这一张卡片怎么显示，不属于更新流程本身）
// ---------------------------------------------------------------------------

/** 用户对这个版本按过 ✕。只管这一次运行——下次启动照样提醒，因为更新确实还没装。
 *  要永久闭嘴走设置页的「忽略此版本」，那是写进配置的。 */
let dismissedVersion = "";
/** 安装失败的卡片被关掉了。重新点「更新」会重置。 */
let dismissedFailureCode = "";
let notesExpanded = false;
/** 「已是最新」这类瞬态的自动消失定时器。 */
let transientTimer = 0;
/** 手动检查刚刚查完——只有这种情况才值得说一句「已经是最新」。后台检查没查到东西时
 *  必须一声不吭：用户没问。 */
let showUpToDate = false;
/** 上一次重画时是否正在检查，用来认出「检查刚刚结束」这一刻。只有手动检查会把
 *  checking 置真（见 update-controller 的 runUpdateCheck），所以不需要另传标记。 */
let wasChecking = false;

const TRANSIENT_MS = 2600;

let host: HTMLElement | null = null;

/** 挂载一次即可；之后由 update-controller 的订阅驱动重画。 */
export function mountUpdateToast(): void {
  if (host) return;
  host = toastStackSlot("update");
  subscribeUpdates(render);
  // 主按钮写「更新」还是「下载安装包」取决于这台机器能不能自更新，卡片弹出来之前
  // 就得知道——探测很便宜，挂载时问一次。
  void ensureUpdaterEnvironment();
  render();
}

function clearTransient(): void {
  if (transientTimer) {
    window.clearTimeout(transientTimer);
    transientTimer = 0;
  }
}

// ---------------------------------------------------------------------------
// 拼装
// ---------------------------------------------------------------------------

type IconTone = "tint" | "ok" | "danger" | "mute";

function card(extraClass = ""): HTMLDivElement {
  const el = document.createElement("div");
  el.className = extraClass ? `utoast ${extraClass}` : "utoast";
  return el;
}

function head(options: {
  icon: "down" | "check" | "warn" | "spin";
  tone: IconTone;
  title: string;
  detail?: string;
  onDismiss?: () => void;
}): HTMLDivElement {
  const row = document.createElement("div");
  row.className = "ut-head";

  const mark = document.createElement("span");
  mark.className = `ut-ico ut-${options.tone}`;
  if (options.icon === "spin") {
    const ring = document.createElement("span");
    ring.className = "ut-spin";
    mark.append(ring);
  } else {
    mark.append(icon(options.icon, { size: "sm" }));
  }
  row.append(mark);

  const copy = document.createElement("span");
  copy.className = "ut-copy";
  const title = document.createElement("b");
  title.textContent = options.title;
  copy.append(title);
  if (options.detail) {
    const detail = document.createElement("span");
    detail.textContent = options.detail;
    copy.append(detail);
  }
  row.append(copy);

  if (options.onDismiss) {
    const close = document.createElement("button");
    close.type = "button";
    close.className = "ut-x";
    close.setAttribute("aria-label", "关闭");
    close.append(icon("close", { size: "sm" }));
    close.addEventListener("click", options.onDismiss);
    row.append(close);
  }
  return row;
}

function footer(children: HTMLElement[]): HTMLDivElement {
  const row = document.createElement("div");
  row.className = "ut-foot";
  for (const child of children) row.append(child);
  return row;
}

function linkButton(label: string, onClick: () => void, chevron?: "up" | "down"): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "ut-link";
  button.append(document.createTextNode(label));
  if (chevron) {
    const mark = icon("chev", { size: "sm" });
    mark.classList.add("ut-chev");
    if (chevron === "up") mark.classList.add("up");
    button.append(mark);
  }
  button.addEventListener("click", onClick);
  return button;
}

function actionButton(label: string, onClick: () => void, primary = false): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = primary ? "ut-btn pri" : "ut-btn";
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

function spacer(): HTMLSpanElement {
  const el = document.createElement("span");
  el.className = "ut-sp";
  return el;
}

// ---------------------------------------------------------------------------
// 各个态
// ---------------------------------------------------------------------------

function renderProgress(): HTMLDivElement {
  const { flow } = updateSnapshot();
  const downloading = flow.phase === "downloading";
  // 下载和安装都不给关闭按钮：updater 插件没有中止下载的接口，一个点了没反应
  // （或者更糟，只是把卡片藏起来而下载还在跑）的 ✕ 比没有 ✕ 更容易让人误判。
  const el = card();
  el.append(head({
    icon: "spin",
    tone: "tint",
    title: downloading ? `正在下载 ${flow.version || "新版本"}` : "正在校验签名并安装",
    detail: downloading ? "装完会告诉你，期间可以继续用" : "这一步通常几秒钟，请勿关闭窗口。",
  }));

  const prog = document.createElement("div");
  prog.className = "ut-prog";
  const bar = document.createElement("div");
  bar.className = downloading && flow.percent !== null ? "ut-bar" : "ut-bar indet";
  const fill = document.createElement("i");
  if (downloading && flow.percent !== null) {
    fill.style.width = `${Math.min(100, Math.max(0, flow.percent))}%`;
  }
  bar.append(fill);
  prog.append(bar);

  if (downloading) {
    const meta = document.createElement("div");
    meta.className = "ut-meta";
    const left = document.createElement("span");
    left.textContent = flow.total
      ? `${formatBytes(flow.received)} / ${formatBytes(flow.total)}`
      : `已下载 ${formatBytes(flow.received)}`;
    meta.append(left);
    if (flow.percent !== null) {
      const right = document.createElement("span");
      right.className = "r";
      right.textContent = `${Math.floor(flow.percent)}%`;
      meta.append(right);
    }
    prog.append(meta);
  }
  el.append(prog);
  return el;
}

function renderReady(): HTMLDivElement {
  const { flow } = updateSnapshot();
  const el = card();
  el.append(head({
    icon: "check",
    tone: "ok",
    title: `${flow.version || "新版本"} 已装好`,
    detail: "重启 Translator 后生效。有任务在跑就先跑完，不急。",
    onDismiss: collapseUpdateReady,
  }));
  el.append(footer([
    spacer(),
    actionButton("稍后", collapseUpdateReady),
    actionButton("立即重启", () => void requestRestart(), true),
  ]));
  return el;
}

function renderFailure(): HTMLDivElement {
  const { flow, result } = updateSnapshot();
  const downloadUrl = text(result?.download_url) || text(result?.release_url);
  const el = card();
  el.append(head({
    icon: "warn",
    tone: "danger",
    title: flow.failureTitle || "更新没装上",
    detail: flow.message,
    onDismiss: () => {
      dismissedFailureCode = flow.code || "dismissed";
      render();
    },
  }));
  const actions: HTMLElement[] = [];
  if (downloadUrl) {
    actions.push(linkButton("改为下载安装包", () => void openExternalUrl(downloadUrl)));
  }
  actions.push(spacer());
  actions.push(actionButton("重试", () => void requestUpdateInstall(), true));
  el.append(footer(actions));
  return el;
}

function renderAvailable(version: string): HTMLDivElement {
  const { result, env } = updateSnapshot();
  const payload: JsonObject = result ?? {};
  const current = text(payload.current_version);
  const releaseUrl = text(payload.release_url);
  const downloadUrl = text(payload.download_url);
  const size = num(payload.asset_size);
  const date = formatReleaseDate(text(payload.release_date));
  const selfUpdate = canSelfUpdate();

  const detail = [
    current ? `当前 ${current}` : "",
    date ? `${date} 发布` : "",
    size > 0 ? formatBytes(size) : "",
  ].filter(Boolean).join(" · ");

  const el = card();
  el.append(head({
    icon: "down",
    tone: "tint",
    title: `Translator ${version} 可用`,
    detail,
    onDismiss: () => {
      dismissedVersion = version;
      render();
    },
  }));

  const toggle = linkButton(
    notesExpanded ? "收起" : "展开完整说明",
    () => {
      notesExpanded = !notesExpanded;
      render();
    },
    notesExpanded ? "up" : "down",
  );
  // 先藏起来：有没有东西可展开要量过才知道（见下）。
  toggle.style.display = "none";

  // 更新说明来自 GitHub Release 正文。这一版没写、或者写的是一堆提交号解析不出东西时，
  // 不显示一个空面板——整块省掉，卡片退化成一行式提示。
  const notes = renderReleaseNotes(text(payload.release_notes));
  if (notes) {
    const wrap = document.createElement("div");
    wrap.className = "ut-notes";
    const label = document.createElement("div");
    label.className = "lbl";
    label.textContent = "本次更新";
    notes.classList.add("ut-rn");
    if (!notesExpanded) notes.classList.add("clamped");
    wrap.append(label, notes);
    el.append(wrap);
    // 夹到 3 行之后究竟有没有藏住东西，只有量过才知道；说明本来就只有两行时，
    // 一个点了什么也不会发生的「展开完整说明」比没有更糟。挂进文档才有尺寸，
    // 所以量测排到下一帧。
    requestAnimationFrame(() => {
      const clipped = notes.scrollHeight - notes.clientHeight > 2;
      toggle.style.display = clipped || notesExpanded ? "" : "none";
    });
  }

  const actions: HTMLElement[] = [toggle];
  if (releaseUrl) {
    actions.push(linkButton("在 GitHub 查看", () => void openExternalUrl(releaseUrl)));
  }
  actions.push(spacer());
  actions.push(selfUpdate
    ? actionButton("更新", () => void requestUpdateInstall(), true)
    : actionButton("下载安装包", () => void openExternalUrl(downloadUrl || releaseUrl), true));
  el.append(footer(actions));

  if (!selfUpdate && env) {
    // 按钮不能假装能用：装不了的时候主按钮已经改成「下载安装包」，这一行说清为什么。
    const note = document.createElement("div");
    note.className = "ut-blocked";
    note.textContent = selfUpdateBlockedCopy(env.reason);
    el.insertBefore(note, el.lastChild);
  }
  return el;
}

function renderSlim(tone: IconTone, mark: "spin" | "check", label: string): HTMLDivElement {
  const el = card("slim");
  el.append(head({ icon: mark, tone, title: label }));
  return el;
}

// ---------------------------------------------------------------------------
// 主渲染
// ---------------------------------------------------------------------------

function render(): void {
  if (!host) return;
  clearTransient();
  while (host.firstChild) host.removeChild(host.firstChild);

  const snapshot = updateSnapshot();
  const { flow } = snapshot;

  // 检查刚刚结束（真→假）：这一轮如果没有新版，下面要说一句「已经是最新」。
  // 同时清掉「这个版本我关过了」——手动检查就是在要一个回答，之前关掉卡片不算
  // 拒绝回答。不清的话，关过 9.4.0 再点检查会得到「已经是最新版本 9.3.0」，
  // 而设置页同一时刻写着「有可用更新」，两边打架。
  if (wasChecking && !snapshot.checking) {
    showUpToDate = true;
    dismissedVersion = "";
  }
  wasChecking = snapshot.checking;

  // 顺序即优先级：正在进行的安装流程压过一切「有新版」的提示——它就是那个新版。
  if (flow.phase === "downloading" || flow.phase === "installing") {
    host.append(renderProgress());
    return;
  }
  if (flow.phase === "ready" && !snapshot.readyCollapsed) {
    host.append(renderReady());
    return;
  }
  if (flow.phase === "failed" && dismissedFailureCode !== (flow.code || "dismissed")) {
    host.append(renderFailure());
    return;
  }

  if (snapshot.checking) {
    host.append(renderSlim("mute", "spin", "正在检查更新…"));
    return;
  }

  const version = availableVersion();
  if (version && dismissedVersion !== version) {
    showUpToDate = false;
    host.append(renderAvailable(version));
    return;
  }

  // 手动检查查完了但没有新版。只说一次，2.6 秒后自己消失——一条不会消失的
  // 「已经是最新」和一条什么都不说的空白同样没用，但前者还挡着内容。
  if (showUpToDate) {
    showUpToDate = false;
    const current = text(snapshot.result?.current_version);
    const ignored = ignoredVersion();
    const latest = text(snapshot.result?.latest_version);
    const label = latest && latest === ignored
      ? `已忽略版本 ${latest}`
      : `已经是最新版本${current ? ` ${current}` : ""}`;
    host.append(renderSlim("ok", "check", label));
    transientTimer = window.setTimeout(() => {
      transientTimer = 0;
      render();
    }, TRANSIENT_MS);
  }
}
