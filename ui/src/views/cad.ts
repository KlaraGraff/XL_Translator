import { navigate, type ViewParams } from "../router";
import { setTopbar } from "../shell";
import { ApiClient, type CadCapabilityStatus, type CadScanItem, type TaskStatus } from "../api-client";
import { createButton, createChip, createEmptyState, createLanguageField, createSwitchRow, createTextField, openMenu, openModal, showToast, type LanguageOption } from "../components";
import { createTaskControlFoot, createTaskListPanel, createTaskLogPanel, createTaskMoreSettings, createTaskProgressPanel, createTaskTerminalBanner, taskResultTone } from "../task-panels";
import { open } from "@tauri-apps/plugin-dialog";
import { invoke } from "@tauri-apps/api/core";
import { focusTask, noteTaskStarted, watchTask } from "./tasks";
import { normalizeUserPath } from "../path-input";
import "./cad.css";

type CadOptions = {
  sourceLang: string;
  targetLang: string;
  domainPreset: string;
  useCustomOutputDir: boolean;
  customOutputDir: string;
  moreSettingsOpen: boolean;
  translateOutputFilename: boolean;
  useMemory: boolean;
  useTerminology: boolean;
  keepWorkDxf: boolean;
  copyRelatedFiles: boolean;
  verifyRoundtrip: boolean;
  scanReplacementChars: boolean;
  includeBlockText: boolean;
  checkEntityCounts: boolean;
  scanResidual: boolean;
};
const defaults: CadOptions = { sourceLang: "auto", targetLang: "zh", domainPreset: "同步工程场景", useCustomOutputDir: false, customOutputDir: "", moreSettingsOpen: true, translateOutputFilename: false, useMemory: true, useTerminology: true, keepWorkDxf: true, copyRelatedFiles: false, verifyRoundtrip: true, scanReplacementChars: true, includeBlockText: true, checkEntityCounts: true, scanResidual: true };
let options: CadOptions = { ...defaults };
let mounted = false;
let rootEl: HTMLElement | null = null;
let capability: CadCapabilityStatus | null = null;
let capabilityError = "";
let languageOptions: { source: LanguageOption[]; target: LanguageOption[] } = { source: [], target: [] };
let paths: string[] = [];
let scanItems: CadScanItem[] = [];
let skipped: Array<{ path: string; reason: string }> = [];
let selected = new Set<string>();
let task: TaskStatus | null = null;
let resumeOutputDir = "";
let scanning = false;
let polling: number | null = null;
let submitting = false;
let submitError = false;
let settingsHydrated = false;

