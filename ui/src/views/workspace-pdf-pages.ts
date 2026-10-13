// 翻译工作区 · PDF 逐页面板：页恢复卡片、逐页审核卡片（失败页批量重跑、分组折叠、
// 只看有问题的页）、页对比弹窗，以及这些面板共用的逐页快照拉取。
//
// 从 workspace.ts 拆出来，因为这一块只服务 PDF 板块、自成一体，却占了模板文件四分之一。
// 对外只暴露五个入口：
//   buildPdfRecoveryCard / buildPdfReviewCard —— 左栏渲染时调用，返回卡片或 null；
//   fetchPdfPagesSnapshot / schedulePdfSnapshotRefresh —— 任务事件流触发的快照拉取；
//   stopPdfRerunTicker —— 任务结束、离开页面时停掉单页重跑的轮询。
//
// 反过来它要用工作区的状态（states、rerender、getClient 等）。这是一个刻意保留的循环
// 引用：这些名字只在函数体里、运行时才被访问，模块求值阶段不碰，ES 模块下是安全的。
// 不要在本文件的顶层直接读取 workspace.ts 导出的值。

import { ApiError, type PdfPage, type PdfPageFile, type PdfPagesSnapshot } from "../api-client";
import { createButton, createChip, openModal, showToast, type ModalHandle } from "../components";
import { t, tx } from "../i18n";
import { icon } from "../icons";
import { getClient, rerender, states, SURFACE_LABEL, type LocalTask, type Surface } from "./workspace";
import { el, redactedText } from "./workspace-util";

// ---------------------------------------------------------------------------
// PDF 页恢复 / 逐页审核 —— 真正的逐页交互面板（GET /pdf-pages 拉取式快照 + 三个操作端点）。
// 样张④只画了单文件、少量示例行；这里额外处理多文件分组与几百页的可滚动表格，
// 具体拉取触发点集中在 fetchPdfPagesSnapshot() 的调用方（watchTask 事件流 / adopt / 任务启动）。
// ---------------------------------------------------------------------------

type PdfPageRow = { file: PdfPageFile; page: PdfPage };

/** 需要出现在「页恢复」卡片里的页：失败/占位、已排队操作、或用户已接受跳过占位——
 *  纯 pending（还没跑到）或已通过的页不算，那些在「逐页审核」表里露出即可。 */
function collectRecoveryRows(snapshot: PdfPagesSnapshot): PdfPageRow[] {
  const rows: PdfPageRow[] = [];
  for (const file of snapshot.files) {
    for (const page of file.pages) {
      if (page.status === "failed" || page.placeholder || page.pending_action || page.user_skipped) {
        rows.push({ file, page });
      }
    }
  }
  return rows;
}

/** 刚点下去、快照还没回来的那一次单页重生成。
 *
 *  后端同一时刻只允许跑一页（rerun_pdf_page 撞上 rerun_active 直接抛 409），但终态没有
 *  逐页 SSE，快照两秒才轮询一次。这两秒里 snapshot.rerun.active 还是 false，逐页表里
 *  每一行的「重新生成」都照常可点——连点两页，第二页换来的只是一句报错。发起时先在本地
 *  记一笔，界面立刻按「有一页在跑」渲染，等快照把真实状态换上来再清掉。 */
type PendingRerun = { taskId: string; relativePath: string; pageNumber: number };

const pendingReruns: Record<Surface, PendingRerun | undefined> = { excel: undefined, word: undefined, pdf: undefined, cad: undefined };

/** 本地这笔只对发起它的那个任务有效——切到别的任务不能跟着禁用。 */
function pendingRerunFor(surface: Surface, taskId: string): PendingRerun | undefined {
  const pending = pendingReruns[surface];
  return pending && pending.taskId === taskId ? pending : undefined;
}

/** actionable=false 时按钮全部禁用但常驻显示，这句短话解释原因。
 *
 *  只在卡片顶部说一次。早先每个按钮的 title 上也挂着同一句，一张十几行的表里
 *  就有二十多个一模一样的悬浮提示，读起来像出了二十个不同的问题。按行不同的原因
 *  （比如按幅面跳过的页）仍然挂在该行的按钮上——那才是 title 该承担的。 */
function pdfActionsDisabledReason(snapshot: PdfPagesSnapshot, pending?: PendingRerun): string {
  if (!snapshot.terminal) return t("暂停任务后才能重新生成或跳过页面；操作会在继续翻译时生效。");
  // 快照还没确认之前，用本地记的那一页说话——否则这句会写着「仍可单独重新生成某一页」，
  // 而下面每一行的入口都已经是灰的，一屏之内自相矛盾。
  // page_number > 0 这道额外的门槛是防「重跑刚结束那一瞬间」的显示闪烁：后端把 rerun
  // 状态标记为「不在跑」和把 page_number 归零不保证是同一次写入，轮询有可能正好夹在
  // 中间读到 active=true、page_number=0——页码从 1 开始，0 从来不是一页真实的页，
  // 直接当「没有有效页码」处理，不把这个假页码报给用户。
  const rerunningPage = snapshot.rerun.active && snapshot.rerun.page_number > 0 ? snapshot.rerun.page_number : pending?.pageNumber;
  if (rerunningPage !== undefined) return t("第 {0} 页正在重新生成，完成后才能操作其他页；其他页仍可查看。", rerunningPage);
  if (snapshot.rerun.error) return t("上次重新生成未成功：{0}", tx(redactedText(snapshot.rerun.error, t("输出文件未改动。"))));
  // 终态能做的只有单页重生成：它不排队，点了立刻重跑，并把输出文件重新合成一遍。
  if (snapshot.rerun_actionable) return t("任务已结束，仍可单独重新生成某一页。重新生成将立即重新处理该页并更新输出文件，原版本保留为「上一版」，可随时在对比窗口中换回。");
  return t("任务已结束，仅可查看页面对比，无法执行其他操作。");
}

/** 这一页是不是正在被重新生成。终态没有逐页 SSE，这个判断只能来自快照。 */
function isPageRerunning(snapshot: PdfPagesSnapshot, file: PdfPageFile, page: PdfPage, pending?: PendingRerun): boolean {
  if (pending && pending.relativePath === file.relative_path && pending.pageNumber === page.page_number) return true;
  return (
    snapshot.rerun.active &&
    snapshot.rerun.relative_path === file.relative_path &&
    snapshot.rerun.page_number === page.page_number
  );
}

async function runPdfPageAction(surface: Surface, taskId: string, file: PdfPageFile, page: PdfPage, action: "regenerate" | "skip"): Promise<void> {
  try {
    const c = await getClient();
    if (action === "regenerate") await c.regeneratePdfPage(taskId, file.relative_path, page.page_number);
    else await c.skipPdfPage(taskId, file.relative_path, page.page_number);
    showToast({ message: action === "regenerate" ? t("已安排重新生成第 {0} 页，继续翻译后执行。", page.page_number) : t("已安排跳过第 {0} 页，继续翻译后执行。", page.page_number) });
    await fetchPdfPagesSnapshot(surface, taskId);
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, t("操作失败。")), error: true });
  }
}

/** 危险操作确认：要再花一次钱、还会改写可能已经交出去的文件，两条都得说。
 *
 *  「覆盖之后取不回来」这句已经不成立了——重生成会把现在这一版留成「上一版」，
 *  在对比弹窗里随时能换回来。文案跟着改口，按钮也不再叫「覆盖并重新生成」：
 *  真正不可逆的只剩「已经发出去的那份文件」，那句提醒保留。 */
function confirmPdfPageRerun(surface: Surface, taskId: string, file: PdfPageFile, page: PdfPage): void {
  openModal({
    tone: "danger",
    icon: "warn",
    title: t("重新生成第 {0} 页？", page.page_number),
    body: [
      t("该页当前译文将保留为「上一版」；如对重新生成的结果不满意，可在对比窗口中换回。重新生成将再次调用模型，按该页用量计费。输出文件将随之更新；如已对外发送，请重新发送。"),
    ],
    actions: [
      { label: t("取消") },
      {
        label: t("重新生成"),
        variant: "danger-solid",
        onClick: () => void runPdfPageRerun(surface, taskId, file, page),
      },
    ],
  });
}

async function runPdfPageRerun(surface: Surface, taskId: string, file: PdfPageFile, page: PdfPage): Promise<void> {
  // 请求还在路上时表里其他行就得先锁上，不能等两秒后的那次轮询。
  const pending: PendingRerun = { taskId, relativePath: file.relative_path, pageNumber: page.page_number };
  pendingReruns[surface] = pending;
  rerender(surface);
  try {
    const c = await getClient();
    await c.rerunPdfPage(taskId, file.relative_path, page.page_number);
    showToast({ message: t("第 {0} 页正在重新生成，完成后将更新输出文件。", page.page_number) });
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, t("重新生成失败。")), error: true });
  }
  // 成功要拿到「重新生成中」这一行，失败也要把后端此刻的真实状态换上来。
  try {
    await fetchPdfPagesSnapshot(surface, taskId);
  } finally {
    // 快照回来了（或者没拉动）就把话语权交回给它：本地这笔只负责盖住中间那一小段。
    // 拉失败时也要清——那种情况下我们并不知道后端在跑什么，不该一直锁着整张表。
    if (pendingReruns[surface] === pending) {
      pendingReruns[surface] = undefined;
      rerender(surface);
    }
  }
}

/** 终态没有 SSE，重生成的进度只能靠轮询这份快照；跑完自己停，不留空转的 interval。
 *
 *  用 setTimeout 自我重排而不是 setInterval：sidecar 中途重启过时 getPdfPages 会连续
 *  失败，纯 setInterval 只会不多不少每 2 秒报一次错、永远不停（高-10）——快照拉不到，
 *  local.pdfPagesSnapshot.rerun.active 停在拉失败前的最后一次真值上，永远不会自己变
 *  false，下一轮判断条件形同虚设。这里改成连续失败计数：失败就退避（翻倍，封顶 20
 *  秒），累计到上限就整个停掉并且只在停的那一刻说一句话，不再每一轮都弹 toast。 */
const rerunTickers: Record<Surface, number | undefined> = { excel: undefined, word: undefined, pdf: undefined, cad: undefined };
const rerunTickerFailures: Record<Surface, number> = { excel: 0, word: 0, pdf: 0, cad: 0 };
const RERUN_POLL_BASE_MS = 2000;
const RERUN_POLL_MAX_MS = 20000;
const RERUN_POLL_MAX_FAILURES = 5;

