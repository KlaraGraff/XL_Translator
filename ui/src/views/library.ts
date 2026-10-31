// 记忆库视图（对应样张屏⑦）—— 语言对/搜索/增删改/导入导出/清洗记忆库/冲突裁决。
// 功能范围以 main.ts 的 renderTmView 及其配套 tm* 函数为准（见文件头注释），
// 版式与文案按样张屏⑦精修；样张未画出的功能（统计条、最近语言对、冲突裁决、任务风险
// 确认）保留但用现有组件与设计令牌收纳，不新增样张之外的视觉语言。

import type { ViewParams } from "../router";
import { navigate } from "../router";
import { setTopbar } from "../shell";
import {
  clearElement,
  createCard,
  createChip,
  createButton,
  createTextField,
  createDropdown,
  createSelectField,
  createLanguagePicker,
  createSwitchRow,
  createEmptyState,
  openModal,
  showToast,
  hideHint,
  closeLanguagePopover,
  closeMenu,
  openMenu as openSharedMenu,
  placePopover,
  type MenuItem,
  type ChipTone,
  type LanguageOption,
} from "../components";
import { icon } from "../icons";
import { ApiClient, ApiError } from "../api-client";
import { t, tx, fmtNum, localized } from "../i18n";
import { consumePendingRestartWarning } from "../update-controller";
import { openRestartBeforeTaskModal } from "../update-toast";
import { saveJsonFile, saveTextFile } from "../save-file";
import { tmSwitchDefs } from "./tm-switches";
import "./library.css";
import { fitPlaceholder } from "../text-fit";

// ---------------------------------------------------------------------------
// 类型 / 工具函数
// ---------------------------------------------------------------------------

type JsonObject = Record<string, unknown>;

interface TmEntry {
  id: number;
  source_text: string;
  target_text: string;
  lang_pair?: string;
  pinned: number;
  word_type: string;
  updated_at?: string;
  /** 备注（适用场景）；空串为通用词条。 */
  note?: string;
  /** 用户选过的采用方式；空串表示按默认。 */
  usage?: string;
  /** 实际采用方式：always 始终采用 / scene 按场景选用 / segment 仅整段复用。 */
  effective_usage?: string;
  scope?: string;
  use_count?: number;
  last_used_at?: string;
  /** 来源：manual 手动维护 / auto 自动保存。 */
  origin?: string;
}

type EntryFilter = "all" | "manual" | "auto" | "always" | "scene" | "segment" | "recent_auto" | "unused";
/** 普通模式只有前三个筛选；其余在高级模式下出现。 */
const BASIC_FILTERS: EntryFilter[] = ["all", "manual", "auto"];
const ADVANCED_FILTERS: EntryFilter[] = ["always", "scene", "segment", "recent_auto", "unused"];
const FILTER_LABELS: Record<EntryFilter, string> = localized(() => ({
  all: t("全部"),
  manual: t("手动维护"),
  auto: t("自动保存"),
  always: t("始终采用"),
  scene: t("按场景选用"),
  segment: t("仅整段复用"),
  recent_auto: t("最近 7 天自动保存"),
  unused: t("一年以上未使用"),
}));
const USAGE_LABELS: Record<string, string> = localized(() => ({
  always: t("始终采用"),
  scene: t("按场景选用"),
  segment: t("仅整段复用"),
}));
const USAGE_HINTS: Record<string, string> = localized(() => ({
  always: t("整段文字完全相同或在句中出现时均采用此译法，翻译完成后核对。"),
  scene: t("由模型对照备注和文档说明判断是否采用。填写备注时默认选择此项。"),
  segment: t("仅在整段文字完全相同时采用，在句中出现时不采用。"),
}));

function originLabel(entry: TmEntry): string {
  return entry.origin === "manual" ? t("手动维护") : t("自动保存");
}

interface TmConflict {
  id: number;
  source_text: string;
  existing_target: string;
  candidate_target: string;
  lang_pair: string;
  status: string;
}

interface TmImportEntry {
  source_text: string;
  target_text: string;
  word_type?: string;
  pinned?: number | boolean | string;
}

