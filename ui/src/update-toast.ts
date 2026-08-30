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

import { openModal } from "./components";
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

/** 下载/安装那一张卡片上会变的节点。
 *
 *  下载期间每涨 1% 就来一次重画通知（节流在 update-controller 里）。整张卡片拆了重建
 *  的话，`.utoast` 的入场动画（淡入 + 上移 8px）会跟着重放一遍——看上去就是进度条每
 *  跳一格闪一下、卡片往上蹿一下，转圈图标也从头开始转。所以进度态改成就地更新：节点
 *  从头到尾是同一批，只改文字和宽度。 */
interface ProgressView {
  root: HTMLDivElement;
  title: HTMLElement;
  detail: HTMLElement;
  bar: HTMLDivElement;
  fill: HTMLElement;
  meta: HTMLDivElement;
  left: HTMLElement;
  right: HTMLElement;
}

let progressView: ProgressView | null = null;

function buildProgress(): ProgressView {
  // 下载和安装都不给关闭按钮：updater 插件没有中止下载的接口，一个点了没反应
  // （或者更糟，只是把卡片藏起来而下载还在跑）的 ✕ 比没有 ✕ 更容易让人误判。
  // 标题和说明的占位文字由 patchProgress 立刻覆盖——建好就补，挂进文档之前就是对的。
  const root = card();
  const headRow = head({ icon: "spin", tone: "tint", title: "…", detail: "…" });
  root.append(headRow);

  const prog = document.createElement("div");
  prog.className = "ut-prog";
  const bar = document.createElement("div");
  bar.className = "ut-bar";
  const fill = document.createElement("i");
  bar.append(fill);
  // 百分比那一格在安装阶段是空的，但节点始终留着——进度态里增删节点等于让卡片改高度，
  // 那正是要消掉的「跳一下」。
  const meta = document.createElement("div");
  meta.className = "ut-meta";
  const left = document.createElement("span");
  const right = document.createElement("span");
  right.className = "r";
  meta.append(left, right);
  prog.append(bar, meta);
  root.append(prog);

  const copy = headRow.querySelector(".ut-copy") as HTMLElement;
  return {
    root,
    title: copy.querySelector("b") as HTMLElement,
    detail: copy.querySelector("span") as HTMLElement,
    bar,
    fill,
    meta,
    left,
    right,
  };
}

function patchProgress(view: ProgressView): void {
  const { flow } = updateSnapshot();
  const downloading = flow.phase === "downloading";
  const determinate = downloading && flow.percent !== null;

  view.title.textContent = downloading
    ? `正在下载 ${flow.version || "新版本"}`
    : "正在校验签名并安装";
  // 「可以继续用」只对下载阶段成立——安装会把程序文件换掉,装好之后新任务
  // 可能失败(高-12),那句话由 renderReady 的警示文案接手。
  view.detail.textContent = downloading
    ? "下载期间可以继续用，装好后会提醒你重启"
    : "这一步通常几秒钟，请勿关闭窗口。";

  const barClass = determinate ? "ut-bar" : "ut-bar indet";
  if (view.bar.className !== barClass) view.bar.className = barClass;
  // 不确定态的宽度归 CSS 管（.ut-bar.indet i 是固定 34% 的滑块），这里必须把内联宽度清掉。
  view.fill.style.width = determinate
    ? `${Math.min(100, Math.max(0, flow.percent ?? 0))}%`
    : "";

  // hidden 属性在这里不管用：.ut-meta 自己写了 display: flex，作者样式压过 UA 的 [hidden]。
  view.meta.style.display = downloading ? "" : "none";
  if (downloading) {
    view.left.textContent = flow.total
      ? `${formatBytes(flow.received)} / ${formatBytes(flow.total)}`
      : `已下载 ${formatBytes(flow.received)}`;
    view.right.textContent = flow.percent !== null ? `${Math.floor(flow.percent)}%` : "";
  }
}

function renderReady(): HTMLDivElement {
  const { flow } = updateSnapshot();
  const el = card();
  el.append(head({
    icon: "check",
    tone: "ok",
    title: `${flow.version || "新版本"} 已装好`,
    // 不能安抚:程序文件已被替换,正在跑的任务不受影响,但新任务的组件加载可能
    // 失败且报错误导(高-12)。
    detail: "请尽快重启完成更新，期间新任务可能失败。",
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

  const snapshot = updateSnapshot();
  const { flow } = snapshot;

  // 检查刚刚结束（真→假）：这一轮如果真的查成了且没有新版，下面要说一句「已经是最新」。
  // 同时清掉「这个版本我关过了」——手动检查就是在要一个回答，之前关掉卡片不算
  // 拒绝回答。不清的话，关过 9.4.0 再点检查会得到「已经是最新版本 9.3.0」，
  // 而设置页同一时刻写着「有可用更新」，两边打架。
  // 用 lastCheckOk（这一轮检查本身成没成）而不是 result.status 来判断「查完了」：
  // 后端把网络故障/超时包成 200 + { status: "error" } 是一条路径，但请求本身抛异常
  // （sidecar 未就绪、超时、5xx）时 runUpdateCheck 根本不会碰 result，若只看
  // result.status，界面会拿上一轮成功检查留下的旧值（甚至首次启动时的 undefined）
  // 误判成「这次也查完了、没有新版」，把绿色对勾和刚弹出的失败 toast 一起画出来。
  if (wasChecking && !snapshot.checking && snapshot.lastCheckOk === true) {
    showUpToDate = true;
    dismissedVersion = "";
  }
  wasChecking = snapshot.checking;

  // 顺序即优先级：正在进行的安装流程压过一切「有新版」的提示——它就是那个新版。
  // 卡片已经在了就只改数字，不重建（见 ProgressView 的注释）。
  const inProgress = flow.phase === "downloading" || flow.phase === "installing";
  if (inProgress && progressView && progressView.root.parentElement === host) {
    patchProgress(progressView);
    return;
  }

  clearTransient();
  progressView = null;
  while (host.firstChild) host.removeChild(host.firstChild);

  if (inProgress) {
    progressView = buildProgress();
    patchProgress(progressView);
    host.append(progressView.root);
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

// ---------------------------------------------------------------------------
// 「更新装好了还没重启」的新建任务拦截(高-12)
// ---------------------------------------------------------------------------

/**
 * 更新落地后第一次新建任务时弹的提醒。搭配 update-controller 的
 * consumePendingRestartWarning() 使用:调用方先 consume,拿到 true 再调这里。
 * 「立即重启」走正常的重启完成更新;「仍要开始」把原始动作接着跑——拦截的目的
 * 是让用户知情,不是禁止。
 */
export function openRestartBeforeTaskModal(onProceed: () => void): void {
  openModal({
    tone: "warn",
    icon: "warn",
    title: "更新已安装，建议先重启",
    body: [
      "新版本的程序文件已经替换完成，当前运行的还是旧版本。",
      "正在进行的任务不受影响；但此时新建任务可能在加载组件时失败，而且报错原因会带偏方向。建议先重启完成更新，再开始新任务。",
    ],
    actions: [
      // 「取消」给反悔留门：既不想现在重启、也不想开这个任务（比如想先换文件）。
      // 警告在弹窗前已被 consume，取消后再点开始不会二次弹——用户已经知情。
      { label: "取消", variant: "default" },
      { label: "仍要开始", variant: "default", onClick: onProceed },
      { label: "立即重启", variant: "primary", onClick: () => void requestRestart() },
    ],
  });
}