function startPdfRerunTicker(surface: Surface, taskId: string): void {
  if (rerunTickers[surface] !== undefined) return;
  // fetchPdfPagesSnapshot 的成功分支无条件调用这里——包括「请求发出去时页面还挂载着，
  // 响应回来时用户已经切走」这种情形（unmountWorkspace 早就 stopPdfRerunTicker 把槽位
  // 清空了）。不看挂载状态就重开，会在用户已经离开的页面上复活出一个 handle：紧接着
  // scheduleRerunTick 的下一轮会把这个新 handle 覆盖掉，stopPdfRerunTicker 永远够不着
  // 它，两条链就在设置、记忆库等不相干的页面上继续轮询、继续弹「已停止自动刷新」的
  // toast——正是高-10 要消灭的跨页面行为（B1-2）。renderer 在 unmountWorkspace 里被置
  // 空、mountWorkspace 里被重新赋值，用它当「这一屏当前是否挂载」的判断刚好合适。
  if (states[surface].renderer === null) return;
  rerunTickerFailures[surface] = 0;
  scheduleRerunTick(surface, taskId, RERUN_POLL_BASE_MS);
}

function scheduleRerunTick(surface: Surface, taskId: string, delay: number): void {
  rerunTickers[surface] = window.setTimeout(() => {
    void (async () => {
      const local = states[surface].task;
      if (!local || local.task.task_id !== taskId || !local.pdfPagesSnapshot?.rerun.active) {
        stopPdfRerunTicker(surface);
        return;
      }
      const ok = await fetchPdfPagesSnapshot(surface, taskId, { silent: true });
      // 这次 await 期间可能已经被 stopPdfRerunTicker 掐掉（unmountWorkspace、任务切走），
      // handle 已经清空，不该再续排下一轮。
      if (rerunTickers[surface] === undefined) return;
      if (!ok) {
        rerunTickerFailures[surface] += 1;
        if (rerunTickerFailures[surface] >= RERUN_POLL_MAX_FAILURES) {
          stopPdfRerunTicker(surface);
          showToast({ message: t("刷新重新生成进度连续失败，已停止自动刷新；重新打开该任务即可重试。"), error: true });
          return;
        }
        scheduleRerunTick(surface, taskId, Math.min(RERUN_POLL_BASE_MS * 2 ** rerunTickerFailures[surface], RERUN_POLL_MAX_MS));
        return;
      }
      rerunTickerFailures[surface] = 0;
      scheduleRerunTick(surface, taskId, RERUN_POLL_BASE_MS);
    })();
  }, delay);
}

export function stopPdfRerunTicker(surface: Surface): void {
  const handle = rerunTickers[surface];
  if (handle !== undefined) {
    window.clearTimeout(handle);
    rerunTickers[surface] = undefined;
  }
  rerunTickerFailures[surface] = 0;
}

export function buildPdfRecoveryCard(surface: Surface, local: LocalTask): HTMLElement | null {
  const snapshot = local.pdfPagesSnapshot;
  if (!snapshot) return null;
  const rows = collectRecoveryRows(snapshot);
  if (!rows.length) return null;
  const taskId = local.task.task_id;
  const multiFile = snapshot.files.length > 1;

  const card = el("div", "card");
  card.style.cssText = "padding:0;overflow:hidden";
  const head = el("div", "tc-head");
  const b = el("b");
  b.textContent = t("页恢复");
  const span = el("span");
  span.textContent = t("失败页在此重试，不影响其他页");
  head.append(b, span);
  card.append(head);

  if (!snapshot.actionable) {
    const note = el("p", "ws-note");
    note.style.cssText = "text-align:left;padding:8px 14px 0";
    note.textContent = pdfActionsDisabledReason(snapshot, pendingRerunFor(surface, taskId));
    card.append(note);
  }

  const list = el("div");
  list.style.cssText = "max-height:220px;overflow:auto";
  list.dataset.keepScroll = "pdf-recovery";
  rows.forEach(({ file, page }, index) => {
    const row = buildRecoveryRow(surface, taskId, snapshot, file, page, multiFile);
    if (index === 0) row.style.borderTop = "0";
    list.append(row);
  });
  card.append(list);
  return card;
}

function buildRecoveryRow(surface: Surface, taskId: string, snapshot: PdfPagesSnapshot, file: PdfPageFile, page: PdfPage, multiFile: boolean): HTMLElement {
  const row = el("div", "filerow");
  const actionable = snapshot.actionable;

  if (page.pending_action === "regenerate") {
    row.append(createChip({ label: t("待执行 · 重新生成"), tone: "tint" }));
  } else if (page.pending_action === "skip") {
    row.append(createChip({ label: t("待执行 · 跳过"), tone: "tint" }));
  } else if (page.user_skipped) {
    row.append(createChip({ label: t("已跳过"), tone: "mute" }));
  } else if (page.attempts > 0) {
    row.append(createChip({ label: t("重试 {0}", page.attempts), tone: "warn" }));
  } else {
    row.append(createChip({ label: t("失败"), tone: "dgr" }));
  }

  const nm = el("span", "nm");
  const filePrefix = multiFile ? `${file.name} · ` : "";
  nm.textContent = filePrefix + t("第 {0} 页", page.page_number) + " · " + tx(redactedText(page.error, page.placeholder ? t("页图生成失败") : t("待处理")));
  row.append(nm);

  row.append(createButton({
    label: t("立即重试"),
    size: "mini",
    disabled: !actionable,
    onClick: () => void runPdfPageAction(surface, taskId, file, page, "regenerate"),
  }));
  row.append(createButton({
    label: t("跳过该页"),
    size: "mini",
    disabled: !actionable,
    onClick: () => void runPdfPageAction(surface, taskId, file, page, "skip"),
  }));
  return row;
}

// ---------------------------------------------------------------------------
// 逐页审核卡片：失败页横幅批量重跑 + 文件分组折叠 + 「只看有问题的页」筛选。
// 三件事有个共同前提——这张卡片每次快照刷新都是整体重建 DOM（buildColLeft 每次都
// 重新调用 buildPdfReviewCard()，不是增量更新），所以「哪个分组展开了」「筛选勾没勾」
// 「批量重跑跑到第几页了」都不能存在 DOM 属性或函数局部变量上，一刷新就清零。全部搬到
// 下面这几个模块级状态里，键是 taskId：同一个任务内随便怎么重渲染都不丢，切到别的
// 任务或重启应用则从空开始（这正是任务书要的行为，不是偷懒没做持久化）。
// ---------------------------------------------------------------------------

interface PdfReviewUiState {
  /** 每个文件分组的展开/收起，键是 file.relative_path；没记录过的文件按
   *  defaultGroupOpen 规则现算，不预先填满整张表。 */
  collapse: Map<string, boolean>;
  /** 「只看有问题的页」勾选框。 */
  onlyBad: boolean;
}
const pdfReviewUiState = new Map<string, PdfReviewUiState>();
function reviewUiState(taskId: string): PdfReviewUiState {
  let state = pdfReviewUiState.get(taskId);
  if (!state) {
    state = { collapse: new Map(), onlyBad: false };
    pdfReviewUiState.set(taskId, state);
  }
  return state;
}

/** 这一页算不算「失败页批量重跑」的目标：跟 reviewResultChip 同一套失败判断
 *  （status === "failed" || placeholder），但排除用户已经在面板里主动跳过的占位页——
 *  那是用户自己认下的结果，批量重跑不该替他们翻案；真要改主意，这一行自己的
 *  「重新生成」链接还在，没有被拿掉。 */
function pageNeedsRerun(page: PdfPage): boolean {
  return (page.status === "failed" || page.placeholder) && !page.user_skipped;
}

/** 这个文件这次有没有写出输出文件。只有 completed / needs_review 两种状态有产物；
 *  后端的单页重跑也只认这两种（rerun_page：没有产物就没有东西可以重装，直接拒绝）。
 *  全部页都失败的文件根本没有「输出的 PDF」，横幅不能说「输出的 PDF 里这几页是占位图」，
 *  也不能给一个点了必然报错的「只重跑这 N 页」。 */
function fileProducedOutput(file: PdfPageFile): boolean {
  return file.status === "completed" || file.status === "needs_review";
}

/** 「这一页算不算有问题」统一按 reviewResultChip 判出来的颜色定，不重新发明一套规则：
 *  dgr（失败 / 已跳过占位）和 warn（建议复核 / 审核未通过 / 待复核 / 任务已停止时的
 *  待处理页）都算。折叠默认展开、分组小结、「只看有问题的页」筛选三处共用这一个答案，
 *  保证它们说的是同一件事，不会出现「筛选说这页没问题，分组小结却把它算进失败」这种
 *  对不上。taskStopped 只影响 reviewResultChip 对 pending 页给出的颜色，见那边注释。 */
function pageIsProblem(page: PdfPage, taskStopped: boolean): boolean {
  const tones = reviewResultChip(page, taskStopped).className.split(" ");
  return tones.includes("dgr") || tones.includes("warn");
}

interface PdfGroupHandle {
  fileKey: string;
  /** 这个文件里有没有需要批量重跑的失败页——只看 pageNeedsRerun，不算「建议复核」。
   *  「定位到这几页」按的就是这条：只展开真正失败的分组，跟决定默认展开/收起状态、
   *  分组小结用的「有问题」（失败 + 建议复核）不是同一个判断，两者故意分开。 */
  hasFailingPage: boolean;
  rows: HTMLTableRowElement[];
  /** 单文件任务不建分组标题行，这时候没有按钮可切，toggleGroupOpen 对它是空操作。 */
  button?: HTMLButtonElement;
}

/** 展开/收起一个文件分组：aria-expanded 驱动箭头旋转（CSS 选择器，见 workspace.css
 *  的 .grp[aria-expanded="false"] .cv），行的 hid 类直接控制显示；顺手把新状态写回
 *  reviewUiState，不用等下一次整卡重渲染。 */
function toggleGroupOpen(ui: PdfReviewUiState, handle: PdfGroupHandle, open?: boolean): void {
  const btn = handle.button;
  if (!btn) return;
  const next = open ?? btn.getAttribute("aria-expanded") !== "true";
  btn.setAttribute("aria-expanded", String(next));
  handle.rows.forEach((row) => row.classList.toggle("hid", !next));
  ui.collapse.set(handle.fileKey, next);
}

