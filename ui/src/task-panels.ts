import { createButton, createProgressBar, type StatusTone } from "./components";
import { t, tx } from "./i18n";
import { icon, type IconName } from "./icons";
import { entranceElapsed } from "./motion";

/** Shared visual building blocks for every translator task surface. */
export type TaskPanelState = "running" | "pausing" | "paused" | "stopping" | "finalizing" | "done" | "completed_with_issues" | "error" | "stopped" | "interrupted";

export interface TaskPipeStep {
  label: string;
  state: "done" | "now" | "todo";
}

export interface TaskRunBoxOptions {
  /** 流水线：任务事件里的真实阶段（读取文件 / 翻译 / 调整行高 / 生成文件），没有阶段数的任务（CAD、扩展类型）不给。 */
  steps?: TaskPipeStep[];
  /** 当前阶段名。没有流水线时写成标题前的小标签，有流水线时由流水线表达。 */
  stage?: string;
  /** 这一阶段里具体在做什么（「正在查找记忆库中已有的译文…」「第 1 / 3 个文件」）。 */
  phase?: string;
  percent?: number | null;
  state?: TaskPanelState | string;
  monitor?: HTMLElement[];
  note?: string;
  /** 进度条下面的折叠块（运行日志），由调用方建好放进来。 */
  footer?: HTMLElement;
}

/** .runbox：进行中的状态区。流水线、阶段文字 + 百分比、进度条、监控芯片，最后是运行日志。 */
export function createTaskRunBox(options: TaskRunBoxOptions): HTMLElement {
  const box = document.createElement("div");
  const paused = options.state === "paused" || options.state === "pausing";
  box.className = paused ? "runbox paused" : "runbox";
  if (options.steps?.length) box.append(createPipeline(options.steps));

  const head = document.createElement("div");
  head.className = "prog";
  const title = document.createElement("b");
  const stage = options.stage?.trim() ?? "";
  const phase = options.phase?.trim() ?? "";
  // 有流水线时阶段名已经在上面亮着，标题只说具体在做什么；没有流水线时阶段名做成小标签放在前面，
  // 四个翻译页同一个格式。
  if (!options.steps?.length && stage && phase && phase !== stage) {
    const step = document.createElement("span");
    step.className = "prog-step";
    step.textContent = tx(stage);
    title.append(step, document.createTextNode(tx(phase)));
  } else {
    title.textContent = phase || stage ? tx(phase || stage) : t("正在准备任务");
  }
  const pct = document.createElement("span");
  pct.className = "pct";
  const percent = typeof options.percent === "number" && Number.isFinite(options.percent) ? Math.round(options.percent) : null;
  pct.textContent = percent === null ? t("处理中") : `${percent}%`;
  head.append(title, pct);
  box.append(head, createProgressBar({ percent: percent ?? 0, tone: paused ? "warn" : "accent" }).root);
  if (options.note) {
    const note = document.createElement("p");
    note.className = "run-note";
    note.textContent = tx(options.note);
    box.append(note);
  }
  if (options.monitor?.length) {
    const monitor = document.createElement("div");
    monitor.className = "mon";
    monitor.append(...options.monitor);
    box.append(monitor);
  }
  if (options.footer) box.append(options.footer);
  return box;
}

function createPipeline(steps: TaskPipeStep[]): HTMLElement {
  const pipe = document.createElement("div");
  pipe.className = "pipe";
  steps.forEach((item, index) => {
    if (index > 0) {
      const link = document.createElement("span");
      // 连线跟着它右边那一步：那一步已完成就是实线，正在做就有一道光往右走。
      link.className = item.state === "todo" ? "seglink" : `seglink is-${item.state}`;
      pipe.append(link);
    }
    const step = document.createElement("span");
    step.className = item.state === "todo" ? "step" : `step is-${item.state}`;
    const dot = document.createElement("span");
    dot.className = "pd";
    if (item.state === "done") dot.append(icon("check"));
    step.append(dot, document.createTextNode(item.label));
    pipe.append(step);
  });
  return pipe;
}

export interface TaskLogEntry {
  seq?: number;
  time?: string;
  message?: string;
  level?: string;
}

export interface TaskLogOptions {
  entries?: TaskLogEntry[];
  limit?: number;
}

/** .log：运行日志的逐行内容，放在进行中状态区的「运行日志」折叠块里。 */
export function createTaskLogLines(options: TaskLogOptions): HTMLElement {
  const log = document.createElement("div");
  log.className = "log";
  const entries = options.entries ?? [];
  for (const entry of entries.slice(-(options.limit ?? 200))) {
    const line = document.createElement("div");
    if (entry.seq !== undefined) line.dataset.seq = String(entry.seq);
    const time = document.createElement("span");
    time.className = "t";
    time.textContent = entry.time ?? "";
    const message = document.createElement("span");
    const level = (entry.level ?? "INFO").toUpperCase();
    if (level === "ERROR" || level === "WARN") message.className = "w";
    else if (level === "OK" || level === "SUCCESS") message.className = "g";
    message.textContent = tx(entry.message ?? "");
    line.append(time, message);
    log.append(line);
  }
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.textContent = t("等待任务开始…");
    log.append(empty);
  }
  return log;
}

export interface TaskListPanelOptions {
  title?: string;
  className?: string;
  header?: (head: HTMLElement) => void;
}

/** Shared task-list shell: the same card/header geometry on every translator page. */
export function createTaskListPanel(options: TaskListPanelOptions = {}): HTMLElement {
  const card = document.createElement("section");
  card.className = `card tablecard task-list-panel${options.className ? ` ${options.className}` : ""}`;
  const head = document.createElement("div");
  head.className = "tc-head";
  const title = document.createElement("b");
  title.textContent = options.title ?? t("任务清单");
  head.append(title);
  options.header?.(head);
  card.append(head);
  return card;
}

