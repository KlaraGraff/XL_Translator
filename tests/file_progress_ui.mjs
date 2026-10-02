import assert from "node:assert/strict";
import { fileProgressLabel, fileProgressTone, reduceFileProgress } from "../ui/src/file-progress.ts";

const a = {
  file_id: "id-a", name: "report.xlsx", relative_path: "one/report.xlsx",
  state: "active", phase: "translate", completed: 3, total: 12, result: {}, revision: 2,
};
const b = { ...a, file_id: "id-b", relative_path: "two/report.xlsx", revision: 1 };
let files = reduceFileProgress(new Map(), { revision: 2, files: [a, b] });
assert.equal(files.size, 2, "duplicate names remain separate by file_id");
files = reduceFileProgress(files, { ...a, state: "waiting", revision: 1 });
assert.equal(files.get("id-a").state, "active", "stale event cannot replace a newer snapshot");
files = reduceFileProgress(files, { revision: 2, files: [{ ...a, state: "waiting", phase: "generate", revision: 2 }] });
assert.equal(files.get("id-a").state, "active", "equal revision snapshot cannot replace current state");
files = reduceFileProgress(files, { ...a, state: "generated", result: { needs_review: true }, revision: 3 });
assert.equal(fileProgressLabel(files.get("id-a")), "已生成 · 需复核");
assert.equal(fileProgressLabel(files.get("id-b")), "处理中 · 翻译内容 · 3 / 12");
assert.equal(fileProgressLabel({ ...a, state: "generated", result: { all_pages_skipped_oversize: true } }), "已生成 · 整份未翻译");
assert.equal(fileProgressLabel({ ...a, state: "generated", result: { page_count: 8, skipped_oversize_page_count: 8, placeholder_page_count: 2 } }), "已生成 · 整份未翻译 · 需复核");
assert.equal(fileProgressLabel({ ...a, state: "generated", result: { quality_flagged_page_count: 1 } }), "已生成 · 需复核");
assert.equal(fileProgressLabel({ ...a, state: "generated", result: { review_failed_page_count: 1 } }), "已生成 · 需复核");
assert.equal(fileProgressLabel({ ...a, state: "generated", result: { placeholder_page_count: 1 } }), "已生成 · 需复核");
assert.equal(fileProgressLabel({ ...a, state: "generated", result: { truncated_cells: 1 } }), "已生成 · 需复核");
assert.equal(fileProgressLabel({ ...a, state: "generated", result: { sheet_name_status: "preserved" } }), "已生成 · 需复核");
assert.equal(fileProgressTone({ ...a, state: "generated", result: { all_pages_skipped_oversize: true, quality_flagged_page_count: 1 } }), "warn");
assert.equal(fileProgressLabel({ ...a, state: "waiting", phase: "adjust" }), "待处理 · 等待调整排版");
assert.equal(fileProgressLabel({ ...a, state: "waiting", phase: "generate" }, "paused"), "已暂停 · 等待继续");
assert.equal(fileProgressLabel(a, "paused"), "处理中 · 等待已提交页面完成 · 3 / 12");
assert.equal(fileProgressLabel({ ...a, state: "active", phase: "not_started" }), "处理中 · 3 / 12");
assert.equal(fileProgressLabel(a, "interrupted"), "应用中断");
assert.equal(fileProgressTone(a, "interrupted"), "dgr");
assert.equal(fileProgressLabel({ ...a, state: "generated" }, "interrupted"), "已生成");
const reset = reduceFileProgress(files, { ...a, state: "waiting", result: {}, revision: 4 });
assert.equal(reset.get("id-a").state, "waiting", "explicit resume supersedes generated state");
assert.deepEqual(reset.get("id-a").result, {}, "resume has no stale output");
console.log("file-status UI: 22 assertions passed");
