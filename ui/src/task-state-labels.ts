// 任务状态词的唯一出处。
//
// 以前工作区和任务中心各存一份表，同一个 state 在两屏上写着两个词：一个「已停止」跑完的
// 任务，横幅上说「已停止」，切到任务中心，列表徽标、详情标题、逐文件状态列全说「已中止」。
// 用户看到的是同一个任务的两种说法，会以为是两回事。四个终态全都对不上，不止 stopped 一个。
//
// 选词的依据：
//   stopped               —— 按钮从头到尾叫「安全停止」，状态词就得是「已停止」，不能换个动词。
//   error                 —— 「任务失败」直接说结果；「发生错误」只说过程里出过事，不说结局。
//   interrupted           —— 「应用中断」点明是程序被关掉/崩了，不是任务自己出的问题。
//   completed_with_issues —— 工作区会往后接后缀（「· 需复核」），「完成但有问题」接得上，
//                            「已完成，有问题 · 需复核」两个逗号级停顿叠在一起读不通。

export const TASK_STATE_LABELS: Record<string, string> = {
  preflight: "等待确认",
  running: "执行中",
  pausing: "暂停提交中",
  paused: "已暂停提交",
  stopping: "安全停止中",
  finalizing: "正在收尾",
  done: "已完成",
  completed_with_issues: "完成但有问题",
  error: "任务失败",
  stopped: "已停止",
  interrupted: "应用中断",
};

/** 终态在横幅/徽标上的那个词。认不出来的 state 一律按「已完成」——终态分支只在任务
 *  真的跑完之后才走到，与其写一个「未知状态」吓人，不如沿用最常见的那一个。 */
export function taskStateWord(state: string): string {
  return TASK_STATE_LABELS[state] ?? "已完成";
}
