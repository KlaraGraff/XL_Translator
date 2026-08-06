// Word 工作区视图 —— 薄封装。实际布局/状态机/任务生命周期全部在 workspace.ts
// 里实现（excel/word/pdf 三个页面同构，仅通过 surface 参数区分差异）。

import type { ViewParams } from "../router";
import { mountWorkspace, unmountWorkspace } from "./workspace";

export function mount(container: HTMLElement, params: ViewParams): void {
  mountWorkspace(container, params, "word");
}

export function unmount(): void {
  unmountWorkspace("word");
}
