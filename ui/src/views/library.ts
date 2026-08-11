// 记忆库视图（对应样张屏⑦）—— 语言对/搜索/增删改/导入导出/深度清洗/冲突裁决。
// 功能范围以 main.ts 的 renderTmView 及其配套 tm* 函数为准（见文件头注释），
// 版式与文案按样张屏⑦精修；样张未画出的功能（统计条、最近语言对、冲突裁决、任务风险
// 确认）保留但用现有组件与设计令牌收纳，不新增样张之外的视觉语言。

import type { ViewParams } from "../router";
import { navigate } from "../router";
import { setTopbar } from "../shell";
import {
  createCard,
  createChip,
  createButton,
  createTextField,
  createSelectField,
  createLanguagePicker,
  createSwitchRow,
  createEmptyState,
  openModal,
  showToast,
  hideHint,
  closeLanguagePopover,
  closeMenu,
  type ChipTone,
  type LanguageOption,
} from "../components";
import { icon } from "../icons";
import { ApiClient } from "../api-client";
import { saveJsonFile, saveTextFile } from "../save-file";

// ---------------------------------------------------------------------------
// 类型 / 工具函数
// ---------------------------------------------------------------------------

type JsonObject = Record<string, unknown>;

interface TmEntry {
  id: number;
  source_text: string;
  target_text: string;
  pinned: number;
  word_type: string;
  updated_at?: string;
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
  const sourceIndex = findColumn(["source_text", "source", "原文"]);
  const targetIndex = findColumn(["target_text", "target", "译文"]);
  const wordTypeIndex = findColumn(["word_type", "type", "来源"]);
  const pinnedIndex = findColumn(["pinned", "固定"]);
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
const selectedIds = new Set<number>();
let conflicts: TmConflict[] = [];
let conflictMessage = "";
let cleaningState: "idle" | "running" | "ready" | "error" = "idle";
let cleanTaskId: string | null = null;
let cleanSuggestions: JsonObject[] = [];

let mounted = false;
let toolbarCardEl: HTMLDivElement | null = null;
let statsRowEl: HTMLDivElement | null = null;
let stateRowEl: HTMLDivElement | null = null;
let tableScrollEl: HTMLDivElement | null = null;
let tcHeadEl: HTMLDivElement | null = null;
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
  return `${sourceOption?.display_name ?? parsed.source} → ${targetOption?.display_name ?? parsed.target}`;
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
  const client = await getClient();
  const payload = await client.request<{
    source_options: LanguageOption[];
    target_options: LanguageOption[];
    selected?: { source_lang?: string; target_lang?: string };
    recent?: string[];
  }>("/api/tm/language-pairs");
  sourceOptions = payload.source_options.filter((option) => option.builtin !== false && option.can_source !== false);
  targetOptions = payload.target_options.filter((option) => option.can_target !== false);
  recentPairs = strings(payload.recent);
  const wantedSource = text(payload.selected?.source_lang, sourceLang);
  const wantedTarget = text(payload.selected?.target_lang, targetLang);
  sourceLang = sourceOptions.some((option) => option.code === wantedSource) ? wantedSource : (sourceOptions[0]?.code ?? "zh");
  targetLang = targetOptions.some((option) => option.code === wantedTarget) ? wantedTarget : (targetOptions[0]?.code ?? "en");
}

async function refreshTm(): Promise<void> {
  const client = await getClient();
  const payload = await client.request<{ entries: TmEntry[]; stats: JsonObject; total?: number }>(
    `/api/tm/entries?lang_pair=${encodeURIComponent(tmLangPair())}&keyword=${encodeURIComponent(keyword)}&page=${page}&page_size=${pageSize}`,
  );
  entries = payload.entries;
  stats = payload.stats ?? {};
  total = num(payload.total, entries.length);
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  if (page > totalPages) {
    page = totalPages;
    await refreshTm();
  }
}

// 「选择全部」跨页拿全量 id：复用 /api/tm/entries 本身（不新建后端接口），把
// page_size 顶到服务端允许的上限（api/app.py list_tm_entries 里 min(page_size, 200)）
// 分批并发拉完。SELECT_ALL_CAP 是前端自设的上限，避免语言对里条目多到失控时
// 一次发几十个请求；超过时如实告知只选中了前 N 条，不做静默截断。
const SELECT_ALL_PAGE_SIZE = 200;
const SELECT_ALL_CAP = 5000;
let selectAllBusy = false;

async function fetchAllMatchingTmIds(): Promise<{ ids: number[]; truncated: boolean }> {
  const targetCount = Math.min(total, SELECT_ALL_CAP);
  if (targetCount <= 0) return { ids: [], truncated: false };
  const client = await getClient();
  const pageCount = Math.ceil(targetCount / SELECT_ALL_PAGE_SIZE);
  const pagePayloads = await Promise.all(
    Array.from({ length: pageCount }, (_, index) =>
      client.request<{ entries: TmEntry[] }>(
        `/api/tm/entries?lang_pair=${encodeURIComponent(tmLangPair())}&keyword=${encodeURIComponent(keyword)}&page=${index + 1}&page_size=${SELECT_ALL_PAGE_SIZE}`,
      ),
    ),
  );
  const ids = pagePayloads.flatMap((payload) => payload.entries.map((entry) => entry.id)).slice(0, targetCount);
  return { ids, truncated: total > SELECT_ALL_CAP };
}

/** 「选择全部」/「取消全选」：选中的是当前语言对 + 搜索关键词过滤后的完整结果集
 * （跨页），不是无视筛选的整个记忆库。已经选满一次可达上限时再点会直接清空，
 * 不必重新发请求。 */
async function handleSelectAllTm(): Promise<void> {
  if (selectAllBusy || total <= 0) return;
  const reachable = Math.min(total, SELECT_ALL_CAP);
  if (selectedIds.size > 0 && selectedIds.size === reachable) {
    selectedIds.clear();
    renderTable();
    return;
  }
  selectAllBusy = true;
  updateSelectionUi();
  try {
    const { ids, truncated } = await fetchAllMatchingTmIds();
    selectedIds.clear();
    for (const id of ids) selectedIds.add(id);
    renderTable();
    if (truncated) {
      showToast({
        message: `已选择前 ${ids.length} 条（共 ${total} 条，超过单次全选上限 ${SELECT_ALL_CAP} 条）。请缩小搜索范围后分批处理剩余条目。`,
        error: true,
      });
    }
  } catch (error) {
    showToast({ message: `选择全部失败：${errorMessage(error)}`, error: true });
  } finally {
    selectAllBusy = false;
    updateSelectionUi();
  }
}

