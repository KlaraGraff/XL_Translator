// CAD 图纸翻译视图 —— 薄封装。布局、扫描、开始/暂停/停止、进度和完成横幅全部复用
// workspace.ts（与 Excel / Word / PDF 同一套模板），这里只声明 surface。
// CAD 自己的差异（转换工具提示、文件表列、开关、提交参数）都在 workspace.ts 里按
// surface === "cad" 分支实现，不要在这里另写页面。

import type { ViewParams } from "../router";
import { mountWorkspace, unmountWorkspace } from "./workspace";

export function mount(container: HTMLElement, params: ViewParams): void {
  mountWorkspace(container, params, "cad");
}

export function unmount(): void {
  unmountWorkspace("cad");
}