/** 「定位到这几页」：照抄样张 focusBad() 的效果——含失败页的分组展开、干净分组收起，
 *  再滚到第一个失败页。单文件任务没有分组按钮可切（toggleGroupOpen 对它是空操作），
 *  滚动仍然生效。 */
function focusFailingPages(ui: PdfReviewUiState, handles: PdfGroupHandle[], firstFailingRow: HTMLTableRowElement | null): void {
  for (const handle of handles) {
    if (handle.button) toggleGroupOpen(ui, handle, handle.hasFailingPage);
  }
  firstFailingRow?.scrollIntoView({ block: "center", behavior: "smooth" });
}

/** 这一页跑完了没有。后端的三个中间态：pending 还没轮到、rendered 页图已渲染正在等
 *  模型、placeholder_pending 等着补占位页。三个的 chip 都是灰的（reviewResultChip），
 *  既不是失败也不是「有问题」——小结要是不单独数它们，就会把它们算进「通过」。 */
function pageIsUnfinished(page: PdfPage): boolean {
  return page.status === "pending" || page.status === "rendered" || page.status === "placeholder_pending";
}

/** 分组标题右侧常驻小结。
 *
 *  这里只说数得出来的事，不下「全部通过」这种总结论，除非真的每一页都跑完且没问题。
 *  之前的写法把「还没跑完」和「通过」混成一档，代价是两处睁眼说瞎话：
 *
 *  1. 任务刚开跑，快照里所有文件的所有页都是 pending，小结却写「17 页全部通过」——
 *     一页都还没翻。多文件任务必然经过这一刻，不是边角情况。
 *  2. 预处理就失败的文件（PDF 打不开、图片校验没过）page_count 是 0、一页都没有，
 *     `total - failed` 算出来是 0，小结写「0 页全部通过」。改动前这里是中性的
 *     「xxx.pdf · 0 页」，不说谎；这是新引入的虚假肯定，而且正好落在最该被看见的
 *     文件上。文件级的失败原因 status/error 快照里现成有，直接用。
 *
 *  所以改成分桶累加：失败 / 已停止 / 建议复核 / 待处理 / 通过，哪档是 0 就不出现。 */
function buildGroupSummaryNodes(file: PdfPageFile, taskStopped: boolean, reviewEnabled: boolean): HTMLElement[] {
  if (file.translation_route === "word") {
    // 整份转成 Word 翻译（Beta）：没有逐页结果，只说这份文件到哪一步了。
    const span = el("span", file.status === "failed" ? "bad" : "");
    span.textContent = file.status === "failed"
      ? t("转成 Word 翻译失败")
      : file.status === "completed" || file.status === "needs_review"
        ? t("已转成 Word 翻译 · {0} 页", file.page_count)
        : file.status === "stopped"
          ? t("转成 Word 翻译 · 已停止")
          : t("转成 Word 翻译 · 处理中");
    return [span];
  }
  if (file.pages.length === 0) {
    // 一页都没有：要么预处理就失败（打不开、图片校验没过），要么还没轮到预处理。
    // 有页的失败文件（例如单张图片那一页没生成出来）不是「打不开」，往下按页分桶说。
    const span = el("span", file.status === "failed" ? "bad" : "");
    span.textContent = file.status === "failed" ? t("无法打开文件") : t("待处理");
    return [span];
  }

  const pages = file.pages;
  const failed = pages.filter(pageNeedsRerun);
  // 停止时还没排上号的页单独一档：它既不是「试过没通过」（失败/建议复核），也不是
  // 「还在排队，等着轮到」（待处理）——任务已经定格在终态，它不会再自己轮到。混进
  // 前两档的任何一档都会让用户会错意（以为程序判定这页有质量问题，或以为程序还在
  // 后台继续跑），见 reviewResultChip 对 pending 页在 taskStopped 下的处理。
  const stoppedSet = new Set(taskStopped ? pages.filter((page) => !pageNeedsRerun(page) && page.status === "pending") : []);
  const flagged = pages.filter((page) => !pageNeedsRerun(page) && !stoppedSet.has(page) && pageIsProblem(page, taskStopped));
  const unfinished = pages.filter((page) => !pageNeedsRerun(page) && !stoppedSet.has(page) && !pageIsProblem(page, taskStopped) && pageIsUnfinished(page));
  // 按幅面跳过的 A3+ 页没有翻译，只是原样保留——不能算进「通过」。以前一份只有一页
  // A3 的文件汇总写着「1 页全部通过」，展开才看到那一页是「按幅面跳过」。
  const isSkippedOversize = (page: PdfPage): boolean =>
    Boolean(page.skipped_oversize) && page.inplace_status !== "done" && !page.kept_reason && page.word_status !== "done";
  const oversize = pages.filter((page) => !pageNeedsRerun(page) && !stoppedSet.has(page) && !flagged.includes(page) && !unfinished.includes(page) && isSkippedOversize(page));
  const passed = pages.length - failed.length - stoppedSet.size - flagged.length - unfinished.length - oversize.length;

  if (failed.length === 0 && stoppedSet.size === 0 && flagged.length === 0 && unfinished.length === 0 && oversize.length === 0) {
    const all = el("span");
    // 没开逐页审核时谁也没判过「通过」，只能说「已生成」。
    all.textContent = reviewEnabled ? t("{0} 页全部通过", pages.length) : t("{0} 页已生成", pages.length);
    return [all];
  }

  const parts: HTMLElement[] = [];
  const push = (text: string, className?: string): void => {
    const node = el("span", className);
    node.textContent = text;
    parts.push(node);
  };
  if (failed.length > 0) push(t("{0} 失败", failed.length), "bad");
  if (stoppedSet.size > 0) push(t("{0} 已停止", stoppedSet.size), "flag");
  if (flagged.length > 0) push(t("{0} 建议复核", flagged.length), "flag");
  if (unfinished.length > 0) push(t("{0} 待处理", unfinished.length));
  if (oversize.length > 0) push(t("{0} 原样保留", oversize.length));
  if (passed > 0) push(reviewEnabled ? t("{0} 通过", passed) : t("{0} 已生成", passed));

  const out: HTMLElement[] = [];
  parts.forEach((node, i) => {
    if (i > 0) {
      const dot = el("span");
      dot.textContent = "·";
      out.push(dot);
    }
    out.push(node);
  });
  return out;
}

interface PdfBatchRerunState {
  taskId: string;
  total: number;
  done: number;
  /** 当前这一页的定位信息，纯粹是这个函数自己数出来的——后端不知道这是「一批」，
   *  快照里只有单页的 rerun.active / page_number，没有「第几页/共几页」这种批次概念，
   *  也就没法从快照反推进度，只能靠发起方自己记。 */
  current: { fileName: string; pageNumber: number } | null;
  /** 用户按了「停止」。含义是「不再发起后面的页」，不是「立刻中断」——后端没有取消
   *  接口（api-client 只有 rerunPdfPage，没有对应的 cancel），已经发出去的那一页在
   *  服务端照跑到底。所以按下之后横幅要改口说「跑完这一页就停」，不能假装已经停了。 */
  stopped: boolean;
}
const pdfBatchRerunState: Record<Surface, PdfBatchRerunState | undefined> = {
  excel: undefined,
  word: undefined,
  pdf: undefined,
  cad: undefined,
};

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

/** 等当前这一页真正跑完（rerun.active 归 false）才能发下一页。终态的单页重跑只有一个
 *  槽位——PdfPagesSnapshot.rerun 是整个任务一份，不是按页分的（见它上面的注释，以及
 *  pdfActionsDisabledReason 里「第 N 页正在重新生成，跑完再操作下一页」那句），批量
 *  重跑没法并发发起，只能串行等。轮询节奏跟 startPdfRerunTicker 一样是 2 秒一次，但
 *  不复用那个 interval：那套是给「用户手动点了单独一页」配的独立计时器，跟这里
 *  「等完一页再发下一页」的串行 await 用途不一样，硬凑一起只会让两边互相踩状态。
 *
 *  返回 false 表示放弃了（连续拉快照失败太多次），调用方应该停掉整批，不能只是
 *  沉默地继续发下一页——快照拉不到时我们并不知道后端到底有没有跑完，硬发下一页
 *  只会撞 409。返回 true 表示槽位正常释放（或任务已经不是这一个了，交回调用方
 *  自己的守卫判断）。 */
async function waitForRerunSlot(surface: Surface, taskId: string): Promise<boolean> {
  // 请求刚发出去，给后端一点时间把 rerun.active 标记上；不等这一下，第一次检查可能
  // 正好撞在「还没来得及写状态」的空档，会把这一页误判成「已经跑完」，提前发下一页。
  await sleep(1500);
  let failures = 0;
  for (;;) {
    const local = states[surface].task;
    if (!local || local.task.task_id !== taskId) return true;
    if (!local.pdfPagesSnapshot?.rerun.active) return true;
    // sidecar 中途重启过时快照会连续拉不到：local.pdfPagesSnapshot.rerun.active 停在
    // 拉失败前的最后一次真值上永远不会自己变 false，这个 for(;;) 不设退出条件就是
    // 死循环，批量重跑卡死在第一页（同高-10 根因）。跟单页那个计时器一样退避 + 封顶。
    const delay = Math.min(RERUN_POLL_BASE_MS * 2 ** failures, RERUN_POLL_MAX_MS);
    await sleep(delay);
    const ok = await fetchPdfPagesSnapshot(surface, taskId, { silent: true });
    if (!ok) {
      failures += 1;
      if (failures >= RERUN_POLL_MAX_FAILURES) {
        showToast({ message: t("多次未能刷新重新生成进度，批量重新生成已停止；请稍后重新打开该任务重试。"), error: true });
        return false;
      }
      continue;
    }
    failures = 0;
  }
}

/** 「只重跑这 N 页」确认后的批量执行。走的是终态专用的 rerunPdfPage——立即触发、跑完
 *  覆盖输出，不是 runPdfPageAction 包的 regeneratePdfPage 那条「排队，继续翻译时生效」
 *  的路：后者是给还没结束的任务用的（触发后要等任务继续跑才生效），api-client.ts 在
 *  rerunPdfPage 上明确写着「终态任务专用：现在就重跑」。这条横幅只在 snapshot.terminal
 *  时出现（见 buildPdfReviewCard），用 regeneratePdfPage 会跟 rerun_actionable 的语义
 *  对不上——终态任务本来就不该走「排队等继续翻译」这条路，因为它不会再继续翻译了。 */
