// 设置 · 翻译参数：各文件类型的「速率与重试」（v38 起），以及记忆库卡片。
//
// 同一组五项（同时请求数、每批条数、每批字符上限、失败重试次数、单段重试次数）
// 按文件类型保存在 settings.rate.<类型>；PDF 整页图像翻译另有三项，存在
// settings.pdf。留空（null）表示按文档翻译模型是云端还是本地取默认值，所以框里
// 显示的数、可填的上限都来自 GET /api/settings/rate，而不是在这里写死。

import { createButton, createCard, createHintBadge, createSwitch } from "../components";
import { t } from "../i18n";
import { tmSwitchDefs } from "./tm-switches";
import {
  client,
  type JsonObject,
  num,
  numberField,
  persistSettings,
  record,
  refreshSettings,
  renderBody,
  reRenderAfter,
  saveSettingPath,
  sectionLabel,
  settings,
} from "./settings";

export type RateSurface = "excel" | "word" | "pdf_text" | "cad";

/** GET /api/settings/rate 的返回；每个文件类型一份已存值、默认值、范围和实际值。 */
export let rateOverview: JsonObject | null = null;

export async function refreshRateOverview(): Promise<void> {
  try {
    rateOverview = await client.request<JsonObject>("/api/settings/rate");
  } catch {
    // 拿不到就按已存值显示，不让整页变成错误页；保存仍走 PUT /api/settings。
    rateOverview = null;
  }
}

// 「?」说明：默认值写明云端、本地两种情况和范围（解锁隐藏上限时范围以实际为准）。
const HINT_CONCURRENCY = () => t("同时向模型服务发送的请求数。数值越大速度越快，但更容易被服务商限速，限速时将自动降低。默认：云端模型 30，本地模型 3。范围：云端 1–50，本地 1–5。");
const HINT_CHARS = () => t("一批内容的原文总字数上限（不含提示词），达到上限即另起一批；单条超过上限时单独成一批。默认：云端模型 1000，本地模型 800。范围 800–12000。");
const HINT_REQUEST_RETRY = () => t("模型服务暂时不可用、请求超时或回答中断时，同一批内容自动重新发送的次数。默认 2，范围 0–5。仍失败的内容保留原文，并在结果中标记。");
const HINT_ITEM_RETRY = () => t("模型返回的译文缺失或不完整、拆分为更小的批次后仍未获得译文时，该条内容单独重新翻译的最多次数。默认 3，范围 0–8；设为 0 时不单独重试，直接保留原文并标记需复核。");

type RateFieldKey = "concurrency" | "batch_items" | "batch_chars" | "request_retry" | "item_retry";
type FieldDef = { key: string; label: string; hint: string };

function textRateFields(surface: RateSurface): FieldDef[] {
  const batchHint = {
    excel: t("每次请求最多包含的单元格数。默认：云端模型 20，本地模型 10。范围：云端 10–30，本地 5–20。"),
    word: t("每次请求最多包含的段落数。默认 8，范围 1–8。"),
    pdf_text: t("每次请求最多包含的文字条数。默认：云端模型 20，本地模型 10；转 Word 处理的页面每批最多 8 段。范围：云端 10–30，本地 5–20。"),
    cad: t("每次请求最多包含的文字条目数。默认：云端模型 20，本地模型 10。范围：云端 10–30，本地 5–20。"),
  }[surface];
  const charsHint = {
    excel: HINT_CHARS(),
    word: t("一批内容的原文总字数上限（不含提示词）；超出部分自动分批，过长的段落自动拆分。默认：云端模型 1000，本地模型 800。范围 800–12000。"),
    pdf_text: t("一批内容的原文总字数上限（不含提示词），达到上限即另起一批；转 Word 处理时过长的段落自动拆分。默认：云端模型 1000，本地模型 800。范围 800–12000。"),
    cad: HINT_CHARS(),
  }[surface];
  const itemHint = {
    excel: HINT_ITEM_RETRY(),
    word: t("译文为空、明显不完整或未通过质量检查时，段落自动重新翻译的最多次数。默认 3，范围 1–8。"),
    pdf_text: t("模型返回的译文缺失或不完整、拆分为更小的批次后仍未获得译文时，该条内容单独重新翻译的最多次数。默认 3，范围 0–8；设为 0 时不单独重试，直接保留原文并标记需复核。转 Word 处理的页面至少重新翻译 1 次。"),
    cad: HINT_ITEM_RETRY(),
  }[surface];
  return [
    { key: "concurrency", label: t("同时请求数"), hint: HINT_CONCURRENCY() },
    { key: "batch_items", label: t("每批条数"), hint: batchHint },
    { key: "batch_chars", label: t("每批字符上限"), hint: charsHint },
    { key: "request_retry", label: t("失败重试次数"), hint: HINT_REQUEST_RETRY() },
    { key: "item_retry", label: t("单段重试次数"), hint: itemHint },
  ];
}

