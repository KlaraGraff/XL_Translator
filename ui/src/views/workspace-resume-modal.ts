/**
 * 工作台：「上次翻到一半」的续译弹窗。
 * 扫描到上次的输出后由 workspace.ts 调用 showResumeModal，用户选「接着翻」或「重新翻」。
 */
import { createChip, createDropdown, createProgressBar, openModal, type ModalAction, type ModalHandle } from "../components";
import { isChineseLocale, t, tx } from "../i18n";
import { icon } from "../icons";
import { FIT_CLIP } from "../text-fit";
import { el } from "./workspace-util";
import { type PreviousOutput, type Surface, type SurfaceState, basename, chooseFresh, chooseResume, resumeHitCount, resumeState, runScan, surfaceLabel } from "./workspace";

// --- 状态 A / B / E：扫描完成后的弹窗 ---------------------------------------

function resumeStatChips(info: PreviousOutput): HTMLElement {
  const row = el("div", "rf-stats");
  const s = info.summary;
  if (s.full > 0) row.append(createChip({ label: t("{0} 个已有完整译文", s.full), tone: "ok" }));
  if (s.found > 0) row.append(createChip({ label: t("{0} 个已有译文", s.found), tone: "ok" }));
  if (s.partial > 0) row.append(createChip({ label: t("{0} 个已部分翻译", s.partial), tone: "warn" }));
  if (s.diverged > 0) row.append(createChip({ label: t("{0} 个源文件已变化", s.diverged), tone: "warn" }));
  if (s.none > 0) row.append(createChip({ label: t("{0} 个尚无译文", s.none), tone: "mute" }));
  return row;
}

/** PDF 专用：page_total 缺失（null 或 0）的文件数。summary.page_done/page_total 只统计
 *  有清单记录的 PDF，这批文件不在里面——续译对它们形同摆设，只能整份重新生成。少了这个
 *  数，「剩余页数」会被当成整批的工作量，漏掉这几个文件的真实成本。
 *  diverged（源文件已变化）单独有自己的文案，不混进「没有页记录」里数两遍。 */
function pdfFilesMissingPageData(info: PreviousOutput): number {
  return info.files.filter((file) => file.status !== "diverged" && !file.page_total).length;
}

/** 弹窗里那个灰底方框：底稿目录 + 统计（或页进度）+ 历史目录下拉。 */
function buildResumeFoundBox(surface: Surface, info: PreviousOutput, handle: () => ModalHandle | null): HTMLElement {
  const box = el("div", "resume-found");
  const pathRow = el("div", "rf-path");
  pathRow.append(icon(surface === "pdf" ? "pdf" : "folder", { size: "sm" }));
  const code = el("code", FIT_CLIP);
  code.textContent = info.selected_dir ? basename(info.selected_dir) : t("（未找到可用的历史目录）");
  pathRow.append(code);
  box.append(pathRow);

  const total = info.summary.page_total;
  const done = info.summary.page_done;
  if (surface === "pdf" && total !== null && total > 0) {
    const missing = pdfFilesMissingPageData(info);
    const line = el("div", "rf-page");
    line.append(createProgressBar({ percent: ((done ?? 0) / total) * 100 }).root);
    const span = el("span");
    // 页进度只覆盖有清单记录的文件；还有 missing 个文件完全没有页记录，不能让这条进度条
    // 看起来像是「整批」的进度——那会把这几个文件的工作量凭空抹掉。
    span.textContent = missing > 0
      ? t("已完成 {0} / {1} 页（另有 {2} 个文件没有页记录，需整份生成）", done ?? 0, total, missing)
      : t("已完成 {0} / {1} 页", done ?? 0, total);
    line.append(span);
    box.append(line);
  } else {
    box.append(resumeStatChips(info));
  }

  if (info.summary.last_task_stopped) {
    const stoppedRow = el("div", "rf-stats");
    const when = info.summary.last_time_label;
    stoppedRow.append(createChip({ label: when ? t("上次任务 {0} 被手动停止", tx(when)) : t("上次任务被手动停止"), tone: "warn" }));
    box.append(stoppedRow);
  }

  // 一份历史输出没什么可选的，下拉只在有第二份时出现。
  if (info.candidates.length >= 2) {
    const selRow = el("div", "rf-sel");
    const isLatest = info.selected_dir === info.candidates[0].dir;
    const hint = el("span");
    hint.textContent = isLatest
      ? t("找到 {0} 次历史输出，已选最近一次", info.candidates.length)
      : t("找到 {0} 次历史输出", info.candidates.length);
    const select = createDropdown({
      className: "rf-history-select",
      options: info.candidates.map((candidate, index) => ({
        value: candidate.dir,
        label: index === 0 ? t("{0}（最近）", tx(candidate.label)) : tx(candidate.label),
      })),
      value: info.selected_dir ?? undefined,
      onChange: (next) => {
      if (!next || next === info.selected_dir) return;
      // 换底稿要重新逐个文件比对，只有后端算得出来：带着 preferred_resume_dir 重扫一次，
      // 新结果回来会重新弹这个弹窗（走 runScan 原有的请求序号守卫，旧结果不会落地）。
      handle()?.close();
      void runScan(surface, next);
      },
    });
    selRow.append(hint, select.root);
    box.append(selRow);
  }
  return box;
}