async function runPdfBatchRerun(surface: Surface, taskId: string, rows: PdfPageRow[]): Promise<void> {
  const batch: PdfBatchRerunState = { taskId, total: rows.length, done: 0, current: null, stopped: false };
  pdfBatchRerunState[surface] = batch;
  rerender(surface);
  try {
    for (const { file, page } of rows) {
      // 三个都得查，少一个就会继续往外发请求：
      //   - stopped：用户按了停止，当前这一页跑完就到此为止；
      //   - 状态被换掉：被新一轮批量盖掉；
      //   - taskId 对不上：用户在同一个工作区切到了别的任务。第三条以前漏了——原来的
      //     注释写着「任务切走了」，但 pdfBatchRerunState 只在这个函数里改，focusTask
      //     换 states[surface].task 时压根不碰它，这个守卫对任务切换是瞎的。后果是
      //     waitForRerunSlot 的 taskId 检查会让它每次立刻返回，剩下几页背靠背连发，
      //     后端一路 409（page_rerun_active），连弹几条错误 toast，一页也没重跑成。
      if (pdfBatchRerunState[surface] !== batch || batch.stopped) return;
      if (states[surface].task?.task.task_id !== taskId) return;
      batch.current = { fileName: file.name, pageNumber: page.page_number };
      rerender(surface);
      try {
        const c = await getClient();
        await c.rerunPdfPage(taskId, file.relative_path, page.page_number);
        await fetchPdfPagesSnapshot(surface, taskId);
        const gotSlot = await waitForRerunSlot(surface, taskId);
        // 放弃等待（连续拉快照失败）时不能装作这一页正常跑完继续发下一页——那样只会
        // 一页页往下、每一页都在死循环里重新攒够失败次数才停，体感上批量重跑还是卡住。
        // waitForRerunSlot 已经弹过一次说明性 toast，这里直接收工。
        if (!gotSlot) {
          batch.stopped = true;
          return;
        }
      } catch (error) {
        showToast({ message: redactedText((error as Error)?.message, t("第 {0} 页重新生成失败。", page.page_number)), error: true });
      }
      if (pdfBatchRerunState[surface] !== batch) return;
      batch.done += 1;
      rerender(surface);
    }
  } finally {
    if (pdfBatchRerunState[surface] === batch) pdfBatchRerunState[surface] = undefined;
    rerender(surface);
  }
}

/** 停止批量重跑：只掐掉「发起下一页」，不碰已经在跑的那一页。
 *
 *  不弹确认框——这是刹车，不是危险操作，中间再挡一层就失去意义了。真正要交代清楚的
 *  是它停不掉什么，那句话放在按下之后的横幅上说，比放在确认框里有用。 */
function stopPdfBatchRerun(surface: Surface, taskId: string): void {
  const batch = pdfBatchRerunState[surface];
  if (!batch || batch.taskId !== taskId || batch.stopped) return;
  batch.stopped = true;
  rerender(surface);
}

/** 危险操作确认：跟单页那个 confirmPdfPageRerun 一个道理——会覆盖已有译文，批量做这件
 *  事更不能让人凭一个数字点下去，先把具体是哪几个文件的哪几页摆出来。 */
function confirmPdfBatchRerun(surface: Surface, taskId: string, rows: PdfPageRow[]): void {
  const list = el("ul");
  list.style.cssText = "margin:2px 0 0;padding-left:18px;line-height:1.8";
  for (const { file, page } of rows) {
    const li = el("li");
    li.textContent = file.name + " · " + t("第 {0} 页", page.page_number);
    list.append(li);
  }
  openModal({
    tone: "danger",
    icon: "warn",
    title: t("重新生成这 {0} 页？", rows.length),
    body: [
      t("这些页面的当前译文将分别保留为「上一版」；如对结果不满意，可在对比窗口中逐页换回。重新生成按页计费，其余页面不受影响，也不会再次调用模型。"),
      list,
      t("页面将逐页重新生成；全部完成后，将自动替换译文页并重新合成对应文件的输出 PDF。如已对外发送这些文件，请重新发送。处理过程中可点击横幅上的「停止」：当前页将处理完成，其余页面不再重新生成。"),
    ],
    actions: [
      { label: t("取消") },
      {
        label: t("重新生成这 {0} 页", rows.length),
        variant: "danger-solid",
        onClick: () => void runPdfBatchRerun(surface, taskId, rows),
      },
    ],
  });
}

