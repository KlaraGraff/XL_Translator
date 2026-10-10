import { createBanner, createButton, createFold, createProgressBar, createSelectField, createTextField, type BannerOptions, type StatusTone } from "./components";

/** Shared visual building blocks for every translator task surface. */
export type TaskPanelState = "running" | "pausing" | "paused" | "stopping" | "finalizing" | "done" | "completed_with_issues" | "error" | "stopped" | "interrupted";

export interface TaskProgressOptions {
  phase?: string;
  percent?: number | null;
  phaseIndex?: number;
  phaseTotal?: number;
  state?: TaskPanelState | string;
  monitor?: HTMLElement[];
  note?: string;
}

export function createTaskProgressPanel(options: TaskProgressOptions): HTMLElement {
  const card = document.createElement("div");
  card.className = "card task-progress-panel";
  card.style.padding = "16px 18px 14px";
  const stage = document.createElement("div");
  stage.className = "prog-stage";
  const title = document.createElement("b");
  title.textContent = options.phase || "正在准备任务";
  const pct = document.createElement("span");
  pct.className = "pct";
  const percent = typeof options.percent === "number" && Number.isFinite(options.percent) ? Math.round(options.percent) : null;
  const phaseSuffix = options.phaseIndex && options.phaseTotal ? ` · 阶段 ${options.phaseIndex} / ${options.phaseTotal}` : "";
  pct.textContent = percent === null ? "处理中" : `${percent}%${phaseSuffix}`;
  if (options.state === "paused" || options.state === "pausing") pct.style.color = "var(--warn)";
  stage.append(title, pct);
  card.append(stage, createProgressBar({ percent: percent ?? 0, tone: options.state === "paused" || options.state === "pausing" ? "warn" : "accent" }).root);
  if (options.monitor?.length) {
    const monitor = document.createElement("div");
    monitor.className = "mon";
    monitor.append(...options.monitor);
    card.append(monitor);
  }
  if (options.note) {
    const note = document.createElement("p");
    note.className = "ws-note";
    note.textContent = options.note;
    card.append(note);
  }
  return card;
}

export interface TaskLogEntry {
  seq?: number;
  time?: string;
  message?: string;
  level?: string;
}

export interface TaskLogOptions {
  entries?: TaskLogEntry[];
  archived?: boolean;
  limit?: number;
}

export function createTaskLogPanel(options: TaskLogOptions): HTMLElement {
  const card = document.createElement("div");
  card.className = "card task-log-panel";
  card.style.cssText = "flex:0 1 auto;min-height:0;display:flex;flex-direction:column;overflow:hidden";
  const head = document.createElement("div");
  head.className = "tc-head";
  const title = document.createElement("b");
  title.textContent = "运行日志";
  const meta = document.createElement("span");
  meta.textContent = options.archived ? "已归档" : "实时";
  head.append(title, meta);
  card.append(head);
  const log = document.createElement("div");
  log.className = "log";
  log.style.cssText = "flex:0 1 auto;min-height:76px;max-height:340px;border:0;border-radius:0;overflow-y:auto";
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
    message.textContent = entry.message ?? "";
    line.append(time, message);
    log.append(line);
  }
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.textContent = "等待引擎事件…";
    log.append(empty);
  }
  card.append(log);
  return card;
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
  title.textContent = options.title ?? "任务清单";
  head.append(title);
  options.header?.(head);
  card.append(head);
  return card;
}

