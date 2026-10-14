// 运行时注册的翻译类型（键为 `ext:<id>`）的视图 —— 和 excel.ts / cad.ts 一样是 workspace.ts
// 的薄封装，只是 surface 要到描述符（GET /api/surfaces）拉到之后才知道，所以按键生成，
// 由 app.ts 的 mountExtensionPages 逐个注册。页面上的名称、文件名词、开关按描述符画
// （见 workspace.ts 的 surfaceLabel / surfaceToggles 等访问函数）。

import type { View } from "../router";
import type { ExtSurface } from "../surface-descriptors";
import { mountWorkspace, unmountWorkspace } from "./workspace";

export function extensionView(surface: ExtSurface): View {
  return {
    mount: (container, params) => mountWorkspace(container, params, surface),
    unmount: () => unmountWorkspace(surface),
  };
}
