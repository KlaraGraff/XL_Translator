import { t } from "./i18n";

// 任务状态词的唯一出处。
//
// 以前工作区和任务中心各存一份表，同一个 state 在两屏上写着两个词：一个「已停止」跑完的
// 任务，横幅上说「已停止」，切到任务中心，列表徽标、详情标题、逐文件状态列全说「已中止」。
// 用户看到的是同一个任务的两种说法，会以为是两回事。四个终态全都对不上，不止 stopped 一个。
//
// 选词的依据：
//   stopped               —— 按钮从头到尾叫「停止」，状态词就得是「已停止」，不能换个动词。
//   error                 —— 「任务失败」直接说结果；「发生错误」只说过程里出过事，不说结局。
//   interrupted           —— 「应用中断」点明是程序被关掉/崩了，不是任务自己出的问题。
//   completed_with_issues —— 工作区会往后接后缀（「· 需复核」），「完成但有问题」接得上，
//                            「已完成，有问题 · 需复核」两个逗号级停顿叠在一起读不通。

export const TASK_STATE_LABELS: Record<string, string> = {
  preflight: t("等待确认"),
  running: t("翻译中"),
  pausing: t("正在暂停"),
  paused: t("已暂停"),
  stopping: t("正在停止"),
  finalizing: t("正在收尾"),
  done: t("已完成"),
  completed_with_issues: t("完成但有问题"),
  error: t("任务失败"),
  stopped: t("已停止"),
  interrupted: t("应用中断"),
};

/** 终态在横幅/徽标上的那个词。认不出来的 state 一律按「已完成」——终态分支只在任务
 *  真的跑完之后才走到，与其写一个「未知状态」吓人，不如沿用最常见的那一个。 */
export function taskStateWord(state: string): string {
  return TASK_STATE_LABELS[state] ?? t("已完成");
}

/** 「停止」确认框正文。工作区和任务中心弹的是同一个确认，所以只在这里写一份；
 *  以前任务中心那份写着「Excel、Word 会结束为终态」，还漏了 CAD。 */
const STOP_CONFIRM_BODY: Record<string, string> = {
  // Excel / Word 是整批先翻再逐个写文件：停止时把已经翻好的内容写进文件，没翻到的
  // 保留原文，之后可以接着翻译。以前写「当前文件写完后停止」，停在翻译中途时其实一个
  // 文件都没写，跟这句话对不上。
  excel: t("停止翻译后，已翻译的内容将写入文件，未翻译的单元格保留原文，之后可继续翻译。源文件不会被修改。"),
  word: t("停止翻译后，已翻译的内容将写入文件，未翻译的段落保留原文，之后可继续翻译。源文件不会被修改。"),
  pdf: t("已生成的页面将保留在输出目录中。如需稍后继续，请改用「暂停」，之后可选择继续或结束任务。"),
  cad: t("当前图纸完成后停止。已生成的图纸和报告将保留在输出目录中，源文件不会被修改；之后可从断点继续。"),
};

export function stopConfirmBody(surface: string): string {
  return STOP_CONFIRM_BODY[surface] ?? t("当前批次处理完成后停止，已完成的部分将保留。");
}