async function refreshConflicts(): Promise<void> {
  const client = await getClient();
  const payload = await client.request<{ conflicts: TmConflict[] }>(`/api/tm/conflicts?lang_pair=${encodeURIComponent(tmLangPair())}`);
  conflicts = Array.isArray(payload.conflicts) ? payload.conflicts : [];
}

async function persistSettings(patch: JsonObject): Promise<void> {
  const client = await getClient();
  await client.request("/api/settings", { method: "PUT", body: JSON.stringify(patch) });
}

async function saveLangPair(source: string, target: string): Promise<void> {
  if (source === target) {
    showToast({ message: "记忆库源语言和目标语言不能相同。", error: true });
    return;
  }
  sourceLang = source;
  targetLang = target;
  page = 1;
  selectedIds.clear();
  cleaningState = "idle";
  conflictMessage = "";
  const pair = tmLangPair();
  recentPairs = [pair, ...recentPairs.filter((item) => item !== pair)].slice(0, 8);
  try {
    await persistSettings({ tm_source_lang: source, tm_target_lang: target, recent_tm_lang_pairs: recentPairs });
    await refreshTm();
    await refreshConflicts();
    rebuildToolbar();
    renderTable();
    renderStatsRow();
    renderTopbarStatus();
  } catch (error) {
    showToast({ message: `切换语言对失败：${errorMessage(error)}`, error: true });
  }
}

function renderTopbarStatus(): void {
  if (!mounted) return;
  setTopbar({
    title: "记忆库",
    status: { label: `${total} 条`, tone: "idle" },
    subtitle: "翻译过的内容自动入库，固定词条优先复用",
  });
}

// ---------------------------------------------------------------------------
// 条目 CRUD
// ---------------------------------------------------------------------------

async function tmPin(entryId: number, pinned: boolean): Promise<void> {
  const client = await getClient();
  await client.request(`/api/tm/entries/${entryId}/pin`, { method: "POST", body: JSON.stringify({ pinned }) });
  await refreshTm();
  renderTable();
  renderStatsRow();
}

async function tmBulkPin(pinned: boolean): Promise<void> {
  if (!selectedIds.size) return;
  const client = await getClient();
  await client.request("/api/tm/entries/bulk/pin", { method: "POST", body: JSON.stringify({ ids: [...selectedIds], pinned }) });
  selectedIds.clear();
  await refreshTm();
  renderTable();
  renderStatsRow();
  showToast({ message: pinned ? "已固定所选记忆条目。" : "已解除所选记忆条目的固定。" });
}

function confirmBulkDelete(): void {
  if (!selectedIds.size) return;
  const count = selectedIds.size;
  openModal({
    tone: "danger",
    icon: "trash",
    title: "批量删除记忆条目？",
    body: [`将永久删除已选中的 ${count} 条记忆条目（固定词条会被保护，不会被删除）。此操作不能撤销。`],
    actions: [
      { label: "取消", variant: "default" },
      {
        label: "删除",
        variant: "danger-solid",
        onClick: async () => {
          const client = await getClient();
          const result = await client.request<{ deleted: number; protected: number; missing: number }>("/api/tm/entries/bulk/delete", {
            method: "POST",
            body: JSON.stringify({ ids: [...selectedIds] }),
          });
          selectedIds.clear();
          await refreshTm();
          renderTable();
          renderStatsRow();
          const detail = result.protected ? `，${result.protected} 条固定词条未删除` : "";
          showToast({ message: `已删除 ${result.deleted} 条记忆条目${detail}。`, error: Boolean(result.protected) });
        },
      },
    ],
  });
}