/** 「**加粗词**：正文」这种一句话说明；openModal 会把它包进 <p>。 */
function resumeLead(lead: string, body: string): HTMLElement {
  const span = el("span");
  const bold = el("b");
  bold.textContent = lead;
  span.append(bold, document.createTextNode(t("：{0}", body)));
  return span;
}

/** 几句说明接在一起：中文直接相连，英法文句子之间补一个空格。 */
function joinNotes(...notes: string[]): string {
  const parts = notes.filter(Boolean);
  if (!parts.length) return "";
  const glue = isChineseLocale() ? "" : " ";
  return glue + parts.join(glue);
}

function resumeFootnote(body: string): HTMLElement {
  const span = el("span");
  span.style.cssText = "color:var(--ink-3);font-size:12.5px";
  span.textContent = body;
  return span;
}

/** onDecided：从「开始翻译」进来时，选了「接着上次继续」或「全部重新翻译」就接着启动。
 *  「稍后再说」只关弹窗，不启动。 */
/** 返回弹窗是否真的打开了。目标语言和上次不一致（resumeState 为空）或没有可续的
 *  产物时不弹，调用方要自己往下走，不能把这次点击吞掉。 */
export function showResumeModal(surface: Surface, st: SurfaceState, onDecided?: () => void): boolean {
  const info = resumeState(st);
  if (!info || resumeHitCount(info) === 0) return false;
  const fileCount = st.files.length || info.files.length;
  const single = fileCount === 1;
  const s = info.summary;
  // 「上次已全部翻完」只认 full：partial 显然还有活，found 是判定不出完整度的，
  // diverged 是源文件已经变了的——都不能拿来当「翻完了」，那会把唯一能补译的
  // 入口从界面上拿掉，或者拿旧版本的产物冒充新版本的成品。
  const allDone = s.partial === 0 && s.none === 0 && s.found === 0 && s.diverged === 0;
  const pdfPages = surface === "pdf" && s.page_total !== null && s.page_total > 0;
  const remaining = pdfPages ? (s.page_total as number) - (s.page_done ?? 0) : 0;
  // pdfPages 为假说明整批 PDF 一份可用的页存档都没有——续译完全靠这份存档才能只补差额，
  // 存档不在，「接着上次继续」在后端会整份重新生成，跟「全部重新翻译」没有区别，
  // 却顶着「不再重复花钱」的许诺，是在骗用户点它。
  const pdfNoPageData = surface === "pdf" && !pdfPages && !allDone;
  // pdfPages 为真时页汇总也可能没盖住整批：还有几个文件完全没有页记录（新加进来的、
  // 或找到产物但读不出页存档），这些文件的工作量不在 s.page_total 里，两个选项的文案
  // 都得把它们单独点出来，不然「10 页」听起来就是这一批的全部代价。
  const pdfMissingPages = pdfPages ? pdfFilesMissingPageData(info) : 0;
  const pdfMissingNote = pdfMissingPages > 0
    ? t("另有 {0} 个文件没有页面记录，无论选择哪种方式都将整份重新生成，不计入此处页数。", pdfMissingPages)
    : "";
  const pdfDivergedNote = surface === "pdf" && s.diverged > 0
    ? t("另有 {0} 个 PDF 的源文件在上次翻译后已修改，上次的页面无法复用，将整份重新生成。", s.diverged)
    : "";

  let handle: ModalHandle | null = null;
  const foundBox = buildResumeFoundBox(surface, info, () => handle);
  const body: (string | HTMLElement)[] = [foundBox];

  if (allDone) {
    body.push(t("此批文件上次已全部翻译完成，无需补译；结果位于上方文件夹中。"));
    body.push(resumeLead(t("全部重新翻译"), t("忽略上次的结果，将 {0} 个文件从头重新翻译。", fileCount)));
    body.push(resumeFootnote(t("结果将保存到新的输出文件夹，上次的输出不会被覆盖或删除。")));
  } else if (pdfPages) {
    body.push(resumeLead(
      t("继续上次翻译"),
      t("跳过已完成的 {0} 页，仅生成剩余的 {1} 页，最后合成为完整的 PDF。已生成的页面直接复用，不会重复产生费用。", s.page_done ?? 0, remaining) + joinNotes(pdfMissingNote, pdfDivergedNote),
    ));
    const warnBox = el("div", "resume-warn");
    warnBox.append(icon("warn", { size: "sm" }));
    const warnText = el("span");
    warnText.textContent = t("中途停止的任务，最后几页可能未完整生成。继续前将逐页核对，占位页或损坏页面将自动重新生成。");
    warnBox.append(warnText);
    body.push(warnBox);
    body.push(resumeLead(t("全部重新翻译"), t("从头重新生成全部 {0} 页。", s.page_total) + joinNotes(pdfMissingNote, pdfDivergedNote)));
    body.push(resumeFootnote(t("无论选择哪种方式，结果都将保存到新的输出文件夹，上次的输出不会被覆盖或删除。")));
  } else if (pdfNoPageData) {
    // 整批都是「源文件已改动」时按真实原因说话，别拿「读不到页存档」搪塞。
    const allChanged = s.diverged > 0 && s.partial + s.found + s.full === 0;
    body.push(allChanged
      ? resumeLead(t("找到上次的翻译结果"), t("但源文件在上次翻译后已修改，上次的页面无法复用，继续翻译也将整份重新生成。"))
      : resumeLead(t("找到上次的翻译结果"), t("但无法读取上次的页面存档，继续翻译也将整份重新生成，无法仅补译缺失部分。")));
    body.push(resumeLead(t("全部重新翻译"), t("忽略上次的结果，将 {0} 个文件从头重新翻译。", fileCount)));
    body.push(resumeFootnote(t("结果将保存到新的输出文件夹，上次的输出不会被覆盖或删除。")));
  } else {
    // diverged 文件的「继续」承诺不了省钱：底稿被拒后整份重翻，只有翻译记忆兜底。
    // 有一个说一个，不能让「一个字不动」的许诺盖到它们头上。
    const resumables = s.partial + s.found + s.full;
    const divergedNote = s.diverged > 0
      ? t("其中 {0} 个文件的源文件在上次翻译后已修改，上次的输出无法作为底稿，将整份重新翻译（已翻译的内容尽量由记忆库复用）。", s.diverged)
      : "";
    body.push(s.diverged > 0 && resumables === 0
      ? resumeLead(t("继续上次翻译"), t("源文件在上次翻译后已修改，上次的输出无法作为底稿，本次将整份重新翻译；已翻译的内容尽量由记忆库复用。"))
      : resumeLead(t("继续上次翻译"), t("以上次的译文为底稿，仅翻译缺少译文的部分；已翻译的内容保持不变，不会重复产生费用。") + joinNotes(divergedNote)));
    body.push(resumeLead(t("全部重新翻译"), t("忽略上次的结果，将 {0} 个文件从头重新翻译。", fileCount)));
    body.push(resumeFootnote(t("无论选择哪种方式，结果都将保存到新的输出文件夹，上次的输出不会被覆盖或删除。")));
  }

  const title = allDone
    ? (single ? t("此文件上次已翻译完成") : t("这批文件上次已全部翻译完成"))
    : pdfNoPageData
      ? (single ? t("此 PDF 无可用的续译记录") : t("这些 PDF 无可用的续译记录"))
      : pdfPages
        ? (single ? t("此 PDF 上次未翻译完成") : t("这些 PDF 上次未翻译完成"))
        : (single ? t("此文件已翻译过") : t("此文件夹已部分翻译"));

  const actions: ModalAction[] = [
    { label: t("稍后再说"), onClick: () => chooseFresh(surface, st) },
    {
      label: t("全部重新翻译"),
      variant: allDone || pdfNoPageData ? "primary" : undefined,
      onClick: () => {
        chooseFresh(surface, st);
        onDecided?.();
      },
    },
  ];
  // pdfNoPageData 时不给「接着上次继续」——那个按钮在后端会整份重新生成，跟「全部重新
  // 翻译」结果一样，留着它只会让用户以为还有一条能省钱的路。
  if (!allDone && !pdfNoPageData) {
    actions.push({
      label: t("继续上次翻译"),
      variant: "primary",
      onClick: () => {
        chooseResume(surface, st);
        onDecided?.();
      },
    });
  }

  handle = openModal({
    tone: (pdfPages || pdfNoPageData) && !allDone ? "warn" : "tint",
    icon: surface === "pdf" ? "pdf" : "folder",
    sourceLabel: onDecided ? t("{0} · 开始前核对", surfaceLabel(surface)) : t("{0} · 扫描完成", surfaceLabel(surface)),
    title,
    body,
    actions,
  });
  return true;
}
