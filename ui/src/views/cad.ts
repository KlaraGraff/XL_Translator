import { navigate, type ViewParams } from "../router";
import { setTopbar } from "../shell";
import { ApiClient, type CadCapabilityStatus, type CadScanItem, type TaskStatus } from "../api-client";
import { createButton, createChip, createLanguageField, createSwitchRow, openModal, showToast, type LanguageOption } from "../components";
import { open } from "@tauri-apps/plugin-dialog";
import { invoke } from "@tauri-apps/api/core";
import { focusTask, noteTaskStarted, watchTask } from "./tasks";
import "./cad.css";

type CadOptions = {
  sourceLang: string;
  targetLang: string;
  untranslatedOnly: boolean;
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
const defaults: CadOptions = { sourceLang: "auto", targetLang: "zh", untranslatedOnly: true, useMemory: true, useTerminology: true, keepWorkDxf: true, copyRelatedFiles: false, verifyRoundtrip: true, scanReplacementChars: true, includeBlockText: true, checkEntityCounts: true, scanResidual: true };
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
let glossaryPath = "";
let outputDir = "";
let resumeOutputDir = "";
let loading = false;
let scanning = false;
let polling: number | null = null;
let submitting = false;
let settingsHydrated = false;

const fileName = (path: string): string => path.split(/[\\/]/).filter(Boolean).pop() || path;
const displayName = (path: string): string => { const name = fileName(path); return name.replace(/\.(dwg|dxf)$/i, ""); };
const dirname = (path: string): string => { const p = path.replace(/[\\/]$/, ""); const i = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\")); return i > 0 ? p.slice(0, i) : p; };
function commonParent(items: string[]): string { if (!items.length) return ""; if (items.length === 1) return dirname(items[0]); const sep = items[0].includes("\\") ? "\\" : "/"; const parts = items.map((p) => p.split(/[\\/]/).filter(Boolean)); const prefix: string[] = []; for (let i = 0; i < parts[0].length; i += 1) { if (parts.every((x) => x[i] === parts[0][i])) prefix.push(parts[0][i]); else break; } return (items[0].startsWith(sep) ? sep : "") + prefix.join(sep) || dirname(items[0]); }
function stateLabel(): string { if (task && !task.terminal) return task.state === "paused" ? "任务已暂停" : "正在翻译 CAD 图纸"; if (capability?.plugin !== "enabled") return "CAD 插件未安装"; if (capability.oda !== "connected") return "需要连接 ODA"; return "CAD 插件已就绪"; }

export function mount(container: HTMLElement, _params: ViewParams): void { mounted = true; rootEl = container; render(); void bootstrap(); }
export function unmount(): void { mounted = false; rootEl = null; if (polling !== null) window.clearInterval(polling); polling = null; }

async function bootstrap(): Promise<void> {
  try { const c = new ApiClient(); await c.connect(); const [cap, langs, tasks, saved] = await Promise.all([c.getCadCapability(), c.request<{ source_options?: LanguageOption[]; target_options?: LanguageOption[] }>("/api/languages"), c.listTasks(), c.request<Record<string, unknown>>("/api/settings")]); capability = cap; const recovered = [...tasks.active, ...tasks.recent].find((x) => x.surface === "cad" && !x.terminal); if (recovered) { task = recovered; startPolling(); } languageOptions = { source: langs.source_options ?? [], target: langs.target_options ?? [] }; if (!settingsHydrated) { const savedSource = typeof saved.source_lang === "string" ? saved.source_lang : typeof saved.excel_source_lang === "string" ? saved.excel_source_lang : ""; const savedTarget = typeof saved.target_lang === "string" ? saved.target_lang : typeof saved.excel_target_lang === "string" ? saved.excel_target_lang : ""; if (savedSource) options.sourceLang = savedSource; if (savedTarget) options.targetLang = savedTarget; settingsHydrated = true; } if (!options.targetLang && languageOptions.target[0]) options.targetLang = languageOptions.target[0].code; } catch (e) { capabilityError = e instanceof Error ? e.message : "CAD 服务暂不可用。"; }
  if (mounted) render();
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string): HTMLElementTagNameMap[K] { const node = document.createElement(tag); if (cls) node.className = cls; return node; }
function heading(title: string, sub: string): HTMLElement { const h = el("div", "rp-title"); h.append(el("b")); h.firstElementChild!.textContent = title; const p = el("p", "cad-muted"); p.textContent = sub; h.append(p); return h; }
function action(label: string, onClick: () => void, variant: "primary" | "default" | "danger" = "default"): HTMLButtonElement { const b = createButton({ label, variant: variant === "primary" ? "primary" : variant === "danger" ? "danger-solid" : "default", onClick }); b.classList.add("btn", variant === "primary" ? "pri" : variant === "danger" ? "dgr" : "mini"); return b; }

function render(): void {
  if (!mounted || !rootEl) return;
  setTopbar({ title: "CAD 图纸翻译", status: { label: stateLabel(), tone: task && !task.terminal ? "run" : capability?.plugin === "enabled" && capability.oda === "connected" ? "ok" : "idle" }, subtitle: "DWG / DXF · 原件不覆盖 · 结果和报告留在输出目录" });
  rootEl.className = "content cad-content"; rootEl.replaceChildren();
  const left = el("div", "col-l"); const right = el("div", "col-r");
  left.append(filesCard(), statusCard()); right.append(settingsCard());
  rootEl.append(left, right);
}

function filesCard(): HTMLElement {
  const card = el("section", "card tablecard cad-files-card"); const head = el("div", "tc-head"); const title = el("b"); title.textContent = "翻译文件"; head.append(title); const tools = el("div", "tc-tools"); tools.append(action("添加文件", () => void choose(false)), action("添加文件夹", () => void choose(true))); if (paths.length) tools.append(action("清空", () => { paths = []; scanItems = []; skipped = []; selected.clear(); render(); })); head.append(tools); card.append(head);
  if (!paths.length) { const empty = el("div", "empty"); const b = el("b"); b.textContent = "尚未添加图纸"; const p = el("p"); p.textContent = "选择 DWG、DXF 或包含图纸的文件夹，文件会增量加入。"; empty.append(b, p); card.append(empty); return card; }
  const table = el("table", "tbl cad-file-table"); const thead = el("thead"); const trh = el("tr"); ["选择", "文件", "格式", "文字", "状态", ""].forEach((x) => { const th = el("th"); th.textContent = x; trh.append(th); }); thead.append(trh); table.append(thead); const body = el("tbody");
  const rows = scanItems.length ? scanItems : paths.map((path) => ({ path, name: fileName(path), format: path.toLowerCase().endsWith(".dxf") ? "dxf" : "dwg" } as CadScanItem));
  rows.forEach((item) => { const tr = el("tr"); const tdCheck = el("td"); const check = el("input"); check.type = "checkbox"; check.className = "ck"; check.checked = selected.has(item.path); check.addEventListener("change", () => { if (check.checked) selected.add(item.path); else selected.delete(item.path); }); tdCheck.append(check); const name = el("td"); name.className = "cad-name"; name.textContent = displayName(item.name || item.path); name.title = item.path; const format = el("td"); const fmt = el("span", "fmt"); fmt.textContent = item.format.toUpperCase(); format.append(fmt); const count = el("td"); count.className = "num"; count.textContent = item.text_count == null ? "—" : String(item.text_count); const status = el("td"); status.append(createChip({ label: item.needs_conversion ? "需转换" : item.text_count == null ? "待扫描" : "已识别", tone: item.needs_conversion ? "warn" : item.text_count == null ? "mute" : "ok" })); const remove = el("td"); const rm = action("移除", () => { paths = paths.filter((p) => p !== item.path); selected.delete(item.path); scanItems = scanItems.filter((x) => x.path !== item.path); render(); }); rm.classList.add("cad-remove"); remove.append(rm); tr.append(tdCheck, name, format, count, status, remove); body.append(tr); }); table.append(body); card.append(table);
  if (skipped.length) { const note = el("p", "cad-warning"); note.textContent = `有 ${skipped.length} 个路径未纳入扫描：${skipped.map((x) => `${fileName(x.path)}（${x.reason}）`).join("、")}`; card.append(note); }
  return card;
}

function statusCard(): HTMLElement { const card = el("section", "card cad-status-card"); const head = el("div", "tc-head"); const b = el("b"); b.textContent = "组件状态"; head.append(b); card.append(head); const grid = el("div", "cad-status-grid"); [["CAD Support", capability?.plugin === "enabled" ? "已启用" : "未安装"], ["ODA 转换器", capability?.oda === "connected" ? "已连接" : "未连接"], ["本机平台", capability?.platform ? `${capability.platform.system} · ${capability.platform.arch}` : "检测中"]].forEach(([label, value]) => { const item = el("div"); const s = el("span"); s.textContent = label; const strong = el("strong"); strong.textContent = value; item.append(s, strong); grid.append(item); }); card.append(grid); if (capabilityError) { const p = el("p", "cad-warning"); p.textContent = capabilityError; card.append(p); } return card; }

function settingsCard(): HTMLElement {
  const card = el("section", "card runpanel"); const scroll = el("div", "rp-scroll"); scroll.append(heading("任务设置", "语言、记忆库与输出检查")); const lang = el("div", "rp-sec"); lang.textContent = "语言"; scroll.append(lang);
  const source = createLanguageField({ label: "源语言", options: languageOptions.source.length ? languageOptions.source : [{ code: "auto", display_name: "自动识别" }], value: options.sourceLang, disabled: Boolean(task && !task.terminal), onChange: (v) => { options.sourceLang = v; } }); const target = createLanguageField({ label: "目标语言", options: languageOptions.target, value: options.targetLang, disabled: Boolean(task && !task.terminal), onChange: (v) => { options.targetLang = v; } }); scroll.append(source.root, target.root);
  const opt = el("div", "rp-sec"); opt.textContent = "本次任务"; scroll.append(opt); scroll.append(switchRow("仅翻译未含目标语言的文字", "untranslatedOnly", "跳过已经是目标语言的实体"), switchRow("优先使用翻译记忆库", "useMemory", "复用已有术语和句对"), switchRow("使用项目术语库", "useTerminology", "可选 JSON / CSV / TSV 文件"));
  const glossary = pathField("项目术语库", glossaryPath, "选择 JSON / CSV", () => void chooseGlossary()); scroll.append(glossary);
  const out = pathField("输出目录", outputDir, "自动生成", () => void chooseOutput()); scroll.append(out);
  const checks = el("div", "rp-sec"); checks.textContent = "输出与检查"; scroll.append(checks); scroll.append(switchRow("保留工作 DXF", "keepWorkDxf", "保留转换中间文件"), switchRow("复制关联文件", "copyRelatedFiles", "复制同目录 XREF、字体等关联文件"), switchRow("写回后重新读取", "verifyRoundtrip", "检查写回结果是否可读"), switchRow("检查文字实体数", "checkEntityCounts", "比较写回前后的实体数量"), switchRow("检查文字乱码", "scanReplacementChars", "检查替代字符"), switchRow("检查残余外文", "scanResidual", "把仍未翻译的文字列入报告"), switchRow("翻译块内文字", "includeBlockText", "包含块定义中的文字"));
  const plugin = el("div", "cad-plugin-actions"); if (capability?.plugin !== "enabled") plugin.append(action(loading ? "安装中…" : "前往设置安装 CAD Support", () => navigate("settings", { page: "about" }), "primary")); if (capability?.plugin === "enabled" && capability.oda !== "connected") plugin.append(action("前往设置连接 ODA", () => navigate("settings", { page: "about" }))); scroll.append(plugin);
  const controls = el("div", "cad-controls"); if (task && !task.terminal) { if (task.state === "paused" || task.state === "pausing") controls.append(action("继续任务", () => void control("resume"), "primary")); else controls.append(action("暂停当前文件后暂停", () => void control("pause"))); controls.append(action("停止任务", () => confirmStop(), "danger")); } else if (scanItems.length || paths.length) controls.append(action(scanItems.length ? "开始翻译" : scanning ? "扫描中…" : "扫描图纸", () => void (scanItems.length ? submit() : startOrScan()), "primary")); scroll.append(controls); if (task) scroll.append(progressCard(task)); card.append(scroll); return card;
}
function switchRow(label: string, key: keyof CadOptions, hint: string): HTMLElement { const row = createSwitchRow({ label, hint, checked: options[key] as boolean, disabled: Boolean(task && !task.terminal), onChange: (v) => { options[key] = v as never; } }); return row; }
function pathField(label: string, path: string, buttonLabel: string, onClick: () => void): HTMLElement { const row = el("div", "cad-path-field"); const top = el("div"); const l = el("span"); l.textContent = label; const b = action(path ? "更换" : buttonLabel, onClick); b.classList.add("mini"); top.append(l, b); const value = el("small"); value.textContent = path ? displayName(path) : "未选择"; value.title = path; row.append(top, value); return row; }

function progressCard(status: TaskStatus): HTMLElement { const card = el("div", "cad-progress-card"); const head = el("div", "cad-progress-head"); const title = el("b"); title.textContent = status.terminal ? (status.state === "completed_with_issues" ? "完成，需复核" : status.state === "done" ? "翻译完成" : "任务已结束") : "当前进度"; head.append(title); const p = progressValues(status); const pct = el("strong"); pct.textContent = p.overall == null ? "处理中" : `${p.overall}%`; head.append(pct); card.append(head); if (p.overall != null) { const bar = el("div", "bar"); const fill = el("i"); fill.style.width = `${Math.max(0, Math.min(100, p.overall))}%`; bar.append(fill); card.append(bar); } const phase = el("p", "cad-muted"); phase.textContent = [p.phase, p.step && p.total ? `${p.step}/${p.total}` : ""].filter(Boolean).join(" · ") || status.state; card.append(phase); if (status.terminal) card.append(resultCard(status)); return card; }
function progressValues(status: TaskStatus): { overall: number | null; phase: string; step: number; total: number } { const p = (status.progress || status.task_snapshot || {}) as Record<string, unknown>; return { overall: typeof p.overall_percent === "number" ? p.overall_percent : null, phase: typeof p.phase_name === "string" ? p.phase_name : typeof p.phase === "string" ? p.phase : "", step: typeof p.step_done === "number" ? p.step_done : 0, total: typeof p.step_total === "number" ? p.step_total : 0 }; }
function resultCard(status: TaskStatus): HTMLElement { const result = status.result || {}; const wrap = el("div", "cad-result"); const files = Array.isArray(result.files) ? result.files : Array.isArray(result.file_results) ? result.file_results : []; if (typeof result.output_dir === "string" && result.output_dir) { wrap.append(action("打开结果目录", () => void openPath(result.output_dir as string, false))); if (status.state === "stopped" && files.some((raw) => raw && typeof raw === "object" && typeof (raw as Record<string, unknown>).source_path === "string")) wrap.append(action("从断点继续", () => prepareResume(status), "primary")); } if (typeof result.report_path === "string" && result.report_path) wrap.append(action("打开报告", () => void openPath(result.report_path as string, true))); if (files.length) { const table = el("table", "tbl"); files.forEach((raw) => { if (!raw || typeof raw !== "object") return; const item = raw as Record<string, unknown>; const tr = el("tr"); const name = el("td"); name.textContent = displayName(String(item.output_path || item.source_path || "结果")); const state = el("td"); state.append(createChip({ label: item.status === "needs_review" ? "需复核" : item.status === "succeeded" ? "已生成" : "失败", tone: item.status === "needs_review" ? "warn" : item.status === "succeeded" ? "ok" : "dgr" })); tr.append(name, state); table.append(tr); }); wrap.append(table); } return wrap; }

function prepareResume(status: TaskStatus): void { const result = status.result || {}; const files = Array.isArray(result.files) ? result.files : Array.isArray(result.file_results) ? result.file_results : []; const sourcePaths = files.filter((raw): raw is Record<string, unknown> => Boolean(raw && typeof raw === "object" && typeof (raw as Record<string, unknown>).source_path === "string")).map((raw) => String(raw.source_path)); const previousDir = typeof result.output_dir === "string" ? result.output_dir : ""; if (!sourcePaths.length || !previousDir) { showToast({ message: "当前任务没有可用的断点信息。", error: true }); return; } paths = sourcePaths; scanItems = sourcePaths.map((path) => ({ path, name: fileName(path), format: path.toLowerCase().endsWith(".dxf") ? "dxf" : "dwg" } as CadScanItem)); selected = new Set(sourcePaths); resumeOutputDir = previousDir; outputDir = dirname(previousDir); task = null; render(); showToast({ message: "已载入断点文件，点击“开始翻译”即可继续。" }); }

async function choose(directory: boolean): Promise<void> { try { const picked = await open({ multiple: !directory, directory, filters: directory ? undefined : [{ name: "CAD 图纸", extensions: ["dwg", "dxf"] }] }); const next = (Array.isArray(picked) ? picked : picked ? [picked] : []).filter((x): x is string => typeof x === "string"); const merged = [...paths, ...next].filter((x, i, all) => all.indexOf(x) === i); if (next.length) { paths = merged; scanItems = []; skipped = []; next.forEach((p) => selected.add(p)); render(); } } catch { showToast({ message: "无法打开文件选择器，请检查桌面权限。", error: true }); } }
async function chooseGlossary(): Promise<void> { try { const picked = await open({ multiple: false, directory: false, filters: [{ name: "术语库", extensions: ["json", "csv", "tsv"] }] }); const path = Array.isArray(picked) ? picked[0] : picked; if (typeof path === "string") { glossaryPath = path; options.useTerminology = true; render(); } } catch { showToast({ message: "无法选择术语库文件。", error: true }); } }
async function chooseOutput(): Promise<void> { try { const picked = await open({ multiple: false, directory: true }); const path = Array.isArray(picked) ? picked[0] : picked; if (typeof path === "string") { outputDir = path; render(); } } catch { showToast({ message: "无法选择输出目录。", error: true }); } }

async function startOrScan(): Promise<void> { if (!paths.length || scanning) return; const c = new ApiClient(); try { await c.connect(); scanning = true; render(); const response = await c.scanCad({ paths, source_language: options.sourceLang, target_language: options.targetLang, include_block_text: options.includeBlockText }); scanItems = response.items; skipped = response.skipped; selected = new Set(scanItems.map((x) => x.path)); capability = response.capability; scanning = false; render(); if (!scanItems.length) showToast({ message: "没有找到可翻译的 DWG / DXF 文件。", error: true }); } catch (e) { scanning = false; capabilityError = e instanceof Error ? e.message : "CAD 扫描失败。"; render(); } }
function payload(): Record<string, unknown> { const picked = scanItems.filter((x) => selected.has(x.path)).map((x) => x.path); return { surface: "cad", source_path: commonParent(picked.length ? picked : paths), selected_paths: picked, source_lang: options.sourceLang, target_lang: options.targetLang, resume_output_dir: resumeOutputDir || undefined, untranslated_only: options.untranslatedOnly, cad_use_memory: options.useMemory, cad_use_terminology: options.useTerminology, cad_glossary_path: glossaryPath || undefined, cad_output_dir: outputDir || undefined, cad_keep_work_dxf: options.keepWorkDxf, cad_copy_related_files: options.copyRelatedFiles, cad_verify_roundtrip: options.verifyRoundtrip, cad_scan_replacement_chars: options.scanReplacementChars, cad_include_block_text: options.includeBlockText, cad_check_entity_counts: options.checkEntityCounts, cad_scan_residual: options.scanResidual }; }
async function submit(): Promise<void> { if (submitting || (task && !task.terminal)) return; submitting = true; const picked = scanItems.filter((x) => selected.has(x.path)); if (!picked.length) { submitting = false; showToast({ message: "请至少选择一个要翻译的文件。", error: true }); return; } const c = new ApiClient(); try { await c.connect(); const body = payload(); const preflight = await c.preflightTask(body); if (preflight.requires_confirmation) { submitting = false; openModal({ tone: "warn", icon: "warn", title: "仍要开始这次 CAD 任务？", body: ["这次任务会和正在运行的任务共用翻译连接，可能排队或变慢。你的设置不会被修改。"], actions: [{ label: "取消" }, { label: "仍要开始", variant: "primary", onClick: () => void createTask(preflight.confirmation_token) }] }); return; } await createTask(); } catch (e) { showToast({ message: e instanceof Error ? e.message : "任务预检失败。", error: true }); } }
async function createTask(token?: string): Promise<void> { try { const c = new ApiClient(); await c.connect(); const taskBody = { ...payload(), ...(token ? { confirmation_token: token } : {}) }; task = await c.startCad(taskBody as never); resumeOutputDir = ""; focusTask(task); noteTaskStarted(task); watchTask(task.task_id); render(); startPolling(); } catch (e) { showToast({ message: e instanceof Error ? e.message : "CAD 任务启动失败。", error: true }); } finally { submitting = false; } }
async function control(actionName: "pause" | "resume" | "stop"): Promise<void> { if (!task) return; try { const c = new ApiClient(); await c.connect(); task = await c.controlTask(task.task_id, actionName); render(); } catch (e) { showToast({ message: e instanceof Error ? e.message : "任务操作失败。", error: true }); } }
function confirmStop(): void { openModal({ tone: "warn", icon: "stop", title: "停止当前 CAD 任务？", body: ["当前文件完成后停止。已经生成的文件和报告会保留，原件不会被覆盖。"], actions: [{ label: "继续执行" }, { label: "停止任务", variant: "danger-solid", onClick: () => void control("stop") }] }); }
function startPolling(): void { if (polling !== null || !task) return; polling = window.setInterval(async () => { if (!task) return; try { const c = new ApiClient(); await c.connect(); task = await c.getTask(task.task_id); if (mounted) render(); if (task.terminal && polling !== null) { window.clearInterval(polling); polling = null; } } catch { /* 事件流仍保留最近一次真实状态 */ } }, 1500); }
async function openPath(path: string, reveal: boolean): Promise<void> { try { await invoke("open_local_path", { path, reveal }); } catch (e) { showToast({ message: e instanceof Error ? e.message : "无法打开结果。", error: true }); } }