function openAddEditModal(editing: TmEntry | null): void {
  const sourceField = createTextField({ label: "原文", value: editing?.source_text ?? "" });
  const targetField = createTextField({ label: "译文", value: editing?.target_text ?? "" });
  const pairField = createTextField({ label: "语言对", value: tmLangPair(), disabled: Boolean(editing) });
  const allowReverse = pairAllowsReverse();
  let syncReverse = false;
  const syncRow = createSwitchRow({
    label: "同时创建/更新反向术语（默认关闭）",
    disabled: !allowReverse,
    onChange: (value) => {
      syncReverse = value;
    },
  });
  const body: HTMLElement[] = [sourceField.root, targetField.root, pairField.root, syncRow];
  if (!allowReverse) {
    const note = document.createElement("p");
    note.textContent = "自定义目标语言不能生成反向语言对。";
    body.push(note);
  }
  trackModal(openModal({
    tone: "warn",
    icon: editing ? "edit" : "plus",
    title: editing ? "编辑记忆条目" : "新增记忆条目",
    body,
    actions: [
      { label: "取消", variant: "default" },
      {
        label: "保存",
        variant: "primary",
        keepOpen: true,
        onClick: async (): Promise<void> => {
          const sourceText = sourceField.input.value.trim();
          const targetText = targetField.input.value.trim();
          if (!sourceText || !targetText) {
            showToast({ message: "原文和译文都不能为空。", error: true });
            return;
          }
          const client = await getClient();
          try {
            if (editing) {
              await client.request(`/api/tm/entries/${editing.id}`, {
                method: "PUT",
                body: JSON.stringify({ source_text: sourceText, target_text: targetText, sync_reverse: syncReverse }),
              });
              showToast({ message: "记忆条目已更新。" });
            } else {
              await client.request("/api/tm/entries", {
                method: "POST",
                body: JSON.stringify({ source_text: sourceText, target_text: targetText, lang_pair: pairField.input.value.trim() || tmLangPair(), sync_reverse: syncReverse }),
              });
              showToast({ message: syncReverse ? "记忆条目已保存，并同步反向语言对。" : "记忆条目已保存。" });
            }
            conflictMessage = "";
            await refreshTm();
            renderTable();
            renderStatsRow();
            currentModalClose?.();
          } catch (error) {
            const message = errorMessage(error);
            if (/409|conflict|冲突|重复/i.test(message)) {
              conflictMessage = message;
              await refreshConflicts();
              renderConflictArea();
              currentModalClose?.();
            } else {
              showToast({ message: `保存失败：${message}`, error: true });
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
  trackModal(
    openModal({
      tone: "danger",
      icon: "trash",
      title: "删除记忆条目",
      body: [`将永久删除“${entryToDelete.source_text}”。此操作不能撤销。`],
      actions: [
        { label: "取消", variant: "default" },
        {
          label: "删除",
          variant: "danger-solid",
          onClick: async () => {
            const client = await getClient();
            try {
              await client.request(`/api/tm/entries/${entryToDelete.id}`, { method: "DELETE" });
            } catch (error) {
              // 固定词条后端回 409。不接住就只剩一个不关也不报错的弹窗，
              // 用户点「删除」什么都不发生（openModal 现在兜底，这里再补上「删除失败」前缀）。
              showToast({ message: `删除失败：${errorMessage(error)}`, error: true });
              return;
            }
            // 条目没了，选中集也得跟着少一个：不删的话「已选 xx / xx」的分子会大于分母，
            // 而且后续批量操作会带上一个已经不存在的 id。
            selectedIds.delete(entryToDelete.id);
            await refreshTm();
            renderTable();
            renderStatsRow();
            showToast({ message: "记忆条目已删除。" });
          },
        },
      ],
    }),
  );
}

// ---------------------------------------------------------------------------
// 深度清洗（tm_clean 任务）
// ---------------------------------------------------------------------------

function connectionRowText(item: JsonObject): string {
  const summary = record(item.summary);
  const label = redactedText(
    item.label || item.connection_summary || item.resource_group || [summary.provider, summary.base_url].filter(Boolean).join(" @ "),
    "共享连接",
  );
  const roles = strings(item.roles).join("、") || "未返回";
  const activeConcurrency = num(item.active_concurrency, 0);
  const candidateConcurrency = num(item.candidate_concurrency, 0);
  const total_ = num(item.total_potential_concurrency, num(item.potential_concurrency, 0));
  return `${label} · 角色 ${roles} · 活动 / 新任务并发 ${activeConcurrency} / ${candidateConcurrency} · 合计潜在 ${total_}`;
}

function openTaskRiskModal(payload: JsonObject, preflight: { requires_confirmation: boolean; confirmation_token?: string; risk?: JsonObject }): void {
  const risk = record(preflight.risk);
  const sharedConnections = Array.isArray(risk.shared_connections) ? risk.shared_connections.map(record) : [];
  const active = Array.isArray(risk.active_tasks) ? risk.active_tasks.map(record) : [];
  const warnings = strings(risk.warnings);

  const body: HTMLElement[] = [];
  const note = document.createElement("p");
  note.textContent = "此任务将与现有活动任务共用至少一个实际 API 连接。继续后会按新任务自己的默认吞吐启动，不会自动减半；服务端会在启动时用一次性令牌原子复检。";
  body.push(note);

  const callout = document.createElement("p");
  callout.style.color = "var(--warn)";
  callout.style.fontWeight = "600";
  callout.textContent = "可能出现 429、排队、超时、失败或额外费用——同一连接的并发会累加。";
  body.push(callout);

  if (sharedConnections.length) {
    const heading = document.createElement("p");
    heading.style.fontWeight = "600";
    heading.textContent = "共同连接与预算";
    body.push(heading);
    const list = document.createElement("ul");
    list.style.paddingLeft = "18px";
    list.style.fontSize = "12.5px";
    list.style.color = "var(--ink-2)";
    for (const item of sharedConnections) {
      const li = document.createElement("li");
      li.textContent = connectionRowText(item);
      list.append(li);
    }
    body.push(list);
  }
  if (active.length) {
    const heading = document.createElement("p");
    heading.style.fontWeight = "600";
    heading.textContent = "活动任务";
    body.push(heading);
    const list = document.createElement("ul");
    list.style.paddingLeft = "18px";
    list.style.fontSize = "12.5px";
    list.style.color = "var(--ink-2)";
    for (const item of active) {
      const li = document.createElement("li");
      li.textContent = `${redactedText(item.source_label || item.task_label || item.task_id, "活动任务")} · 并发 ${num(item.concurrency, 0)}`;
      list.append(li);
    }
    body.push(list);
  }
  if (warnings.length) {
    const heading = document.createElement("p");
    heading.style.fontWeight = "600";
    heading.textContent = "额外提示";
    body.push(heading);
    const list = document.createElement("ul");
    list.style.paddingLeft = "18px";
    list.style.fontSize = "12.5px";
    list.style.color = "var(--ink-2)";
    for (const warning of warnings) {
      const li = document.createElement("li");
      li.textContent = redactedText(warning);
      list.append(li);
    }
    body.push(list);
  }

  openModal({
    tone: "warn",
    icon: "warn",
    title: "共享 API 并行风险",
    body,
    actions: [
      { label: "取消", variant: "default" },
      {
        label: "仍要启动",
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
    showToast({ message: "深度清洗任务已提交，前往任务中心查看进度。" });
    navigate("tasks", { taskId: task.task_id });
  } catch (error) {
    cleaningState = "error";
    renderStateRow();
    showToast({ message: `深度清洗启动失败：${errorMessage(error)}`, error: true });
  }
}

async function tmClean(): Promise<void> {
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
    showToast({ message: `深度清洗启动失败：${errorMessage(error)}`, error: true });
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
  if (!suggestions.length) {
    try {
      const payload = await client.request<JsonObject>(`/api/tm/clean/suggestions?lang_pair=${encodeURIComponent(tmLangPair())}`);
      suggestions = resultEntries(payload, ["suggestions", "items"]);
    } catch {
      // 没有可写入的建议也是合法结果。
    }
  }
  cleanSuggestions = suggestions;
  openCleanReviewModal();
}

function openCleanReviewModal(): void {
  const checks: HTMLInputElement[] = [];
  const targets: HTMLInputElement[] = [];
  const body: HTMLElement[] = [];
  if (!cleanSuggestions.length) {
    const empty = document.createElement("p");
    empty.textContent = "未生成可写入的建议。";
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
    check.checked = true;
    check.style.marginTop = "3px";
    checks.push(check);
    const info = document.createElement("div");
    info.style.flex = "1";
    const title = document.createElement("b");
    title.textContent = text(suggestion.source_text);
    const line = document.createElement("p");
    line.style.margin = "3px 0 0";
    line.append(document.createTextNode(`${text(suggestion.old_target)} → `));
    const targetInput = document.createElement("input");
    targetInput.type = "text";
    targetInput.value = text(suggestion.new_target);
    targetInput.style.width = "60%";
    targets.push(targetInput);
    line.append(targetInput);
    info.append(title, line);
    row.append(check, info);
    body.push(row);
  });

  openModal({
    tone: "warn",
    icon: "book",
    title: "清洗建议",
    body: body.length ? body : ["未生成可写入的建议。"],
    actions: [
      { label: "取消", variant: "default" },
      {
        label: "写入已勾选建议",
        variant: "primary",
        onClick: async () => {
          const client = await getClient();
          const suggestions = cleanSuggestions.map((suggestion, index) => ({
            entry_id: num(suggestion.entry_id),
            source_text: text(suggestion.source_text),
            old_target: text(suggestion.old_target),
            new_target: targets[index]?.value ?? text(suggestion.new_target),
            accepted: checks[index]?.checked ?? false,
          }));
          const result = await client.request<{ applied: number }>("/api/tm/clean/apply", {
            method: "POST",
            body: JSON.stringify({ suggestions, auto_pin: false }),
          });
          cleanSuggestions = [];
          cleaningState = "idle";
          await refreshTm();
          renderTable();
          renderStatsRow();
          renderStateRow();
          showToast({ message: `已写入 ${result.applied} 条清洗建议。` });
        },
      },
    ],
  });
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
    showToast({ message: `已导出当前语言对的 ${format.toUpperCase()} 文件到 ${saved}，共 ${list.length} 条。` });
  } catch (error) {
    showToast({ message: `导出失败：${errorMessage(error)}`, error: true });
  }
}

async function exportFullTm(): Promise<void> {
  try {
    const client = await getClient();
    const payload = await client.request<JsonObject>("/api/tm/export/full");
    const saved = await saveJsonFile("translator-tm-full.json", payload);
    if (!saved) return; // 用户取消保存框：静默返回
    showToast({ message: `已导出当前完整记忆库到 ${saved}（全部语言对、可信等级、冲突候选和自定义目标语言定义）。` });
  } catch (error) {
    showToast({ message: `导出失败：${errorMessage(error)}`, error: true });
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
      showToast({ message: `无法读取 TM 导入文件：${errorMessage(error)}`, error: true });
    }
  });
}

function importFullTm(): void {
  pickFile("application/json,.json", async (file) => {
    try {
      const payload = JSON.parse(await file.text()) as JsonObject;
      if (text(payload.format_version) !== "tm-full-v1") throw new Error("这不是当前格式的完整 TM 备份。");
      openFullImportPreviewModal(file.name, payload);
    } catch (error) {
      showToast({ message: `无法读取完整 TM 备份：${errorMessage(error)}`, error: true });
    }
  });
}

function openImportPreviewModal(fileName: string, format: "json" | "csv", langPair: string, previewEntries: TmImportEntry[]): void {
  const pairField = createTextField({ label: "目标语言对", value: langPair, placeholder: "例如 zh-en" });
  const modeField = createSelectField({
    label: "重复项",
    value: "skip",
    options: [
      { value: "skip", label: "跳过重复项" },
      { value: "overwrite", label: "覆盖重复项" },
      { value: "keep_both", label: "保留两份" },
    ],
  });
  let syncReverse = false;
  const syncRow = createSwitchRow({
    label: "同时写入反向语言对",
    disabled: targetIsCustom(splitTmPair(langPair)?.target ?? targetLang),
    onChange: (value) => {
      syncReverse = value;
    },
  });

  const summary = document.createElement("p");
  summary.textContent = `${fileName} · ${format.toUpperCase()} · 共 ${previewEntries.length} 条。请确认字段映射和重复项策略后再写入。`;

  const table = document.createElement("table");
  table.className = "tbl";
  const head = document.createElement("tr");
  for (const label of ["原文", "译文", "来源"]) {
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
    note.textContent = "仅显示前 8 条预览。";
    body.push(note);
  }

  openModal({
    tone: "warn",
    icon: "folder",
    title: "导入预览",
    body,
    actions: [
      { label: "取消", variant: "default" },
      {
        label: "确认导入",
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
            renderStatsRow();
            showToast({ message: `已完成导入：新增或更新 ${num(result.inserted)}，跳过 ${num(result.skipped)}，重复 ${num(result.duplicates)}。` });
          } catch (error) {
            showToast({ message: `导入失败：${errorMessage(error)}`, error: true });
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
  summary.textContent = `${fileName} · 当前格式 tm-full-v1。将校验并恢复 ${entryCount} 条词条、${conflictCount} 条冲突候选和 ${customLangCount} 个自定义目标语言。`;

  const modeField = createSelectField({
    label: "重复项",
    value: "skip",
    options: [
      { value: "skip", label: "跳过重复项" },
      { value: "overwrite", label: "覆盖低等级重复项" },
      { value: "keep_both", label: "保留冲突候选" },
    ],
  });
  const codeMapField = createTextField({ label: "自定义代码映射（可选 JSON）", value: "{}", placeholder: '例如 {"x-custom-old":"x-custom-new"}' });
  const note = document.createElement("p");
  note.textContent = "完整备份包含原文和译文；代码定义冲突时必须填写映射，无法映射则取消恢复。";

  openModal({
    tone: "warn",
    icon: "folder",
    title: "恢复完整记忆库",
    body: [summary, modeField.root, codeMapField.root, note],
    actions: [
      { label: "取消", variant: "default" },
      {
        label: "确认恢复",
        variant: "primary",
        onClick: async () => {
          if (!entryCount) return;
          let codeMap: Record<string, string> = {};
          try {
            const parsed = JSON.parse(codeMapField.input.value.trim() || "{}") as unknown;
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("代码映射必须是 JSON 对象。");
            codeMap = Object.fromEntries(Object.entries(parsed as Record<string, unknown>).map(([key, value]) => [key, String(value)]));
          } catch (error) {
            showToast({ message: `代码映射格式无效：${errorMessage(error)}`, error: true });
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
            renderStatsRow();
            renderConflictArea();
            showToast({
              message: `新增或更新 ${num(result.inserted)} 条，跳过 ${num(result.skipped)} 条，重复 ${num(result.duplicates)} 条，恢复冲突候选 ${num(result.conflicts)} 条。`,
            });
          } catch (error) {
            showToast({ message: `恢复失败：${errorMessage(error)}`, error: true });
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
    renderStatsRow();
  } catch (error) {
    showToast({ message: `裁决失败：${errorMessage(error)}`, error: true });
  }
}

// ---------------------------------------------------------------------------
// 下拉菜单（导入 ▾ / 导出 ▾）
// ---------------------------------------------------------------------------

function closeMenus(): void {
  document.querySelectorAll(".v9-tm-menu").forEach((node) => node.remove());
}

function openMenu(anchor: HTMLElement, items: Array<{ label: string; onClick: () => void }>): void {
  closeMenus();
  const rect = anchor.getBoundingClientRect();
  const menu = document.createElement("div");
  menu.className = "v9-tm-menu";
  menu.style.position = "fixed";
  menu.style.top = `${rect.bottom + 4}px`;
  menu.style.left = `${rect.left}px`;
  menu.style.zIndex = "250";
  menu.style.background = "var(--surface)";
  menu.style.border = "1px solid var(--line)";
  menu.style.borderRadius = "var(--r-md)";
  menu.style.boxShadow = "var(--sh-lg)";
  menu.style.padding = "6px";
  menu.style.display = "flex";
  menu.style.flexDirection = "column";
  menu.style.gap = "2px";
  menu.style.minWidth = "190px";
  for (const item of items) {
    const button = document.createElement("button");
    button.className = "btn mini";
    button.style.justifyContent = "flex-start";
    button.style.width = "100%";
    button.textContent = item.label;
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      closeMenus();
      item.onClick();
    });
    menu.append(button);
  }
  document.body.append(menu);
  window.setTimeout(() => {
    document.addEventListener(
      "click",
      function closer(event: MouseEvent) {
        if (!menu.contains(event.target as Node)) {
          closeMenus();
          document.removeEventListener("click", closer, true);
        }
      },
      true,
    );
  }, 0);
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

function rebuildToolbar(): void {
  if (!toolbarCardEl) return;
  toolbarCardEl.innerHTML = "";
  toolbarCardEl.style.cssText = "display:flex;flex-direction:column;gap:8px;padding:11px 14px";

  const row = document.createElement("div");
  row.style.cssText = "display:flex;gap:8px;align-items:center";

  row.append(
    langPicker(sourceOptions, sourceLang, "tm-source", (next) => void saveLangPair(next, targetLang)),
  );
  const arrow = document.createElement("span");
  arrow.style.color = "var(--ink-3)";
  arrow.textContent = "→";
  row.append(arrow);
  row.append(
    langPicker(targetOptions, targetLang, "tm-target", (next) => void saveLangPair(sourceLang, next)),
  );

  const searchWrap = document.createElement("div");
  searchWrap.style.cssText = "flex:1;position:relative;max-width:340px";
  const searchInput = document.createElement("input");
  searchInput.type = "text";
  searchInput.placeholder = "搜索原文或译文…";
  searchInput.value = keyword;
  searchInput.style.cssText =
    "width:100%;height:32px;border:1px solid var(--line-2);border-radius:8px;background:var(--surface);color:var(--ink);font:inherit;font-size:12.5px;padding:0 10px 0 32px";
  searchInput.addEventListener("input", () => {
    keyword = searchInput.value;
    if (searchDebounce !== null) window.clearTimeout(searchDebounce);
    searchDebounce = window.setTimeout(() => {
      page = 1;
      // 搜索关键词变了，「选择全部」圈定的结果集也跟着变——旧的选中集合可能包含
      // 现在已经看不见的条目，继续留着容易误批量删除，所以筛选条件一变就清空。
      selectedIds.clear();
      void refreshTm().then(() => {
        renderTable();
        renderTopbarStatus();
      });
    }, 320);
  });
  const searchIcon = icon("search", { size: "sm" });
  searchIcon.style.cssText = "position:absolute;left:10px;top:8px;color:var(--ink-3)";
  searchWrap.append(searchInput, searchIcon);
  row.append(searchWrap);

  const actions = document.createElement("div");
  actions.style.cssText = "margin-left:auto;display:flex;gap:7px";
  actions.append(createButton({ label: "新增词条", icon: "plus", onClick: () => openAddEditModal(null) }));
  const importBtn = createButton({
    label: "导入",
    icon: "chev",
    onClick: (event) =>
      openMenu(event.currentTarget as HTMLElement, [
        { label: "从 JSON / CSV 导入", onClick: () => importTm() },
        { label: "恢复完整备份（JSON）", onClick: () => importFullTm() },
      ]),
  });
  const exportBtn = createButton({
    label: "导出",
    icon: "chev",
    onClick: (event) =>
      openMenu(event.currentTarget as HTMLElement, [
        { label: "导出 JSON", onClick: () => void exportTm("json") },
        { label: "导出 CSV", onClick: () => void exportTm("csv") },
        { label: "导出完整备份", onClick: () => void exportFullTm() },
      ]),
  });
  actions.append(importBtn, exportBtn);
  actions.append(createButton({ label: "深度清洗", variant: "primary", disabled: cleaningState === "running", onClick: () => void tmClean() }));
  row.append(actions);
  toolbarCardEl.append(row);

  if (recentPairs.length) {
    const recentRow = document.createElement("div");
    recentRow.style.cssText = "display:flex;gap:6px;align-items:center;flex-wrap:wrap";
    const label = document.createElement("span");
    label.style.cssText = "font-size:11.5px;color:var(--ink-3)";
    label.textContent = "最近使用";
    recentRow.append(label);
    for (const pair of recentPairs.slice(0, 8)) {
      const parsed = splitTmPair(pair);
      const button = createButton({
        label: tmPairLabel(pair),
        size: "mini",
        variant: pair === tmLangPair() ? "primary" : "default",
        onClick: () => {
          if (parsed) void saveLangPair(parsed.source, parsed.target);
        },
      });
      recentRow.append(button);
    }
    toolbarCardEl.append(recentRow);
  }

  stateRowEl = document.createElement("div");
  toolbarCardEl.append(stateRowEl);
  renderStateRow();

  const conflictRowHost = document.createElement("div");
  conflictRowHost.id = "v9-tm-conflicts";
  toolbarCardEl.append(conflictRowHost);
  renderConflictArea();
}

function renderStateRow(): void {
  if (!stateRowEl) return;
  stateRowEl.innerHTML = "";
  if (cleaningState === "idle") return;
  const tone: ChipTone = cleaningState === "error" ? "dgr" : cleaningState === "ready" ? "warn" : "tint";
  const label =
    cleaningState === "running" ? "正在分析未固定条目…" : cleaningState === "ready" ? "清洗建议已生成，请复核后写入。" : "清洗失败，请检查模型连接后重试。";
  const chip = createChip({ label, tone });
  stateRowEl.append(chip);
  if (cleaningState === "ready" && cleanTaskId) {
    const reviewBtn = createButton({ label: "查看建议", size: "mini", onClick: () => openCleanReviewModal() });
    reviewBtn.style.marginLeft = "8px";
    stateRowEl.append(reviewBtn);
  }
}

function renderConflictArea(): void {
  const host = toolbarCardEl?.querySelector<HTMLDivElement>("#v9-tm-conflicts");
  if (!host) return;
  host.innerHTML = "";
  if (conflictMessage) {
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;gap:8px";
    row.append(createChip({ label: conflictMessage, tone: "dgr" }));
    const closeBtn = createButton({ label: "关闭", size: "mini", onClick: () => { conflictMessage = ""; renderConflictArea(); } });
    row.append(closeBtn);
    host.append(row);
  }
  if (!conflicts.length) return;
  const header = document.createElement("div");
  header.style.cssText = "display:flex;align-items:center;gap:8px;margin-top:6px";
  const title = document.createElement("strong");
  title.style.fontSize = "12.5px";
  title.textContent = `待裁决冲突 ${conflicts.length}`;
  header.append(title);
  header.append(createButton({ label: "刷新", size: "mini", onClick: () => void refreshConflicts().then(renderConflictArea) }));
  host.append(header);
  for (const item of conflicts) {
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;gap:10px;padding:7px 0;border-top:1px solid var(--line);font-size:12.5px";
    const info = document.createElement("div");
    info.style.flex = "1";
    const strong = document.createElement("strong");
    strong.textContent = item.source_text;
    const small = document.createElement("div");
    small.style.cssText = "color:var(--ink-3);font-size:11.5px";
    small.textContent = `${item.lang_pair} · ${item.existing_target} → ${item.candidate_target}`;
    info.append(strong, small);
    row.append(info);
    row.append(createButton({ label: "保留当前", size: "mini", onClick: () => void resolveTmConflict(item.id, "keep_existing") }));
    row.append(createButton({ label: "采用候选", size: "mini", variant: "primary", onClick: () => void resolveTmConflict(item.id, "use_candidate") }));
    row.append(createButton({ label: "拒绝", size: "mini", variant: "danger", onClick: () => void resolveTmConflict(item.id, "reject") }));
    host.append(row);
  }
}

function renderStatsRow(): void {
  if (!statsRowEl) return;
  statsRowEl.innerHTML = "";
  const tiles: Array<[string, string, "book" | "pin" | "check" | "doc-file"]> = [
    ["总条目", String(num(stats.total)), "book"],
    ["已固定", String(num(stats.pinned)), "pin"],
    ["手动维护", String(num(stats.manual)), "check"],
    ["未固定", String(num(stats.unpinned)), "doc-file"],
  ];
  for (const [label, value, iconName] of tiles) {
    const tile = document.createElement("div");
    tile.className = "stat";
    const head = document.createElement("div");
    head.style.cssText = "display:flex;align-items:center;gap:6px;color:var(--ink-3)";
    head.append(icon(iconName, { size: "sm" }));
    const span = document.createElement("span");
    span.textContent = label;
    head.append(span);
    const b = document.createElement("b");
    b.textContent = value;
    tile.append(head, b);
    statsRowEl.append(tile);
  }
}

function updateSelectionUi(): void {
  if (!tcHeadEl) return;
  const chip = tcHeadEl.querySelector<HTMLSpanElement>("[data-role=sel-chip]");
  // 「已选 x / xx」：分母是当前语言对 + 搜索关键词过滤后的匹配总数（与「选择全部」
  // 圈定的范围一致），不是当前页的条数，避免用户误以为只有一页那么多可选。
  if (chip) chip.textContent = `已选 ${selectedIds.size} / ${total}`;
  const pinBtn = tcHeadEl.querySelector<HTMLButtonElement>("[data-role=bulk-pin]");
  const deleteBtn = tcHeadEl.querySelector<HTMLButtonElement>("[data-role=bulk-delete]");
  if (pinBtn) pinBtn.disabled = selectedIds.size === 0;
  if (deleteBtn) deleteBtn.disabled = selectedIds.size === 0;
  const selectAllLink = tcHeadEl.querySelector<HTMLSpanElement>("[data-role=select-all]");
  if (selectAllLink) {
    const reachable = Math.min(total, SELECT_ALL_CAP);
    const allSelected = reachable > 0 && selectedIds.size === reachable;
    selectAllLink.textContent = selectAllBusy ? "选取中…" : allSelected ? "取消全选" : "选择全部";
    selectAllLink.style.opacity = selectAllBusy ? "0.5" : "1";
    selectAllLink.style.pointerEvents = selectAllBusy ? "none" : "auto";
  }
}

function renderTable(): void {
  if (!tableScrollEl) return;
  tableScrollEl.innerHTML = "";
  if (!entries.length) {
    const empty = createEmptyState({
      title: "当前语言对没有记忆条目",
      description: "翻译任务会自动写入记忆，也可以用上方“新增词条”或“导入”手动补充。",
      icon: "book",
    });
    tableScrollEl.append(empty);
  } else {
    const table = document.createElement("table");
    table.className = "tbl";
    const head = document.createElement("tr");
    const selectAll = document.createElement("th");
    selectAll.style.width = "34px";
    const selectAllCheckbox = document.createElement("input");
    selectAllCheckbox.type = "checkbox";
    selectAllCheckbox.className = "ck";
    selectAllCheckbox.checked = entries.length > 0 && entries.every((entry) => selectedIds.has(entry.id));
    // 用户实测反馈：这个复选框看起来像「全选所有页」，实际只勾选当前页——行为不改
    // （改了会打乱现有习惯），但补一句悬停说明，让人一眼看懂它管的范围。
    selectAllCheckbox.title = "只勾选当前页显示的条目，不包括其他页；跨页请用下方“选择全部”。";
    selectAllCheckbox.addEventListener("change", () => {
      if (selectAllCheckbox.checked) entries.forEach((entry) => selectedIds.add(entry.id));
      else entries.forEach((entry) => selectedIds.delete(entry.id));
      renderTable();
      updateSelectionUi();
    });
    selectAll.append(selectAllCheckbox);
    head.append(selectAll);
    for (const label of ["原文", "译文", "固定", "更新时间", "操作"]) {
      const th = document.createElement("th");
      th.textContent = label;
      if (label === "固定") th.style.width = "64px";
      if (label === "更新时间") th.style.width = "120px";
      if (label === "操作") th.style.width = "118px";
      head.append(th);
    }
    table.append(head);

    for (const entry of entries) {
      const tr = document.createElement("tr");
      const selectTd = document.createElement("td");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "ck";
      checkbox.checked = selectedIds.has(entry.id);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) selectedIds.add(entry.id);
        else selectedIds.delete(entry.id);
        updateSelectionUi();
      });
      selectTd.append(checkbox);
      tr.append(selectTd);

      const sourceTd = document.createElement("td");
      sourceTd.textContent = entry.source_text;
      tr.append(sourceTd);
      const targetTd = document.createElement("td");
      targetTd.textContent = entry.target_text;
      tr.append(targetTd);

      const pinTd = document.createElement("td");
      const pinIcon = icon("pin", { size: "sm" });
      pinIcon.style.color = entry.pinned ? "var(--accent)" : "var(--line-2)";
      pinIcon.style.cursor = "pointer";
      pinTd.append(pinIcon);
      pinTd.addEventListener("click", () => void tmPin(entry.id, !entry.pinned));
      tr.append(pinTd);

      const updatedTd = document.createElement("td");
      updatedTd.className = "num";
      updatedTd.textContent = entry.updated_at ?? "—";
      tr.append(updatedTd);

      const actionsTd = document.createElement("td");
      const editLink = document.createElement("span");
      editLink.className = "linklike";
      editLink.textContent = "编辑";
      editLink.addEventListener("click", () => openAddEditModal(entry));
      const sep = document.createTextNode(" · ");
      const deleteLink = document.createElement("span");
      deleteLink.className = "linklike";
      deleteLink.textContent = "删除";
      if (entry.pinned) {
        // 固定词条后端一定回 409。与其让用户点开弹窗、确认、再看一条失败提示，
        // 不如在这一行就说清楚下一步该做什么（先解除固定）。
        deleteLink.style.color = "var(--ink-3)";
        deleteLink.title = "固定词条不能直接删除，请先解除固定。";
        deleteLink.addEventListener("click", () => {
          showToast({ message: "固定词条不能直接删除，请先点这一行的固定图标解除固定。", error: true });
        });
      } else {
        deleteLink.style.color = "var(--danger)";
        deleteLink.addEventListener("click", () => openDeleteModal(entry));
      }
      actionsTd.append(editLink, sep, deleteLink);
      tr.append(actionsTd);

      table.append(tr);
    }
    tableScrollEl.append(table);
  }
  updateSelectionUi();
  renderPaginationBar();
}

function renderPaginationBar(): void {
  if (!tcHeadEl) return;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const label = tcHeadEl.querySelector<HTMLSpanElement>("[data-role=page-label]");
  if (label) label.textContent = `第 ${page} / ${totalPages} 页`;
  const prevBtn = tcHeadEl.querySelector<HTMLButtonElement>("[data-role=page-prev]");
  const nextBtn = tcHeadEl.querySelector<HTMLButtonElement>("[data-role=page-next]");
  if (prevBtn) prevBtn.disabled = page <= 1;
  if (nextBtn) nextBtn.disabled = page >= totalPages;
}

function buildTcHead(): HTMLDivElement {
  const bar = document.createElement("div");
  bar.className = "tc-head";
  bar.style.borderTop = "1px solid var(--line)";
  bar.style.borderBottom = "0";

  const chip = createChip({ label: `已选 ${selectedIds.size} / ${total}`, tone: "tint" });
  chip.dataset.role = "sel-chip";
  bar.append(chip);

  // 「选择全部」紧挨着「已选」徽章，跟它组成一组（已选 x / xx · 选择全部），
  // 「批量固定」「批量删除」留在右边——原来在最右边的「本页全选」按钮挪到这里，
  // 语义也从「只选本页」换成「选中当前筛选条件下的全部匹配项（跨页）」。
  const selectAllLink = document.createElement("span");
  selectAllLink.className = "linklike";
  selectAllLink.dataset.role = "select-all";
  selectAllLink.textContent = "选择全部";
  selectAllLink.addEventListener("click", () => void handleSelectAllTm());
  bar.append(document.createTextNode(" · "), selectAllLink);

  const pinBtn = createButton({ label: "批量固定", icon: "pin", size: "mini", onClick: () => void tmBulkPin(true) });
  pinBtn.dataset.role = "bulk-pin";
  bar.append(pinBtn);

  const deleteBtn = createButton({ label: "批量删除", icon: "trash", size: "mini", variant: "danger", onClick: () => confirmBulkDelete() });
  deleteBtn.dataset.role = "bulk-delete";
  bar.append(deleteBtn);

  const tools = document.createElement("div");
  tools.className = "tc-tools";
  const sizeSelect = document.createElement("select");
  sizeSelect.style.cssText = "height:27px;border:1px solid var(--line-2);border-radius:7px;background:var(--surface);color:var(--ink);font:inherit;font-size:12px;padding:0 6px";
  for (const size of [25, 50, 100]) {
    const option = document.createElement("option");
    option.value = String(size);
    option.textContent = `${size} / 页`;
    if (size === pageSize) option.selected = true;
    sizeSelect.append(option);
  }
  sizeSelect.addEventListener("change", () => {
    pageSize = Number(sizeSelect.value) || 25;
    page = 1;
    void refreshTm().then(() => renderTable());
  });
  tools.append(sizeSelect);

  const pageLabel = document.createElement("span");
  pageLabel.style.cssText = "color:var(--ink-3);font-size:12px";
  pageLabel.dataset.role = "page-label";
  tools.append(pageLabel);

  const prevBtn = createButton({ label: "‹", size: "mini", onClick: () => { if (page > 1) { page -= 1; void refreshTm().then(() => renderTable()); } } });
  prevBtn.dataset.role = "page-prev";
  const nextBtn = createButton({
    label: "›",
    size: "mini",
    onClick: () => {
      const totalPages = Math.max(1, Math.ceil(total / pageSize));
      if (page < totalPages) {
        page += 1;
        void refreshTm().then(() => renderTable());
      }
    },
  });
  nextBtn.dataset.role = "page-next";
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
  placeholderEl = createCard([createEmptyState({ title: "正在加载记忆库…", icon: "book" })]);
  container.append(placeholderEl);
}

/** 加载失败：沿用 settings.ts「无法连接本地翻译引擎」那张卡片的标题/副标题/图标用法，
 * 额外挂一个「重试」按钮——记忆库最常见的失败场景是桌面应用刚启动、Python sidecar 还没
 * 起来，用户手快点进来，稍等重试往往就能成功，不该逼用户切页面再切回来。 */
function renderLoadFailure(container: HTMLElement, message: string, onRetry: () => void): void {
  placeholderEl?.remove();
  const empty = createEmptyState({ title: "记忆库加载失败", description: message, icon: "warn" });
  const retryBtn = createButton({ label: "重试", variant: "primary", onClick: onRetry });
  retryBtn.style.marginTop = "4px";
  empty.append(retryBtn);
  placeholderEl = createCard([empty]);
  container.append(placeholderEl);
}

/** 三个加载请求都成功后才搭建工具条 / 统计条 / 表格的容器结构。 */
function buildLayout(container: HTMLElement): void {
  toolbarCardEl = createCard();
  container.append(toolbarCardEl);

  statsRowEl = document.createElement("div");
  statsRowEl.className = "stats";
  container.append(statsRowEl);

  const tableCard = createCard([], "tablecard");
  tableScrollEl = document.createElement("div");
  tableScrollEl.style.cssText = "flex:1;overflow:auto";
  tableCard.append(tableScrollEl);
  tcHeadEl = buildTcHead();
  tableCard.append(tcHeadEl);
  container.append(tableCard);
}

async function loadLibrary(container: HTMLElement, reviewTaskId: string | null): Promise<void> {
  try {
    await refreshLanguagePairs();
    await Promise.all([refreshTm(), refreshConflicts()]);
  } catch (error) {
    if (!mounted) return;
    setTopbar({ title: "记忆库", status: { label: "加载失败", tone: "warn" }, subtitle: "翻译过的内容自动入库，固定词条优先复用" });
    renderLoadFailure(container, errorMessage(error), () => {
      if (!mounted) return;
      setTopbar({ title: "记忆库", status: { label: "加载中…", tone: "idle" }, subtitle: "翻译过的内容自动入库，固定词条优先复用" });
      renderLoadingPlaceholder(container);
      void loadLibrary(container, reviewTaskId);
    });
    return;
  }
  if (!mounted) return;
  placeholderEl?.remove();
  placeholderEl = null;
  buildLayout(container);
  rebuildToolbar();
  renderStatsRow();
  renderTable();
  renderTopbarStatus();
  if (reviewTaskId) {
    cleaningState = "ready";
    renderStateRow();
    await loadCleanSuggestions(reviewTaskId);
  }
}

export function mount(container: HTMLElement, params: ViewParams): void {
  mounted = true;
  container.style.flexDirection = "column";

  setTopbar({ title: "记忆库", status: { label: "加载中…", tone: "idle" }, subtitle: "翻译过的内容自动入库，固定词条优先复用" });

  renderLoadingPlaceholder(container);

  const reviewTaskId = typeof params.reviewCleanTaskId === "string" ? params.reviewCleanTaskId : null;
  void loadLibrary(container, reviewTaskId);
}

export function unmount(): void {
  mounted = false;
  // closeMenus() 关的是本文件自己的「导入 ▾ / 导出 ▾」下拉（.v9-tm-menu，本文件独立实现，
  // 没有借用 components.ts 的 openMenu）；closeMenu() 关的是 components.ts 里锚定菜单的
  // 模块级单例——本文件目前不触发它，纯粹是防御性收尾，两者管的是各自独立的浮层，不冲突。
  closeMenus();
  // 提示浮层和语言选择器的浮层都挂在 document.body 上，视图切走时不会随 container 一起被清掉，
  // 不主动关就会变成孤儿面板，还会让模块级的“当前展开项”指针继续指向已死的闭包。
  hideHint();
  closeLanguagePopover();
  closeMenu();
  if (searchDebounce !== null) {
    window.clearTimeout(searchDebounce);
    searchDebounce = null;
  }
  toolbarCardEl = null;
  statsRowEl = null;
  stateRowEl = null;
  tableScrollEl = null;
  tcHeadEl = null;
  placeholderEl = null;
}
