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
  prepare: "准备文件",
  extract: "提取文字",
  detect: "识别语言",
  translate: "翻译内容",
  generate: "生成文件",
  adjust: "调整排版",
};

export function fileProgressLabel(file: FileProgress, taskState?: string): string {
  if (taskState === "interrupted" && ["waiting", "active", "paused"].includes(file.state)) return "应用中断";
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
  const untranslated = result.untranslated === true
    || result.all_pages_skipped_oversize === true
    || (Number(result.page_count ?? 0) > 0 && Number(result.skipped_oversize_page_count ?? 0) >= Number(result.page_count));
  switch (file.state) {
    case "waiting":
      if (taskState === "paused") return "已暂停 · 等待继续";
      if (file.phase === "translate") return "待处理 · 等待翻译";
      if (file.phase === "generate") return "待处理 · 等待生成文件";
      if (file.phase === "adjust") return "待处理 · 等待调整排版";
      return "待处理";
    case "active": {
      const phase = taskState === "paused" && file.phase === "translate"
        ? "等待已提交页面完成"
        : PHASE_LABELS[file.phase] ?? "";
      const count = file.total > 0 ? ` · ${file.completed} / ${file.total}` : "";
      return `处理中${phase ? ` · ${phase}` : ""}${count}`;
    }
    case "generated": return `已生成${untranslated ? " · 整份未翻译" : ""}${needsReview ? " · 需复核" : ""}`;
    case "failed": return "未生成";
    case "unstarted": return "未开始";
    case "stopped": return "已停止";
    case "interrupted": return "应用中断";
    case "paused": return taskState === "paused" ? "已暂停 · 等待继续" : "待处理";
  }
}

export function fileProgressTone(file: FileProgress, taskState?: string): "ok" | "warn" | "dgr" | "mute" | "tint" {
  if (taskState === "interrupted" && ["waiting", "active", "paused"].includes(file.state)) return "dgr";
  if (file.state === "generated") return fileProgressLabel(file).includes("需复核") ? "warn" : "ok";
  if (file.state === "active") return "tint";
  if (file.state === "failed" || file.state === "interrupted") return "dgr";
  if (file.state === "stopped" || file.state === "unstarted" || file.state === "waiting" || file.state === "paused") return "mute";
  return "warn";
}
