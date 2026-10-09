import { navigate, type ViewParams } from "../router";
import { setTopbar } from "../shell";
import { ApiClient, type CadCapabilityStatus, type CadScanItem, type TaskStatus } from "../api-client";
import { createButton, createChip, createEmptyState, createLanguageField, createSwitchRow, createTextField, openMenu, openModal, showToast, type LanguageOption } from "../components";
import { open } from "@tauri-apps/plugin-dialog";
import { invoke } from "@tauri-apps/api/core";
import { focusTask, noteTaskStarted, watchTask } from "./tasks";
import "./cad.css";

type CadOptions = {
  sourceLang: string;
  targetLang: string;
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
const defaults: CadOptions = { sourceLang: "auto", targetLang: "zh", translateOutputFilename: false, useMemory: true, useTerminology: true, keepWorkDxf: true, copyRelatedFiles: false, verifyRoundtrip: true, scanReplacementChars: true, includeBlockText: true, checkEntityCounts: true, scanResidual: true };
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
let settingsHydrated = false;

const fileName = (path: string): string => path.split(/[\\/]/).filter(Boolean).pop() || path;
const displayName = (path: string): string => { const name = fileName(path); return name.replace(/\.(dwg|dxf)$/i, ""); };
const dirname = (path: string): string => { const p = path.replace(/[\\/]$/, ""); const i = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\")); return i > 0 ? p.slice(0, i) : p; };
function commonParent(items: string[]): string { if (!items.length) return ""; if (items.length === 1) return dirname(items[0]); const sep = items[0].includes("\\") ? "\\" : "/"; const parts = items.map((p) => p.split(/[\\/]/).filter(Boolean)); const prefix: string[] = []; for (let i = 0; i < parts[0].length; i += 1) { if (parts.every((x) => x[i] === parts[0][i])) prefix.push(parts[0][i]); else break; } return (items[0].startsWith(sep) ? sep : "") + prefix.join(sep) || dirname(items[0]); }
function stateLabel(): string { if (task && !task.terminal) return task.state === "paused" ? "任务已暂停" : "正在翻译 CAD 图纸"; if (capability?.plugin !== "enabled") return "翻译组件未启用"; if (capability.oda !== "connected") return "需要连接图纸转换工具"; return "图纸翻译已就绪"; }

export function mount(container: HTMLElement, _params: ViewParams): void { mounted = true; rootEl = container; render(); void bootstrap(); }
export function unmount(): void { mounted = false; rootEl = null; if (polling !== null) window.clearInterval(polling); polling = null; }

async function bootstrap(): Promise<void> {
  try { const c = new ApiClient(); await c.connect(); const [cap, langs, tasks] = await Promise.all([c.getCadCapability(), c.request<{ source_options?: LanguageOption[]; target_options?: LanguageOption[] }>("/api/languages"), c.listTasks(), c.request<Record<string, unknown>>("/api/settings")]); capability = cap; const recovered = [...tasks.active, ...tasks.recent].find((x) => x.surface === "cad" && !x.terminal); if (recovered) { task = recovered; startPolling(); } languageOptions = { source: langs.source_options ?? [], target: langs.target_options ?? [] }; if (!settingsHydrated) { options.sourceLang = "auto"; options.targetLang = "zh"; settingsHydrated = true; } } catch (e) { capabilityError = e instanceof Error ? e.message : "CAD 服务暂不可用。"; }
  if (mounted) render();
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string): HTMLElementTagNameMap[K] { const node = document.createElement(tag); if (cls) node.className = cls; return node; }
function heading(title: string, sub: string): HTMLElement { const h = el("div", "rp-title"); h.append(el("b")); h.firstElementChild!.textContent = title; const p = el("p", "cad-muted"); p.textContent = sub; h.append(p); return h; }
function action(label: string, onClick: () => void, variant: "primary" | "default" | "danger" = "default"): HTMLButtonElement { const b = createButton({ label, variant: variant === "primary" ? "primary" : variant === "danger" ? "danger-solid" : "default", onClick }); b.classList.add("btn", variant === "primary" ? "pri" : variant === "danger" ? "dgr" : "mini"); return b; }

function render(): void {
  if (!mounted || !rootEl) return;
  setTopbar({ title: "CAD 图纸翻译", status: { label: stateLabel(), tone: task && !task.terminal ? "run" : capability?.plugin === "enabled" && capability.oda === "connected" ? "ok" : "idle" }, subtitle: "DWG / DXF · 原件不覆盖 · 使用文档翻译配置" });
  rootEl.className = "content cad-content"; rootEl.replaceChildren();
  const left = el("div", "col-l"); const right = el("div", "col-r");
  left.append(filesCard(), statusCard()); right.append(settingsCard());
  rootEl.append(left, right);
}

function filesCard(): HTMLElement {
  const wrap = el("div", "cad-left-stack");
  const source = el("div", "card srcbar");
  const field = createTextField({ label: "", value: paths.length === 1 ? paths[0] : commonParent(paths), placeholder: "选择或粘贴文件、文件夹路径…", onInput: (value) => { paths = value.trim() ? [value.trim()] : []; scanItems = []; skipped = []; selected.clear(); } }); field.root.style.margin = "0"; field.root.style.flex = "1"; source.append(field.root);
  const browse = action("浏览", () => openMenu(browse, [{ label: "选择文件夹…", description: "递归扫描目录下所有 DWG / DXF", onSelect: () => void choose(true) }, { label: "选择 CAD 文件…", description: "可多选 DWG、DXF 文件", onSelect: () => void choose(false) }])); source.append(browse, action(scanning ? "扫描中…" : "扫描", () => void startOrScan(), "primary")); wrap.append(source);
  const stats = el("div", "stats"); [["已扫描文件", scanItems.length ? String(scanItems.length) : "—"], ["文字实体", scanItems.length ? scanItems.reduce((n, x) => n + (x.text_count || 0), 0).toLocaleString("zh-CN") : "—"], ["待翻译", scanItems.length ? scanItems.reduce((n, x) => n + (x.candidate_count ?? x.text_count ?? 0), 0).toLocaleString("zh-CN") : "—"], ["跳过项", skipped.length ? String(skipped.length) : "—"]].forEach(([label, value]) => { const cell = el("div", "stat"); const a = el("span"); a.textContent = label; const b = el("b"); b.textContent = value; cell.append(a, b); stats.append(cell); }); wrap.append(stats);
  const card = el("section", "card tablecard cad-files-card"); const head = el("div", "tc-head"); const title = el("b"); title.textContent = "任务清单"; head.append(title); if (paths.length) head.append(action("清空", () => { paths = []; scanItems = []; skipped = []; selected.clear(); render(); })); card.append(head);
  if (!paths.length) card.append(createEmptyState({ title: "尚未添加图纸", description: "选择 DWG、DXF 文件或图纸文件夹，文件会出现在这里。", icon: "doc-file" }));
  else if (!scanItems.length) card.append(createEmptyState({ title: scanning ? "正在扫描图纸" : "等待扫描", description: scanning ? "正在读取图纸中的文字实体。" : "点击上方“扫描”查看待处理图纸。" }));
  else { const table = el("table", "tbl cad-file-table"); const thead = el("thead"); const trh = el("tr"); ["选择", "文件", "格式", "文字", "状态", ""].forEach((x) => { const th = el("th"); th.textContent = x; trh.append(th); }); thead.append(trh); table.append(thead); const body = el("tbody"); scanItems.forEach((item) => { const tr = el("tr"); const td = el("td"); const ck = el("input"); ck.type = "checkbox"; ck.className = "ck"; ck.checked = selected.has(item.path); ck.addEventListener("change", () => ck.checked ? selected.add(item.path) : selected.delete(item.path)); td.append(ck); const name = el("td", "cad-name"); name.textContent = displayName(item.name || item.path); name.title = item.path; const fmt = el("td"); const f = el("span", "fmt"); f.textContent = item.format.toUpperCase(); fmt.append(f); const count = el("td", "num"); count.textContent = String(item.text_count ?? "—"); const state = el("td"); state.append(createChip({ label: item.needs_conversion ? "需转换" : "已识别", tone: item.needs_conversion ? "warn" : "ok" })); const rem = el("td"); rem.append(action("移除", () => { paths = paths.filter((p) => p !== item.path); selected.delete(item.path); scanItems = scanItems.filter((x) => x.path !== item.path); render(); })); tr.append(td, name, fmt, count, state, rem); body.append(tr); }); table.append(body); card.append(table); }
  if (skipped.length) { const note = el("p", "cad-warning"); note.textContent = `有 ${skipped.length} 个路径被跳过`; card.append(note); } wrap.append(card); return wrap;
}
function statusCard(): HTMLElement { const card = el("section", "card cad-status-card"); const head = el("div", "tc-head"); const b = el("b"); b.textContent = "组件状态"; head.append(b); card.append(head); const grid = el("div", "cad-status-grid"); [["图纸翻译", capability?.plugin === "enabled" && capability.oda === "connected" ? "OK" : "未就绪"], ["图纸转换工具", capability?.oda === "connected" ? "OK" : "未找到图纸转换工具"], ["本机平台", capability?.platform ? "OK" : "检测中"]].forEach(([label, value]) => { const item = el("div"); const s = el("span"); s.textContent = label; const strong = el("strong"); strong.textContent = value; item.append(s, strong); if (label === "图纸转换工具") { const actions = el("div", "cad-status-actions"); if (capability?.plugin !== "enabled") actions.append(action("打开插件管理", () => navigate("plugins"), "primary")); else if (capability.oda !== "connected") actions.append(action("打开插件管理连接转换工具", () => navigate("plugins"))); if (actions.childElementCount) item.append(actions); } grid.append(item); }); card.append(grid); if (capabilityError) { const p = el("p", "cad-warning"); p.textContent = capabilityError; card.append(p); } return card; }

function settingsCard(): HTMLElement {
  const card = el("section", "card runpanel"); const scroll = el("div", "rp-scroll"); scroll.append(heading("运行设置", "语言与自动检查")); const lang = el("div", "rp-sec"); lang.textContent = "语言"; scroll.append(lang);
  const target = createLanguageField({ label: "目标语言", options: languageOptions.target, value: options.targetLang, disabled: Boolean(task && !task.terminal), onChange: (v) => { options.targetLang = v; } }); const source = createLanguageField({ label: "源语言", options: languageOptions.source.length ? languageOptions.source : [{ code: "auto", display_name: "自动识别" }], value: options.sourceLang, disabled: Boolean(task && !task.terminal), onChange: (v) => { options.sourceLang = v; } }); scroll.append(target.root, source.root);
  const opt = el("div", "rp-sec"); opt.textContent = "输出"; scroll.append(opt); scroll.append(switchRow("翻译输出文件名", "translateOutputFilename", "按目标语言生成输出文件名；关闭时沿用原文件名"));
  const controls = el("div", "cad-controls"); if (task && !task.terminal) { if (task.state === "paused" || task.state === "pausing") controls.append(action("继续任务", () => void control("resume"), "primary")); else controls.append(action("暂停当前文件后暂停", () => void control("pause"))); controls.append(action("停止任务", () => confirmStop(), "danger")); } else if (scanItems.length || paths.length) controls.append(action(scanItems.length ? "开始翻译" : scanning ? "扫描中…" : "扫描图纸", () => void (scanItems.length ? submit() : startOrScan()), "primary")); scroll.append(controls); if (task) scroll.append(progressCard(task)); card.append(scroll); return card;
}
function switchRow(label: string, key: keyof CadOptions, hint: string): HTMLElement { const row = createSwitchRow({ label, hint, checked: options[key] as boolean, disabled: Boolean(task && !task.terminal), onChange: (v) => { options[key] = v as never; } }); return row; }

function progressCard(status: TaskStatus): HTMLElement { const card = el("div", "cad-progress-card"); const head = el("div", "cad-progress-head"); const title = el("b"); title.textContent = status.terminal ? (status.state === "completed_with_issues" ? "完成，需复核" : status.state === "done" ? "翻译完成" : "任务已结束") : "当前进度"; head.append(title); const p = progressValues(status); const pct = el("strong"); pct.textContent = p.overall == null ? "处理中" : `${p.overall}%`; head.append(pct); card.append(head); if (p.overall != null) { const bar = el("div", "bar"); const fill = el("i"); fill.style.width = `${Math.max(0, Math.min(100, p.overall))}%`; bar.append(fill); card.append(bar); } const phase = el("p", "cad-muted"); phase.textContent = [p.phase, p.step && p.total ? `${p.step}/${p.total}` : ""].filter(Boolean).join(" · ") || status.state; card.append(phase); if (status.terminal) card.append(resultCard(status)); return card; }
function progressValues(status: TaskStatus): { overall: number | null; phase: string; step: number; total: number } { const p = (status.progress || status.task_snapshot || {}) as Record<string, unknown>; return { overall: typeof p.overall_percent === "number" ? p.overall_percent : null, phase: typeof p.phase_name === "string" ? p.phase_name : typeof p.phase === "string" ? p.phase : "", step: typeof p.step_done === "number" ? p.step_done : 0, total: typeof p.step_total === "number" ? p.step_total : 0 }; }
function resultCard(status: TaskStatus): HTMLElement { const result = status.result || {}; const wrap = el("div", "cad-result"); const files = Array.isArray(result.files) ? result.files : Array.isArray(result.file_results) ? result.file_results : []; if (typeof result.output_dir === "string" && result.output_dir) { wrap.append(action("打开结果目录", () => void openPath(result.output_dir as string, false))); if (status.state === "stopped" && files.some((raw) => raw && typeof raw === "object" && typeof (raw as Record<string, unknown>).source_path === "string")) wrap.append(action("从断点继续", () => prepareResume(status), "primary")); } if (typeof result.report_path === "string" && result.report_path) wrap.append(action("打开报告", () => void openPath(result.report_path as string, true))); if (files.length) { const table = el("table", "tbl"); files.forEach((raw) => { if (!raw || typeof raw !== "object") return; const item = raw as Record<string, unknown>; const tr = el("tr"); const name = el("td"); name.textContent = displayName(String(item.output_path || item.source_path || "结果")); const state = el("td"); state.append(createChip({ label: item.status === "needs_review" ? "需复核" : item.status === "succeeded" ? "已生成" : "失败", tone: item.status === "needs_review" ? "warn" : item.status === "succeeded" ? "ok" : "dgr" })); tr.append(name, state); table.append(tr); }); wrap.append(table); } return wrap; }

function prepareResume(status: TaskStatus): void { const result = status.result || {}; const files = Array.isArray(result.files) ? result.files : Array.isArray(result.file_results) ? result.file_results : []; const sourcePaths = files.filter((raw): raw is Record<string, unknown> => Boolean(raw && typeof raw === "object" && typeof (raw as Record<string, unknown>).source_path === "string")).map((raw) => String(raw.source_path)); const previousDir = typeof result.output_dir === "string" ? result.output_dir : ""; if (!sourcePaths.length || !previousDir) { showToast({ message: "当前任务没有可用的断点信息。", error: true }); return; } paths = sourcePaths; scanItems = sourcePaths.map((path) => ({ path, name: fileName(path), format: path.toLowerCase().endsWith(".dxf") ? "dxf" : "dwg" } as CadScanItem)); selected = new Set(sourcePaths); resumeOutputDir = previousDir; task = null; render(); showToast({ message: "已载入断点文件，点击“开始翻译”即可继续。" }); }

async function choose(directory: boolean): Promise<void> { try { const picked = await open({ multiple: !directory, directory, filters: directory ? undefined : [{ name: "CAD 图纸", extensions: ["dwg", "dxf"] }] }); const next = (Array.isArray(picked) ? picked : picked ? [picked] : []).filter((x): x is string => typeof x === "string"); const merged = [...paths, ...next].filter((x, i, all) => all.indexOf(x) === i); if (next.length) { paths = merged; scanItems = []; skipped = []; next.forEach((p) => selected.add(p)); render(); } } catch { showToast({ message: "无法打开文件选择器，请检查桌面权限。", error: true }); } }
async function startOrScan(): Promise<void> { if (!paths.length || scanning) return; const c = new ApiClient(); try { await c.connect(); scanning = true; render(); const response = await c.scanCad({ paths, source_language: options.sourceLang, target_language: options.targetLang, include_block_text: options.includeBlockText }); scanItems = response.items; skipped = response.skipped; selected = new Set(scanItems.map((x) => x.path)); capability = response.capability; scanning = false; render(); if (!scanItems.length) showToast({ message: "没有找到可翻译的 DWG / DXF 文件。", error: true }); } catch (e) { scanning = false; capabilityError = e instanceof Error ? e.message : "CAD 扫描失败。"; render(); } }
function payload(): Record<string, unknown> { const picked = scanItems.filter((x) => selected.has(x.path)).map((x) => x.path); return { surface: "cad", source_path: commonParent(picked.length ? picked : paths), selected_paths: picked, source_lang: options.sourceLang, target_lang: options.targetLang, resume_output_dir: resumeOutputDir || undefined, untranslated_only: true, cad_translate_output_filename: options.translateOutputFilename, cad_use_memory: true, cad_use_terminology: true, cad_keep_work_dxf: options.keepWorkDxf, cad_copy_related_files: options.copyRelatedFiles, cad_verify_roundtrip: true, cad_scan_replacement_chars: true, cad_include_block_text: true, cad_check_entity_counts: true, cad_scan_residual: true }; }
async function submit(): Promise<void> { if (submitting || (task && !task.terminal)) return; submitting = true; const picked = scanItems.filter((x) => selected.has(x.path)); if (!picked.length) { submitting = false; showToast({ message: "请至少选择一个要翻译的文件。", error: true }); return; } const c = new ApiClient(); try { await c.connect(); const body = payload(); const preflight = await c.preflightTask(body); if (preflight.requires_confirmation) { submitting = false; openModal({ tone: "warn", icon: "warn", title: "仍要开始这次 CAD 任务？", body: ["这次任务会和正在运行的任务共用翻译连接，可能排队或变慢。你的设置不会被修改。"], actions: [{ label: "取消" }, { label: "仍要开始", variant: "primary", onClick: () => void createTask(preflight.confirmation_token) }] }); return; } await createTask(); } catch (e) { showToast({ message: e instanceof Error ? e.message : "任务预检失败。", error: true }); } }
async function createTask(token?: string): Promise<void> { try { const c = new ApiClient(); await c.connect(); const taskBody = { ...payload(), ...(token ? { confirmation_token: token } : {}) }; task = await c.startCad(taskBody as never); resumeOutputDir = ""; focusTask(task); noteTaskStarted(task); watchTask(task.task_id); render(); startPolling(); } catch (e) { showToast({ message: e instanceof Error ? e.message : "CAD 任务启动失败。", error: true }); } finally { submitting = false; } }
async function control(actionName: "pause" | "resume" | "stop"): Promise<void> { if (!task) return; try { const c = new ApiClient(); await c.connect(); task = await c.controlTask(task.task_id, actionName); render(); } catch (e) { showToast({ message: e instanceof Error ? e.message : "任务操作失败。", error: true }); } }
function confirmStop(): void { openModal({ tone: "warn", icon: "stop", title: "停止当前 CAD 任务？", body: ["当前文件完成后停止。已经生成的文件和报告会保留，原件不会被覆盖。"], actions: [{ label: "继续执行" }, { label: "停止任务", variant: "danger-solid", onClick: () => void control("stop") }] }); }
function startPolling(): void { if (polling !== null || !task) return; polling = window.setInterval(async () => { if (!task) return; try { const c = new ApiClient(); await c.connect(); task = await c.getTask(task.task_id); if (mounted) render(); if (task.terminal && polling !== null) { window.clearInterval(polling); polling = null; } } catch { /* 事件流仍保留最近一次真实状态 */ } }, 1500); }
async function openPath(path: string, reveal: boolean): Promise<void> { try { await invoke("open_local_path", { path, reveal }); } catch (e) { showToast({ message: e instanceof Error ? e.message : "无法打开结果。", error: true }); } }
