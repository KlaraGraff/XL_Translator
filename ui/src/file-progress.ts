import { t } from "./i18n";

/** Shared, revision-aware file status handling for workspace and task center. */
export type FileProgressState = "waiting" | "active" | "generated" | "failed" | "unstarted" | "stopped" | "interrupted" | "paused";

export type FileProgress = {
  file_id: string;
  name: string;
  relative_path: string;
  state: FileProgressState;
  phase: string;
  completed: number;
  total: number;
  result: Record<string, unknown>;
  revision: number;
};

export type FileProgressSnapshot = { revision: number; files: FileProgress[] };

export function reduceFileProgress(
  current: Map<string, FileProgress>,
  incoming: FileProgress | FileProgressSnapshot | undefined,
): Map<string, FileProgress> {
  if (!incoming) return current;
  const next = new Map(current);
  const entries = "files" in incoming ? incoming.files : [incoming];
  for (const entry of entries) {
    if (!entry || typeof entry.file_id !== "string" || !entry.file_id) continue;
    const old = next.get(entry.file_id);
    if (!old || entry.revision > old.revision) next.set(entry.file_id, entry);
  }
  return next;
}

const PHASE_LABELS: Record<string, string> = {
  prepare: t("准备文件"),
  extract: t("提取文字"),
  // PDF/图片：把页面渲染成图片交给图像模型（core/pdf_image_translation.py）。
  render: t("渲染页面"),
  detect: t("识别语言"),
  translate: t("翻译内容"),
  generate: t("生成文件"),
  adjust: t("调整排版"),
  // CAD 每个图纸的阶段（core/cad_task_runner.py 的 _PHASES）。
  scan: t("读取文件"),
  match: t("匹配记忆库与术语"),
  write: t("写回图纸"),
  verify: t("校验结果"),
};

/** 已生成的文件是否带着需复核内容。徽章配色直接读这个判断，不去翻译后的文字里找「需复核」。 */
function generatedNeedsReview(file: FileProgress): boolean {
  const result = file.result ?? {};
  const issues = Array.isArray(result.issues) ? result.issues : [];
  const needsReview = Number(result.needs_review_count ?? result.review_count ?? 0) > 0
    || result.needs_review === true
    || result.status === "needs_review"
    || Number(result.placeholder_page_count ?? 0) > 0
    || Number(result.suspect_adopted_page_count ?? 0) > 0
    || Number(result.quality_flagged_page_count ?? 0) > 0
    || Number(result.review_failed_page_count ?? 0) > 0
    || Number(result.truncated_cells ?? 0) > 0
    || result.sheet_name_status === "preserved"
    || issues.some((issue) => issue && typeof issue === "object" && (issue as Record<string, unknown>).severity === "needs_review");
  return needsReview;
}

/** 停止后写出、里面还留着一部分原文的文件。徽章配色同样直接读这个判断。 */
function generatedUnfinished(file: FileProgress): boolean {
  return Number((file.result ?? {}).unfinished_text_count ?? 0) > 0;
}

export function fileProgressLabel(file: FileProgress, taskState?: string): string {
  if (taskState === "interrupted" && ["waiting", "active", "paused"].includes(file.state)) return t("应用中断");
  const result = file.result ?? {};
  const needsReview = generatedNeedsReview(file);
  // 停止后写出的 Excel / Word：文件是生成了，可里面还有一部分是原文（后端按续译检测
  // 同一口径数出来的格/段数）。叫「已生成」会像翻完了一样。
  const unfinished = generatedUnfinished(file);
  const untranslated = result.untranslated === true
    || result.all_pages_skipped_oversize === true
    || (Number(result.page_count ?? 0) > 0 && Number(result.skipped_oversize_page_count ?? 0) >= Number(result.page_count));
  switch (file.state) {
    case "waiting":
      // 内容已经全部译完、只等生成文件时（PDF 要等所有页译完才装订）停在
      // waiting/generate——活已经干完大半，再叫「待处理」或「已暂停」都像还没轮到它。
      if (file.phase === "generate" && file.total > 0 && file.completed >= file.total) return t("已译完 · 等待生成文件");
      if (taskState === "paused") return t("已暂停 · 等待继续");
      if (file.phase === "translate") return t("待处理 · 等待翻译");
      if (file.phase === "generate") return t("待处理 · 等待生成文件");
      if (file.phase === "adjust") return t("待处理 · 等待调整排版");
      return t("待处理");
    case "active": {
      const count = file.total > 0 ? `${file.completed} / ${file.total}` : "";
      // 文件翻到一半时暂停：PDF 停止提交新页面、CAD 停在两张图之间都会走到这里，所以
      // 不写哪一种的细节（以前写「等待已提交页面完成」，CAD 图纸上也这么显示）。PDF 已提交
      // 的页还会陆续跑完，那一层由进度卡标题「已暂停提交新页面」交代。
      if (taskState === "paused" && file.phase === "translate") return [t("已暂停"), count].filter(Boolean).join(" · ");
      const phase = PHASE_LABELS[file.phase] ?? "";
      return [t("处理中"), phase, count].filter(Boolean).join(" · ");
    }
    case "generated":
      if (unfinished) return [t("部分翻译"), needsReview ? t("需复核") : ""].filter(Boolean).join(" · ");
      return [t("已生成"), untranslated ? t("整份未翻译") : "", needsReview ? t("需复核") : ""].filter(Boolean).join(" · ");
    case "failed": return t("未生成");
    case "unstarted": return t("未开始");
    case "stopped": return t("已停止");
    case "interrupted": return t("应用中断");
    case "paused": return taskState === "paused" ? t("已暂停 · 等待继续") : t("待处理");
  }
}

export function fileProgressTone(file: FileProgress, taskState?: string): "ok" | "warn" | "dgr" | "mute" | "tint" {
  if (taskState === "interrupted" && ["waiting", "active", "paused"].includes(file.state)) return "dgr";
  if (file.state === "generated") return generatedNeedsReview(file) || generatedUnfinished(file) ? "warn" : "ok";
  if (file.state === "active") return "tint";
  if (file.state === "failed" || file.state === "interrupted") return "dgr";
  if (file.state === "stopped" || file.state === "unstarted" || file.state === "waiting" || file.state === "paused") return "mute";
  return "warn";
}