function record(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}
function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}
function num(value: unknown, fallback = 0): number {
  return typeof value === "number" ? value : fallback;
}
function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0) : [];
}
function resultEntries(result: JsonObject, keys: string[]): JsonObject[] {
  for (const key of keys) {
    const value = result[key];
    if (Array.isArray(value) && value.length) return value.map(record);
  }
  return [];
}
function redactedText(value: unknown, fallback = ""): string {
  const raw = text(value, fallback);
  if (!raw) return raw;
  return raw
    .replace(/(authorization\s*[:=]\s*)([^\s,;]+)/gi, "$1[redacted]")
    .replace(/\b(sk|rk|pk|api)[-_][a-z0-9_-]{8,}\b/gi, "[redacted]")
    .replace(/\bBearer\s+[^\s,;]+/gi, "Bearer [redacted]");
}
function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function parseTmCsv(input: string): TmImportEntry[] {
  // 前导 BOM 要先剥掉，否则它会粘在第一个表头单元格上（"﻿source"），列名对不上，
  // 整份导入变成 0 条。我们自己导出的 CSV 就带 BOM（见 exportTm），Excel 另存的
  // UTF-8 CSV 也带，所以这是最常见的一条导入路径，不是边角情况。
  const raw = input.charCodeAt(0) === 0xfeff ? input.slice(1) : input;
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let index = 0; index < raw.length; index += 1) {
    const char = raw[index];
    if (char === '"') {
      if (quoted && raw[index + 1] === '"') {
        field += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
    } else if (char === "," && !quoted) {
      row.push(field);
      field = "";
    } else if ((char === "\n" || char === "\r") && !quoted) {
      if (char === "\r" && raw[index + 1] === "\n") index += 1;
      row.push(field);
      if (row.some((value) => value.trim())) rows.push(row);
      row = [];
      field = "";
    } else {
      field += char;
    }
  }
  if (field || row.length) {
    row.push(field);
    if (row.some((value) => value.trim())) rows.push(row);
  }
  if (!rows.length) return [];
  const headers = rows.shift()!.map((header) => header.trim().toLocaleLowerCase());
  const findColumn = (names: string[]) => headers.findIndex((header) => names.includes(header));
  const sourceIndex = findColumn(["source_text", "source", "原文"]); // i18n-ignore 导入文件的列名
  const targetIndex = findColumn(["target_text", "target", "译文"]); // i18n-ignore 导入文件的列名
  const wordTypeIndex = findColumn(["word_type", "type", "来源"]); // i18n-ignore 导入文件的列名
  const pinnedIndex = findColumn(["pinned", "固定"]); // i18n-ignore 导入文件的列名
  return rows
    .map((values) => ({
      source_text: values[sourceIndex >= 0 ? sourceIndex : 0]?.trim() ?? "",
      target_text: values[targetIndex >= 0 ? targetIndex : 1]?.trim() ?? "",
      ...(wordTypeIndex >= 0 ? { word_type: values[wordTypeIndex]?.trim() } : {}),
      ...(pinnedIndex >= 0 ? { pinned: values[pinnedIndex]?.trim() } : {}),
    }))
    .filter((entry) => entry.source_text && entry.target_text);
}
function csvCell(value: unknown): string {
  const raw = String(value ?? "");
  return /[",\r\n]/.test(raw) ? `"${raw.replaceAll('"', '""')}"` : raw;
}
function toTmCsv(entries: JsonObject[]): string {
  const headers = ["source_text", "target_text", "word_type", "pinned", "updated_at"];
  const lines = [headers.join(",")];
  for (const entry of entries) lines.push(headers.map((header) => csvCell(entry[header])).join(","));
  return `﻿${lines.join("\r\n")}\r\n`;
}

// ---------------------------------------------------------------------------
// 模块状态
// ---------------------------------------------------------------------------

let sourceOptions: LanguageOption[] = [];
let targetOptions: LanguageOption[] = [];
let recentPairs: string[] = [];
let sourceLang = "zh";
let targetLang = "en";
let keyword = "";
let page = 1;
let pageSize = 25;
let total = 0;
let entries: TmEntry[] = [];
let stats: JsonObject = {};
let filterCounts: JsonObject = {};
let entryFilter: EntryFilter = "all";
/** 高级模式（设置 tm.advanced_mode）：多出采用方式、生效范围、使用次数等。 */
let advancedMode = false;
const selectedIds = new Set<number>();
let conflicts: TmConflict[] = [];
let conflictMessage = "";
let cleaningState: "idle" | "running" | "ready" | "error" = "idle";
let cleanTaskId: string | null = null;
let cleanSuggestions: JsonObject[] = [];
// 常驻待复核提示：与 cleaningState 是两套状态。cleaningState 只在「刚跑完清洗」
// 这一次会话内有值，翻页/切语言对/重进页面就丢了；pendingCleanCount 每次加载都
// 重新拉一次待审建议数，只要语言对下还有没处理的建议就一直提示，直到清零。
let pendingCleanCount = 0;
// 复核面板里的「另有 N 条建议已失效」汇总——只在弹窗内使用，随每次打开面板刷新。
let cleanStaleCount = 0;

let mounted = false;
let mountGeneration = 0;
let tmRequestRevision = 0;
let tmQueryRevision = 0;
// 回滚到最后一次真正显示的数据，不能回滚到另一个仍在等待的查询。
let confirmedQuery: { page: number; keyword: string; pageSize: number; sourceLang: string; targetLang: string; entryFilter: EntryFilter } = { page, keyword, pageSize, sourceLang, targetLang, entryFilter };
let confirmedSelectedIds: number[] = [];
let tmQueryPending = true;
function tmQueryReady(): boolean {
  return mounted && !tmQueryPending && sourceLang === confirmedQuery.sourceLang
    && targetLang === confirmedQuery.targetLang && keyword === confirmedQuery.keyword
    && page === confirmedQuery.page && pageSize === confirmedQuery.pageSize
    && entryFilter === confirmedQuery.entryFilter;
}
function restoreConfirmedQuery(): void {
  ({ page, keyword, pageSize, sourceLang, targetLang, entryFilter } = confirmedQuery);
  selectedIds.clear();
  for (const id of confirmedSelectedIds) selectedIds.add(id);
  tmQueryPending = false;
}
function currentTmScope(generation: number, revision: number): boolean {
  return mounted && generation === mountGeneration && revision === tmQueryRevision;
}
function invalidateTmQuery(): number {
  if (!tmQueryPending) confirmedSelectedIds = [...selectedIds];
  tmQueryPending = true;
  selectAllBusy = false;
  return ++tmQueryRevision;
}
// 页面骨架（buildLayout）：.lib 根节点下依次是工具行、筛选行、最近语言对、提示区、表格、底栏。
let libTopEl: HTMLDivElement | null = null;
let libFiltersEl: HTMLDivElement | null = null;
let libRecentEl: HTMLDivElement | null = null;
let libNoticeEl: HTMLDivElement | null = null;
let stateRowEl: HTMLDivElement | null = null;
let conflictHostEl: HTMLDivElement | null = null;
let tableScrollEl: HTMLDivElement | null = null;
/** 底栏 .lib-foot：「已选」「选择全部」「批量删除」和分页，按 data-role 找控件。 */
let tcHeadEl: HTMLDivElement | null = null;
/** 上一次画出行的查询版本：只有数据换了那一次加行浮现动画，勾选引起的重绘不重播。 */
let renderedRevision = -1;
let searchDebounce: number | null = null;
// 加载中 / 加载失败时占位卡片：语言对、TM 条目、冲突三个请求任何一个失败都不构建下面
// 的工具条 / 表格结构（否则会出现空 select + 空表格，且永远没有重试入口），而是把这张
// 卡片留在容器里，成功后再整体替换成真正的版式。
let placeholderEl: HTMLDivElement | null = null;

let connectPromise: Promise<ApiClient> | null = null;
async function getClient(): Promise<ApiClient> {
  if (!connectPromise) {
    const instance = new ApiClient();
    connectPromise = instance
      .connect()
      .then(() => instance)
      .catch((error) => {
        connectPromise = null;
        throw error;
      });
  }
  return connectPromise;
}

function tmLangPair(): string {
  return `${sourceLang || "zh"}-${targetLang || "en"}`;
}
function splitTmPair(pair: string): { source: string; target: string } | null {
  const [source, ...targetParts] = pair.split("-");
  const target = targetParts.join("-");
  return source && target ? { source, target } : null;
}
function tmPairLabel(pair: string): string {
  const parsed = splitTmPair(pair);
  if (!parsed) return pair;
  const sourceOption = sourceOptions.find((option) => option.code === parsed.source);
  const targetOption = targetOptions.find((option) => option.code === parsed.target);
  return `${sourceOption ? tx(sourceOption.display_name) : parsed.source} → ${targetOption ? tx(targetOption.display_name) : parsed.target}`;
}
function targetIsCustom(code: string): boolean {
  return targetOptions.find((option) => option.code === code)?.builtin === false || code.startsWith("x-custom-");
}
function pairAllowsReverse(): boolean {
  return !targetIsCustom(targetLang);
}

// ---------------------------------------------------------------------------
// 数据加载
// ---------------------------------------------------------------------------

async function refreshLanguagePairs(): Promise<void> {
  const generation = mountGeneration;
  const client = await getClient();
  const payload = await client.request<{
    source_options: LanguageOption[];
    target_options: LanguageOption[];
    selected?: { source_lang?: string; target_lang?: string };
    recent?: string[];
    advanced_mode?: boolean;
  }>("/api/tm/language-pairs");
  if (!mounted || generation !== mountGeneration) return;
  advancedMode = payload.advanced_mode === true;
  sourceOptions = payload.source_options.filter((option) => option.builtin !== false && option.can_source !== false);
  targetOptions = payload.target_options.filter((option) => option.can_target !== false);
  recentPairs = strings(payload.recent);
  const wantedSource = text(payload.selected?.source_lang, sourceLang);
  const wantedTarget = text(payload.selected?.target_lang, targetLang);
  sourceLang = sourceOptions.some((option) => option.code === wantedSource) ? wantedSource : (sourceOptions[0]?.code ?? "zh");
  targetLang = targetOptions.some((option) => option.code === wantedTarget) ? wantedTarget : (targetOptions[0]?.code ?? "en");
}

async function refreshTm(): Promise<boolean> {
  const generation = mountGeneration;
  const queryRevision = tmQueryRevision;
  const requestRevision = ++tmRequestRevision;
  const query = { pair: tmLangPair(), keyword, page, pageSize, filter: entryFilter };
  const isCurrent = () => currentTmScope(generation, queryRevision) && requestRevision === tmRequestRevision;
  try {
    const client = await getClient();
    if (!isCurrent()) return false;
    while (true) {
      const payload = await client.request<{ entries: TmEntry[]; stats: JsonObject; total?: number; filter_counts?: JsonObject }>(
        `/api/tm/entries?lang_pair=${encodeURIComponent(query.pair)}&keyword=${encodeURIComponent(query.keyword)}&page=${query.page}&page_size=${query.pageSize}&entry_filter=${encodeURIComponent(query.filter)}`,
      );
      if (!isCurrent()) return false;
      const nextTotal = num(payload.total, payload.entries.length);
      const totalPages = Math.max(1, Math.ceil(nextTotal / query.pageSize));
      if (query.page > totalPages) {
        query.page = totalPages;
        continue;
      }
      entries = payload.entries;
      stats = payload.stats ?? {};
      filterCounts = record(payload.filter_counts);
      total = nextTotal;
      page = query.page;
      confirmedQuery = { page, keyword: query.keyword, pageSize: query.pageSize, sourceLang, targetLang, entryFilter: query.filter };
      tmQueryPending = false;
      return true;
    }
  } catch (error) {
    if (!isCurrent()) return false;
    throw error;
  }
}

/**
 * 换页 / 改每页条数 / 搜索都走这里：先改查询状态再取数，失败就原样退回。
 *
 * 旧代码在请求发出前就清空了选中集合，失败后既不重画表格也不出提示——
 * 界面停在上一页的数据上，选中却已经没了，用户看不出发生过什么，
 * 再点一次「批量删除」删的还是上一次的范围。返回 true 表示这次取数成功。
 */
async function applyTmQueryChange(change: () => void): Promise<boolean> {
  const generation = mountGeneration;
  const revision = invalidateTmQuery();
  change();
  renderTable();
  updateSelectionUi();
  let ok = true;
  try {
    ok = await refreshTm();
  } catch (error) {
    if (!currentTmScope(generation, revision)) return false;
    ok = false;
    restoreConfirmedQuery();
    showToast({ message: t("记忆库加载失败：{0}", errorMessage(error)), error: true });
  }
  if (!currentTmScope(generation, revision)) return false;
  if (ok) {
    selectedIds.clear();
    confirmedSelectedIds = [];
  }
  renderTable();
  renderTopbarStatus();
  return ok;
}

// 「选择全部」跨页拿全量 id：复用 /api/tm/entries 本身（不新建后端接口），把
// page_size 顶到服务端允许的上限（api/app.py list_tm_entries 里 min(page_size, 200)）
// 分批并发拉完。SELECT_ALL_CAP 是前端自设的上限，避免语言对里条目多到失控时
// 一次发几十个请求；超过时如实告知只选中了前 N 条，不做静默截断。
const SELECT_ALL_PAGE_SIZE = 200;
const SELECT_ALL_CAP = 5000;
let selectAllBusy = false;

async function fetchAllMatchingTmIds(): Promise<{ ids: number[]; truncated: boolean }> {
  const query = { pair: tmLangPair(), keyword, total, filter: entryFilter };
  const targetCount = Math.min(query.total, SELECT_ALL_CAP);
  if (targetCount <= 0) return { ids: [], truncated: false };
  const client = await getClient();
  const pageCount = Math.ceil(targetCount / SELECT_ALL_PAGE_SIZE);
  const pagePayloads = await Promise.all(
    Array.from({ length: pageCount }, (_, index) =>
      client.request<{ entries: TmEntry[] }>(
        `/api/tm/entries?lang_pair=${encodeURIComponent(query.pair)}&keyword=${encodeURIComponent(query.keyword)}&page=${index + 1}&page_size=${SELECT_ALL_PAGE_SIZE}&entry_filter=${encodeURIComponent(query.filter)}`,
      ),
    ),
  );
  const ids = pagePayloads.flatMap((payload) => payload.entries.map((entry) => entry.id)).slice(0, targetCount);
  return { ids, truncated: query.total > SELECT_ALL_CAP };
}

/** 「选择全部」/「取消全选」：选中的是当前语言对 + 搜索关键词过滤后的完整结果集
 * （跨页），不是无视筛选的整个记忆库。已经选满一次可达上限时再点会直接清空，
 * 不必重新发请求。 */
async function handleSelectAllTm(): Promise<void> {
  if (!tmQueryReady() || selectAllBusy || total <= 0) return;
  const reachable = Math.min(total, SELECT_ALL_CAP);
  if (selectedIds.size > 0 && selectedIds.size === reachable) {
    selectedIds.clear();
    renderTable();
    return;
  }
  const generation = mountGeneration;
  const revision = tmQueryRevision;
  selectAllBusy = true;
  updateSelectionUi();
  try {
    const { ids, truncated } = await fetchAllMatchingTmIds();
    if (!currentTmScope(generation, revision)) return;
    selectedIds.clear();
    for (const id of ids) selectedIds.add(id);
    renderTable();
    if (truncated) {
      showToast({
        message: t("已选择前 {0} 条（共 {1} 条，超过单次全选上限 {2} 条）。请缩小搜索范围后分批处理剩余条目。", fmtNum(ids.length), fmtNum(total), fmtNum(SELECT_ALL_CAP)),
        error: true,
      });
    }
  } catch (error) {
    if (!currentTmScope(generation, revision)) return;
    showToast({ message: t("选择全部失败：{0}", errorMessage(error)), error: true });
  } finally {
    if (currentTmScope(generation, revision)) {
      selectAllBusy = false;
      updateSelectionUi();
    }
  }
}

async function refreshConflicts(): Promise<void> {
  const generation = mountGeneration;
  const revision = tmQueryRevision;
  const pair = tmLangPair();
  const client = await getClient();
  const payload = await client.request<{ conflicts: TmConflict[] }>(`/api/tm/conflicts?lang_pair=${encodeURIComponent(pair)}`);
  if (!currentTmScope(generation, revision)) return;
  conflicts = Array.isArray(payload.conflicts) ? payload.conflicts : [];
}

async function persistSettings(patch: JsonObject): Promise<void> {
  const client = await getClient();
  await client.request("/api/settings", { method: "PUT", body: JSON.stringify(patch) });
}

// 单独串行化语言对保存：后发意图最后落盘，不让慢的旧 PUT 覆盖新语言对。
let langPairSaveQueue: Promise<void> = Promise.resolve();
let langPairSaveRevision = 0;
function persistLangPairInOrder(patch: JsonObject, generation: number, revision: number): Promise<void> {
  const saving = langPairSaveQueue.catch(() => {}).then(async () => {
    if (!mounted || generation !== mountGeneration || revision !== langPairSaveRevision) return;
    await persistSettings(patch);
  });
  langPairSaveQueue = saving.catch(() => {});
  return saving;
}

async function saveLangPair(source: string, target: string): Promise<void> {
  if (source === target) {
    showToast({ message: t("记忆库源语言和目标语言不能相同。"), error: true });
    return;
  }
  const generation = mountGeneration;
  const revision = invalidateTmQuery();
  const saveRevision = ++langPairSaveRevision;
  sourceLang = source;
  targetLang = target;
  page = 1;
  selectedIds.clear();
  cleaningState = "idle";
  conflictMessage = "";
  const pair = tmLangPair();
  recentPairs = [pair, ...recentPairs.filter((item) => item !== pair)].slice(0, 8);
  renderTable();
  updateSelectionUi();
  let queryLoaded = false;
  try {
    await persistLangPairInOrder({ tm_source_lang: source, tm_target_lang: target, recent_tm_lang_pairs: recentPairs }, generation, saveRevision);
    if (!currentTmScope(generation, revision)) return;
    if (!await refreshTm()) return;
    queryLoaded = true;
    await refreshConflicts();
    if (!currentTmScope(generation, revision)) return;
    rebuildToolbar();
    renderTable();
    renderFilterRow();
    renderTopbarStatus();
    // 切了语言对，「有 N 条清洗建议待复核」也要跟着换成新语言对下的数字。
    await refreshCleanBadge();
  } catch (error) {
    if (!currentTmScope(generation, revision)) return;
    if (!queryLoaded) restoreConfirmedQuery();
    rebuildToolbar();
    renderTable();
    renderFilterRow();
    renderTopbarStatus();
    showToast({ message: t("切换语言对失败：{0}", errorMessage(error)), error: true });
  }
}

function libSubtitle(): string {
  return t("翻译 Excel、Word 和 CAD 图纸时优先复用这里的译法");
}

function renderTopbarStatus(): void {
  if (!mounted) return;
  setTopbar({
    title: t("记忆库"),
    status: { label: t("{0} 条", fmtNum(total)), tone: "idle" },
    subtitle: libSubtitle(),
  });
}

// ---------------------------------------------------------------------------
// 条目 CRUD
// ---------------------------------------------------------------------------

/** 切换高级模式：存进设置（tm.advanced_mode），任务详情和设置页读同一项。 */
async function setAdvancedMode(next: boolean): Promise<void> {
  const previous = advancedMode;
  advancedMode = next;
  if (!next && ADVANCED_FILTERS.includes(entryFilter)) {
    await applyTmQueryChange(() => {
      entryFilter = "all";
      page = 1;
      selectedIds.clear();
    });
  }
  rebuildToolbar();
  renderFilterRow();
  renderTable();
  try {
    await persistSettings({ tm: { advanced_mode: next } });
  } catch (error) {
    advancedMode = previous;
    rebuildToolbar();
    renderFilterRow();
    renderTable();
    showToast({ message: t("保存失败：{0}", errorMessage(error)), error: true });
  }
}

// ---------------------------------------------------------------------------
// 右上角「设置」浮层
// ---------------------------------------------------------------------------

let tmSettingsBtn: HTMLButtonElement | null = null;
/** 展开中的「设置」浮层的关闭函数和重新定位函数；没展开时为 null。 */
let closeTmSettings: (() => void) | null = null;
let repositionTmSettings: (() => void) | null = null;

/**
 * 右上角「设置」：最上面是高级模式，打开后下面出现记忆库的三个开关。三个开关与
 * 「设置 · 翻译参数」的记忆库卡片是同一组设置（tm.*），数值项只在那边。每次展开都
 * 重新读一遍设置，设置页刚改过也能看到。
 */
async function toggleTmSettings(): Promise<void> {
  if (closeTmSettings) {
    closeTmSettings();
    return;
  }
  const generation = mountGeneration;
  let tm: JsonObject;
  try {
    const client = await getClient();
    tm = record((await client.request<JsonObject>("/api/settings")).tm);
  } catch (error) {
    showToast({ message: t("读取设置失败：{0}", errorMessage(error)), error: true });
    return;
  }
  if (!mounted || generation !== mountGeneration || closeTmSettings || !tmSettingsBtn) return;
  hideHint();
  closeMenus();

  const pop = document.createElement("div");
  pop.className = "menu tm-settings-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", t("记忆库设置"));

  const place = () => {
    if (tmSettingsBtn?.isConnected) placePopover(pop, tmSettingsBtn);
  };
  const saveSwitch = async (key: string, next: boolean): Promise<void> => {
    const previous = tm[key];
    tm[key] = next;
    try {
      await persistSettings({ tm: { [key]: next } });
    } catch (error) {
      tm[key] = previous;
      render();
      showToast({ message: t("保存失败：{0}", errorMessage(error)), error: true });
    }
  };
  const render = () => {
    pop.innerHTML = "";
    pop.append(createSwitchRow({
      label: t("高级模式"),
      hint: t("开启后，记忆库页面、记忆条目编辑窗口和任务详情中将显示采用方式、生效范围、使用次数等信息，并可在此调整记忆库选项。默认关闭。"),
      checked: advancedMode,
      onChange: (next) => {
        const saving = setAdvancedMode(next);
        render();
        // 保存失败时 setAdvancedMode 会把开关退回去，这里跟着重画。
        void saving.then(render);
      },
    }));
    if (advancedMode) {
      const group = document.createElement("div");
      group.className = "tm-settings-group";
      for (const def of tmSwitchDefs()) {
        group.append(createSwitchRow({
          label: def.label,
          hint: def.hint,
          checked: typeof tm[def.key] === "boolean" ? Boolean(tm[def.key]) : def.fallback,
          onChange: (next) => void saveSwitch(def.key, next),
        }));
      }
      pop.append(group);
    }
    const foot = document.createElement("p");
    foot.className = "tm-settings-foot";
    foot.append(document.createTextNode(t("重试次数和每批词条上限在「翻译参数」中设置。")));
    const go = document.createElement("button");
    go.type = "button";
    go.className = "resume-link";
    go.textContent = t("前往");
    go.addEventListener("click", () => {
      close();
      navigate("settings", { page: "params" });
    });
    foot.append(go);
    pop.append(foot);
    place();
  };

  const close = () => {
    pop.remove();
    document.removeEventListener("pointerdown", onOutside, true);
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("scroll", onScroll, true);
    window.removeEventListener("resize", close);
    if (closeTmSettings === close) {
      closeTmSettings = null;
      repositionTmSettings = null;
    }
    tmSettingsBtn?.setAttribute("aria-expanded", "false");
  };
  // 「?」提示浮层挂在 body 上，点它不算点到外面。
  const onOutside = (event: PointerEvent) => {
    const target = event.target as Element | null;
    if (target && (pop.contains(target) || tmSettingsBtn?.contains(target) || target.closest?.(".tip"))) return;
    close();
  };
  const onScroll = (event: Event) => {
    const target = event.target as Node | null;
    if (target && pop.contains(target)) return;
    close();
  };
  const onKey = (event: KeyboardEvent) => {
    if (event.key === "Escape") close();
  };

  document.body.append(pop);
  closeTmSettings = close;
  repositionTmSettings = place;
  tmSettingsBtn.setAttribute("aria-expanded", "true");
  render();
  document.addEventListener("pointerdown", onOutside, true);
  document.addEventListener("keydown", onKey);
  window.addEventListener("scroll", onScroll, true);
  window.addEventListener("resize", close);
}