const fileName = (path: string): string => path.split(/[\\/]/).filter(Boolean).pop() || path;
const displayName = (path: string): string => { const name = fileName(path); return name.replace(/\.(dwg|dxf)$/i, ""); };
const dirname = (path: string): string => { const p = path.replace(/[\\/]$/, ""); const i = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\")); return i > 0 ? p.slice(0, i) : p; };
function commonParent(items: string[]): string { if (!items.length) return ""; if (items.length === 1) return dirname(items[0]); const sep = items[0].includes("\\") ? "\\" : "/"; const parts = items.map((p) => p.split(/[\\/]/).filter(Boolean)); const prefix: string[] = []; for (let i = 0; i < parts[0].length; i += 1) { if (parts.every((x) => x[i] === parts[0][i])) prefix.push(parts[0][i]); else break; } return (items[0].startsWith(sep) ? sep : "") + prefix.join(sep) || dirname(items[0]); }
function stateLabel(): string { if (task && !task.terminal) return task.state === "paused" ? "任务已暂停" : "正在翻译 CAD 图纸"; if (task?.state === "completed_with_issues") return "翻译完成，需复核"; if (task?.state === "done") return "翻译完成"; if (task?.state === "stopped") return "任务已停止，结果已保留"; if (task?.state === "error") return "翻译失败"; if (capability?.oda !== "connected") return "需要连接图纸转换工具"; return "图纸翻译已就绪"; }

export function mount(container: HTMLElement, _params: ViewParams): void { mounted = true; rootEl = container; render(); void bootstrap(); }
export function unmount(): void { mounted = false; rootEl = null; if (polling !== null) window.clearInterval(polling); polling = null; }

async function bootstrap(): Promise<void> {
  try { const c = new ApiClient(); await c.connect(); const [cap, langs, tasks, settings] = await Promise.all([c.getCadCapability(), c.request<{ source_options?: LanguageOption[]; target_options?: LanguageOption[] }>("/api/languages"), c.listTasks(), c.request<Record<string, unknown>>("/api/settings")]); capability = cap; const recovered = [...tasks.active, ...tasks.recent].find((x) => x.surface === "cad" && !x.terminal); if (recovered) { task = recovered; startPolling(); } languageOptions = { source: langs.source_options ?? [], target: langs.target_options ?? [] }; if (!settingsHydrated) { const rawOutput = settings.cad_output && typeof settings.cad_output === "object" ? settings.cad_output as Record<string, unknown> : {}; options.domainPreset = typeof settings.cad_domain_preset === "string" ? settings.cad_domain_preset : defaults.domainPreset; options.useCustomOutputDir = Boolean(rawOutput.use_custom_output_dir); options.customOutputDir = typeof rawOutput.custom_output_dir === "string" ? rawOutput.custom_output_dir : ""; options.translateOutputFilename = Boolean(rawOutput.translate_output_filename); options.sourceLang = "auto"; options.targetLang = "zh"; settingsHydrated = true; } } catch (e) { capabilityError = e instanceof Error ? e.message : "CAD 服务暂不可用。"; }
  if (mounted) render();
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string): HTMLElementTagNameMap[K] { const node = document.createElement(tag); if (cls) node.className = cls; return node; }
function heading(title: string): HTMLElement { const h = el("div", "rp-title"); h.textContent = title; return h; }
function action(label: string, onClick: () => void, variant: "primary" | "default" | "danger" = "default"): HTMLButtonElement { return createButton({ label, variant: variant === "primary" ? "primary" : variant === "danger" ? "danger-solid" : "default", onClick }); }

function render(): void {
  if (!mounted || !rootEl) return;
  const topTone = task && !task.terminal ? (task.state === "paused" ? "pause" : "run") : task?.state === "error" ? "danger" : task?.state === "completed_with_issues" || task?.state === "stopped" ? "warn" : capability?.oda === "connected" ? "ok" : "idle";
  setTopbar({ title: "CAD 图纸翻译", status: { label: stateLabel(), tone: topTone }, subtitle: "DWG / DXF · 原件不覆盖 · 使用文档翻译配置" });
  rootEl.className = "content cad-content"; rootEl.removeAttribute("style"); rootEl.replaceChildren();
  const left = el("div", "col-l"); const right = el("div", "col-r");
  if (task && !task.terminal) {
    left.append(activeFilesCard(task), progressCard(task), logCard(task));
  } else {
    if (capability?.oda !== "connected") left.append(statusCard());
    left.append(filesCard());
  }
  right.append(settingsCard());
  if (task?.terminal) {
    rootEl.style.flexDirection = "column";
    rootEl.append(resultView(task));
    const row = el("div");
    row.style.cssText = "flex:1;display:flex;gap:16px;min-height:0";
    row.append(left, right);
    rootEl.append(row);
  } else {
    rootEl.append(left, right);
  }
}

function activeFilesCard(status: TaskStatus): HTMLElement {
  const card = el("section", "card tablecard cad-files-card cad-active-files");
  const head = el("div", "tc-head");
  const title = el("b"); title.textContent = "任务清单"; head.append(title);
  head.append(createChip({ label: status.state === "paused" ? "已暂停" : "翻译中", tone: status.state === "paused" ? "warn" : "tint" }));
  card.append(head);
  const table = el("table", "tbl cad-file-table"); const thead = el("thead"); const trh = el("tr");
  ["文件", "格式", "文字", "状态"].forEach((label) => { const th = el("th"); th.textContent = label; trh.append(th); });
  thead.append(trh); table.append(thead);
  const body = el("tbody");
  let entries = [...(status.file_progress?.files ?? [])];
  if (!entries.length && scanItems.length) {
    entries = scanItems.filter((item) => selected.has(item.path)).map((item) => ({ file_id: item.path, name: item.name, relative_path: item.name, state: "waiting", phase: "", completed: 0, total: 0, result: {}, revision: 0 }));
  }
  entries.forEach((entry) => {
    const row = el("tr"); const name = el("td", "cad-name"); name.textContent = String(entry.name || entry.relative_path || "CAD 文件");
    const fmt = el("td"); const suffix = String(entry.name || "").split(".").pop() || ""; const format = el("span", "fmt"); format.textContent = suffix.toUpperCase(); fmt.append(format);
    const count = el("td", "num"); const result = entry.result && typeof entry.result === "object" ? entry.result as Record<string, unknown> : {};
    const stats = result.stats && typeof result.stats === "object" ? result.stats as Record<string, unknown> : {}; count.textContent = String(stats.candidate_entities ?? "—");
    const state = el("td"); state.append(createChip({ label: entry.state === "generated" ? "已完成" : entry.state === "failed" ? "失败" : entry.state === "stopped" ? "已停止" : "翻译中", tone: entry.state === "generated" ? "ok" : entry.state === "failed" ? "dgr" : "tint" }));
    row.append(name, fmt, count, state); body.append(row);
  });
  table.append(body); card.append(table); return card;
}

function logCard(status: TaskStatus): HTMLElement {
  const entries = (status.logs ?? []).map((entry) => ({ time: String(entry.time || entry.ts || ""), message: String(entry.message || ""), level: String(entry.level || "INFO") }));
  return createTaskLogPanel({ entries, archived: status.terminal });
}

function filesCard(): HTMLElement {
  const wrap = el("div", "cad-left-stack");
  const source = el("div", "card srcbar");
  const { root: fieldRoot, input } = createTextField({ label: "", value: paths.length === 1 ? paths[0] : commonParent(paths), placeholder: "选择或粘贴文件、文件夹路径…", onInput: (value) => { const normalized = normalizeUserPath(value); if (normalized !== value) input.value = normalized; paths = normalized ? [normalized] : []; scanItems = []; skipped = []; selected.clear(); } }); fieldRoot.style.margin = "0"; fieldRoot.style.flex = "1"; source.append(input);
  const browse = createButton({ label: "浏览", icon: "folder", disabled: scanning, title: scanning ? "正在扫描当前路径，完成后再选新的来源。" : undefined, onClick: () => openMenu(browse, [{ label: "选择文件夹…", description: "递归扫描目录下所有 DWG / DXF", onSelect: () => void choose(true) }, { label: "选择 CAD 文件…", description: "可多选 DWG、DXF 文件", onSelect: () => void choose(false) }]) });
  const scan = createButton({ label: scanning ? "扫描中…" : "扫描", variant: "primary", disabled: scanning || !paths.length, onClick: () => void startOrScan() });
  input.addEventListener("input", () => { scan.disabled = scanning || !input.value.trim(); });
  source.append(browse, scan); wrap.append(source);
  const stats = el("div", "stats"); [["已扫描文件", scanItems.length ? String(scanItems.length) : "—"], ["文字实体", scanItems.length ? scanItems.reduce((n, x) => n + (x.text_count || 0), 0).toLocaleString("zh-CN") : "—"], ["待翻译", scanItems.length ? scanItems.reduce((n, x) => n + (x.candidate_count ?? x.text_count ?? 0), 0).toLocaleString("zh-CN") : "—"], ["跳过项", skipped.length ? String(skipped.length) : "—"]].forEach(([label, value]) => { const cell = el("div", "stat"); const a = el("span"); a.textContent = label; const b = el("b"); b.textContent = value; cell.append(a, b); stats.append(cell); }); wrap.append(stats);
  const card = createTaskListPanel({ className: "cad-files-card", header: (head) => { if (paths.length) head.append(action("清空", () => { paths = []; scanItems = []; skipped = []; selected.clear(); render(); })); } });
  if (!paths.length) card.append(createEmptyState({ title: "尚未添加图纸", description: "选择 DWG、DXF 文件或图纸文件夹，文件会出现在这里。", icon: "cad" }));
  else if (!scanItems.length) card.append(createEmptyState({ title: scanning ? "正在扫描图纸" : "等待扫描", description: scanning ? "正在读取图纸中的文字实体。" : "点击上方“扫描”查看待处理图纸。" }));
  else { const table = el("table", "tbl cad-file-table"); const thead = el("thead"); const trh = el("tr"); ["选择", "文件", "格式", "文字", "状态", ""].forEach((x) => { const th = el("th"); th.textContent = x; trh.append(th); }); thead.append(trh); table.append(thead); const body = el("tbody"); scanItems.forEach((item) => { const tr = el("tr"); const td = el("td"); const ck = el("input"); ck.type = "checkbox"; ck.className = "ck"; ck.checked = selected.has(item.path); ck.addEventListener("change", () => ck.checked ? selected.add(item.path) : selected.delete(item.path)); td.append(ck); const name = el("td", "cad-name"); name.textContent = displayName(item.name || item.path); name.title = item.path; const fmt = el("td"); const f = el("span", "fmt"); f.textContent = item.format.toUpperCase(); fmt.append(f); const count = el("td", "num"); count.textContent = String(item.text_count ?? "—"); const state = el("td"); state.append(createChip({ label: item.needs_conversion ? "需转换" : "已识别", tone: item.needs_conversion ? "warn" : "ok" })); const rem = el("td"); rem.append(action("移除", () => { paths = paths.filter((p) => p !== item.path); selected.delete(item.path); scanItems = scanItems.filter((x) => x.path !== item.path); render(); })); tr.append(td, name, fmt, count, state, rem); body.append(tr); }); table.append(body); card.append(table); }
  if (skipped.length) { const note = el("p", "cad-warning"); note.textContent = `有 ${skipped.length} 个路径被跳过`; card.append(note); } wrap.append(card); return wrap;
}
function statusCard(): HTMLElement { const card = el("section", "card cad-status-card"); const head = el("div", "tc-head"); const b = el("b"); b.textContent = "组件状态"; head.append(b); const actions = el("div", "cad-status-actions"); if (capability?.oda !== "connected") actions.append(action("打开插件管理", () => navigate("plugins"), "primary")); if (actions.childElementCount) head.append(actions); card.append(head); const grid = el("div", "cad-status-grid"); [["图纸转换工具", capability?.oda === "connected" ? "已连接" : capability?.oda === "incompatible" ? "不可用" : "未检测到"]].forEach(([label, value]) => { const item = el("div"); const s = el("span"); s.textContent = label; const strong = el("strong"); strong.textContent = value; item.append(s, strong); grid.append(item); }); card.append(grid); if (capabilityError) { const p = el("p", "cad-warning"); p.textContent = capabilityError; card.append(p); } return card; }

function settingsCard(): HTMLElement {
  const card = el("section", "card runpanel"); const scroll = el("div", task && !task.terminal ? "rp-scroll dis" : "rp-scroll"); const title = heading("运行设置"); if (task && !task.terminal) title.append(createChip({ label: "任务中锁定", tone: "tint" })); scroll.append(title); const lang = el("div", "rp-sec"); lang.textContent = "语言"; scroll.append(lang);
  const target = createLanguageField({ label: "目标语言", options: languageOptions.target, value: options.targetLang, disabled: Boolean(task && !task.terminal), onChange: (v) => { options.targetLang = v; } }); const source = createLanguageField({ label: "源语言", options: languageOptions.source.length ? languageOptions.source : [{ code: "auto", display_name: "自动识别" }], value: options.sourceLang, disabled: Boolean(task && !task.terminal), onChange: (v) => { options.sourceLang = v; } }); scroll.append(target.root, source.root);
  const opt = el("div", "rp-sec"); opt.textContent = "本类型选项 · CAD"; scroll.append(opt); scroll.append(switchRow("翻译输出文件名", "translateOutputFilename", "按目标语言生成输出文件名；关闭时沿用原文件名"));
  scroll.append(createTaskMoreSettings({
    open: options.moreSettingsOpen,
    disabled: Boolean(task && !task.terminal),
    onToggle: (open) => { options.moreSettingsOpen = open; },
    domain: {
      value: options.domainPreset,
      options: ["无", "同步工程场景", "资料管理场景", "行政生活化场景"],
      onChange: (value) => { const previous = options.domainPreset; options.domainPreset = value; void persistCadSettings({ cad_domain_preset: value }, () => { options.domainPreset = previous; }); },
    },
    output: {
      useCustom: options.useCustomOutputDir,
      customDir: options.customOutputDir,
      onModeChange: (useCustom) => { const previous = options.useCustomOutputDir; options.useCustomOutputDir = useCustom; render(); void persistCadSettings({ cad_output: { use_custom_output_dir: useCustom } }, () => { options.useCustomOutputDir = previous; render(); }); },
      onDirInput: (value) => { options.customOutputDir = value; },
      onDirCommit: () => { void persistCadSettings({ cad_output: { custom_output_dir: options.customOutputDir } }, () => undefined); },
    },
  }));
  const active = Boolean(task && !task.terminal);
  const foot = createTaskControlFoot({
    idle: {
      label: submitError ? "重试启动" : "开始翻译",
      disabled: scanning || submitting || !scanItems.length || selected.size === 0,
      onClick: () => void submit(),
    },
    active: active ? {
      paused: task?.state === "paused" || task?.state === "pausing",
      onResume: () => void control("resume"),
      onPause: () => void control("pause"),
      onStop: () => confirmStop(),
      pauseLabel: "暂停当前文件后暂停",
      stopLabel: "停止任务",
    } : undefined,
  });
  card.append(scroll, foot); return card;
}
function switchRow(label: string, key: keyof CadOptions, hint: string): HTMLElement { const row = createSwitchRow({ label, hint, checked: options[key] as boolean, disabled: Boolean(task && !task.terminal), onChange: (v) => { options[key] = v as never; } }); return row; }

function progressCard(status: TaskStatus): HTMLElement { const p = progressValues(status); return createTaskProgressPanel({ phase: p.phase || "正在处理 CAD 图纸", percent: p.overall, phaseIndex: p.step, phaseTotal: p.total, state: status.state }); }
function progressValues(status: TaskStatus): { overall: number | null; phase: string; step: number; total: number } { const p = (status.progress || status.task_snapshot || {}) as Record<string, unknown>; return { overall: typeof p.overall_percent === "number" ? p.overall_percent : null, phase: typeof p.phase_name === "string" ? p.phase_name : typeof p.phase === "string" ? p.phase : "", step: typeof p.step_done === "number" ? p.step_done : 0, total: typeof p.step_total === "number" ? p.step_total : 0 }; }
function resultView(status: TaskStatus): HTMLElement {
  const result = status.result || {};
  const files = Array.isArray(result.files) ? result.files : Array.isArray(result.file_results) ? result.file_results : [];
  const issueCount = Array.isArray(result.issues) ? result.issues.length : files.filter((raw) => raw && typeof raw === "object" && (raw as Record<string, unknown>).status === "needs_review").length;
  const wrap = el("div", "cad-terminal-stack");
  wrap.append(createTaskTerminalBanner({
    title: status.state === "completed_with_issues" || issueCount ? "翻译完成，但有项目需要复核" : status.state === "done" ? "翻译完成" : status.state === "stopped" ? "任务已停止，结果已保留" : "任务未完成",
    subtitle: `${files.length} 个 CAD 文件已处理；原件未覆盖。${issueCount ? ` ${issueCount} 项已写入 review。` : ""}`,
    tone: taskResultTone(status.state, issueCount),
    icon: status.state === "error" ? "warn" : issueCount || status.state === "stopped" ? "warn" : "check",
  }));
  wrap.append(resultCard(status));
  return wrap;
}
function resultCard(status: TaskStatus): HTMLElement {
  const result = status.result || {}; const wrap = el("section", "card cad-result");
  const actions = el("div", "result-actions");
  if (typeof result.output_dir === "string" && result.output_dir) actions.append(action("打开结果目录", () => void openPath(result.output_dir as string, false)));
  if (typeof result.review_dir === "string" && result.review_dir) actions.append(action("打开复核目录", () => void openPath(result.review_dir as string, false)));
  if (typeof result.report_path === "string" && result.report_path) actions.append(action("打开报告", () => void openPath(result.report_path as string, true)));
  if (typeof result.manifest_path === "string" && result.manifest_path) actions.append(action("打开清单", () => void openPath(result.manifest_path as string, true)));
  if (typeof result.summary_path === "string" && result.summary_path) actions.append(action("打开摘要", () => void openPath(result.summary_path as string, true)));
  if (status.state === "stopped") actions.append(action("从断点继续", () => prepareResume(status), "primary"));
  if (actions.childElementCount) wrap.append(actions);
  const files = Array.isArray(result.files) ? result.files : Array.isArray(result.file_results) ? result.file_results : [];
  if (files.length) {
    const head = el("div", "tc-head"); const title = el("b"); title.textContent = "结果文件"; head.append(title); wrap.append(head);
    const table = el("table", "tbl result-table"); const thead = el("thead"); const trh = el("tr"); ["文件", "输出", "检查"].forEach((label) => { const th = el("th"); th.textContent = label; trh.append(th); }); thead.append(trh); table.append(thead);
    const body = el("tbody"); files.forEach((raw) => { if (!raw || typeof raw !== "object") return; const item = raw as Record<string, unknown>; const tr = el("tr"); const name = el("td"); name.textContent = displayName(String(item.output_path || item.source_path || "结果")); const state = el("td"); const statusLabel = item.status === "needs_review" ? "需复核" : item.status === "succeeded" ? "已生成" : "失败"; state.append(createChip({ label: statusLabel, tone: item.status === "needs_review" ? "warn" : item.status === "succeeded" ? "ok" : "dgr" })); const check = el("td"); const stats = item.stats && typeof item.stats === "object" ? item.stats as Record<string, unknown> : {}; const unresolved = Array.isArray(item.unresolved) ? item.unresolved.length : 0; const residual = typeof stats.residual_foreign_text_count === "number" ? stats.residual_foreign_text_count : 0; check.textContent = item.status === "needs_review" ? `${unresolved + residual} 项待复核` : "通过"; tr.append(name, state, check); body.append(tr); }); table.append(body); wrap.append(table);
  }
  return wrap;
}

function prepareResume(status: TaskStatus): void { const result = status.result || {}; const files = Array.isArray(result.files) ? result.files : Array.isArray(result.file_results) ? result.file_results : []; const sourcePaths = files.filter((raw): raw is Record<string, unknown> => Boolean(raw && typeof raw === "object" && typeof (raw as Record<string, unknown>).source_path === "string")).map((raw) => String(raw.source_path)); const previousDir = typeof result.output_dir === "string" ? result.output_dir : ""; if (!sourcePaths.length || !previousDir) { showToast({ message: "当前任务没有可用的断点信息。", error: true }); return; } paths = sourcePaths; scanItems = sourcePaths.map((path) => ({ path, name: fileName(path), format: path.toLowerCase().endsWith(".dxf") ? "dxf" : "dwg" } as CadScanItem)); selected = new Set(sourcePaths); resumeOutputDir = previousDir; task = null; render(); showToast({ message: "已载入断点文件，点击“开始翻译”即可继续。" }); }

async function persistCadSettings(patch: Record<string, unknown>, rollback: () => void): Promise<void> {
  try {
    const c = new ApiClient();
    await c.connect();
    await c.request("/api/settings", { method: "PUT", body: JSON.stringify(patch) });
  } catch (error) {
    rollback();
    render();
    showToast({ message: error instanceof Error ? error.message : "CAD 设置保存失败。", error: true });
  }
}

async function choose(directory: boolean): Promise<void> { try { const picked = await open({ multiple: !directory, directory, filters: directory ? undefined : [{ name: "CAD 图纸", extensions: ["dwg", "dxf"] }] }); const next = (Array.isArray(picked) ? picked : picked ? [picked] : []).filter((x): x is string => typeof x === "string"); const merged = [...paths, ...next].filter((x, i, all) => all.indexOf(x) === i); if (next.length) { paths = merged; scanItems = []; skipped = []; next.forEach((p) => selected.add(p)); render(); } } catch { showToast({ message: "无法打开文件选择器，请检查桌面权限。", error: true }); } }
async function startOrScan(): Promise<void> { paths = paths.map(normalizeUserPath).filter(Boolean); if (!paths.length || scanning) return; const c = new ApiClient(); try { await c.connect(); scanning = true; render(); const response = await c.scanCad({ paths, source_language: options.sourceLang, target_language: options.targetLang, include_block_text: options.includeBlockText }); scanItems = response.items; skipped = response.skipped; selected = new Set(scanItems.map((x) => x.path)); capability = response.capability; scanning = false; render(); if (!scanItems.length) showToast({ message: "没有找到可翻译的 DWG / DXF 文件。", error: true }); } catch (e) { scanning = false; capabilityError = e instanceof Error ? e.message : "CAD 扫描失败。"; render(); } }
function payload(): Record<string, unknown> { const picked = scanItems.filter((x) => selected.has(x.path)).map((x) => normalizeUserPath(x.path)); const sourcePaths = paths.map(normalizeUserPath); return { surface: "cad", source_path: commonParent(picked.length ? picked : sourcePaths), selected_paths: picked, source_lang: options.sourceLang, target_lang: options.targetLang, cad_output_dir: options.useCustomOutputDir && options.customOutputDir.trim() ? normalizeUserPath(options.customOutputDir) : undefined, resume_output_dir: resumeOutputDir ? normalizeUserPath(resumeOutputDir) : undefined, untranslated_only: true, cad_translate_output_filename: options.translateOutputFilename, cad_use_memory: true, cad_use_terminology: true, cad_keep_work_dxf: options.keepWorkDxf, cad_copy_related_files: options.copyRelatedFiles, cad_verify_roundtrip: true, cad_scan_replacement_chars: true, cad_include_block_text: true, cad_check_entity_counts: true, cad_scan_residual: true }; }
async function submit(): Promise<void> { if (submitting || (task && !task.terminal)) return; submitting = true; submitError = false; render(); const picked = scanItems.filter((x) => selected.has(x.path)); if (!picked.length) { submitting = false; submitError = true; render(); showToast({ message: "请至少选择一个要翻译的文件。", error: true }); return; } const c = new ApiClient(); try { await c.connect(); const body = payload(); const preflight = await c.preflightTask(body); if (preflight.requires_confirmation) { submitting = false; render(); openModal({ tone: "warn", icon: "warn", title: "仍要开始这次 CAD 任务？", body: ["这次任务会和正在运行的任务共用翻译连接，可能排队或变慢。你的设置不会被修改。"], actions: [{ label: "取消" }, { label: "仍要开始", variant: "primary", onClick: () => void createTask(preflight.confirmation_token) }] }); return; } await createTask(); } catch (e) { submitting = false; submitError = true; render(); showToast({ message: e instanceof Error ? e.message : "任务预检失败。", error: true }); } }
async function createTask(token?: string): Promise<void> { try { const c = new ApiClient(); await c.connect(); const taskBody = { ...payload(), ...(token ? { confirmation_token: token } : {}) }; task = await c.startCad(taskBody as never); submitError = false; resumeOutputDir = ""; focusTask(task); noteTaskStarted(task); watchTask(task.task_id); } catch (e) { submitError = true; showToast({ message: e instanceof Error ? e.message : "CAD 任务启动失败。", error: true }); } finally { submitting = false; render(); if (task && !task.terminal) startPolling(); } }
async function control(actionName: "pause" | "resume" | "stop"): Promise<void> { if (!task) return; try { const c = new ApiClient(); await c.connect(); task = await c.controlTask(task.task_id, actionName); render(); } catch (e) { showToast({ message: e instanceof Error ? e.message : "任务操作失败。", error: true }); } }
function confirmStop(): void { openModal({ tone: "warn", icon: "stop", title: "停止当前 CAD 任务？", body: ["当前文件完成后停止。已经生成的文件和报告会保留，原件不会被覆盖。"], actions: [{ label: "继续执行" }, { label: "停止任务", variant: "danger-solid", onClick: () => void control("stop") }] }); }
function startPolling(): void { if (polling !== null || !task) return; polling = window.setInterval(async () => { if (!task) return; try { const c = new ApiClient(); await c.connect(); task = await c.getTask(task.task_id); if (mounted) render(); if (task.terminal && polling !== null) { window.clearInterval(polling); polling = null; } } catch { /* 事件流仍保留最近一次真实状态 */ } }, 1500); }
async function openPath(path: string, reveal: boolean): Promise<void> { try { await invoke("open_local_path", { path, reveal }); } catch (e) { showToast({ message: e instanceof Error ? e.message : "无法打开结果。", error: true }); } }