function pdfImageFields(): FieldDef[] {
  return [
    { key: "page_generation_concurrency", label: t("同时生成页数"), hint: t("同时翻译的页数。默认：云端模型 10，本地模型 3。范围 1–20。") },
    { key: "page_retry_attempts", label: t("单页重试次数"), hint: t("单页翻译失败后的最多重试次数。默认 1，范围 0–8。") },
    { key: "review_concurrency", label: t("同时审核页数"), hint: t("开启「逐页审核」时同时审核的页数。默认 1，范围 1–20。未开启逐页审核时不使用。") },
  ];
}

// 只在没拿到概览时兜底，和 core/surface_rate.py 的云端默认值一致。
const FALLBACK_TEXT: Record<RateFieldKey, number> = {
  concurrency: 30, batch_items: 20, batch_chars: 1000, request_retry: 2, item_retry: 3,
};
const FALLBACK_PDF: Record<string, number> = {
  page_generation_concurrency: 10, page_retry_attempts: 1, review_concurrency: 1,
};
const UNLIMITED = 2 ** 31 - 1;

function surfaceOverview(key: string): { effective: JsonObject; bounds: JsonObject } {
  const surface = record(record(rateOverview?.surfaces)[key]);
  return { effective: record(surface.effective), bounds: record(surface.bounds) };
}

function throughputUnlocked(): boolean {
  return Boolean(record(record(settings).engine).concurrency_unlocked);
}

/** 存一项再取一次概览：实际值和默认值都由后端按当前模型算，界面不自己推。 */
async function saveRateValue(path: string, value: number | null): Promise<void> {
  await saveSettingPath(path, value);
  await refreshRateOverview();
}

async function resetRateValues(patch: JsonObject): Promise<void> {
  await persistSettings(patch);
  await refreshRateOverview();
}

/**
 * 隐藏的解锁码：在「同时请求数」里输入 OA 回车，取消并发和批次上限。原来挂在
 * 模型服务页的同名输入框上，速率挪到这里后跟着挪过来。
 */
function attachUnlockCode(input: HTMLInputElement, current: number): void {
  if (throughputUnlocked()) return;
  // 文本框而不是数字框：数字框里打不进字母。数字以外的输入显示成掩码。
  input.type = "text";
  input.inputMode = "numeric";
  input.addEventListener("input", () => {
    const nextType = /^\d*$/.test(input.value) ? "text" : "password";
    if (input.type !== nextType) input.type = nextType;
  });
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || input.value.trim().toUpperCase() !== "OA") return;
    event.preventDefault();
    input.value = String(current);
    input.type = "text";
    void (async () => {
      try {
        await client.request("/api/models/throughput/unlock", { method: "POST", body: JSON.stringify({ code: "OA" }) });
        await refreshSettings();
        await refreshRateOverview();
        renderBody();
      } catch {
        // 隐藏入口没有单独的反馈位置。
      }
    })();
  });
}

function rateGrid(
  fields: FieldDef[],
  overviewKey: string,
  fallback: Record<string, number>,
  pathOf: (key: string) => string,
): HTMLDivElement {
  const { effective, bounds } = surfaceOverview(overviewKey);
  const grid = document.createElement("div");
  grid.className = "grid3";
  for (const def of fields) {
    const value = num(effective[def.key], fallback[def.key] ?? 1);
    const range = Array.isArray(bounds[def.key]) ? (bounds[def.key] as unknown[]) : [];
    const min = num(range[0], 0);
    const rawMax = num(range[1], 0);
    const max = rawMax && rawMax < UNLIMITED ? rawMax : undefined;
    const field = numberField(
      def.label,
      value,
      (next) => void reRenderAfter(() => saveRateValue(pathOf(def.key), next), { rerenderOnError: true }),
      { min, max, integer: true, hint: def.hint },
    );
    const input = field.querySelector("input");
    if (input && def.key === "concurrency") attachUnlockCode(input, value);
    grid.append(field);
  }
  return grid;
}

/** 组标题右侧的「恢复默认值」：把这一组的数值清空，回到按模型取的默认值。 */
function resetButton(resetPatch: JsonObject): HTMLButtonElement {
  return createButton({
    label: t("恢复默认值"),
    size: "mini",
    onClick: () => void reRenderAfter(() => resetRateValues(resetPatch), { rerenderOnError: true }),
  });
}

function textRateGrid(surface: RateSurface): HTMLDivElement {
  return rateGrid(textRateFields(surface), surface, FALLBACK_TEXT, (key) => `rate.${surface}.${key}`);
}

