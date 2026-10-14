// 板块类型描述符（GET /api/surfaces）的内存缓存：启动时拉一次，之后同步读取。
//
// 四个内置页面仍按 workspace.ts / shell.ts / tasks.ts 里编译期的表来画；描述符给的是
// 运行时才注册的页面（以后的格式扩展）用的名称、文件名词、开关。拉取失败不影响启动，
// 只是那些页面暂时不显示。

import { ApiClient, type SurfaceDescriptor } from "./api-client";

let descriptors: SurfaceDescriptor[] = [];
let pending: Promise<SurfaceDescriptor[]> | null = null;

/** 向后端要一次描述符并缓存；并发调用共用同一个请求，失败时下次重新请求。 */
export function loadSurfaceDescriptors(): Promise<SurfaceDescriptor[]> {
  if (!pending) {
    pending = (async () => {
      const client = new ApiClient();
      await client.connect();
      descriptors = await client.fetchSurfaces();
      return descriptors;
    })().catch((error: unknown) => {
      pending = null;
      throw error;
    });
  }
  return pending;
}

/** 已拉到的全部描述符（还没拉到或拉取失败时为空）。 */
export function surfaceDescriptors(): readonly SurfaceDescriptor[] {
  return descriptors;
}

/** 某个板块的描述符；没有时为 undefined。 */
export function surfaceDescriptor(key: string): SurfaceDescriptor | undefined {
  return descriptors.find((item) => item.key === key);
}