function confirmBulkDelete(): void {
  if (!tmQueryReady() || !selectedIds.size) return;
  const count = selectedIds.size;
  const generation = mountGeneration;
  const revision = tmQueryRevision;
  const ids = [...selectedIds];
  openModal({
    tone: "danger",
    icon: "trash",
    title: t("批量删除记忆条目？"),
    body: [t("将永久删除已选中的 {0} 条记忆条目。此操作不能撤销。", fmtNum(count))],
    actions: [
      { label: t("取消"), variant: "default" },
      {
        label: t("删除"),
        variant: "danger-solid",
        onClick: async () => {
          const client = await getClient();
          if (!tmQueryReady() || !currentTmScope(generation, revision)) return;
          const result = await client.request<{ deleted: number; protected: number; missing: number }>("/api/tm/entries/bulk/delete", {
            method: "POST",
            // 记忆库页不再有「固定」的概念：页面上选中的就是要删的。
            body: JSON.stringify({ ids, allow_pinned: true }),
          });
          selectedIds.clear();
          await refreshTm();
          renderTable();
          renderFilterRow();
          showToast({
            message: result.protected
              ? t("已删除 {0} 条记忆条目，{1} 条固定词条未删除。", fmtNum(result.deleted), fmtNum(result.protected))
              : t("已删除 {0} 条记忆条目。", fmtNum(result.deleted)),
            error: Boolean(result.protected),
          });
        },
      },
    ],
  });
}

function fieldHint(textContent: string): HTMLDivElement {
  const hint = document.createElement("div");
  hint.className = "tm-hint";
  hint.textContent = textContent;
  return hint;
}

/** 新增 / 编辑记忆条目。普通模式只有原文、译文、备注；高级模式多出采用方式、生效范围等。 */
function openAddEditModal(editing: TmEntry | null): void {
  if (!tmQueryReady()) return;
  const generation = mountGeneration;
  const revision = tmQueryRevision;
  const sourceField = createTextField({ label: t("原文"), value: editing?.source_text ?? "", placeholder: t("例如：基坑支护") });
  const targetField = createTextField({ label: t("译文"), value: editing?.target_text ?? "", placeholder: t("例如：foundation pit support") });
  const noteField = createTextField({ label: t("备注（适用场景，可留空）"), value: editing?.note ?? "", placeholder: t("例如：基坑验收") });
  const body: Array<HTMLElement | string> = [
    t("手动维护的词条始终按设定译法翻译，在句中出现时同样采用。"),
    sourceField.root,
    targetField.root,
    noteField.root,
  ];

  // 采用方式：只有用户动过单选才写进去，否则按来源和备注推出默认值（备注一填就变成按场景选用）。
  let chosenUsage: string | null = null;
  const scopeField = createTextField({ label: t("生效范围（可选）"), value: editing?.scope ?? "" });
  const pairField = createTextField({ label: t("语言对"), value: tmLangPair(), disabled: Boolean(editing) });
  const allowReverse = pairAllowsReverse();
  let syncReverse = false;
  if (advancedMode) {
    const box = document.createElement("div");
    box.className = "tm-adv";
    const usageLabel = document.createElement("span");
    usageLabel.className = "tm-adv-label";
    usageLabel.textContent = t("采用方式");
    box.append(usageLabel);
    const current = text(editing?.effective_usage, noteField.input.value.trim() ? "scene" : "always");
    const radioName = `tm-usage-${Date.now()}`;
    for (const usage of ["always", "scene", "segment"]) {
      const option = document.createElement("label");
      option.className = "tm-opt";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = radioName;
      radio.value = usage;
      radio.checked = usage === current;
      radio.addEventListener("change", () => {
        if (radio.checked) chosenUsage = usage;
      });
      const copy = document.createElement("span");
      const name = document.createElement("b");
      name.textContent = USAGE_LABELS[usage];
      copy.append(name, fieldHint(USAGE_HINTS[usage]));
      option.append(radio, copy);
      box.append(option);
    }
    scopeField.root.append(fieldHint(t("仅当文件名或文件夹名包含所填文字时生效。")));
    box.append(scopeField.root, pairField.root);
    const syncRow = createSwitchRow({
      label: t("同时创建/更新反向术语（默认关闭）"),
      disabled: !allowReverse,
      onChange: (value) => {
        syncReverse = value;
      },
    });
    box.append(syncRow);
    if (!allowReverse) box.append(fieldHint(t("自定义目标语言不能生成反向语言对。")));
    if (editing) {
      const meta = document.createElement("div");
      meta.className = "tm-meta";
      for (const item of [
        t("来源：{0}", originLabel(editing)),
        t("使用次数：{0}", fmtNum(num(editing.use_count))),
        t("上次使用：{0}", text(editing.last_used_at).slice(0, 10) || "—"),
      ]) {
        const span = document.createElement("span");
        span.textContent = item;
        meta.append(span);
      }
      box.append(meta);
    }
    body.push(box);
  }

  trackModal(openModal({
    tone: "tint",
    icon: editing ? "edit" : "plus",
    title: editing ? t("编辑词条") : t("新增词条"),
    body,
    actions: [
      { label: t("取消"), variant: "default" },
      {
        label: t("保存"),
        variant: "primary",
        keepOpen: true,
        onClick: async (): Promise<void> => {
          const sourceText = sourceField.input.value.trim();
          const targetText = targetField.input.value.trim();
          const note = noteField.input.value.trim();
          if (!sourceText || !targetText) {
            showToast({ message: t("原文和译文都不能为空。"), error: true });
            return;
          }
          const client = await getClient();
          if (!tmQueryReady() || !currentTmScope(generation, revision)) return;
          // 被固定词条挡下时会登记一条待裁决冲突，这时要留住冲突提示，别顺手清空
          let keepConflictMessage = false;
          try {
            if (editing) {
              await client.request(`/api/tm/entries/${editing.id}`, {
                method: "PUT",
                body: JSON.stringify({
                  source_text: sourceText,
                  target_text: targetText,
                  sync_reverse: syncReverse,
                  note,
                  usage: chosenUsage,
                  scope: advancedMode ? scopeField.input.value.trim() : null,
                  // 记忆库页不再有「固定」的概念：固定过的旧词条同样可以直接编辑。
                  allow_pinned: true,
                }),
              });
              showToast({ message: t("记忆条目已更新。") });
            } else {
              // 后端如实回报这次新增的去向：被固定词条挡下、库里已有同一条译文，
              // 都不是「已保存」。照着 status 说，别再一律报成功。
              const saved = await client.request<{ changed?: boolean; status?: string; message?: string }>(
                "/api/tm/entries",
                {
                  method: "POST",
                  body: JSON.stringify({
                    source_text: sourceText,
                    target_text: targetText,
                    lang_pair: pairField.input.value.trim() || tmLangPair(),
                    sync_reverse: syncReverse,
                    note,
                    usage: chosenUsage ?? "",
                    scope: advancedMode ? scopeField.input.value.trim() : "",
                  }),
                },
              );
              const status = text(saved.status, saved.changed === false ? "unchanged" : "written");
              const backendMessage = text(saved.message);
              if (status === "written") {
                showToast({ message: syncReverse ? t("记忆条目已保存，并同步反向语言对。") : t("记忆条目已保存。") });
              } else if (status === "blocked_pinned") {
                showToast({ message: backendMessage ? tx(backendMessage) : t("该原文已有固定词条，新译文未写入。"), error: true });
                conflictMessage = backendMessage;
                keepConflictMessage = true;
                await refreshConflicts();
                renderConflictArea();
              } else {
                showToast({ message: backendMessage ? tx(backendMessage) : t("记忆条目未写入。") });
              }
            }
            if (!keepConflictMessage) conflictMessage = "";
            await refreshTm();
            rebuildToolbar();
            renderTable();
            renderFilterRow();
            currentModalClose?.();
          } catch (error) {
            const message = errorMessage(error);
            const rawMessage = error instanceof ApiError ? error.rawMessage : message;
            // 只有真的原文冲突才转到冲突裁决区；固定/清洗锁定这类拒绝原因
            // 和冲突无关，塞进冲突区会让用户照着完全无关的提示去操作。
            if (/conflict|冲突|重复/i.test(rawMessage)) { // i18n-ignore 匹配后端原话
              conflictMessage = message;
              await refreshConflicts();
              renderConflictArea();
              currentModalClose?.();
            } else {
              showToast({ message: t("保存失败：{0}", message), error: true });
            }
          }
        },
      },
    ],
  }));
}
// openModal 没有暴露「点击保存后手动关闭」以外的钩子；用一个模块级引用记录当前弹窗的
// close()，让保存成功的分支能主动收起弹窗（失败分支保留 keepOpen，让用户重试）。
let currentModalClose: (() => void) | null = null;
function trackModal<T extends { close(): void }>(handle: T): T {
  currentModalClose = handle.close;
  return handle;
}

