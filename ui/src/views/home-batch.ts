// 「翻译」页的「本批文件」卡片：拖入的文件夹里混着几种文件时，各类已在各自的页面读取（drop-dispatch.ts），
// 这里每类一行，写着这一页的设置（这里不改设置，点「去设置」到那一页去改），一个按钮全部开始。
// 一批任务由后端一次建好（POST /api/tasks/batch），结果都写进同一个输出文件夹；
// 共用一条连接时同时发出的请求总数封顶在几类设置里最高的那个，不再在批内弹确认（core/task_resources.py）。

import { createButton, createChip, openModal } from "../components";
import { icon } from "../icons";
import { t, tx } from "../i18n";
import { navigate } from "../router";
import type { TaskStatus } from "../api-client";
import { consumePendingRestartWarning } from "../update-controller";
import { openRestartBeforeTaskModal } from "../update-toast";
import {
  adoptBatchTasks,
  batchStartPayload,
  clearDispatchBatch,
  dispatchBatchView,
  fileNounPhrase,
  getClient,
  onWorkspaceChange,
  openOutputFolder,
  surfaceIcon,
  surfaceLabel,
  surfaceRecipeSummary,
  type BatchView,
  type Surface,
} from "./workspace";
import { redactedText } from "./workspace-util";

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

interface BatchStartResponse {
  requires_confirmation: boolean;
  shared_with?: string[];
  output_dir?: string;
  tasks?: TaskStatus[];
  failed?: Array<{ surface: string; message: string; reason?: string }>;
}

/** 后端说模型还没配好时，失败弹窗给一个去「设置 › 模型服务」的按钮。 */
const MODEL_MISSING = new Set(["translation_model_missing", "image_model_missing", "pdf_review_model_missing"]);

let starting = false;

function baseName(path: string): string {
  return path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;
}

async function startAll(view: BatchView, allowShared = false, repaint: () => void = () => undefined): Promise<void> {
  if (starting) return;
  // 更新装好还没重启时，新任务可能按旧组件启动失败：和各页「开始翻译」同一道提醒。
  if (consumePendingRestartWarning()) {
    openRestartBeforeTaskModal(() => void startAll(view, allowShared, repaint));
    return;
  }
  const ready = view.rows.filter((row) => row.phase === "ready");
  if (!ready.length) return;
  starting = true;
  repaint();
  try {
    const c = await getClient();
    const response = await c.request<BatchStartResponse>("/api/tasks/batch", {
      method: "POST",
      body: JSON.stringify({ items: ready.map((row) => batchStartPayload(row.surface)), allow_shared: allowShared }),
    });
    if (response.requires_confirmation) {
      confirmShared(view, response.shared_with ?? [], repaint);
      return;
    }
    adoptBatchTasks(response.output_dir ?? "", response.tasks ?? []);
    const failed = response.failed ?? [];
    if (failed.length) showFailed(failed);
  } catch (error) {
    openModal({
      tone: "warn",
      icon: "warn",
      title: t("本批文件未能开始"),
      body: [redactedText((error as Error)?.message, t("任务准备失败，无更多信息。"))],
      actions: [{ label: t("知道了"), variant: "primary" }],
    });
  } finally {
    starting = false;
    repaint();
  }
}

/** 这一批要和批外正在跑的任务共用连接：和单页开始同样问一句。批内几类之间共用不问。 */
function confirmShared(view: BatchView, labels: string[], repaint: () => void): void {
  openModal({
    tone: "warn",
    icon: "warn",
    title: t("与正在运行的任务共用同一条连接"),
    body: [
      t("本批文件将与正在运行的「{0}」共用同一条 API 连接。几个任务轮流使用这条连接，同时发出的请求总数不超过其中最高的那项设置。", labels.map((label) => tx(label)).join("、")),
      t("因此每个任务都会比单独运行时慢；如该连接按用量计费，几个任务将同时产生费用。如遇服务限速，程序将自动降低发送速度，设置中的速度不会被修改。"),
    ],
    actions: [
      { label: t("取消") },
      { label: t("仍要开始"), variant: "primary", onClick: () => void startAll(view, true, repaint) },
    ],
  });
}

function showFailed(failed: NonNullable<BatchStartResponse["failed"]>): void {
  const needsModel = failed.some((item) => MODEL_MISSING.has(item.reason ?? ""));
  openModal({
    tone: "warn",
    icon: "warn",
    title: t("部分类型未能开始"),
    body: [
      ...failed.map((item) => t("{0}：{1}", surfaceLabel(item.surface as Surface), tx(redactedText(item.message, t("任务准备失败，无更多信息。"))))),
      t("其余类型已开始。处理好上述问题后，可在对应页面单独开始。"),
    ],
    actions: needsModel
      ? [{ label: t("知道了") }, { label: t("前往设置"), variant: "primary", onClick: () => navigate("settings", { page: "models" }) }]
      : [{ label: t("知道了"), variant: "primary" }],
  });
}

