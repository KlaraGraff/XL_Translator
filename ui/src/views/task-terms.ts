// 任务详情里的「记忆库」一节：读结果里的 terms（core/term_session.py 的 finish()）。
//
// 普通模式只给一句结论：有没有没采用设定译法的地方、本次自动保存了哪些短词。
// 高级模式（设置 tm.advanced_mode）再展开文档说明、命中统计和三张明细表。

import type { ApiClient, TaskStatus } from "../api-client";
import { createButton, createChip, showToast } from "../components";
import { navigate } from "../router";
import { currentLocale, t } from "../i18n";

interface TermPair {
  term: string;
  expected: string;
}

interface NotAppliedItem {
  snippet: string;
  output_snippet: string;
  retries: number;
  terms: TermPair[];
  locations: string[];
}

interface AutoSavedItem {
  id: number;
  term: string;
  target: string;
  note: string;
  lang_pair: string;
  count: number;
  /** document：本文档统一用词；其余为短词的场景译法。 */
  kind: string;
}

interface SceneChoice {
  term: string;
  candidates: Array<{ target: string; note: string }>;
  chosen: Array<{ target: string; count: number }>;
  count: number;
}

export interface TermsResult {
  profile: string;
  hits: { always: number; scene: number; segment: number };
  appliedDirect: number;
  appliedAfterRetry: number;
  notApplied: NotAppliedItem[];
  autoSaved: AutoSavedItem[];
  sceneChoices: SceneChoice[];
  manualTermsHit: number;
  retryAttempts: number;
  /** 需复核标在哪里：空为输出文件（Excel、Word）；cad_review 为 CAD 译文对照表；其余为任务里的复核清单。 */
  markedIn: string;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function list(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(record) : [];
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function int(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/** 结果里没有 terms（旧任务、CAD/PDF、没用到记忆库）时返回 null，整节不显示。 */
export function readTermsResult(task: TaskStatus): TermsResult | null {
  const raw = task.result?.terms;
  if (!raw || typeof raw !== "object") return null;
  const data = record(raw);
  const hits = record(data.hits);
  return {
    profile: str(data.profile),
    hits: { always: int(hits.always), scene: int(hits.scene), segment: int(hits.segment) },
    appliedDirect: int(data.applied_direct),
    appliedAfterRetry: int(data.applied_after_retry),
    notApplied: list(data.not_applied).map((item) => ({
      snippet: str(item.snippet),
      output_snippet: str(item.output_snippet),
      retries: int(item.retries),
      terms: list(item.terms).map((pair) => ({ term: str(pair.term), expected: str(pair.expected) })),
      locations: Array.isArray(item.locations) ? item.locations.map(str).filter(Boolean) : [],
    })),
    autoSaved: list(data.auto_saved).map((item) => ({
      id: int(item.id),
      term: str(item.term),
      target: str(item.target),
      note: str(item.note),
      lang_pair: str(item.lang_pair),
      count: int(item.count),
      kind: str(item.kind),
    })),
    sceneChoices: list(data.scene_choices).map((item) => ({
      term: str(item.term),
      candidates: list(item.candidates).map((cand) => ({ target: str(cand.target), note: str(cand.note) })),
      chosen: list(item.chosen).map((cand) => ({ target: str(cand.target), count: int(cand.count) })),
      count: int(item.count),
    })),
    manualTermsHit: int(data.manual_terms_hit),
    retryAttempts: int(data.retry_attempts),
    markedIn: str(data.marked_in),
  };
}

function hasContent(result: TermsResult): boolean {
  return result.notApplied.length > 0 || result.autoSaved.length > 0 || result.manualTermsHit > 0;
}

// ── 高级模式开关：和记忆库页面共用设置 tm.advanced_mode ─────────────────────

let advancedMode: boolean | null = null;
let advancedRequest: Promise<void> | null = null;

/** 进入任务中心时重新读一次；读完有变化就让调用方重画。 */
export function refreshTermsAdvancedMode(client: () => Promise<ApiClient>, onChange: () => void): void {
  if (advancedRequest) return;
  advancedRequest = client()
    .then((api) => api.request<{ advanced_mode?: boolean }>("/api/tm/language-pairs"))
    .then((payload) => {
      const next = payload.advanced_mode === true;
      if (next !== advancedMode) {
        advancedMode = next;
        onChange();
      }
    })
    .catch(() => undefined)
    .finally(() => {
      advancedRequest = null;
    });
}

/** 已撤销的自动保存词条：只记在本次运行里，用来把那一行显示成「已撤销」。 */
const undoneEntries = new Set<number>();

/** 自动保存词条的当前内容（null 表示已删除）。在记忆库页编辑过的，回到任务详情要如实显示。 */
interface CurrentEntry {
  target: string;
  note: string;
  origin: string;
}
const currentEntries = new Map<number, CurrentEntry | null>();
let currentEntriesTask = "";

/** 进入任务中心时清空：从记忆库页改完回来，重新读一次。 */
export function resetTermsEntryCache(): void {
  currentEntries.clear();
  currentEntriesTask = "";
}

function ensureCurrentEntries(taskId: string, ids: number[], client: () => Promise<ApiClient>, rerender: () => void): void {
  if (!ids.length || currentEntriesTask === taskId) return;
  currentEntriesTask = taskId;
  void client()
    .then((api) => api.request<{ entries?: Array<Record<string, unknown>> }>(`/api/tm/entries/by-ids?ids=${ids.join(",")}`))
    .then((payload) => {
      for (const id of ids) currentEntries.set(id, null);
      for (const row of payload.entries ?? []) {
        currentEntries.set(int(row.id), { target: str(row.target_text), note: str(row.note), origin: str(row.origin) });
      }
      if (currentEntriesTask === taskId) rerender();
    })
    .catch(() => {
      if (currentEntriesTask === taskId) currentEntriesTask = "";
    });
}

function sectionTitle(label: string, hint = ""): HTMLDivElement {
  const title = document.createElement("div");
  title.className = "tm-report-title";
  title.textContent = label;
  if (hint) {
    const span = document.createElement("span");
    span.textContent = " " + hint;
    title.append(span);
  }
  return title;
}

function line(tone: "warn" | "ok" | "note", message: string): HTMLDivElement {
  const row = document.createElement("div");
  row.className = `tm-report-line ${tone}`;
  const copy = document.createElement("span");
  copy.textContent = message;
  row.append(copy);
  return row;
}

function listSep(): string {
  return currentLocale() === "zh" ? "、" : ", ";
}

/** 「译文（备注）」：英法界面用半角括号，备注本身是用户或模型写的原文，不翻。 */
function withNote(target: string, note: string): string {
  return currentLocale() === "zh" ? `${target}（${note}）` : `${target} (${note})`;
}

function termLabel(pair: TermPair): string {
  return `${pair.term} → ${pair.expected}`;
}

function summaryOfMissed(items: NotAppliedItem[]): string {
  const seen: string[] = [];
  for (const item of items) {
    for (const pair of item.terms) {
      const label = termLabel(pair);
      if (!seen.includes(label)) seen.push(label);
    }
  }
  const shown = seen.slice(0, 2).join(listSep());
  return seen.length > 2 ? t("{0} 等", shown) : shown;
}

function table(headers: string[], widths: string[]): HTMLTableElement {
  const el = document.createElement("table");
  el.className = "tbl review-tbl tm-report-tbl";
  const cols = document.createElement("colgroup");
  for (const width of widths) {
    const col = document.createElement("col");
    col.style.width = width;
    cols.append(col);
  }
  el.append(cols);
  const head = document.createElement("tr");
  for (const label of headers) {
    const th = document.createElement("th");
    th.textContent = label;
    head.append(th);
  }
  el.append(head);
  return el;
}

function cell(value: string, title = false): HTMLTableCellElement {
  const td = document.createElement("td");
  td.textContent = value || "—";
  if (title && value) td.title = value;
  return td;
}

function stat(label: string, value: string, sub: string, warn = false): HTMLDivElement {
  const tile = document.createElement("div");
  tile.className = warn ? "kpi warn" : "kpi";
  const span = document.createElement("span");
  span.textContent = label;
  const b = document.createElement("b");
  b.textContent = value;
  tile.append(span, b);
  if (sub) {
    const small = document.createElement("small");
    small.textContent = sub;
    tile.append(small);
  }
  return tile;
}

async function undoAutoSaved(client: () => Promise<ApiClient>, item: AutoSavedItem, rerender: () => void): Promise<void> {
  try {
    const api = await client();
    await api.request(`/api/tm/entries/${item.id}`, { method: "DELETE" });
    undoneEntries.add(item.id);
    showToast({ message: t("已从记忆库撤销「{0}」", item.term) });
    rerender();
  } catch (error) {
    showToast({ message: t("撤销失败：{0}", error instanceof Error ? error.message : String(error)), error: true });
  }
}

/**
 * 在任务详情里画出「记忆库」一节。没有可说的内容时什么也不画。
 * scrollToReview：「查看位置」跳到下方的复核清单（未采用的位置已列在那里）。
 */
export function renderTermsSection(
  container: HTMLElement,
  task: TaskStatus,
  options: { client: () => Promise<ApiClient>; rerender: () => void; scrollToReview: (() => void) | null },
): void {
  const result = readTermsResult(task);
  if (!result) return;
  const advanced = advancedMode === true;
  if (!advanced && !hasContent(result)) return;

  const section = document.createElement("div");
  section.className = "tm-report";
  section.append(sectionTitle(t("记忆库")));

  if (advanced && result.profile) {
    const profile = document.createElement("div");
    profile.className = "tm-report-profile";
    const label = document.createElement("b");
    label.textContent = t("文档说明：");
    const body = document.createElement("span");
    body.textContent = result.profile;
    profile.append(label, body);
    section.append(profile);
  }

  if (result.notApplied.length) {
    const missed = summaryOfMissed(result.notApplied);
    const warn = line(
      "warn",
      result.markedIn === "cad_review"
        ? t("有 {0} 处未采用您设定的译法（{1}），已在译文对照表中标记需复核。", result.notApplied.length, missed)
        : result.markedIn
          ? t("有 {0} 处未采用您设定的译法（{1}），已标记需复核。", result.notApplied.length, missed)
          : t("有 {0} 处未采用您设定的译法（{1}），已在输出文件中标记需复核的位置。", result.notApplied.length, missed),
    );
    if (options.scrollToReview) {
      const link = document.createElement("span");
      link.className = "linklike";
      link.textContent = t("查看位置");
      link.addEventListener("click", () => options.scrollToReview?.());
      warn.append(link);
    }
    section.append(warn);
  } else if (result.manualTermsHit > 0) {
    section.append(line("ok", t("本次涉及的 {0} 条手动维护词条均已采用您设定的译法。", result.manualTermsHit)));
  }

  const pending = result.autoSaved.filter((item) => !undoneEntries.has(item.id));
  const pendingShort = pending.filter((item) => item.kind !== "document");
  const pendingDocument = pending.filter((item) => item.kind === "document");
  if (!advanced && pendingShort.length) {
    section.append(
      line(
        "note",
        t("「{0}」这类短词的本次译法将自动保存到记忆库，并附上标明适用场景的备注。之后遇到其他场景时，由模型对照备注选用，无需手动操作。", pendingShort[0].term),
      ),
    );
  }
  if (!advanced && pendingDocument.length) {
    section.append(
      line(
        "note",
        t("文中反复出现的 {0} 个词（如「{1}」）已统一译法，并自动保存到记忆库，附有标明适用场景的备注。之后翻译同类文档时，由模型对照备注选用，无需手动操作。", pendingDocument.length, pendingDocument[0].term),
      ),
    );
  }

  if (advanced) renderAdvanced(section, result, { ...options, taskId: task.task_id });
  container.append(section);
}

function renderAdvanced(
  section: HTMLElement,
  result: TermsResult,
  options: { client: () => Promise<ApiClient>; rerender: () => void; taskId: string },
): void {
  const totalHits = result.hits.always + result.hits.scene + result.hits.segment;
  const grid = document.createElement("div");
  grid.className = "kpis five";
  grid.append(
    stat(t("记忆库命中"), t("{0} 处", totalHits), t("始终采用 {0} · 按场景选用 {1}", result.hits.always + result.hits.segment, result.hits.scene)),
    stat(t("直接采用"), t("{0} 处", result.appliedDirect), ""),
    stat(t("重译后采用"), t("{0} 处", result.appliedAfterRetry), ""),
    stat(t("未采用设定译法"), t("{0} 处", result.notApplied.length), result.notApplied.length ? t("已标记需复核") : "", result.notApplied.length > 0),
    stat(t("自动保存"), t("{0} 条", result.autoSaved.length), t("新场景的译法")),
  );
  section.append(grid);

  if (result.notApplied.length) {
    section.append(sectionTitle(t("未采用设定译法的位置")));
    const el = table([t("位置"), t("原文摘录"), t("译文"), t("设定译法"), t("处理")], ["16%", "24%", "24%", "18%", "18%"]);
    for (const item of result.notApplied.slice(0, 50)) {
      const tr = document.createElement("tr");
      const where = item.locations.length ? (item.locations.length > 3 ? t("{0} 等", item.locations.slice(0, 3).join(listSep())) : item.locations.join(listSep())) : t("见下方复核清单");
      tr.append(cell(where, true), cell(item.snippet, true), cell(item.output_snippet, true), cell(item.terms.map(termLabel).join("\n")));
      const action = document.createElement("td");
      const stack = document.createElement("div");
      stack.className = "review-act";
      stack.append(createChip({ label: t("需复核"), tone: "warn" }));
      const note = document.createElement("div");
      note.className = "note";
      note.textContent = item.retries > 0 ? t("重译 {0} 次后仍未采用", item.retries) : t("未重译");
      stack.append(note);
      action.append(stack);
      tr.append(action);
      el.append(tr);
    }
    section.append(el);
  }

  if (result.autoSaved.length) {
    section.append(sectionTitle(t("本次自动保存的词条"), t("如译法有误，请点击「撤销」；无误则无需处理。")));
    const el = table([t("原文"), t("译文"), t("备注（适用场景）"), t("出现次数"), t("操作")], ["14%", "20%", "36%", "12%", "18%"]);
    ensureCurrentEntries(options.taskId, result.autoSaved.map((item) => item.id), options.client, options.rerender);
    for (const item of result.autoSaved) {
      const tr = document.createElement("tr");
      const current = currentEntries.get(item.id);
      tr.append(
        cell(item.term),
        cell(current ? current.target : item.target, true),
        cell(current ? current.note : item.note, true),
        cell(item.count > 0 ? String(item.count) : ""),
      );
      const action = document.createElement("td");
      if (undoneEntries.has(item.id) || current === null) {
        action.append(createChip({ label: t("已撤销"), tone: "mute" }));
      } else if (current && current.origin === "manual") {
        // 已在记忆库页改过，成了手动维护的词条：不再提供「撤销」，免得删掉用户自己定的译法。
        action.append(createChip({ label: t("手动维护"), tone: "ok" }));
      } else {
        const actions = document.createElement("div");
        actions.className = "tm-report-acts";
        actions.append(
          createButton({ label: t("撤销"), size: "mini", onClick: () => void undoAutoSaved(options.client, item, options.rerender) }),
          createButton({
            label: t("编辑"),
            size: "mini",
            onClick: () => navigate("library", { focusEntry: { id: item.id, term: item.term, langPair: item.lang_pair } }),
          }),
        );
        action.append(actions);
      }
      tr.append(action);
      el.append(tr);
    }
    section.append(el);
  }

  if (result.sceneChoices.length) {
    section.append(sectionTitle(t("按场景选用结果")));
    const el = table([t("原文"), t("候选译法"), t("本次采用"), t("出现次数")], ["16%", "42%", "30%", "12%"]);
    for (const item of result.sceneChoices.slice(0, 50)) {
      const tr = document.createElement("tr");
      const candidates = item.candidates.map((cand) => (cand.note ? withNote(cand.target, cand.note) : cand.target)).join(" · ");
      const chosen = item.chosen
        .map((choice) => {
          const label = choice.target || t("未判定");
          const known = item.candidates.some((cand) => cand.target === choice.target);
          const named = known || !choice.target ? label : t("{0}（新增）", label);
          return item.chosen.length > 1 ? `${named} × ${choice.count}` : named;
        })
        .join(" · ");
      tr.append(cell(item.term), cell(candidates, true), cell(chosen, true), cell(String(item.count)));
      el.append(tr);
    }
    section.append(el);
  }
}