function openDeleteModal(entryToDelete: TmEntry): void {
  if (!tmQueryReady()) return;
  const generation = mountGeneration;
  const revision = tmQueryRevision;
  trackModal(
    openModal({
      tone: "danger",
      icon: "trash",
      title: t("删除词条「{0}」？", entryToDelete.source_text),
      body: [t("删除后，之后翻译时不再采用这条译法；已生成的文件不受影响。此操作不能撤销。")],
      actions: [
        { label: t("取消"), variant: "default" },
        {
          label: t("删除"),
          variant: "danger-solid",
          onClick: async () => {
            const client = await getClient();
            if (!tmQueryReady() || !currentTmScope(generation, revision)) return;
            try {
              await client.request(`/api/tm/entries/${entryToDelete.id}?allow_pinned=true`, { method: "DELETE" });
            } catch (error) {
              // 固定词条后端回 409。不接住就只剩一个不关也不报错的弹窗，
              // 用户点「删除」什么都不发生（openModal 现在兜底，这里再补上「删除失败」前缀）。
              showToast({ message: t("删除失败：{0}", errorMessage(error)), error: true });
              return;
            }
            // 条目没了，选中集也得跟着少一个：不删的话「已选 xx / xx」的分子会大于分母，
            // 而且后续批量操作会带上一个已经不存在的 id。
            selectedIds.delete(entryToDelete.id);
            await refreshTm();
            renderTable();
            renderFilterRow();
            showToast({ message: t("记忆条目已删除。") });
          },
        },
      ],
    }),
  );
}

// ---------------------------------------------------------------------------
// 清洗记忆库（tm_clean 任务）
// ---------------------------------------------------------------------------

function openTaskRiskModal(payload: JsonObject, preflight: { requires_confirmation: boolean; confirmation_token?: string; risk?: JsonObject }): void {
  const risk = record(preflight.risk);
  const active = Array.isArray(risk.active_tasks) ? risk.active_tasks.map(record) : [];

  // 措辞和翻译工作区的同名确认框（workspace.ts showTaskRiskModal）保持一致。这里以前
  // 印的是「一次性令牌原子复检」「429」「合计潜在并发」和逐条连接的角色/并发明细——
  // 都是实现细节，用户要知道的只有：共用了什么、可能出什么事、自己的设置会不会被改。
  const body: Array<HTMLElement | string> = [
    t("本次清洗将与正在运行的任务使用同一条 API 连接。双方按各自的速度发送请求，服务商将同时收到两组请求。"),
    t("因此可能变慢或排队，个别请求可能超时失败；如该连接按用量计费，两项任务都将产生费用。设置中填写的速度不会改变。"),
  ];
  if (active.length) {
    const heading = document.createElement("p");
    heading.style.fontWeight = "var(--fw-600)";
    heading.textContent = t("正在运行的任务");
    body.push(heading);
    const list = document.createElement("ul");
    list.style.paddingLeft = "18px";
    list.style.fontSize = "12.5px";
    list.style.color = "var(--ink-2)";
    for (const item of active) {
      const li = document.createElement("li");
      li.textContent = tx(redactedText(item.source_label || item.task_label, t("未命名任务")));
      list.append(li);
    }
    body.push(list);
  }
  body.push(t("如不急于处理，建议待前一个任务完成后再开始。"));

  openModal({
    tone: "warn",
    icon: "warn",
    title: t("与正在运行的任务共用同一条连接"),
    body,
    actions: [
      { label: t("取消"), variant: "default" },
      {
        label: t("仍要并行启动"),
        variant: "primary",
        onClick: async () => {
          await submitTmCleanStart(payload, preflight.confirmation_token ?? "");
        },
      },
    ],
  });
}

async function submitTmCleanStart(payload: JsonObject, confirmationToken: string): Promise<void> {
  try {
    const client = await getClient();
    const task = await client.request<{ task_id: string }>("/api/tasks", {
      method: "POST",
      body: JSON.stringify(confirmationToken ? { ...payload, confirmation_token: confirmationToken } : payload),
    });
    cleaningState = "idle";
    showToast({ message: t("记忆库清洗已开始，可在任务中心查看进度。") });
    navigate("tasks", { taskId: task.task_id });
  } catch (error) {
    cleaningState = "error";
    renderStateRow();
    showToast({ message: t("记忆库清洗未能开始：{0}", errorMessage(error)), error: true });
  }
}

function confirmTmClean(): void {
  const langName = (options: LanguageOption[], code: string) => {
    const found = options.find((o) => o.code === code);
    return found ? tx(found.display_name) : code;
  };
  const pairLabel = `${langName(sourceOptions, sourceLang)} → ${langName(targetOptions, targetLang)}`;
  openModal({
    tone: "warn",
    icon: "warn",
    title: t("清洗记忆库？"),
    body: [
      keyword
        ? t("将由模型检查「{0}」中未固定的词条，找出译法不统一或有误之处，并给出修改建议。此操作将调用模型，按条数产生费用。", pairLabel)
        : t("将由模型检查「{0}」中未固定的词条（共 {1} 条，固定词条不参与），找出译法不统一或有误之处，并给出修改建议。此操作将调用模型，按条数产生费用。", pairLabel, fmtNum(total)),
      t("生成的建议需逐条确认后才会写入，记忆库不会被直接修改。"),
    ],
    actions: [
      { label: t("取消"), variant: "default" },
      { label: t("开始清洗"), variant: "primary", onClick: () => void tmClean() },
    ],
  });
}

async function tmClean(): Promise<void> {
  // 记忆库清洗也是新任务,同样吃「更新装好没重启 → 组件加载失败」这一刀(高-12)。
  if (consumePendingRestartWarning()) {
    openRestartBeforeTaskModal(() => void tmClean());
    return;
  }
  cleaningState = "running";
  conflictMessage = "";
  renderStateRow();
  renderConflictArea();
  try {
    const payload: JsonObject = { surface: "tm_clean", source_path: tmLangPair(), lang_pair: tmLangPair() };
    const client = await getClient();
    const preflight = await client.preflightTask(payload);
    if (preflight.requires_confirmation) {
      cleaningState = "idle";
      renderStateRow();
      openTaskRiskModal(payload, preflight);
      return;
    }
    await submitTmCleanStart(payload, "");
  } catch (error) {
    cleaningState = "error";
    renderStateRow();
    showToast({ message: t("记忆库清洗未能开始：{0}", errorMessage(error)), error: true });
  }
}

/** 拉一次当前语言对的待审建议——GET 本身会触发服务端过期清理，所以这也是
 * 「常驻待复核提示」与「面板顶部失效汇总」共用的唯一数据源，不另开接口。 */
async function fetchCleanSuggestions(): Promise<{ suggestions: JsonObject[]; staleCount: number }> {
  const client = await getClient();
  const payload = await client.request<JsonObject>(`/api/tm/clean/suggestions?lang_pair=${encodeURIComponent(tmLangPair())}`);
  return { suggestions: resultEntries(payload, ["suggestions", "items"]), staleCount: num(payload.stale_count) };
}

/** 刷新常驻提示的待审计数。取不到就当没有——这一行是锦上添花，不能因为它
 * 失败拖垮整页记忆库加载，也不该在拿不到数时弹错误 toast 打扰用户。 */
async function refreshCleanBadge(): Promise<void> {
  const generation = mountGeneration;
  const revision = tmQueryRevision;
  try {
    const { suggestions } = await fetchCleanSuggestions();
    if (!currentTmScope(generation, revision)) return;
    pendingCleanCount = suggestions.length;
  } catch {
    if (!currentTmScope(generation, revision)) return;
    pendingCleanCount = 0;
  }
  renderStateRow();
}

/** 常驻提示里的「查看建议」入口：不依赖上一次清洗任务，现拉现开。 */
async function openCleanReviewForCurrentPair(): Promise<void> {
  try {
    const { suggestions, staleCount } = await fetchCleanSuggestions();
    cleanSuggestions = suggestions;
    cleanStaleCount = staleCount;
    pendingCleanCount = suggestions.length;
    renderStateRow();
    openCleanReviewModal();
  } catch (error) {
    showToast({ message: t("加载清洗建议失败：{0}", errorMessage(error)), error: true });
  }
}

async function loadCleanSuggestions(taskId: string): Promise<void> {
  cleanTaskId = taskId;
  let suggestions: JsonObject[] = [];
  const client = await getClient();
  try {
    const resultStatus = await client.getTaskResult(taskId);
    suggestions = resultEntries(record(resultStatus.result), ["suggestions", "tm_suggestions", "review_suggestions"]);
  } catch {
    // 该结果可能有意不包含 TM 建议明细；下面走专门的复核接口兜底。
  }
  // 无论上面拿没拿到，都再走一次复核接口——它是 stale_count 的唯一来源，
  // 且顺带触发一次服务端过期清理，不能只在 suggestions 为空时才调用。
  const fetched = await fetchCleanSuggestions().catch(() => ({ suggestions: [] as JsonObject[], staleCount: 0 }));
  if (!suggestions.length) suggestions = fetched.suggestions;
  cleanSuggestions = suggestions;
  cleanStaleCount = fetched.staleCount;
  pendingCleanCount = fetched.suggestions.length;
  openCleanReviewModal();
}

interface SubmittedSuggestion {
  suggestion_id: number;
  entry_id: number;
  source_text: string;
  old_target: string;
  new_target: string;
  accepted: boolean;
  lang_pair: string;
  expected_version: string;
}

interface ApplySuggestionsResult {
  applied: number;
  unchanged: number;
  skipped: number;
  outcomes: { suggestion_id: number; outcome: string }[];
}

// 逐条去向 → chip 语气 + 文案 + 原因说明。「已写入」没有额外原因（写成功了不需要
// 解释）；其余几种都要说清楚「为什么没按你勾的写」，用户才知道要不要再处理一次。
const CLEAN_OUTCOME_INFO: Record<string, { tone: ChipTone; label: string; reason?: string }> = localized(() => ({
  updated: { tone: "ok", label: t("已写入") },
  unchanged: { tone: "mute", label: t("未改动"), reason: t("库中译文已经是这个值") },
  pinned: { tone: "warn", label: t("已跳过"), reason: t("词条已固定，不接受清洗建议；如需修改，请先解除固定") },
  stale: { tone: "warn", label: t("已跳过"), reason: t("词条在审阅期间已被修改，建议已过期") },
  missing: { tone: "warn", label: t("已跳过"), reason: t("词条已被删除") },
  invalid: { tone: "warn", label: t("已跳过"), reason: t("译文为空，未写入；该条仍保留在待审列表") },
}));
const CLEAN_OUTCOME_UNSELECTED = localized(() => ({ tone: "mute" as ChipTone, label: t("未勾选"), reason: t("保留在待审列表，下次打开时可继续处理") }));
// 服务端没回报去向时（旧版本服务端、或响应里缺了这一条）不编结论：
// 「未改动＝库里已经是这个值」是个事实断言，猜错会误导用户。
const CLEAN_OUTCOME_UNKNOWN = localized(() => ({ tone: "mute" as ChipTone, label: t("已提交"), reason: t("服务端未返回该条的写入结果") }));