export function buildPdfReviewCard(surface: Surface, local: LocalTask): HTMLElement | null {
  const snapshot = local.pdfPagesSnapshot;
  if (!snapshot || !snapshot.files.length) return null;
  const taskId = local.task.task_id;
  const multiFile = snapshot.files.length > 1;
  const ui = reviewUiState(taskId);
  const batch = pdfBatchRerunState[surface];
  const batchRerunActive = !!batch && batch.taskId === taskId;
  // 只认「已停止」这一个终态。注意这是范围裁剪、不是成因差异：error 终态下同样
  // 会留下 pending 页（_discard_stopped_pages 在 _process_prepared_pages 的
  // finally 里，对致命模型错误等所有出口一样生效，见
  // core/pdf_image_translation.py），那些页现在仍显示灰色「待处理」。审计批次 2
  // 第①条的范围只覆盖「停止」，error 终态的同类改进是另一条待办。
  const taskStopped = local.task.state === "stopped";

  // .pdf-review-card 是「只看有问题的页」筛选的挂载点（card.filter .row-ok /
  // .grp-clean 定义在 workspace.css），特意挂在这张卡片自己身上而不是 body：卡片会
  // 反复整体重建并重新插入页面，全局类名一勾就会连累界面上其它跟这次筛选无关的东西。
  const card = el("div", "card pdf-review-card");
  card.classList.toggle("filter", ui.onlyBad);
  // 终态和上面的任务清单对半分高度（见 buildColLeft）；按内容撑高的话，展开几个文件
  // 就会把任务清单压成只剩标题。
  card.style.cssText = snapshot.terminal
    ? "flex:1 1 0%;min-height:240px;display:flex;flex-direction:column;overflow:hidden"
    : "flex:3 1 0%;min-height:0;display:flex;flex-direction:column;overflow:hidden";
  const head = el("div", "tc-head");
  const b = el("b");
  // 没开审核时这块只是逐页的生成结果，叫「逐页审核」会让人以为每页都审过了。
  b.textContent = snapshot.review_enabled ? t("逐页审核") : t("逐页结果");
  const span = el("span");
  // 审核关着的时候不能还说「审核模型逐页检查」：同一张表里每一行写的都是「未审核 · 本次
  // 没有让审核模型看这一页」，一屏之内两句话对不上。卡片本身照常用（重试、跳过都在这里）。
  span.textContent = snapshot.review_enabled
    ? t("审核模型逐页检查版式与译文完整性")
    : t("本次未开启逐页审核，以下为各页的生成结果");
  head.append(b, span);

  const tools = el("div", "tc-tools");
  const onlyBadLabel = el("label", "onlybad");
  const onlyBadInput = document.createElement("input");
  onlyBadInput.type = "checkbox";
  onlyBadInput.checked = ui.onlyBad;
  onlyBadInput.addEventListener("change", () => {
    ui.onlyBad = onlyBadInput.checked;
    card.classList.toggle("filter", ui.onlyBad);
  });
  onlyBadLabel.append(onlyBadInput, document.createTextNode(" " + t("只看有问题的页")));
  tools.append(onlyBadLabel);
  head.append(tools);
  card.append(head);

  if (!snapshot.actionable) {
    const note = el("p", "ws-note");
    note.style.cssText = "text-align:left;padding:8px 14px 0";
    note.textContent = pdfActionsDisabledReason(snapshot, pendingRerunFor(surface, taskId));
    card.append(note);
  }

  // 表先建、横幅后建：横幅要报「一共 N 页失败」「第一处失败在哪一行」，这些数只有
  // 扫过整张表才数得出来，所以构建顺序是表在前；最后再按视觉顺序（头→提示→横幅→表）
  // 把节点拼回卡片，构建顺序和视觉顺序不必是同一件事。
  const table = el("table", "tbl");
  const headRow = el("tr");
  const headLabels = [t("页"), snapshot.review_enabled ? t("审核结果") : t("结果"), t("说明"), ""];
  headLabels.forEach((label, i) => {
    const th = el("th");
    th.textContent = label;
    if (i === 3) th.style.width = "190px";
    headRow.append(th);
  });
  table.append(headRow);

  const groupHandles: PdfGroupHandle[] = [];
  const failingRows: PdfPageRow[] = [];
  let firstFailingRow: HTMLTableRowElement | null = null;
  let totalPages = 0;
  let anyProblemFile = false;

  for (const file of snapshot.files) {
    const fileKey = file.relative_path;
    // 预处理就失败的文件一页都没有，file.pages.some(...) 恒为 false——不额外认一下
    // file.status，它就会被当成「干净」：默认收起，勾上「只看有问题的页」时整行消失。
    // 最该被看见的文件反而是唯一看不见的，正好反了。
    const anyProblem = file.status === "failed" || file.pages.length === 0 || file.pages.some((page) => pageIsProblem(page, taskStopped));
    const handle: PdfGroupHandle = { fileKey, hasFailingPage: file.pages.some(pageNeedsRerun), rows: [] };
    if (file.pages.length === 0 && anyProblem) anyProblemFile = true;

    if (multiFile) {
      // 默认展开规则：有失败或建议复核的文件默认展开，全部通过的文件默认收起——打开
      // 这张表多数是为了找问题页，不是逐页确认几十个「通过」。
      //
      // ui.collapse 只存用户自己点过的分组，默认值每次现算，绝不写回去。原先第一次
      // 渲染就把默认值固化进 Map，而「第一次渲染」发生在任务刚开跑、所有页都还是
      // pending 的时刻：那时 anyProblem 一律是 false，全部分组被钉死成收起状态。
      // 等文件真的跑出失败页，stored 已经有值，默认展开永远不会再生效——「有失败的
      // 文件默认展开」这条规则在主流程里等于不存在。
      const open = ui.collapse.get(fileKey) ?? anyProblem;
      const groupRow = el("tr", anyProblem ? "pdf-file-group" : "pdf-file-group grp-clean");
      const cell = el("td");
      cell.colSpan = 4;
      const btn = el("button", "grp");
      btn.type = "button";
      btn.setAttribute("aria-expanded", String(open));
      btn.append(icon("chev", { size: "sm", className: "cv" }));
      const fn = el("span", "fn");
      fn.textContent = file.name;
      const sum = el("span", "sum");
      sum.append(...buildGroupSummaryNodes(file, taskStopped, snapshot.review_enabled));
      btn.append(fn, sum);
      btn.addEventListener("click", () => toggleGroupOpen(ui, handle));
      cell.append(btn);
      groupRow.append(cell);
      table.append(groupRow);
      handle.button = btn;
    }

    const open = multiFile ? ui.collapse.get(fileKey) ?? anyProblem : true;
    for (const page of file.pages) {
      const row = buildReviewRow(surface, taskId, snapshot, file, page, batchRerunActive, taskStopped);
      if (!pageIsProblem(page, taskStopped)) row.classList.add("row-ok");
      if (multiFile && !open) row.classList.add("hid");
      table.append(row);
      handle.rows.push(row);
      totalPages += 1;
      if (pageNeedsRerun(page)) {
        failingRows.push({ file, page });
        if (!firstFailingRow) firstFailingRow = row;
      }
    }
    groupHandles.push(handle);
  }

  // 勾上「只看有问题的页」而一页问题都没有时，表格只剩表头，像是没加载出来。
  // 这一行平时隐藏，只在筛选打开时由 CSS 显示（.pdf-review-card.filter .row-none-bad）。
  // 一页都没有的失败文件（打不开）只有分组标题行、没有页行，它在筛选下照样露出来，
  // 这时再说「没有需要处理的页」就跟它对不上，所以也要排除。
  if (totalPages > 0 && !anyProblemFile && !table.querySelector("tr:not(.row-ok):not(.pdf-file-group) td")) {
    const noneRow = el("tr", "row-none-bad");
    const cell = el("td");
    cell.colSpan = 4;
    cell.textContent = t("没有需要处理的页。");
    noneRow.append(cell);
    table.append(noneRow);
  }

  // 失败页横幅：只在终态出现——非终态的失败页走的是 buildPdfRecoveryCard 那张独立的
  // 「页恢复」卡片（排队式的 regeneratePdfPage，继续翻译才生效），这里的批量重跑走的
  // 是终态专用、点了立刻重跑的 rerunPdfPage，两条路语义不同，不能在还没结束的任务上
  // 出现「立刻重跑」的横幅。
  let banner: HTMLElement | null = null;
  if (snapshot.terminal && batchRerunActive && batch) {
    banner = el("div", "pdf-fixbar busy");
    banner.append(el("span", "spin"));
    const txt = el("span", "txt");
    const em = el("em");
    const small = el("small");
    if (batch.stopped) {
      // 按下停止之后，剩下多少页比「第几页」更要紧——用户按刹车就是想知道还会不会
      // 继续烧钱，答案是「不会，就这一页了」。停在最后一页上时剩余是 0，那句话得换
      // 个说法，否则界面上会出现「剩下的 0 页不再重跑」这种绕口的废话。
      const remaining = Math.max(0, batch.total - batch.done - 1);
      if (remaining > 0) {
        em.textContent = t("{0} 页", remaining);
        appendAroundEm(txt, t("已停止，其余 {0} 不再重新生成"), em);
      } else {
        txt.append(document.createTextNode(t("已停止，当前为最后一页")));
      }
      if (batch.current) txt.append(document.createTextNode(t("；正在等待 {0} 第 {1} 页完成", batch.current.fileName, batch.current.pageNumber)));
      small.textContent = t("已发送的当前页无法中止，将处理完成并照常计费；该页完成后即停止。");
    } else {
      em.textContent = t("第 {0} / {1} 页", batch.done + 1, batch.total);
      appendAroundEm(txt, t("正在重新生成 {0}"), em);
      if (batch.current) txt.append(document.createTextNode(t("：{0} 第 {1} 页", batch.current.fileName, batch.current.pageNumber)));
      small.textContent = t("完成后将自动替换译文页并重新合成该文件的输出 PDF，其余页面不受影响。");
    }
    txt.append(small);
    banner.append(txt);
    if (!batch.stopped) {
      banner.append(createButton({
        label: t("停止"),
        size: "mini",
        onClick: () => stopPdfBatchRerun(surface, taskId),
      }));
    }
  } else if (snapshot.terminal && failingRows.length > 0) {
    // 失败页按「所在文件有没有写出来」分两拨说：写出来的文件里，失败页是一张占位图，
    // 可以单页重跑；没写出来的文件（每一页都失败）压根没有「输出的 PDF」，单页重跑
    // 后端也不接（rerun_page 直接拒绝），只能整份重新翻译。以前不分，全部页失败时横幅
    // 写「输出的 PDF 里这几页是占位图」，上面的大横幅却是「没有生成文件」。
    const rerunRows = failingRows.filter(({ file }) => fileProducedOutput(file));
    const deadRows = failingRows.filter(({ file }) => !fileProducedOutput(file));
    banner = el("div", "pdf-fixbar");
    banner.append(el("span", "dot"));
    const txt = el("span", "txt");
    const small = el("small");
    if (rerunRows.length > 0) {
      const em = el("em");
      em.textContent = t("{0} 页", rerunRows.length);
      if (deadRows.length > 0) {
        appendAroundEm(txt, t("{0}未生成译文，输出 PDF 中这些页面为占位图；另有 {1}及其所在文件均未生成。", "{0}", deadUnitsLabel(deadRows)), em);
      } else {
        appendAroundEm(txt, t("{0}未生成译文，输出 PDF 中这些页面为占位图。"), em);
      }
      // 「其余」得真有其余：页页都要重跑时不说「其余 0 页」。没生成的文件里那几页前面
      // 已经单独说过，不再算进「其余」。
      const rest = totalPages - failingRows.length;
      small.textContent = rest > 0
        ? t("重新生成仅涉及这 {0} 页，其余 {1} 页不会再次调用模型。", rerunRows.length, rest)
        : t("将逐页重新生成这 {0} 页，完成后替换输出文件中的占位图。", rerunRows.length);
    } else {
      const em = el("em");
      em.textContent = deadUnitsLabel(deadRows);
      const deadFiles = new Set(deadRows.map(({ file }) => file.relative_path)).size;
      appendAroundEm(txt, deadFiles > 1
        ? t("{0}未生成译文，这 {1} 个文件均未生成。", "{0}", deadFiles)
        : t("{0}未生成译文，该文件未生成。"), em);
      small.textContent = t("文件未能生成，单页重新生成无法解决；请在上方清单中仅勾选这些文件，重新开始翻译。");
    }
    txt.append(small);
    banner.append(txt);
    banner.append(createButton({
      label: t("定位到这几页"),
      size: "mini",
      onClick: () => focusFailingPages(ui, groupHandles, firstFailingRow),
    }));
    if (rerunRows.length > 0) {
      banner.append(createButton({
        label: t("仅重新生成这 {0} 页", rerunRows.length),
        size: "mini",
        variant: "primary",
        onClick: () => confirmPdfBatchRerun(surface, taskId, rerunRows),
      }));
    }
  }

  const tableWrap = el("div");
  // 真正的修复是这两行的先后顺序：style.cssText 是整体替换，原先写在它前面的
  // maxHeight 会被整块冲掉，420px 的上限等于没写，84 行的表就把外层顶穿了。
  // min-height:0 是顺手补的保险，不是这次生效的原因——flex 子项的 min-height:auto
  // 只在 overflow 计算值为 visible 时才按内容撑开，而这里已经是 overflow:auto。
  tableWrap.style.cssText = "flex:1;min-height:0;overflow:auto";
  tableWrap.dataset.keepScroll = "pdf-review";
  // 运行中和终态都由外层 flex 定高度，表格在卡片里自己滚动。
  tableWrap.append(table);

  if (banner) card.append(banner);
  card.append(tableWrap);
  return card;
}

/** reviewEnabled 只影响「没经过审核模型」那一档的说法，不改颜色分档——pageIsProblem
 *  按颜色判「有没有问题」，不传这个参数也得到同一个答案。 */
/** 没生成出来的文件里的失败页怎么数：PDF 按页、图片按张，跟任务中心的「失败页（已放
 *  占位）」同一口径——那一格只数 PDF 页（图片失败不放占位页），这里要是把图片也叫「页」，
 *  同一个任务两屏就是 5 和 4 两个数。 */
function deadUnitsLabel(rows: PdfPageRow[]): string {
  const images = rows.filter(({ file }) => file.source_type === "image").length;
  const pages = rows.length - images;
  const parts: string[] = [];
  if (pages > 0) parts.push(t("{0} 页", pages));
  if (images > 0) parts.push(t("{0} 张图片", images));
  return parts.join(t("、"));
}