export interface TaskControlFootOptions {
  idle: { label: string; disabled?: boolean; onClick: () => void };
  active?: {
    paused: boolean;
    onResume: () => void;
    onPause: () => void;
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
    foot.append(createButton({ label: options.idle.label, icon: options.idle.disabled ? undefined : "play", variant: "primary", size: "big", disabled: options.idle.disabled, onClick: options.idle.onClick }));
    return foot;
  }
  const active = options.active;
  if (active.paused) {
    foot.append(createButton({ label: "继续翻译", icon: "play", variant: "primary", size: "big", onClick: active.onResume }));
    if (active.finishPaused) {
      foot.append(createButton({ label: active.finishPaused.label, icon: "stop", size: "big", onClick: active.finishPaused.onClick }));
      if (active.finishPaused.note) {
        const note = document.createElement("div");
        note.className = "ws-note";
        note.style.textAlign = "center";
        note.textContent = active.finishPaused.note;
        foot.append(note);
      }
    }
    return foot;
  }
  foot.append(createButton({ label: active.pauseLabel ?? "暂停提交", icon: "pause", size: "big", onClick: active.onPause }));
  foot.append(createButton({ label: active.stopLabel ?? "安全停止", icon: "stop", variant: "danger", size: "big", onClick: active.onStop }));
  return foot;
}

export function createTaskTerminalBanner(options: BannerOptions): HTMLElement {
  return createBanner(options);
}

export interface TaskMoreSettingsOptions {
  open?: boolean;
  onToggle?: (open: boolean) => void;
  disabled?: boolean;
  domain?: {
    value: string;
    options: string[];
    onChange: (value: string) => void;
    onEdit?: () => void;
  };
  output?: {
    useCustom: boolean;
    customDir: string;
    onModeChange: (useCustom: boolean) => void;
    onDirInput: (value: string) => void;
    onDirCommit?: () => void;
  };
}

/** Shared “更多设置” block used by every translation surface. */
export function createTaskMoreSettings(options: TaskMoreSettingsOptions): HTMLElement {
  const content = document.createElement("div");
  if (options.domain) {
    const domainField = createSelectField({
      label: "专业领域",
      options: options.domain.options.map((value) => ({ value, label: value })),
      value: options.domain.value,
      disabled: options.disabled,
      onChange: options.domain.onChange,
    });
    if (options.domain.onEdit) {
      const label = domainField.root.querySelector("label");
      if (label) {
        const link = document.createElement("span");
        link.className = "linklike";
        link.style.fontSize = "11px";
        link.textContent = "编辑 Prompt ↗ 设置";
        link.addEventListener("click", options.domain.onEdit);
        label.append(" ", link);
      }
    }
    content.append(domainField.root);
  }
  if (options.output) {
    const output = document.createElement("div");
    output.className = "field";
    output.append(document.createElement("label"));
    output.firstElementChild!.textContent = "输出位置";
    const radios = document.createElement("div");
    radios.className = "radio-row";
    const name = `task-output-${Math.random().toString(36).slice(2)}`;
    for (const [value, label] of [[false, "源目录内"], [true, "自定义"]] as const) {
      const wrap = document.createElement("label");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = name;
      radio.checked = options.output.useCustom === value;
      radio.disabled = Boolean(options.disabled);
      radio.addEventListener("change", () => options.output?.onModeChange(value));
      wrap.append(radio, document.createTextNode(` ${label}`));
      radios.append(wrap);
    }
    output.append(radios);
    if (options.output.useCustom) {
      const field = createTextField({
        label: "",
        value: options.output.customDir,
        placeholder: "自定义输出目录路径…",
        disabled: options.disabled,
        onInput: options.output.onDirInput,
      });
      field.root.style.marginTop = "6px";
      if (options.output.onDirCommit) field.input.addEventListener("change", options.output.onDirCommit);
      output.append(field.root);
    }
    content.append(output);
  }
  return createFold({ title: "更多设置", content, open: options.open, onToggle: options.onToggle }).root;
}

export function taskStatusTone(state: string): StatusTone {
  if (state === "error") return "danger";
  if (state === "paused" || state === "pausing") return "pause";
  if (state === "stopped" || state === "completed_with_issues" || state === "stopping" || state === "finalizing") return "warn";
  if (state === "done") return "ok";
  return "run";
}

export function taskResultTone(state: string, issueCount = 0): BannerOptions["tone"] {
  if (state === "error") return "fail";
  if (state === "stopped" || state === "completed_with_issues" || issueCount > 0) return "warn";
  return "ok";
}