function openCleanReviewModal(): void {
  const checks: HTMLInputElement[] = [];
  const targets: HTMLInputElement[] = [];
  const body: HTMLElement[] = [];
  if (cleanStaleCount > 0) {
    const staleNote = document.createElement("div");
    staleNote.style.cssText =
      "background:var(--ground);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:13px";
    staleNote.textContent = t("另有 {0} 条建议已失效：建议生成后，对应词条已被修改或固定；为避免覆盖您的修改，已自动作废。", fmtNum(cleanStaleCount));
    body.push(staleNote);
  }
  if (!cleanSuggestions.length) {
    const empty = document.createElement("p");
    empty.textContent = t("未生成可写入的建议。");
    body.push(empty);
  }
  cleanSuggestions.forEach((suggestion, index) => {
    const row = document.createElement("div");
    row.style.display = "flex";
    row.style.gap = "8px";
    row.style.alignItems = "flex-start";
    row.style.padding = "8px 0";
    row.style.borderTop = index ? "1px solid var(--line)" : "none";
    const check = document.createElement("input");
    check.type = "checkbox";
    check.className = "ck";
    // 默认全不勾：清洗建议要用户逐条看过才写入。默认全勾时「确认写入」一键
    // 就能拿建议盖掉记忆库里的人工校对（审计 高-7 的产品半边）。
    check.checked = false;
    check.style.marginTop = "3px";
    checks.push(check);
    const info = document.createElement("div");
    info.style.flex = "1";
    const title = document.createElement("b");
    title.textContent = text(suggestion.source_text);
    // 原译文单独一行，建议译文的输入框另起一行占满整行：以前两者挤在同一行、输入框只有
    // 六成宽，「Construction Schedule No. 11」被截成「No. 1」，用户看不全就没法判断该不该写入。
    const line = document.createElement("p");
    line.style.cssText = "margin:3px 0 0;font-size:12.5px;color:var(--ink-3)";
    line.textContent = t("原译文：{0}", text(suggestion.old_target));
    const targetInput = document.createElement("input");
    targetInput.type = "text";
    targetInput.value = text(suggestion.new_target);
    targetInput.title = text(suggestion.new_target);
    targetInput.setAttribute("aria-label", t("建议译文"));
    targetInput.style.cssText = "width:100%;box-sizing:border-box;margin-top:6px";
    targets.push(targetInput);
    info.style.minWidth = "0";
    info.append(title, line, targetInput);
    row.append(check, info);
    body.push(row);
  });

  const handle = openModal({
    tone: "warn",
    icon: "book",
    title: t("清洗建议"),
    body: body.length ? body : [t("未生成可写入的建议。")],
    // 译文常是一整句英文，400px 的默认宽度放不下。
    wide: true,
    actions: [
      { label: t("取消"), variant: "default" },
      {
        // 一条没勾就点（默认全不勾之后大概率是误触）时留在原地提示、不发请求——
        // 原来会无声提交一个全 false 的空单，后端不动一条。写入成功也不在这里
        // 收弹窗：结果原地重渲染，用户看完每一行的去向再点「完成」才关。
        label: t("写入已勾选建议"),
        variant: "primary",
        keepOpen: true,
        onClick: async () => {
          const client = await getClient();
          // 建议主键与 expected_version 必须原样回传：前者让后端把已处理的建议
          // 从待审列表里销账，后者是乐观并发的凭据——不带就等于关掉版本校验，
          // 审阅期间被别人改过的译文会被这次确认无声覆盖。
          const suggestions: SubmittedSuggestion[] = cleanSuggestions.map((suggestion, index) => ({
            suggestion_id: num(suggestion.id),
            entry_id: num(suggestion.entry_id),
            source_text: text(suggestion.source_text),
            old_target: text(suggestion.old_target),
            new_target: targets[index]?.value ?? text(suggestion.new_target),
            accepted: checks[index]?.checked ?? false,
            lang_pair: text(suggestion.lang_pair, tmLangPair()),
            expected_version: text(suggestion.expected_version),
          }));
          const accepted = suggestions.filter((item) => item.accepted).length;
          if (!accepted) {
            showToast({ message: t("尚未勾选任何建议。请勾选后再写入；如暂不处理，请点击「取消」。") });
            return;
          }
          const result = await client.request<{
            applied: number;
            unchanged?: number;
            skipped?: number;
            outcomes?: { suggestion_id: number; outcome: string }[];
          }>("/api/tm/clean/apply", {
            method: "POST",
            body: JSON.stringify({ suggestions, auto_pin: false }),
          });
          cleanSuggestions = [];
          cleaningState = "idle";
          // 「库里本来就一样」与「被别人改过／已固定」是两回事，分开说。
          // 后端未回这两个字段时（旧服务端）才退回按差额估算，宁可少说也不误报。
          const applied = num(result.applied);
          const unchanged = num(result.unchanged);
          const skipped =
            result.skipped === undefined
              ? Math.max(accepted - applied, 0)
              : num(result.skipped);
          // 先把逐行结果画出来再刷新背后的表格：写入已经发生，结果弹窗是它
          // 唯一的凭据，不能因为列表刷新抛错就一并丢掉。
          renderApplyResult(suggestions, {
            applied,
            unchanged,
            skipped,
            outcomes: Array.isArray(result.outcomes) ? result.outcomes : [],
          });
          try {
            await refreshTm();
            renderTable();
            renderFilterRow();
          } catch {
            showToast({ message: t("记忆库列表刷新失败，切换语言对或重新进入本页可重新加载。") });
          }
        },
      },
    ],
  });

  /** 写入完成后原地重渲染弹窗正文与底部按钮：复选框/输入框全撤下，换成
   * 「顶部汇总 + 每行去向 chip」，直到用户看完点「完成」才真正关闭。
   * 直接操作 handle.element 里的 .mbody/.macts——openModal 本身没提供
   * 「换内容」的接口，这两个类名是它内部渲染出来的挂载点。 */
  function renderApplyResult(submitted: SubmittedSuggestion[], result: ApplySuggestionsResult): void {
    const mbody = handle.element.querySelector<HTMLDivElement>(".mbody");
    const acts = handle.element.querySelector<HTMLDivElement>(".macts");
    if (!mbody || !acts) return;
    const outcomeById = new Map(result.outcomes.map((item) => [num(item.suggestion_id), text(item.outcome)]));
    const notAccepted = submitted.filter((item) => !item.accepted).length;
    const parts: string[] = [];
    if (result.applied > 0) parts.push(t("已写入 {0} 条", fmtNum(result.applied)));
    if (result.unchanged > 0) parts.push(t("{0} 条与库中译文相同", fmtNum(result.unchanged)));
    if (result.skipped > 0) parts.push(t("{0} 条跳过", fmtNum(result.skipped)));
    if (notAccepted > 0) parts.push(t("{0} 条未勾选，留待下次复核", fmtNum(notAccepted)));

    mbody.innerHTML = "";
    const summary = document.createElement("p");
    summary.style.fontWeight = "var(--fw-600)";
    summary.textContent = parts.length ? t("{0}。", parts.join(t("；"))) : t("没有条目被处理。");
    mbody.append(summary);

    submitted.forEach((item, index) => {
      const row = document.createElement("div");
      row.style.display = "flex";
      row.style.flexDirection = "column";
      row.style.gap = "4px";
      row.style.padding = "8px 0";
      row.style.borderTop = index ? "1px solid var(--line)" : "none";
      if (!item.accepted) row.style.opacity = "0.55";

      const title = document.createElement("b");
      title.textContent = item.source_text;
      // 结果页不再可编辑，用整段文字代替禁用的窄输入框，长译文能折行看全。
      const line = document.createElement("p");
      line.style.margin = "3px 0 0";
      line.textContent = `${item.old_target} → ${item.new_target}`;

      const tail = document.createElement("div");
      tail.style.cssText = "display:flex;align-items:center;gap:6px;margin-top:2px;flex-wrap:wrap";
      const outcomeInfo = item.accepted
        ? (CLEAN_OUTCOME_INFO[outcomeById.get(item.suggestion_id) ?? ""] ?? CLEAN_OUTCOME_UNKNOWN)
        : CLEAN_OUTCOME_UNSELECTED;
      tail.append(createChip({ label: outcomeInfo.label, tone: outcomeInfo.tone }));
      if (outcomeInfo.reason) {
        const reasonEl = document.createElement("span");
        reasonEl.style.cssText = "font-size:12px;color:var(--ink-3)";
        reasonEl.textContent = outcomeInfo.reason;
        tail.append(reasonEl);
      }
      row.append(title, line, tail);
      mbody.append(row);
    });

    acts.innerHTML = "";
    acts.append(
      createButton({
        label: t("完成"),
        variant: "primary",
        onClick: () => {
          handle.close();
          void refreshCleanBadge();
        },
      }),
    );
  }
}

// ---------------------------------------------------------------------------
// 导入 / 导出
// ---------------------------------------------------------------------------

async function exportTm(format: "json" | "csv"): Promise<void> {
  try {
    const client = await getClient();
    const payload = await client.request<JsonObject>(`/api/tm/export?lang_pair=${encodeURIComponent(tmLangPair())}`);
    const list = Array.isArray(payload.entries) ? (payload.entries as JsonObject[]) : [];
    // 成功 toast 只能等写盘结束后再弹；saved 为 null 说明用户在保存框里按了取消，
    // 这时候什么都不该说（旧代码是无条件弹的，于是界面报告了一件没发生的事）。
    // CSV 前面加 UTF-8 BOM：不带 BOM 的 UTF-8 CSV 被 Excel 当成本地编码（简中 Windows
    // 上是 GBK）打开，整份译文变乱码——而记忆库导出的第一去处就是 Excel。JSON 不能加，
    // 那会让它不再是合法 JSON。导入侧 parseTmCsv 会把 BOM 剥掉，往返仍然成立。
    const saved = format === "csv"
      ? await saveTextFile(`translator-tm-${tmLangPair()}.csv`, `﻿${toTmCsv(list)}`)
      : await saveJsonFile(`translator-tm-${tmLangPair()}.json`, payload);
    if (!saved) return;
    showToast({ message: t("已导出当前语言对的 {0} 文件到 {1}，共 {2} 条。", format.toUpperCase(), saved, fmtNum(list.length)) });
  } catch (error) {
    showToast({ message: t("导出失败：{0}", errorMessage(error)), error: true });
  }
}

async function exportFullTm(): Promise<void> {
  try {
    const client = await getClient();
    const payload = await client.request<JsonObject>("/api/tm/export/full");
    const saved = await saveJsonFile("translator-tm-full.json", payload);
    if (!saved) return; // 用户取消保存框：静默返回
    showToast({ message: t("已导出完整记忆库到 {0}（全部语言对、待裁决冲突和自定义目标语言）。", saved) });
  } catch (error) {
    showToast({ message: t("导出失败：{0}", errorMessage(error)), error: true });
  }
}

function pickFile(accept: string, onFile: (file: File) => void): void {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = accept;
  input.onchange = () => {
    const file = input.files?.[0];
    if (file) onFile(file);
  };
  input.click();
}

function importTm(): void {
  pickFile("application/json,.json,text/csv,.csv", async (file) => {
    try {
      const raw = await file.text();
      const isCsv = file.name.toLocaleLowerCase().endsWith(".csv") || file.type.includes("csv");
      let previewEntries: TmImportEntry[];
      let langPair = tmLangPair();
      if (isCsv) {
        previewEntries = parseTmCsv(raw);
      } else {
        const payload = JSON.parse(raw) as JsonObject;
        if (text(payload.format_version) === "tm-full-v1") {
          openFullImportPreviewModal(file.name, payload);
          return;
        }
        previewEntries = Array.isArray(payload.entries)
          ? (payload.entries as unknown[]).filter((entry): entry is TmImportEntry => Boolean(entry && typeof entry === "object"))
          : [];
        langPair = text(payload.lang_pair, tmLangPair());
      }
      openImportPreviewModal(file.name, isCsv ? "csv" : "json", langPair, previewEntries);
    } catch (error) {
      showToast({ message: t("无法读取导入文件：{0}", errorMessage(error)), error: true });
    }
  });
}

function importFullTm(): void {
  pickFile("application/json,.json", async (file) => {
    try {
      const payload = JSON.parse(await file.text()) as JsonObject;
      if (text(payload.format_version) !== "tm-full-v1") throw new Error(t("这不是 Translator 导出的完整记忆库备份。"));
      openFullImportPreviewModal(file.name, payload);
    } catch (error) {
      showToast({ message: t("无法读取完整记忆库备份：{0}", errorMessage(error)), error: true });
    }
  });
}