export interface TaskControlFootOptions {
  /** variant 默认主色；完成后的「再翻译一次」用普通按钮，图标换成 restart。 */
  idle: { label: string; disabled?: boolean; onClick: () => void; icon?: IconName; variant?: "primary" | "default" };
  active?: {
    paused: boolean;
    /** 已请求暂停、正在等当前单元（CAD 是已发出的这批请求）收尾：暂停按钮变成不可点的「正在暂停…」。 */
    pausing?: boolean;
    onResume: () => void;
    /** 不支持暂停的任务（Excel / Word）不传，底栏只留「安全停止」。 */
    onPause?: () => void;
    onStop: () => void;
    finishPaused?: { label: string; onClick: () => void; note?: string };
    pauseLabel?: string;
    stopLabel?: string;
  };
}

export function createTaskControlFoot(options: TaskControlFootOptions): HTMLElement {
  const foot = document.createElement("div");
  foot.className = "rp-foot";
  if (!options.active) {
    foot.append(createButton({
      label: options.idle.label,
      icon: options.idle.disabled ? undefined : options.idle.icon ?? "play",
      variant: options.idle.variant ?? "primary",
      size: "big",
      disabled: options.idle.disabled,
      onClick: options.idle.onClick,
    }));
    return foot;
  }
  const active = options.active;
  if (active.paused) {
    foot.append(createButton({ label: t("继续翻译"), icon: "play", variant: "primary", size: "big", onClick: active.onResume }));
    if (active.finishPaused) {
      foot.append(createButton({ label: active.finishPaused.label, icon: "stop", size: "big", onClick: active.finishPaused.onClick }));
      if (active.finishPaused.note) {
        const note = document.createElement("div");
        note.className = "ws-note";
        note.style.textAlign = "center";
        note.textContent = active.finishPaused.note;
        foot.append(note);
      }
    } else {
      // 没有「结束任务」的（CAD）暂停后也要能停：后端 stop() 会先放开暂停再停下。
      foot.append(createButton({ label: active.stopLabel ?? t("停止"), icon: "stop", variant: "danger", size: "big", onClick: active.onStop }));
    }
    return foot;
  }
  if (active.pausing) {
    foot.append(createButton({ label: t("正在暂停…"), icon: "pause", size: "big", disabled: true, onClick: () => undefined }));
  } else if (active.onPause) {
    foot.append(createButton({ label: active.pauseLabel ?? t("暂停"), icon: "pause", size: "big", onClick: active.onPause }));
  }
  foot.append(createButton({ label: active.stopLabel ?? t("停止"), icon: "stop", variant: "danger", size: "big", onClick: active.onStop }));
  return foot;
}

export interface TaskResultOptions {
  title: string;
  subtitle?: string;
  /** 绿（全部产出）/ 橙（有需要人处理的事）/ 红（没能产出），与 finishTask 的判断同一份。 */
  tone?: "ok" | "warn" | "fail";
  actions?: HTMLElement[];
  onClose?: () => void;
  /** 结果卡左边那个勾的弹出只播一次：slot 是卡片所在的位置，identity 是这张卡说的那次结果。
   *  不给就每次创建都播（整块重画时会再弹一次）。见 motion.ts entranceElapsed。 */
  entrance?: { slot: string; identity: string };
}

/** 勾弹出（--t-slow）加上勾线描出来（.15s 起、.5s）的总时长，同 workspace.css .res .mk。 */
const RESULT_MARK_MS = 650;

/** .res：已完成的状态区。左边勾（或感叹号）、结论和一句小结，右边是后续动作。 */
export function createTaskResultCard(options: TaskResultOptions): HTMLElement {
  const tone = options.tone ?? "ok";
  const card = document.createElement("div");
  card.className = tone === "ok" ? "res" : `res ${tone}`;
  card.setAttribute("role", "status");
  const mark = document.createElement("span");
  mark.className = "mk";
  if (options.entrance) {
    const elapsed = entranceElapsed(options.entrance.slot, options.entrance.identity, RESULT_MARK_MS);
    if (elapsed === null) card.classList.add("settled");
    else card.style.setProperty("--res-elapsed", `${Math.round(elapsed)}ms`);
  }
  mark.append(icon(tone === "ok" ? "check" : "warn"));
  const body = document.createElement("div");
  body.className = "tx";
  const title = document.createElement("b");
  title.textContent = options.title;
  body.append(title);
  if (options.subtitle) {
    const sub = document.createElement("div");
    sub.className = "sub";
    sub.textContent = options.subtitle;
    body.append(sub);
  }
  card.append(mark, body);
  if (options.actions?.length) {
    const acts = document.createElement("div");
    acts.className = "acts";
    acts.append(...options.actions);
    card.append(acts);
  }
  if (options.onClose) {
    const close = document.createElement("button");
    close.type = "button";
    close.className = "res-close";
    close.setAttribute("aria-label", t("关闭"));
    close.append(icon("close", { size: "sm" }));
    close.addEventListener("click", options.onClose);
    card.append(close);
  }
  return card;
}

export function taskStatusTone(state: string): StatusTone {
  if (state === "error") return "danger";
  if (state === "paused" || state === "pausing") return "pause";
  if (state === "stopped" || state === "completed_with_issues" || state === "stopping" || state === "finalizing") return "warn";
  if (state === "done") return "ok";
  return "run";
}

export function taskResultTone(state: string, issueCount = 0): TaskResultOptions["tone"] {
  if (state === "error") return "fail";
  if (state === "stopped" || state === "completed_with_issues" || issueCount > 0) return "warn";
  return "ok";
}