function reviewResultChip(page: PdfPage, taskStopped: boolean, reviewEnabled = true): HTMLElement {
  // 按尺寸跳过的页必须排在最前面判断，而且不能落到「待复核」上：它没有译文也没有页图，
  // 但那是按设定不翻，不是翻译失败。混进复核队列会让人挨个点开看一堆没必要看的页。
  if (page.inplace_status === "done") {
    const review = Number(page.inplace_review ?? 0);
    return review > 0
      ? createChip({ label: t("原位替换 · {0} 处保留原文", review), tone: "warn" })
      : createChip({ label: t("原位替换"), tone: "ok" });
  }
  if (page.word_status === "done") return createChip({ label: t("转 Word"), tone: "ok" });
  if (page.word_requested && page.word_status && !page.inplace_requested) return createChip({ label: t("转 Word 未成功"), tone: "warn" });
  if (page.inplace_requested && page.inplace_status) return createChip({ label: t("原位替换未成功"), tone: "warn" });
  if (page.kept_reason) return createChip({ label: t("保留原样"), tone: "warn" });
  if (page.skipped_oversize) return createChip({ label: t("按幅面跳过"), tone: "mute" });
  if (page.status === "failed" || page.placeholder) {
    return createChip({ label: page.user_skipped ? t("已跳过占位") : t("生成失败"), tone: "dgr" });
  }
  // 译文页还没出来的一律不给审核结论。后端的中间态有三个：pending 还没轮到、
  // rendered 页图已渲染正在等模型、placeholder_pending 等着补占位页。9.2.6 只认识
  // pending，另外两个落到了下面的 review_status 分支上，于是页面还没生成就写「待复核」。
  if (page.status === "pending") {
    // 中止时这一页可能一次模型调用都没发生：071e130 之后后端把它退回「未开始」而不是
    // 烧成失败占位页（core/pdf_image_translation.py 的 _discard_stopped_page），所以
    // 快照里跟「还在排队、马上轮到」长得一模一样，都是 status: "pending"。区别只能靠
    // 任务是否已经落在终态「已停止」上：还在跑时这就是正常的排队中（灰色、不打扰）；
    // 一旦定格在「已停止」，它就再也不会自己轮到，用黄色「已停止」把它跟「失败」
    // （红色）和「还在排队」（灰色）都分开，不能三者共用一个灰色「待处理」。
    return taskStopped
      ? createChip({ label: t("已停止"), tone: "warn" })
      : createChip({ label: t("待处理"), tone: "mute" });
  }
  if (page.status === "rendered") return createChip({ label: t("生成中"), tone: "mute" });
  if (page.status === "placeholder_pending") return createChip({ label: t("待补占位"), tone: "mute" });
  // 审核结论要排在质检疑点前面：同一页可能既被质检挂了疑点又被审核判不通过，那时候
  // 「审核未通过」信息量更大。
  // 走到这里的页一定有译文（没输出的页在上面几个分支就被拦掉了），所以不能用「生成失败」
  // 那种深红：小结把这一页算进「已采用但建议复核」，chip 不该看起来像「这一页没有译文」。
  if (page.review_status === "failed") {
    return createChip({ label: t("审核未通过 · 已采用"), tone: "warn" });
  }
  if (page.review_status === "retrying") return createChip({ label: t("重试中"), tone: "mute" });
  // 本地质检的疑点必须压在 passed 前面。质检跑在送审之前，审核判「通过」不会清掉它，
  // 所以一页可以同时是 quality_flagged 和 review_status="passed"。9.2.6 只看审核结论，
  // 于是小结说「N 页译文有疑点」，逐页表格里却每一页都是绿色的「通过」——用户找不到
  // 是哪一页。这两个数就是小结里 suspect_adopted 的来源，必须在这里露出来。
  if (page.quality_flagged || page.emergency_ratio_normalized) {
    return createChip({ label: t("建议复核"), tone: "warn" });
  }
  if (page.review_status === "passed") return createChip({ label: t("通过"), tone: "ok" });
  // "skipped" 是 PdfPageRecord.review_status 的默认值，意思是这一页没经过审核模型
  // （关了逐页审核，或者还没轮到审核）。它是个真值字符串，不能当成「有审核结论」。
  if (!page.review_status || page.review_status === "skipped") {
    // 这次根本没开审核：这一页正常生成了，就说「已生成」。「未审核」留给开了审核
    // 却没轮到审核的页——那才是值得提一句的事。
    return reviewEnabled
      ? createChip({ label: t("未审核"), tone: "mute" })
      : createChip({ label: t("已生成"), tone: "ok" });
  }
  return createChip({ label: t("待复核"), tone: "warn" });
}

/** 原位替换（Beta）页的说明：结果一句话 + 前几条保留原文的位置，用户能对着图找到。 */
function inplaceNote(page: PdfPage): string {
  const head = page.inplace_message ? tx(page.inplace_message) : "";
  const items = (page.inplace_review_items ?? []).slice(0, 5);
  if (!items.length) return head;
  const list = items.map((item) => t("「{0}」{1}", item.text, tx(item.reason))).join(t("；"));
  const more = (page.inplace_review_items ?? []).length > items.length ? t("等") : "";
  const kept = t("保留原文的地方：{0}{1}", list, more);
  return head ? t("{0}。{1}", head, kept) : kept;
}

function reviewNote(page: PdfPage, taskStopped: boolean, reviewEnabled = true): string {
  if (page.word_status === "done") return tx(page.word_message ?? "");
  if (page.inplace_requested && page.inplace_status) return inplaceNote(page);
  if (page.word_requested && page.word_status) return tx(page.word_message ?? "");
  if (page.kept_reason) return tx(page.kept_reason);
  if (page.skipped_oversize) return t("幅面较大，未送翻译，原始内容已原样保留在输出文件中。");
  if (page.pending_action === "regenerate") return t("待执行重新生成，继续翻译后执行。");
  if (page.pending_action === "skip") return t("待执行跳过，继续翻译后执行。");
  if (page.review_summary) return tx(redactedText(page.review_summary));
  if (page.status === "failed" || page.placeholder) return tx(redactedText(page.error, t("页面生成失败。")));
  if (page.status === "pending") {
    // 跟 reviewResultChip 用同一个 taskStopped 信号：任务已经停止时，这一页不是「还没
    // 轮到」，是「不会再轮到了」；同时说清这不是失败，也不用「单页重新生成」（那是给
    // 已经跑出结果、要推倒重来的页用的），真正能补上它的是对同一输出目录发起续译。
    return taskStopped
      ? t("任务已停止，尚未处理到该页，并非生成失败；对同一输出目录执行「继续上次翻译」即可将其重新加入队列。")
      : t("尚未处理。");
  }
  if (page.status === "rendered") return t("页图已渲染，正在等待模型返回该页译文。");
  if (page.status === "placeholder_pending") return t("该页未能生成，正在补充占位页。");
  // 跟 reviewResultChip 同理：质检疑点要压在「通过」前面，否则这一页会摆着一句
  // 「版式一致，文本完整」，而小结正把它算进「有疑点仍采用」。
  if (page.quality_flagged || page.emergency_ratio_normalized) {
    const detail = redactedText(page.quality_message);
    if (detail) return t("译文已采用，但自动检查有疑点：{0}", tx(detail));
    if (page.emergency_ratio_normalized) return t("译文偏长，已压缩排版以放入原位置，建议检查版面。");
    return t("译文已采用，但自动检查发现疑点，建议检查该页。");
  }
  // 「版式一致，文本完整」是一句审核结论，只有审核模型真的判过并通过了才能写。9.2.6 把它
  // 当成兜底文案，于是页面还没生成、审核还关着的时候，每一页都摆着这句凭空的好消息。
  if (page.review_status === "passed") return t("版式一致，文本完整");
  if (!page.review_status || page.review_status === "skipped") {
    return reviewEnabled ? t("本次未使用审核模型检查该页。") : t("译文页已生成。");
  }
  return "";
}

function buildReviewRow(surface: Surface, taskId: string, snapshot: PdfPagesSnapshot, file: PdfPageFile, page: PdfPage, batchRerunActive: boolean, taskStopped: boolean): HTMLTableRowElement {
  const row = el("tr");
  const pendingRerun = pendingRerunFor(surface, taskId);
  const rerunning = isPageRerunning(snapshot, file, page, pendingRerun);
  const pageCell = el("td");
  pageCell.textContent = t("第 {0} 页", page.page_number);
  row.append(pageCell);

  const resultCell = el("td");
  resultCell.append(rerunning ? createChip({ label: t("重新生成中"), tone: "tint" }) : reviewResultChip(page, taskStopped, snapshot.review_enabled));
  row.append(resultCell);

  const noteCell = el("td");
  noteCell.style.color = "var(--ink-2)";
  noteCell.textContent = rerunning ? t("正在重新生成该页，完成后将更新输出文件") : reviewNote(page, taskStopped, snapshot.review_enabled);
  row.append(noteCell);

  const actionsCell = el("td");
  // 三个链接挤在一格里，窄一点的窗口就会折成两行；不许折行，标签也压短（见下面「跳过」）。
  actionsCell.style.whiteSpace = "nowrap";
  const actionable = snapshot.actionable;
  const notFinished = page.status === "pending";
  const needsSkip = page.status === "failed" || page.placeholder;

  // 按幅面跳过的页既没有原页图也没有译文页图（压根没渲染过），重新生成也只会按同一条尺寸
  // 规则再跳一次。两个入口都关掉并说明原因，比让人点进一个空白对比框强。
  const oversizeSkipped = page.skipped_oversize;
  const oversizeReason = page.word_requested
    ? t("该页是正文，和相邻正文页一起转成 Word 翻译后拼回 PDF，没有单独的页图。")
    : page.inplace_requested
    ? t("该页走原位替换，没有页图；如需重做，请对整份文件重新翻译。")
    : page.kept_reason
      ? t("该页需要整页图像翻译，但还没有设置图像翻译模型，未生成页图。")
      : t("该页因幅面被跳过，未生成页图；如需翻译，请关闭「跳过 A3 及更大的页面」或在「翻译方式」里改选原位替换后重新翻译。");

  actionsCell.append(
    buildActionLink(
      t("查看对比"),
      oversizeSkipped || rerunning,
      oversizeSkipped ? oversizeReason : rerunning ? t("该页正在重新生成，请在完成后查看。") : undefined,
      () => openPdfPageCompareModal(surface, taskId, file, page),
    ),
  );
  actionsCell.append(document.createTextNode(" · "));

  if (snapshot.terminal) {
    // 终态不排队：点下去立刻重跑并覆盖输出文件，所以先过一道危险操作确认。
    // 「跳过该页」在终态没有意义（它的含义是「接受这张失败占位页」），不给入口。
    // 文件整份没生成出来时后端不接单页重跑（rerun_page 直接拒绝），入口要提前关上，
    // 不能等用户点了、确认了、花了一个来回才看到报错。
    const fileDead = !fileProducedOutput(file);
    const rerunnable = !oversizeSkipped && !fileDead && !notFinished && page.status !== "rendered" && page.status !== "placeholder_pending";
    // rerun_actionable 在后端就已经带上了 not rerun.active，但那是快照说的话；本地
    // 这笔刚发出去的还没进快照，所以两个来源都要看。
    const rerunBusy = snapshot.rerun.active || pendingRerun !== undefined;
    const rerunReason = oversizeSkipped
      ? oversizeReason
      : fileDead && !notFinished
        ? file.status === "stopped"
          ? t("该文件在停止前未处理完成，尚未生成；对同一输出目录执行「继续上次翻译」即可补全。")
          : t("该文件本次未能生成，单页重新生成无法解决；请仅勾选该文件，重新开始翻译。")
      : !rerunnable
        ? notFinished && taskStopped
          // 停止时还没跑到的页不是「试过没成」，「单页重新生成」这个动作本身也不适用
          // （它的前提是已经有一版结果可以推倒重来）。真正能补上它的路是续译——同一个
          // 输出目录发起「接着上次继续」会把这一页重新排进队列，不需要整份推倒重来，
          // 「请重新建立任务」这句在这里是错的，会让用户以为要连已经跑完的页一起重翻。
          ? t("停止时尚未处理到该页，并非生成失败，单页重新生成无法解决；请对同一输出目录执行「继续上次翻译」，将其重新加入队列。")
          : t("该页未生成结果，单页重新生成无法解决；请重新创建任务。")
        // 三把锁串在一起，从具体到笼统：批量在跑（这一批还剩好几页）→ 有一页在跑
        // （含本地那笔还没进快照的）→ 逐页记录已释放。顺序不能反：批量重跑期间
        // rerunBusy 同样为真，先撞上笼统那句，用户就看不到「可以按停止」这个出路了。
        //
        // 注意这条链子不看 rerunning：正在被批量处理的那一行也会落到 batchRerunActive
        // 这句提示上（它的 status 仍是 failed/placeholder，rerunnable 为真）。显示没
        // 问题——按钮文案由下面的 `rerunning ? "重新生成中"` 单独决定，跟这里的悬停
        // 说明是两回事——但别照着「那一行不会走到这里」去改，那个前提不成立。
        : batchRerunActive
          ? t("正在批量重新生成失败页，请待完成后再单独操作该页；如需提前停止，请点击上方横幅中的「停止」。")
          : rerunBusy
            ? t("有页面正在重新生成，请待完成后再操作其他页面。")
            : !snapshot.rerun_actionable
              ? t("该任务的逐页记录已清理，无法再重新生成单页。")
              : undefined;
    actionsCell.append(
      buildActionLink(
        rerunning ? t("重新生成中") : t("重新生成"),
        rerunning || rerunBusy || !rerunnable || !snapshot.rerun_actionable || batchRerunActive,
        rerunReason,
        () => confirmPdfPageRerun(surface, taskId, file, page),
      ),
    );
    row.append(actionsCell);
    return row;
  }

  const regenDisabled = !actionable || notFinished || oversizeSkipped;
  const regenReason = oversizeSkipped
    ? oversizeReason
    : actionable && notFinished
      ? t("该页尚未处理完成，暂时无法重新生成。")
      : undefined;
  actionsCell.append(buildActionLink(t("重新生成"), regenDisabled, regenReason, () => void runPdfPageAction(surface, taskId, file, page, "regenerate")));

  if (needsSkip) {
    actionsCell.append(document.createTextNode(" · "));
    actionsCell.append(buildActionLink(t("跳过"), !actionable, t("跳过这一页，输出文件里保留占位图"), () => void runPdfPageAction(surface, taskId, file, page, "skip")));
  }
  row.append(actionsCell);
  return row;
}