function openImportPreviewModal(fileName: string, format: "json" | "csv", langPair: string, previewEntries: TmImportEntry[]): void {
  const pairField = createTextField({ label: t("目标语言对"), value: langPair, placeholder: t("例如 zh-en") });
  const modeField = createSelectField({
    label: t("重复项"),
    value: "skip",
    options: [
      { value: "skip", label: t("跳过重复项") },
      { value: "overwrite", label: t("覆盖重复项") },
      { value: "keep_both", label: t("保留两者，列入待裁决冲突") },
    ],
  });
  let syncReverse = false;
  const syncRow = createSwitchRow({
    label: t("同时写入反向语言对"),
    disabled: targetIsCustom(splitTmPair(langPair)?.target ?? targetLang),
    onChange: (value) => {
      syncReverse = value;
    },
  });

  const summary = document.createElement("p");
  summary.textContent = t("{0} · {1} · 共 {2} 条。请确认字段映射和重复项策略后再写入。", fileName, format.toUpperCase(), fmtNum(previewEntries.length));

  const table = document.createElement("table");
  table.className = "tbl";
  const head = document.createElement("tr");
  for (const label of [t("原文"), t("译文"), t("来源")]) {
    const th = document.createElement("th");
    th.textContent = label;
    head.append(th);
  }
  table.append(head);
  for (const entry of previewEntries.slice(0, 8)) {
    const tr = document.createElement("tr");
    for (const value of [entry.source_text, entry.target_text, text(entry.word_type, "-")]) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(td);
    }
    table.append(tr);
  }
  const tableWrap = document.createElement("div");
  tableWrap.style.maxHeight = "220px";
  tableWrap.style.overflow = "auto";
  tableWrap.style.border = "1px solid var(--line)";
  tableWrap.style.borderRadius = "var(--r-md)";
  tableWrap.append(table);

  const body: HTMLElement[] = [summary, pairField.root, modeField.root, syncRow, tableWrap];
  if (previewEntries.length > 8) {
    const note = document.createElement("p");
    note.textContent = t("仅显示前 {0} 条预览。", 8);
    body.push(note);
  }

  openModal({
    tone: "warn",
    icon: "folder",
    title: t("导入预览"),
    body,
    actions: [
      { label: t("取消"), variant: "default" },
      {
        label: t("确认导入"),
        variant: "primary",
        onClick: async () => {
          if (!previewEntries.length) return;
          try {
            const client = await getClient();
            const pairValue = pairField.input.value.trim() || langPair;
            const result = await client.request<{ inserted?: number; skipped?: number; duplicates?: number }>("/api/tm/import", {
              method: "POST",
              body: JSON.stringify({
                lang_pair: pairValue,
                mode: modeField.select.value,
                entries: previewEntries,
                sync_reverse: !targetIsCustom(splitTmPair(pairValue)?.target ?? targetLang) && syncReverse,
              }),
            });
            await refreshTm();
            renderTable();
            renderFilterRow();
            showToast({ message: t("已完成导入：新增或更新 {0}，跳过 {1}，重复 {2}。", fmtNum(num(result.inserted)), fmtNum(num(result.skipped)), fmtNum(num(result.duplicates))) });
          } catch (error) {
            showToast({ message: t("导入失败：{0}", errorMessage(error)), error: true });
          }
        },
      },
    ],
  });
}

function openFullImportPreviewModal(fileName: string, payload: JsonObject): void {
  const entryCount = Array.isArray(payload.entries) ? payload.entries.length : 0;
  const conflictCount = Array.isArray(payload.conflict_candidates) ? payload.conflict_candidates.length : 0;
  const customLangCount = Array.isArray(payload.custom_target_langs) ? payload.custom_target_langs.length : 0;

  const summary = document.createElement("p");
  summary.textContent = t("{0} · 将检查并恢复 {1} 条词条、{2} 条待裁决冲突和 {3} 个自定义目标语言。", fileName, fmtNum(entryCount), fmtNum(conflictCount), fmtNum(customLangCount));

  const modeField = createSelectField({
    label: t("重复项"),
    value: "skip",
    options: [
      { value: "skip", label: t("跳过重复项") },
      { value: "overwrite", label: t("用备份里的译文覆盖重复项") },
      { value: "keep_both", label: t("保留两者，列入待裁决冲突") },
    ],
  });
  const codeMapField = createTextField({ label: t("自定义代码映射（可选 JSON）"), value: "{}", placeholder: t("例如 {0}", '{"x-custom-old":"x-custom-new"}') });
  const note = document.createElement("p");
  note.textContent = t("完整备份包含原文和译文；代码定义冲突时必须填写映射，无法映射则取消恢复。");

  openModal({
    tone: "warn",
    icon: "folder",
    title: t("恢复完整记忆库"),
    body: [summary, modeField.root, codeMapField.root, note],
    actions: [
      { label: t("取消"), variant: "default" },
      {
        label: t("确认恢复"),
        variant: "primary",
        onClick: async () => {
          if (!entryCount) return;
          let codeMap: Record<string, string> = {};
          try {
            const parsed = JSON.parse(codeMapField.input.value.trim() || "{}") as unknown;
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(t("代码映射必须是 JSON 对象。"));
            codeMap = Object.fromEntries(Object.entries(parsed as Record<string, unknown>).map(([key, value]) => [key, String(value)]));
          } catch (error) {
            showToast({ message: t("代码映射格式无效：{0}", errorMessage(error)), error: true });
            return;
          }
          try {
            const client = await getClient();
            const result = await client.request<{ inserted?: number; skipped?: number; duplicates?: number; conflicts?: number }>("/api/tm/import/full", {
              method: "POST",
              body: JSON.stringify({ ...payload, mode: modeField.select.value, code_map: codeMap, sync_reverse: false }),
            });
            await refreshTm();
            await refreshConflicts();
            renderTable();
            renderFilterRow();
            renderConflictArea();
            showToast({
              message: t("新增或更新 {0} 条，跳过 {1} 条，重复 {2} 条，恢复待裁决冲突 {3} 条。", fmtNum(num(result.inserted)), fmtNum(num(result.skipped)), fmtNum(num(result.duplicates)), fmtNum(num(result.conflicts))),
            });
          } catch (error) {
            showToast({ message: t("恢复失败：{0}", errorMessage(error)), error: true });
          }
        },
      },
    ],
  });
}

async function resolveTmConflict(candidateId: number, action: string): Promise<void> {
  try {
    const client = await getClient();
    await client.request(`/api/tm/conflicts/${candidateId}/resolve`, { method: "POST", body: JSON.stringify({ action }) });
    await refreshConflicts();
    await refreshTm();
    renderConflictArea();
    renderTable();
    renderFilterRow();
  } catch (error) {
    showToast({ message: t("裁决失败：{0}", errorMessage(error)), error: true });
  }
}

// ---------------------------------------------------------------------------
// 下拉菜单（导入 ▾ / 导出 ▾）
// ---------------------------------------------------------------------------

function openMenu(anchor: HTMLElement, items: Array<{ label: string; onClick: () => void }>): void {
  openSharedMenu(anchor, items.map((item): MenuItem => ({ label: item.label, onSelect: item.onClick })));
}

function closeMenus(): void {
  closeMenu();
}

// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------

/**
 * 工具条上的语言框。原来是个固定 32px 高、宽度随内容压缩的原生 select，长语言名
 * （「柬埔寨语（高棉语）」这种）在这条挤满按钮的工具条里显示不全——换成共享的可搜索
 * 选择器后，按钮按样张给的 min-width:150px 起步、随内容变宽，长名能完整显示，
 * 挑语言也不用再从 59 项里滚。
 */
function langPicker(
  options: LanguageOption[],
  value: string,
  recentKey: string,
  onChange: (next: string) => void,
): HTMLDivElement {
  return createLanguagePicker({ options, value, recentKey, onChange }).root;
}

/** 交换语言方向：自定义目标语言没有对应的源语言，不能反过来。 */
function canSwapPair(): boolean {
  return pairAllowsReverse()
    && sourceOptions.some((option) => option.code === targetLang)
    && targetOptions.some((option) => option.code === sourceLang);
}

/** 「更多 ▾」：清洗记忆库、记忆库设置。条件不满足时项仍在，点了说明原因。 */
function openMoreMenu(anchor: HTMLButtonElement): void {
  // 设置浮层开着时再点「更多」就是收起它，和原来的「设置」按钮一样。
  if (closeTmSettings) {
    closeTmSettings();
    return;
  }
  openMenu(anchor, [
    {
      label: t("清洗记忆库"),
      onClick: () => {
        // 清洗会调用模型、按条数花钱，所以点了先确认；库是空的时候没东西可洗。
        if (cleaningState === "running") showToast({ message: t("正在分析未固定条目…") });
        else if (!keyword && total === 0) showToast({ message: t("当前语言对没有可清洗的词条") });
        else confirmTmClean();
      },
    },
    { label: t("记忆库设置"), onClick: () => void toggleTmSettings() },
  ]);
}

function rebuildToolbar(): void {
  if (!libTopEl) return;
  clearElement(libTopEl);

  // 语言对组合控件：源语言 ⇄ 目标语言，一个外框。
  const pair = document.createElement("div");
  pair.className = "pair";
  const swap = document.createElement("button");
  swap.type = "button";
  swap.className = "swap";
  swap.append(icon("convert", { size: "sm" }));
  const swappable = canSwapPair();
  swap.disabled = !swappable;
  swap.title = swappable ? t("交换语言方向") : t("自定义目标语言不能交换");
  swap.setAttribute("aria-label", swap.title);
  swap.addEventListener("click", () => {
    if (canSwapPair()) void saveLangPair(targetLang, sourceLang);
  });
  pair.append(
    langPicker(sourceOptions, sourceLang, "tm-source", (next) => void saveLangPair(next, targetLang)),
    swap,
    langPicker(targetOptions, targetLang, "tm-target", (next) => void saveLangPair(sourceLang, next)),
  );
  libTopEl.append(pair);

  const searchWrap = document.createElement("div");
  searchWrap.className = "lib-search";
  const searchInput = document.createElement("input");
  searchInput.type = "search";
  // 窄窗口里整句放不下就写「搜索…」，整句进悬停提示。
  fitPlaceholder(searchInput, t("搜索原文、译文或备注…"), t("搜索…"));
  searchInput.setAttribute("aria-label", t("搜索原文、译文或备注…"));
  searchInput.value = keyword;
  searchInput.addEventListener("input", () => {
    if (searchDebounce !== null) window.clearTimeout(searchDebounce);
    searchDebounce = window.setTimeout(() => {
      // keyword 直到真正发起这次取数才更新：提前赋值会让失败回滚拿不到
      // 「当前表格对应的关键词」，回滚后状态和屏幕上的数据对不上。
      void applyTmQueryChange(() => {
        keyword = searchInput.value;
        page = 1;
        // 搜索关键词变了，「选择全部」圈定的结果集也跟着变——旧的选中集合可能包含
        // 现在已经看不见的条目，继续留着容易误批量删除，所以筛选条件一变就清空。
        selectedIds.clear();
      });
    }, 320);
  });
  searchWrap.append(icon("search", { size: "sm" }), searchInput);
  libTopEl.append(searchWrap);

  // 按钮组：主按钮「新增词条」+ 导入 / 导出 + 更多。窄窗口下整组换行，按钮文字不折行。
  const actions = document.createElement("div");
  actions.className = "acts";
  actions.append(createButton({ label: t("新增词条"), icon: "plus", variant: "primary", onClick: () => openAddEditModal(null) }));
  const importBtn = createButton({
    label: t("导入"),
    icon: "down",
    onClick: (event) =>
      openMenu(event.currentTarget as HTMLElement, [
        { label: t("从 JSON / CSV 导入"), onClick: () => importTm() },
        { label: t("恢复完整备份（JSON）"), onClick: () => importFullTm() },
      ]),
  });
  const exportBtn = createButton({
    label: t("导出"),
    icon: "upload",
    onClick: (event) =>
      openMenu(event.currentTarget as HTMLElement, [
        { label: t("导出 JSON"), onClick: () => void exportTm("json") },
        { label: t("导出 CSV"), onClick: () => void exportTm("csv") },
        { label: t("导出完整备份"), onClick: () => void exportFullTm() },
      ]),
  });
  // 「记忆库设置」浮层（toggleTmSettings）挂在「更多」按钮上；工具条重建后浮层改挂到新按钮。
  const moreBtn = createButton({ label: t("更多"), onClick: () => openMoreMenu(moreBtn) });
  moreBtn.classList.add("lib-more");
  moreBtn.append(icon("chev", { size: "sm" }));
  moreBtn.setAttribute("aria-haspopup", "menu");
  moreBtn.setAttribute("aria-expanded", String(Boolean(closeTmSettings)));
  tmSettingsBtn = moreBtn;
  actions.append(importBtn, exportBtn, moreBtn);
  libTopEl.append(actions);
  repositionTmSettings?.();

  renderFilterRow();
  renderRecentPairs();
  renderStateRow();
  renderConflictArea();
}

/** 筛选行：页签（计数在后）+ 规则一句话；高级模式在行尾多一句本月统计。 */
function renderFilterRow(): void {
  if (!libFiltersEl) return;
  clearElement(libFiltersEl);
  const filters = document.createElement("div");
  filters.className = "tk-filters wrap";
  for (const filter of advancedMode ? [...BASIC_FILTERS, ...ADVANCED_FILTERS] : BASIC_FILTERS) {
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = filter === entryFilter ? "f on" : "f";
    const count = document.createElement("b");
    count.textContent = fmtNum(num(filterCounts[filter]));
    pill.append(document.createTextNode(FILTER_LABELS[filter]), count);
    pill.addEventListener("click", () => {
      if (filter === entryFilter) return;
      void applyTmQueryChange(() => {
        entryFilter = filter;
        page = 1;
        selectedIds.clear();
      }).then(() => renderFilterRow());
    });
    filters.append(pill);
  }
  libFiltersEl.append(filters);

  const rule = document.createElement("span");
  rule.className = "lib-rule";
  rule.textContent = t("手动维护的词条始终按设定译法翻译；自动保存的仅供模型参考，按备注里的场景选用。");
  libFiltersEl.append(rule);
  renderMetaLine();
}