function buildRows(view: BatchView): HTMLElement {
  const rows = el("div", "bc-rows");
  for (const row of view.rows) {
    const line = el("div", "bc-row");
    line.dataset.surface = row.surface;
    line.append(icon(surfaceIcon(row.surface), { size: "sm" }));
    const name = el("div", "bc-nm");
    name.append(el("b", undefined, surfaceLabel(row.surface)), el("span", "bc-ct", fileNounPhrase(row.surface, row.found)));
    const recipe = el("div", "bc-recipe");
    const summary = el("span", "bc-set", surfaceRecipeSummary(row.surface));
    summary.title = summary.textContent ?? "";
    recipe.append(summary);
    if (row.note) recipe.append(el("span", `bc-note${row.noteWarn ? " warn" : ""}`, row.note));
    const chip = createChip({ label: row.label, tone: row.tone });
    const go = el("button", "linklike bc-go", row.phase === "running" || row.phase === "finished" ? t("查看 ↗") : t("去设置 ↗"));
    go.type = "button";
    go.title = surfaceLabel(row.surface);
    go.addEventListener("click", () => navigate(row.surface));
    line.append(name, recipe, chip, go);
    if (row.phase === "running") {
      const bar = el("div", "bc-bar");
      const fill = el("i");
      fill.style.width = `${Math.max(2, Math.min(100, row.percent))}%`;
      bar.append(fill);
      line.append(bar);
    }
    rows.append(line);
  }
  return rows;
}

function buildFoot(view: BatchView, repaint: () => void): HTMLElement {
  const foot = el("div", "bc-foot");
  const started = view.rows.some((row) => row.phase === "running" || row.phase === "finished");
  const words = el("div", "bc-out");
  if (started && view.outputDir) {
    // 输出文件夹就建在拖入的文件夹里，写它自己的名字就够了；完整路径放在悬停提示里。
    words.textContent = t("输出至 {0}", baseName(view.outputDir));
    words.title = view.outputDir;
  } else {
    words.textContent = t("各类文件按各自页面的设置翻译，结果统一写入「{0}」中新建的一个输出文件夹。", baseName(view.root));
  }
  foot.append(words);
  if (started) {
    const running = view.rows.some((row) => row.phase === "running");
    foot.append(running
      ? createButton({ label: t("全部任务"), icon: "tasks", onClick: () => navigate("tasks") })
      : createButton({ label: t("打开输出文件夹"), icon: "folder", variant: "primary", onClick: () => openOutputFolder(view.outputDir), disabled: !view.outputDir }));
    return foot;
  }
  const reading = view.rows.some((row) => row.phase === "reading");
  const count = view.rows.reduce((sum, row) => sum + (row.phase === "ready" ? row.count : 0), 0);
  const label = starting ? t("正在启动…") : reading ? t("正在读取…") : t("全部开始（{0} 个文件）", count);
  foot.append(createButton({
    label,
    icon: "play",
    variant: "primary",
    disabled: starting || reading || count === 0,
    onClick: () => void startAll(view, false, repaint),
  }));
  return foot;
}

/** 「翻译」页挂载时调用：返回卡片节点（没有混合的一批时是空占位）和卸载时要调的清理函数。 */
export function createBatchCard(): { node: HTMLElement; dispose: () => void } {
  const slot = el("div", "bc-slot");
  let queued = false;
  const paint = (): void => {
    queued = false;
    const view = dispatchBatchView();
    slot.textContent = "";
    if (!view) return;
    const card = el("section", "batch-card");
    const head = el("div", "bc-head");
    head.append(icon("folder", { size: "sm" }), el("b", undefined, t("本批文件")));
    const from = el("span", "bc-from", baseName(view.root));
    from.title = view.root;
    head.append(from);
    const close = el("button", "linklike bc-x", "×");
    close.type = "button";
    close.setAttribute("aria-label", t("不再提示"));
    close.addEventListener("click", clearDispatchBatch);
    head.append(close);
    card.append(head, buildRows(view), buildFoot(view, repaint));
    slot.append(card);
  };
  // 进度事件一秒能来好几次，合成一帧画一次。
  const repaint = (): void => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(paint);
  };
  paint();
  const off = onWorkspaceChange(repaint);
  return { node: slot, dispose: off };
}