/** 把一句带 {0} 的译文拆开，{0} 处放强调节点（各语言 {0} 位置不同，不能写死前后两段）。 */
function appendAroundEm(parent: HTMLElement, template: string, em: HTMLElement): void {
  const [before, after = ""] = template.split("{0}");
  if (before) parent.append(document.createTextNode(before));
  parent.append(em);
  if (after) parent.append(document.createTextNode(after));
}

function buildActionLink(label: string, disabled: boolean, title: string | undefined, onClick: () => void): HTMLElement {
  const span = el("span", disabled ? "linklike disabled" : "linklike");
  span.textContent = label;
  if (title) span.title = title;
  if (!disabled) span.addEventListener("click", onClick);
  return span;
}

// ---------------------------------------------------------------------------
// 页对比模态：原文/译文页图并排。图片走鉴权 fetch + blob（<img src> 加不了自定义头），
// 关闭时统一 revoke，避免泄漏；缺图/加载失败给占位文案，不留空白。
// ---------------------------------------------------------------------------

interface CompareColHandle {
  root: HTMLElement;
  setImage(url: string): void;
  setError(message: string): void;
  setLoading(): void;
  /** 表头右侧的挂载点：译文栏在这里放「当前版／上一版」切换和「换回这一版」。 */
  header: HTMLElement;
}

function buildCompareCol(label: string): CompareColHandle {
  const root = el("div", "pdf-compare-col");
  // 表头一行两端对齐：左边是「译文 · 第 N 页」，右边是版本切换。没有右侧内容时
  // 和原来的单行标题完全一样（flex 空容器不占位），无上一版的页看不出区别。
  const head = el("div");
  head.style.cssText = "display:flex;align-items:center;justify-content:space-between;gap:10px;min-height:24px";
  const labelEl = el("div", "pdf-compare-label");
  labelEl.textContent = label;
  const header = el("div");
  header.style.cssText = "display:flex;align-items:center;gap:8px";
  head.append(labelEl, header);
  const imgWrap = el("div", "pdf-compare-imgwrap");
  const loading = el("div", "ph");
  loading.textContent = t("加载中…");
  imgWrap.append(loading);
  root.append(head, imgWrap);

  const clear = () => {
    while (imgWrap.firstChild) imgWrap.removeChild(imgWrap.firstChild);
  };
  return {
    root,
    header,
    setLoading: () => {
      clear();
      const ph = el("div", "ph");
      ph.textContent = t("加载中…");
      imgWrap.append(ph);
    },
    setImage: (url: string) => {
      clear();
      const img = document.createElement("img");
      img.alt = label;
      img.addEventListener("error", () => {
        clear();
        const err = el("div", "ph");
        err.textContent = t("图片加载失败");
        imgWrap.append(err);
      });
      img.src = url;
      imgWrap.append(img);
    },
    setError: (message: string) => {
      clear();
      const err = el("div", "ph");
      err.textContent = message;
      imgWrap.append(err);
    },
  };
}

/** 译文栏表头的「当前版／上一版」胶囊切换。只有这一页真的留着上一版时才建。 */
function buildVersionToggle(onPick: (kind: "translated" | "previous") => void): {
  root: HTMLElement;
  setKind(kind: "translated" | "previous"): void;
} {
  const root = el("div");
  root.style.cssText =
    "display:inline-flex;gap:2px;padding:2px;border:1px solid var(--line-2);border-radius:99px;background:var(--surface)";
  const make = (label: string, kind: "translated" | "previous") => {
    const pill = el("span");
    pill.textContent = label;
    pill.addEventListener("click", () => onPick(kind));
    return pill;
  };
  const current = make(t("当前版"), "translated");
  const previous = make(t("上一版"), "previous");
  root.append(current, previous);
  const paint = (pill: HTMLElement, on: boolean) => {
    pill.style.cssText = on
      ? "padding:2px 10px;border-radius:99px;font-size:11.5px;font-weight:600;cursor:pointer;background:var(--accent);color:var(--on-accent)"
      : "padding:2px 10px;border-radius:99px;font-size:11.5px;cursor:pointer;color:var(--ink-2)";
  };
  return {
    root,
    setKind: (kind) => {
      paint(current, kind === "translated");
      paint(previous, kind === "previous");
    },
  };
}

function openPdfPageCompareModal(surface: Surface, taskId: string, file: PdfPageFile, page: PdfPage): void {
  const wrap = el("div", "pdf-compare");
  const sourceCol = buildCompareCol(t("原文 · 第 {0} 页", page.page_number));
  const translatedCol = buildCompareCol(t("译文 · 第 {0} 页", page.page_number));
  wrap.append(sourceCol.root, translatedCol.root);

  const objectUrls: string[] = [];
  // 换回成功后要用新快照重开这个弹窗；不先关掉旧的，两层对比弹窗会叠在一起，
  // 关掉上面那层露出来的是换回之前的旧图。
  let handle: ModalHandle | null = null;
  const closeCompare = () => {
    for (const url of objectUrls) URL.revokeObjectURL(url);
    handle?.close();
  };
  const load = async (kind: "source" | "translated" | "previous", has: boolean, col: CompareColHandle) => {
    if (!has) {
      col.setError(
        kind === "source" ? t("该页尚无原文页图") : kind === "previous" ? t("该页未保留上一版译文") : t("该页尚无译文页图"),
      );
      return;
    }
    try {
      const c = await getClient();
      const blob = await c.getPdfPageImage(taskId, file.relative_path, page.page_number, kind);
      const url = URL.createObjectURL(blob);
      objectUrls.push(url);
      col.setImage(url);
    } catch (error) {
      col.setError(redactedText((error as Error)?.message, t("图片加载失败")));
    }
  };
  void load("source", page.has_source_image, sourceCol);
  void load("translated", page.has_translated_image, translatedCol);
  let shown: "translated" | "previous" = "translated";

  // 没有上一版的页：表头右侧空着，弹窗和以前完全一样。
  if (page.has_previous_image) {
    const restoreBtn = createButton({
      label: t("换回这一版"),
      size: "mini",
      onClick: () => confirmPdfPageRestorePrevious(surface, taskId, file, page, closeCompare),
    });
    // 换回按钮只在看着「上一版」时出现：看当前版时它没有指代对象，摆在那里只会
    // 让人以为「换回」的是眼前这一版。
    restoreBtn.style.display = "none";
    const toggle = buildVersionToggle((kind) => {
      if (kind === shown) return;
      shown = kind;
      toggle.setKind(kind);
      restoreBtn.style.display = kind === "previous" ? "" : "none";
      translatedCol.setLoading();
      void load(kind, kind === "previous" ? page.has_previous_image : page.has_translated_image, translatedCol);
    });
    toggle.setKind("translated");
    translatedCol.header.append(toggle.root, restoreBtn);
    // 重生成正在跑（或刚点下去还没确认）时，输出文件正在被另一件事重装，换回
    // 只会撞上后端的忙锁；先禁掉，理由挂在 title 上。
    const local = states[surface].task;
    const snapshot = local?.task.task_id === taskId ? local.pdfPagesSnapshot : undefined;
    const busy = !!snapshot?.rerun.active || !!pendingRerunFor(surface, taskId);
    if (busy) {
      restoreBtn.disabled = true;
      restoreBtn.title = t("有页面正在重新生成，请待完成后再换回上一版。");
    }
  }

  // 来源标签跟卡片标题同一个说法：没开审核时那张卡片叫「逐页结果」。
  const reviewEnabled = states[surface].task?.pdfPagesSnapshot?.review_enabled ?? true;
  handle = openModal({
    tone: "warn",
    icon: "pdf",
    sourceLabel: (reviewEnabled ? t("逐页审核") : t("逐页结果")) + " · " + SURFACE_LABEL[surface],
    title: t("第 {0} 页对比 · {1}", page.page_number, file.name),
    body: [wrap],
    actions: [
      {
        label: t("关闭"),
        onClick: () => {
          for (const url of objectUrls) URL.revokeObjectURL(url);
        },
      },
    ],
  });

  // 任务还在跑时打开的对比：这一页的译图稍后才出来（或被重新生成），弹窗要自己跟上，
  // 而不是一直停在「该页尚无译文页图」，逼用户关掉再开。
  let seen = page;
  const unwatch = watchPdfPagesSnapshot(taskId, (snapshot) => {
    if (!handle?.element.isConnected) {
      unwatch();
      return;
    }
    const next = snapshot.files
      .find((item) => item.relative_path === file.relative_path)
      ?.pages.find((item) => item.page_number === page.page_number);
    if (!next) return;
    if (!seen.has_source_image && next.has_source_image) {
      sourceCol.setLoading();
      void load("source", true, sourceCol);
    }
    const translatedChanged = next.has_translated_image
      && (!seen.has_translated_image || next.attempts !== seen.attempts);
    if (translatedChanged && shown === "translated") {
      translatedCol.setLoading();
      void load("translated", true, translatedCol);
    }
    seen = next;
  });
}