/** 高级模式的本月统计（原来的统计卡）；普通模式不显示。未采用设定译法要人工核对，>0 时标橙。 */
function renderMetaLine(): void {
  if (!libFiltersEl || !advancedMode) return;
  const notApplied = num(stats.month_not_applied);
  const meta = document.createElement("span");
  meta.className = "lib-meta";
  const line = t("本月使用 {0} 次 · 未采用设定译法 {1} 处", fmtNum(num(stats.month_uses)), fmtNum(notApplied));
  const cut = line.lastIndexOf(" · ");
  if (notApplied > 0 && cut > 0) {
    const attn = document.createElement("span");
    attn.className = "attn";
    attn.textContent = line.slice(cut + 3);
    meta.append(document.createTextNode(line.slice(0, cut + 3)), attn);
  } else {
    meta.textContent = line;
  }
  libFiltersEl.append(meta);
}

/** 最近使用的语言对：小字标签 + 胶囊，当前语言对高亮；没有记录时不占位。 */
function renderRecentPairs(): void {
  if (!libRecentEl) return;
  clearElement(libRecentEl);
  libRecentEl.hidden = recentPairs.length === 0;
  if (!recentPairs.length) return;
  const label = document.createElement("span");
  label.className = "lib-recent-label";
  label.textContent = t("最近使用");
  libRecentEl.append(label);
  for (const pair of recentPairs.slice(0, 8)) {
    const parsed = splitTmPair(pair);
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = pair === tmLangPair() ? "f on" : "f";
    pill.textContent = tmPairLabel(pair);
    pill.addEventListener("click", () => {
      if (parsed) void saveLangPair(parsed.source, parsed.target);
    });
    libRecentEl.append(pill);
  }
}

/** 提示区（清洗状态、冲突）没有内容时整块收起，不占高度。 */
function syncNoticeVisibility(): void {
  if (!libNoticeEl) return;
  libNoticeEl.hidden = !stateRowEl?.childElementCount && !conflictHostEl?.childElementCount;
}

function renderStateRow(): void {
  if (!stateRowEl) return;
  clearElement(stateRowEl);
  if (cleaningState !== "idle") {
    // 正在跑清洗 / 刚失败 / 刚生成待复核，这三种「本次会话内」的状态优先于
    // 常驻提示——用户此刻更需要知道这次操作的结果，不是「还有旧建议没看」。
    const tone: ChipTone = cleaningState === "error" ? "dgr" : cleaningState === "ready" ? "warn" : "tint";
    const label =
      cleaningState === "running" ? t("正在分析未固定条目…") : cleaningState === "ready" ? t("清洗建议已生成，请复核后写入。") : t("清洗失败，请检查模型连接后重试。");
    stateRowEl.append(createChip({ label, tone }));
    if (cleaningState === "ready" && cleanTaskId) {
      stateRowEl.append(createButton({ label: t("查看建议"), size: "mini", onClick: () => openCleanReviewModal() }));
    }
  } else if (pendingCleanCount > 0) {
    // 常驻提示：不依赖「刚跑完清洗」这次会话，只要当前语言对下还有没处理的
    // 建议就一直显示，进任务中心深链复核完、或写完最后一条后自动消失。
    stateRowEl.append(createChip({ label: t("有 {0} 条清洗建议待复核", fmtNum(pendingCleanCount)), tone: "warn" }));
    stateRowEl.append(createButton({ label: t("查看建议"), size: "mini", onClick: () => void openCleanReviewForCurrentPair() }));
  }
  syncNoticeVisibility();
}

function renderConflictArea(): void {
  const host = conflictHostEl;
  if (!host) return;
  clearElement(host);
  if (conflictMessage) {
    const row = document.createElement("div");
    row.className = "lib-conflict-msg";
    row.append(createChip({ label: tx(conflictMessage), tone: "dgr" }));
    const closeBtn = createButton({ label: t("关闭"), size: "mini", onClick: () => { conflictMessage = ""; renderConflictArea(); } });
    row.append(closeBtn);
    host.append(row);
  }
  if (conflicts.length) {
    const list = document.createElement("div");
    list.className = "lib-conflicts";
    const header = document.createElement("div");
    header.className = "lib-conflicts-head";
    const title = document.createElement("strong");
    title.textContent = t("待裁决冲突 {0}", fmtNum(conflicts.length));
    header.append(title);
    header.append(createButton({ label: t("刷新"), size: "mini", onClick: () => void refreshConflicts().then(renderConflictArea) }));
    list.append(header);
    for (const item of conflicts) {
      const row = document.createElement("div");
      row.className = "lib-conflict";
      const info = document.createElement("div");
      info.className = "info";
      const strong = document.createElement("strong");
      strong.textContent = item.source_text;
      const small = document.createElement("small");
      small.textContent = `${item.lang_pair} · ${item.existing_target} → ${item.candidate_target}`;
      info.append(strong, small);
      row.append(info);
      row.append(createButton({ label: t("保留当前"), size: "mini", onClick: () => void resolveTmConflict(item.id, "keep_existing") }));
      row.append(createButton({ label: t("采用候选"), size: "mini", variant: "primary", onClick: () => void resolveTmConflict(item.id, "use_candidate") }));
      row.append(createButton({ label: t("拒绝"), size: "mini", variant: "danger", onClick: () => void resolveTmConflict(item.id, "reject") }));
      list.append(row);
    }
    host.append(list);
  }
  syncNoticeVisibility();
}

function updateSelectionUi(): void {
  if (!tcHeadEl) return;
  const chip = tcHeadEl.querySelector<HTMLSpanElement>("[data-role=sel-chip]");
  // 「已选 x / xx」：分母是当前语言对 + 搜索关键词过滤后的匹配总数（与「选择全部」
  // 圈定的范围一致），不是当前页的条数，避免用户误以为只有一页那么多可选。
  if (chip) chip.textContent = t("已选 {0} / {1}", fmtNum(selectedIds.size), fmtNum(total));
  const deleteBtn = tcHeadEl.querySelector<HTMLButtonElement>("[data-role=bulk-delete]");
  if (deleteBtn) {
    // 没选中时不显示「批量删除」；有选中但数据还在加载时显示为禁用。
    deleteBtn.style.display = selectedIds.size === 0 ? "none" : "";
    deleteBtn.disabled = !tmQueryReady() || selectedIds.size === 0;
  }
  const selectAllLink = tcHeadEl.querySelector<HTMLSpanElement>("[data-role=select-all]");
  if (selectAllLink) {
    const reachable = Math.min(total, SELECT_ALL_CAP);
    const allSelected = reachable > 0 && selectedIds.size === reachable;
    selectAllLink.textContent = selectAllBusy ? t("选取中…") : allSelected ? t("取消全选") : t("选择全部");
    selectAllLink.style.opacity = selectAllBusy || !tmQueryReady() ? "0.5" : "1";
    selectAllLink.style.pointerEvents = selectAllBusy || !tmQueryReady() ? "none" : "auto";
  }
}

/** 加载骨架：三行渐变条，代替原来的「加载中…」。 */
function createTableSkeleton(): HTMLDivElement {
  const list = document.createElement("div");
  list.className = "lib-sk-list";
  list.setAttribute("role", "status");
  list.setAttribute("aria-label", t("加载中…"));
  for (let index = 0; index < 3; index += 1) {
    const bar = document.createElement("div");
    bar.className = "lib-sk";
    list.append(bar);
  }
  return list;
}

function renderTable(): void {
  if (!tableScrollEl) return;
  clearElement(tableScrollEl);
  if (!tmQueryReady()) {
    tableScrollEl.append(createTableSkeleton());
    updateSelectionUi();
    return;
  }
  if (!entries.length) {
    const empty = createEmptyState({
      title: t("当前语言对没有记忆条目"),
      description: t("Excel 和 Word 翻译中符合入库条件的内容将保存为记忆，也可通过上方「新增词条」或「导入」手动添加。"),
      icon: "book",
    });
    tableScrollEl.append(empty);
  } else {
    const table = document.createElement("table");
    table.className = "tbl";
    const thead = document.createElement("thead");
    const head = document.createElement("tr");
    const selectAll = document.createElement("th");
    selectAll.style.width = "36px";
    const selectAllCheckbox = document.createElement("input");
    selectAllCheckbox.type = "checkbox";
    selectAllCheckbox.className = "ck";
    selectAllCheckbox.checked = entries.length > 0 && entries.every((entry) => selectedIds.has(entry.id));
    // 用户实测反馈：这个复选框看起来像「全选所有页」，实际只勾选当前页——行为不改
    // （改了会打乱现有习惯），但补一句悬停说明，让人一眼看懂它管的范围。
    selectAllCheckbox.title = t("仅勾选当前页显示的条目；如需跨页选择，请使用下方「选择全部」。");
    selectAllCheckbox.addEventListener("change", () => {
      if (selectAllCheckbox.checked) entries.forEach((entry) => selectedIds.add(entry.id));
      else entries.forEach((entry) => selectedIds.delete(entry.id));
      renderTable();
      updateSelectionUi();
    });
    selectAll.append(selectAllCheckbox);
    head.append(selectAll);
    // 宽度按列 id 定，不看表头文字（表头随界面语言变）。表头 nowrap，英法文表头更长时
    // 自动表格布局会把列撑到放得下，这里的宽度只是下限。
    const columns: Array<{ id: string; label: string; width?: string }> = [
      { id: "source", label: t("原文") },
      { id: "target", label: t("译文") },
      { id: "note", label: t("备注（适用场景）") },
      { id: "origin", label: t("来源"), width: "88px" },
      ...(advancedMode
        ? [
            { id: "usage", label: t("采用方式"), width: "96px" },
            { id: "scope", label: t("生效范围"), width: "120px" },
            { id: "uses", label: t("使用次数"), width: "72px" },
          ]
        : []),
      { id: "updated", label: t("更新时间"), width: "96px" },
    ];
    for (const column of columns) {
      const th = document.createElement("th");
      th.textContent = column.label;
      th.dataset.col = column.id;
      if (column.width) th.style.width = column.width;
      head.append(th);
    }
    // 操作列表头留空：编辑 / 删除是悬停才出现的图标按钮，读屏仍能读到列名。
    const actionsTh = document.createElement("th");
    actionsTh.dataset.col = "actions";
    actionsTh.className = "acts-col";
    actionsTh.setAttribute("aria-label", t("操作"));
    head.append(actionsTh);
    thead.append(head);
    table.append(thead);

    const tbody = document.createElement("tbody");
    // 只在数据换了之后那一次渲染加行浮现；勾选引起的重绘不重播。
    if (renderedRevision !== tmQueryRevision) tbody.className = "stag";
    renderedRevision = tmQueryRevision;
    entries.forEach((entry, index) => {
      const tr = document.createElement("tr");
      tr.style.setProperty("--i", String(Math.min(index, 12)));
      const selectTd = document.createElement("td");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "ck";
      checkbox.checked = selectedIds.has(entry.id);
      checkbox.setAttribute("aria-label", t("选择 {0}", entry.source_text));
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) selectedIds.add(entry.id);
        else selectedIds.delete(entry.id);
        updateSelectionUi();
      });
      selectTd.append(checkbox);
      tr.append(selectTd);

      const sourceTd = document.createElement("td");
      sourceTd.className = "lib-src";
      sourceTd.textContent = entry.source_text;
      tr.append(sourceTd);
      const targetTd = document.createElement("td");
      targetTd.textContent = entry.target_text;
      tr.append(targetTd);

      const noteTd = document.createElement("td");
      noteTd.className = entry.note ? "note" : "dim";
      noteTd.textContent = entry.note || "—";
      tr.append(noteTd);

      const originTd = document.createElement("td");
      originTd.append(createChip({ label: originLabel(entry), tone: entry.origin === "manual" ? "tint" : "mute" }));
      tr.append(originTd);

      if (advancedMode) {
        const usage = text(entry.effective_usage, "segment");
        const usageTd = document.createElement("td");
        usageTd.append(createChip({ label: USAGE_LABELS[usage] ?? usage, tone: usage === "always" ? "ok" : usage === "scene" ? "tint" : "mute" }));
        tr.append(usageTd);
        const scopeTd = document.createElement("td");
        scopeTd.textContent = entry.scope ? t("文件名或文件夹名包含「{0}」", entry.scope) : "—";
        if (!entry.scope) scopeTd.className = "dim";
        tr.append(scopeTd);
        const usesTd = document.createElement("td");
        usesTd.className = "num";
        usesTd.textContent = fmtNum(num(entry.use_count));
        tr.append(usesTd);
      }

      const updatedTd = document.createElement("td");
      updatedTd.className = "num";
      updatedTd.textContent = entry.updated_at ? entry.updated_at.slice(0, 10) : "—";
      if (entry.updated_at) updatedTd.title = entry.updated_at;
      tr.append(updatedTd);

      // 行内操作：悬停或 Tab 到这一行时出现（library.css .rowacts）。
      const actionsTd = document.createElement("td");
      actionsTd.className = "acts-cell";
      const rowActs = document.createElement("div");
      rowActs.className = "rowacts";
      const editBtn = createButton({ icon: "edit", variant: "ghost", size: "mini", title: t("编辑"), onClick: () => openAddEditModal(entry) });
      editBtn.setAttribute("aria-label", t("编辑"));
      const deleteBtn = createButton({ icon: "trash", variant: "ghost", size: "mini", title: t("删除"), onClick: () => openDeleteModal(entry) });
      deleteBtn.classList.add("del");
      deleteBtn.setAttribute("aria-label", t("删除"));
      rowActs.append(editBtn, deleteBtn);
      actionsTd.append(rowActs);
      tr.append(actionsTd);

      tbody.append(tr);
    });
    table.append(tbody);
    tableScrollEl.append(table);
  }
  updateSelectionUi();
  renderPaginationBar();
}