function textResetPatch(surface: RateSurface): JsonObject {
  const reset: JsonObject = {};
  for (const key of Object.keys(FALLBACK_TEXT)) reset[key] = null;
  return { rate: { [surface]: reset } };
}

function rateNote(textContent: string): HTMLParagraphElement {
  const note = document.createElement("p");
  note.className = "note rate-note";
  note.textContent = textContent;
  return note;
}

function groupTitle(title: string, subtitle: string | null, reset: HTMLButtonElement): HTMLDivElement {
  const head = sectionLabel(title);
  head.classList.add("rate-head");
  if (subtitle) {
    const small = document.createElement("small");
    small.className = "rate-sub";
    small.textContent = subtitle;
    head.append(small);
  }
  head.append(reset);
  return head;
}

/** 「翻译参数」各页签共用的「速率与重试」一节；PDF 页签分整页图像和文字翻译两组。 */
export function renderRateSection(tab: "excel" | "word" | "pdf" | "cad"): HTMLElement[] {
  const parts: HTMLElement[] = [];
  if (tab === "pdf") {
    parts.push(rateNote(t("翻译方式为「整页图像翻译」时仅使用第一组设置；为「自动组合」时，各页按其处理方式使用对应的一组。")));
    parts.push(groupTitle(
      t("整页图像翻译"),
      t("翻译方式为「整页图像翻译」，或自动组合中按整页图像处理的页面"),
      resetButton({ pdf: { page_generation_concurrency: null, page_retry_attempts: FALLBACK_PDF.page_retry_attempts, review_concurrency: null } }),
    ));
    parts.push(rateGrid(pdfImageFields(), "pdf_image", FALLBACK_PDF, (key) => `pdf.${key}`));
    parts.push(groupTitle(
      t("文字翻译"),
      t("自动组合中按「转 Word」或「原位替换」（含文字识别）处理的页面"),
      resetButton(textResetPatch("pdf_text")),
    ));
    parts.push(textRateGrid("pdf_text"));
  } else {
    parts.push(groupTitle(t("速率与重试"), null, resetButton(textResetPatch(tab))));
    parts.push(textRateGrid(tab));
  }
  parts.push(rateNote(t("更改即时保存，不影响运行中的任务。")));
  return parts;
}

// ---------------------------------------------------------------------------
// 记忆库卡片：开关在上、数值在下，始终显示。开关和记忆库页右上角「设置」里的是
// 同一项（tm.*），高级模式只决定记忆库页显示多少信息，只在那边切换。
// ---------------------------------------------------------------------------

export function renderTmParamsCard(): HTMLElement {
  const tm = record(settings?.tm);
  const card = createCard([]);
  const body = document.createElement("div");
  body.style.padding = "14px 16px";
  body.style.display = "flex";
  body.style.flexDirection = "column";
  const head = sectionLabel(t("记忆库"));
  const scope = document.createElement("small");
  scope.className = "rate-sub";
  scope.textContent = t("适用于 Excel、Word、PDF、CAD");
  head.append(scope);
  body.append(head);

  for (const def of tmSwitchDefs()) {
    const row = document.createElement("div");
    row.className = "swrow";
    row.append(document.createTextNode(def.label), createHintBadge(def.hint, "swrow-hint"));
    const checked = typeof tm[def.key] === "boolean" ? Boolean(tm[def.key]) : def.fallback;
    row.append(createSwitch({
      checked,
      onChange: (next) => void reRenderAfter(() => saveSettingPath(`tm.${def.key}`, next), { rerenderOnError: true }),
    }));
    body.append(row);
  }

  const grid = document.createElement("div");
  grid.className = "grid2 rate-tm-grid";
  grid.append(numberField(
    t("未采用设定译法时重试次数"),
    num(tm.term_retry_attempts, 2),
    (v) => void reRenderAfter(() => saveSettingPath("tm.term_retry_attempts", v), { rerenderOnError: true }),
    { min: 0, max: 5, integer: true, hint: t("译文未采用设定译法时，段落自动重新翻译的最多次数。默认 2，范围 0–5；仍未采用的位置将在输出文件中标记需复核，设为 0 时不重新翻译，直接标记。") },
  ));
  grid.append(numberField(
    t("每批词条上限"),
    num(tm.term_batch_budget, 20),
    (v) => void reRenderAfter(() => saveSettingPath("tm.term_batch_budget", v), { rerenderOnError: true }),
    { min: 5, max: 100, integer: true, hint: t("单批内容涉及的词条超过此数时，将自动拆分为更小的批次，确保相关词条均随内容发送。默认 20，范围 5–100。") },
  ));
  body.append(grid);
  card.append(body);
  return card;
}