/** 逐页快照更新时要跟着刷新的地方（目前是打开着的对比弹窗）。按任务分组；回调自己判断
 *  是否还需要（弹窗已关就退订）。 */
type PdfSnapshotWatcher = (snapshot: PdfPagesSnapshot) => void;
const pdfSnapshotWatchers = new Map<string, Set<PdfSnapshotWatcher>>();

function watchPdfPagesSnapshot(taskId: string, watcher: PdfSnapshotWatcher): () => void {
  let set = pdfSnapshotWatchers.get(taskId);
  if (!set) {
    set = new Set();
    pdfSnapshotWatchers.set(taskId, set);
  }
  set.add(watcher);
  return () => {
    set.delete(watcher);
    if (!set.size) pdfSnapshotWatchers.delete(taskId);
  };
}

function notifyPdfSnapshotWatchers(taskId: string, snapshot: PdfPagesSnapshot): void {
  for (const watcher of [...(pdfSnapshotWatchers.get(taskId) ?? [])]) watcher(snapshot);
}

/** 换回上一版的确认框。不是危险操作（换回来的那一版仍然留得住、随时能再换回去），
 *  但它会改写可能已经交出去的输出文件，所以照样先说清后果再动手。 */
function confirmPdfPageRestorePrevious(
  surface: Surface,
  taskId: string,
  file: PdfPageFile,
  page: PdfPage,
  closeCompare: () => void,
): void {
  const handle = openModal({
    tone: "warn",
    icon: "warn",
    title: t("换回第 {0} 页的上一版？", page.page_number),
    body: [
      t("该页将换回上一版译文，当前版本将成为新的「上一版」，可随时再次换回。输出文件将重新合成，不调用模型，不产生费用；如已对外发送，请重新发送。"),
    ],
    actions: [
      { label: t("取消") },
      {
        label: t("换回上一版"),
        variant: "primary",
        // 后端是同步装配完才返回的，大文件要等上几秒。原来这里先关弹窗再发请求，
        // 这几秒里界面毫无动静，用户多点几下别的页操作只会撞出一串 409——所以
        // 确认框留在原地当忙态：按钮全部禁用、文字换成「换回中…」，做完再统一收。
        keepOpen: true,
        onClick: async () => {
          const buttons = Array.from(handle.element.querySelectorAll("button"));
          for (const button of buttons) button.disabled = true;
          const confirmButton = buttons[buttons.length - 1];
          if (confirmButton) confirmButton.textContent = t("换回中…");
          await runPdfPageRestorePrevious(surface, taskId, file, page, () => {
            handle.close();
            closeCompare();
          });
        },
      },
    ],
  });
}

async function runPdfPageRestorePrevious(
  surface: Surface,
  taskId: string,
  file: PdfPageFile,
  page: PdfPage,
  closeModals: () => void,
): Promise<void> {
  try {
    const c = await getClient();
    // 后端是同步做完才返回的：这个 await 结束时，页图和输出文件都已经换好。
    await c.restorePdfPagePrevious(taskId, file.relative_path, page.page_number);
    showToast({ message: t("已换回上一版，输出文件已重新合成。") });
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, t("换回上一版失败。")), error: true });
  }
  // 请求已经落定，忙态确认框完成使命；先把旧的两层弹窗收掉，再按新快照重开对比，
  // 免得新旧对比弹窗叠在一起。
  closeModals();
  // 成功要把两版对调后的新快照换上来（哪一版是「上一版」变了），失败也要看后端此刻
  // 的真实状态。拿到新快照后重开对比弹窗，图片跟着重新拉一次。
  const ok = await fetchPdfPagesSnapshot(surface, taskId);
  if (!ok) return;
  const local = states[surface].task;
  if (local?.task.task_id !== taskId) return;
  const nextFile = local.pdfPagesSnapshot?.files.find((item) => item.relative_path === file.relative_path);
  const nextPage = nextFile?.pages.find((item) => item.page_number === page.page_number);
  if (nextFile && nextPage) openPdfPageCompareModal(surface, taskId, nextFile, nextPage);
}

// ---------------------------------------------------------------------------
// 逐页快照拉取：没有逐页 SSE，靠聚合事件/任务状态变化触发的一次性 GET。
// 调用点：handleTaskEvent 的 pdf_page_action / paused / resumed /
// 终态分支（file_progress / pdf_page_recovery / pdf_review 走 schedulePdfSnapshotRefresh 合并刷新），
// adoptExistingTask 接管已有任务时，submitTaskStart 启动新任务后，以及 refetchTask 兜底刷新时。
// ---------------------------------------------------------------------------

/** 返回是否拿到了新快照。轮询类调用方（重跑计时器、批量重跑的等槽位循环）传
 *  `silent: true` 自己管失败计数和退避，不需要也不该在每一轮失败上都弹一条 toast——
 *  那正是高-10 的报错洪水。一次性调用点（任务事件、接管、启动任务）保持原样：
 *  单次失败就该让用户知道。 */
/** 同一任务同一时刻只允许一个逐页快照请求在路上。
 *
 *  以前每个触发点各发各的：一次暂停/继续、审核、恢复事件接连到达时会同时飞出好几个
 *  GET，返回顺序不定，晚到的旧快照会把新快照盖掉，失败时还各弹一条 toast。现在请求
 *  在路上时再来的调用都合并成「等它回来后再拉一次」，保证调用方拿到的是自己发起之后的
 *  状态，又不会叠出一串请求。 */
type PdfSnapshotFlight = { queued: Promise<boolean> | null; queuedSilent: boolean };
const pdfSnapshotFlights = new Map<string, PdfSnapshotFlight & { running: Promise<boolean> }>();

export function fetchPdfPagesSnapshot(surface: Surface, taskId: string, options?: { silent?: boolean }): Promise<boolean> {
  if (surface !== "pdf") return Promise.resolve(false);
  const flight = pdfSnapshotFlights.get(taskId);
  if (flight) {
    // 只要有一个调用方不要求静默，补拉那一次失败就该提示。
    if (!options?.silent) flight.queuedSilent = false;
    flight.queued ??= flight.running.then(() => fetchPdfPagesSnapshot(surface, taskId, { silent: flight.queuedSilent }));
    return flight.queued;
  }
  const entry = { queued: null, queuedSilent: true } as PdfSnapshotFlight & { running: Promise<boolean> };
  entry.running = (async () => {
    try {
      return await loadPdfPagesSnapshot(surface, taskId, options);
    } finally {
      // 先摘掉自己，再让排队的那次开始：它进来时看到的就是「没有请求在路上」。
      pdfSnapshotFlights.delete(taskId);
    }
  })();
  pdfSnapshotFlights.set(taskId, entry);
  return entry.running;
}

/** 运行中每译完一页就有一条 file_progress，逐页表要跟着长出来，但不必每条都拉一次：
 *  合并到最多每 1.5 秒一次。 */
const PDF_SNAPSHOT_REFRESH_MS = 1500;
const pdfSnapshotRefreshTimers = new Map<string, number>();

export function schedulePdfSnapshotRefresh(surface: Surface, taskId: string): void {
  if (surface !== "pdf" || pdfSnapshotRefreshTimers.has(taskId)) return;
  pdfSnapshotRefreshTimers.set(taskId, window.setTimeout(() => {
    pdfSnapshotRefreshTimers.delete(taskId);
    void fetchPdfPagesSnapshot(surface, taskId, { silent: true });
  }, PDF_SNAPSHOT_REFRESH_MS));
}

async function loadPdfPagesSnapshot(surface: Surface, taskId: string, options?: { silent?: boolean }): Promise<boolean> {
  const st = states[surface];
  if (!st.task || st.task.task.task_id !== taskId) return false;
  const local = st.task;
  if (local.pdfPagesUnavailable) return false;
  try {
    const c = await getClient();
    const snapshot = await c.getPdfPages(taskId);
    if (st.task !== local) return false;
    local.pdfPagesSnapshot = snapshot;
    notifyPdfSnapshotWatchers(taskId, snapshot);
    if (st.historyOnly) {
      for (const file of st.files) {
        const pageFile = snapshot.files.find((entry) => entry.relative_path === file.relative_path);
        if (pageFile) file.page_count = pageFile.page_count;
      }
    }
    // 单页重生成跑在终态任务上，SSE 那时已经收摊了；接管一个正在重生成的任务
    // （切回页面、重开程序）也要能自己接上进度，所以起点放在这里而不是发起处。
    if (snapshot.rerun?.active) startPdfRerunTicker(surface, taskId);
    rerender(surface);
    return true;
  } catch (error) {
    if (st.task !== local) return false;
    // Persisted summaries survive sidecar restarts; the in-memory page runner does not.
    if (local.task.terminal && error instanceof ApiError && error.status === 404) {
      local.pdfPagesUnavailable = true;
      rerender(surface);
      return false;
    }
    if (!options?.silent) {
      showToast({ message: redactedText((error as Error)?.message, t("刷新逐页状态失败。")), error: true });
    }
    return false;
  }
}