function renderPaginationBar(): void {
  if (!tcHeadEl) return;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const label = tcHeadEl.querySelector<HTMLSpanElement>("[data-role=page-label]");
  if (label) label.textContent = t("第 {0} / {1} 页", fmtNum(page), fmtNum(totalPages));
  const prevBtn = tcHeadEl.querySelector<HTMLButtonElement>("[data-role=page-prev]");
  const nextBtn = tcHeadEl.querySelector<HTMLButtonElement>("[data-role=page-next]");
  if (prevBtn) prevBtn.disabled = page <= 1;
  if (nextBtn) nextBtn.disabled = page >= totalPages;
}

/** 底栏：左边「已选 x / xx · 选择全部 · 批量删除」，右边每页条数和翻页。 */
function buildFoot(): HTMLDivElement {
  const bar = document.createElement("div");
  bar.className = "lib-foot";

  const selected = document.createElement("span");
  selected.className = "sel";
  selected.dataset.role = "sel-chip";
  selected.textContent = t("已选 {0} / {1}", fmtNum(selectedIds.size), fmtNum(total));
  bar.append(selected);

  // 「选择全部」选中当前筛选条件下的全部匹配项（跨页），不只是本页。
  const selectAllLink = document.createElement("span");
  selectAllLink.className = "linklike";
  selectAllLink.dataset.role = "select-all";
  selectAllLink.setAttribute("role", "button");
  selectAllLink.tabIndex = 0;
  selectAllLink.textContent = t("选择全部");
  selectAllLink.addEventListener("click", () => void handleSelectAllTm());
  selectAllLink.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      void handleSelectAllTm();
    }
  });
  bar.append(selectAllLink);

  const deleteBtn = createButton({ label: t("批量删除"), icon: "trash", size: "mini", variant: "danger", onClick: () => confirmBulkDelete() });
  deleteBtn.dataset.role = "bulk-delete";
  bar.append(deleteBtn);

  const tools = document.createElement("div");
  tools.className = "pg-ctl";
  const sizeSelect = createDropdown({
    className: "lib-page-size",
    options: [25, 50, 100].map((size) => ({ value: String(size), label: t("{0} / 页", size) })),
    value: String(pageSize),
    onChange: (value) => {
      void applyTmQueryChange(() => {
        pageSize = Number(value) || 25;
        page = 1;
      }).then((ok) => {
        // 取数失败时 pageSize 已回滚，下拉框也要跟着退回去，否则控件显示的
        // 每页条数和表格里的实际条数对不上。
        if (!ok) sizeSelect.setValue(String(pageSize));
      });
    },
  });
  tools.append(sizeSelect.root);

  const pageLabel = document.createElement("span");
  pageLabel.className = "num";
  pageLabel.dataset.role = "page-label";
  tools.append(pageLabel);

  const prevBtn = createButton({
    label: "‹",
    variant: "ghost",
    size: "mini",
    title: t("上一页"),
    onClick: () => {
      if (page > 1) void applyTmQueryChange(() => { page -= 1; });
    },
  });
  prevBtn.dataset.role = "page-prev";
  prevBtn.setAttribute("aria-label", t("上一页"));
  const nextBtn = createButton({
    label: "›",
    variant: "ghost",
    size: "mini",
    title: t("下一页"),
    onClick: () => {
      const totalPages = Math.max(1, Math.ceil(total / pageSize));
      if (page < totalPages) void applyTmQueryChange(() => { page += 1; });
    },
  });
  nextBtn.dataset.role = "page-next";
  nextBtn.setAttribute("aria-label", t("下一页"));
  tools.append(prevBtn, nextBtn);
  bar.append(tools);

  return bar;
}

// ---------------------------------------------------------------------------
// View 生命周期
// ---------------------------------------------------------------------------

/** 加载中占位：内容区不能提前画出空工具条 / 空表格，否则用户会误以为语言选择是空的。 */
function renderLoadingPlaceholder(container: HTMLElement): void {
  placeholderEl?.remove();
  placeholderEl = createCard([createEmptyState({ title: t("正在加载记忆库…"), icon: "book" })]);
  container.append(placeholderEl);
}

/** 加载失败：沿用 settings.ts「无法连接本地翻译引擎」那张卡片的标题/副标题/图标用法，
 * 额外挂一个「重试」按钮——记忆库最常见的失败场景是桌面应用刚启动、Python sidecar 还没
 * 起来，用户手快点进来，稍等重试往往就能成功，不该逼用户切页面再切回来。 */
function renderLoadFailure(container: HTMLElement, message: string, onRetry: () => void): void {
  placeholderEl?.remove();
  const empty = createEmptyState({ title: t("记忆库加载失败"), description: message, icon: "warn" });
  const retryBtn = createButton({ label: t("重试"), variant: "primary", onClick: onRetry });
  retryBtn.style.marginTop = "4px";
  empty.append(retryBtn);
  placeholderEl = createCard([empty]);
  container.append(placeholderEl);
}

/** 三个加载请求都成功后才搭建页面骨架（不套卡片，照原型 .lib）。 */
function buildLayout(container: HTMLElement): void {
  const root = document.createElement("div");
  root.className = "lib";

  libTopEl = document.createElement("div");
  libTopEl.className = "lib-top";
  libFiltersEl = document.createElement("div");
  libFiltersEl.className = "lib-filters";
  libRecentEl = document.createElement("div");
  libRecentEl.className = "lib-recent tk-filters";
  libRecentEl.hidden = true;

  libNoticeEl = document.createElement("div");
  libNoticeEl.className = "lib-notice";
  libNoticeEl.hidden = true;
  stateRowEl = document.createElement("div");
  stateRowEl.className = "lib-state";
  conflictHostEl = document.createElement("div");
  conflictHostEl.className = "lib-conflict-host";
  conflictHostEl.id = "v9-tm-conflicts";
  libNoticeEl.append(stateRowEl, conflictHostEl);

  tableScrollEl = document.createElement("div");
  tableScrollEl.className = "lib-scroll";
  tcHeadEl = buildFoot();

  root.append(libTopEl, libFiltersEl, libRecentEl, libNoticeEl, tableScrollEl, tcHeadEl);
  container.append(root);
}

/** 任务详情「编辑」深链：定位到这条词条所在的语言对并按原文搜索，找到后直接打开编辑窗口。 */
interface EntryFocus {
  id: number;
  term: string;
  langPair: string;
}

function applyEntryFocus(focus: EntryFocus): void {
  const pair = splitTmPair(focus.langPair);
  if (pair && sourceOptions.some((option) => option.code === pair.source) && targetOptions.some((option) => option.code === pair.target)) {
    sourceLang = pair.source;
    targetLang = pair.target;
  }
  keyword = focus.term;
  entryFilter = "all";
  page = 1;
}

async function loadLibrary(container: HTMLElement, reviewTaskId: string | null, focus: EntryFocus | null = null): Promise<void> {
  const generation = mountGeneration;
  const isCurrent = () => mounted && generation === mountGeneration;
  try {
    await refreshLanguagePairs();
    if (!isCurrent()) return;
    if (focus) applyEntryFocus(focus);
    await Promise.all([refreshTm(), refreshConflicts()]);
  } catch (error) {
    if (!isCurrent()) return;
    setTopbar({ title: t("记忆库"), status: { label: t("加载失败"), tone: "warn" }, subtitle: libSubtitle() });
    renderLoadFailure(container, errorMessage(error), () => {
      if (!isCurrent()) return;
      setTopbar({ title: t("记忆库"), status: { label: t("加载中…"), tone: "idle" }, subtitle: libSubtitle() });
      renderLoadingPlaceholder(container);
      void loadLibrary(container, reviewTaskId, focus);
    });
    return;
  }
  if (!isCurrent()) return;
  placeholderEl?.remove();
  placeholderEl = null;
  buildLayout(container);
  rebuildToolbar();
  renderFilterRow();
  renderTable();
  renderTopbarStatus();
  const focused = focus ? entries.find((item) => item.id === focus.id) : undefined;
  if (focused) openAddEditModal(focused);
  if (reviewTaskId) {
    // 任务中心深链进来：loadCleanSuggestions 自己会把 pendingCleanCount 和
    // stale_count 一起拉齐，不必再单独调 refreshCleanBadge 重复请求一次。
    cleaningState = "ready";
    renderStateRow();
    await loadCleanSuggestions(reviewTaskId);
  } else {
    // 每次进页都按当前语言对拉一次待审建议——这个 GET 本身会触发服务端
    // 过期清理，常驻提示才能如实反映「现在还有几条真正待处理」。
    await refreshCleanBadge();
  }
}

export function mount(container: HTMLElement, params: ViewParams): void {
  mountGeneration += 1;
  invalidateTmQuery();
  mounted = true;
  selectedIds.clear();
  container.style.flexDirection = "column";

  setTopbar({ title: t("记忆库"), status: { label: t("加载中…"), tone: "idle" }, subtitle: libSubtitle() });

  renderLoadingPlaceholder(container);

  const reviewTaskId = typeof params.reviewCleanTaskId === "string" ? params.reviewCleanTaskId : null;
  const rawFocus = params.focusEntry as Partial<EntryFocus> | undefined;
  const focus = rawFocus && typeof rawFocus.id === "number" && typeof rawFocus.term === "string"
    ? { id: rawFocus.id, term: rawFocus.term, langPair: typeof rawFocus.langPair === "string" ? rawFocus.langPair : "" }
    : null;
  void loadLibrary(container, reviewTaskId, focus);
}

export function unmount(): void {
  mounted = false;
  mountGeneration += 1;
  invalidateTmQuery();
  // closeMenus() 关的是本文件自己的「导入 ▾ / 导出 ▾」下拉（.v9-tm-menu，本文件独立实现，
  // 没有借用 components.ts 的 openMenu）；closeMenu() 关的是 components.ts 里锚定菜单的
  // 模块级单例——本文件目前不触发它，纯粹是防御性收尾，两者管的是各自独立的浮层，不冲突。
  closeMenus();
  // 提示浮层和语言选择器的浮层都挂在 document.body 上，视图切走时不会随 container 一起被清掉，
  // 不主动关就会变成孤儿面板，还会让模块级的“当前展开项”指针继续指向已死的闭包。
  hideHint();
  closeLanguagePopover();
  closeMenu();
  closeTmSettings?.();
  tmSettingsBtn = null;
  if (searchDebounce !== null) {
    window.clearTimeout(searchDebounce);
    searchDebounce = null;
  }
  libTopEl = null;
  libFiltersEl = null;
  libRecentEl = null;
  libNoticeEl = null;
  stateRowEl = null;
  conflictHostEl = null;
  tableScrollEl = null;
  tcHeadEl = null;
  placeholderEl = null;
}
