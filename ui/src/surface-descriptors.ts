// 板块类型描述符（GET /api/surfaces）的内存缓存：启动时拉一次，之后同步读取。
//
// 四个内置页面仍按 workspace.ts / shell.ts / tasks.ts 里编译期的表来画；描述符给的是
// 运行时才注册的页面（以后的格式扩展，键为 `ext:<id>`）用的名称、图标、文件名词、开关。
// 拉取失败不影响启动，只是那些页面暂时不显示。

import { ApiClient, type LocalizedText, type SurfaceDescriptor } from "./api-client";
import { currentLocale } from "./i18n";
import { ICON_NAMES, type IconName } from "./icons";
import { setExtensionSideItems } from "./shell";

/** 运行时注册的翻译类型的键。内置页面的键里没有冒号，两者不会撞。 */
export type ExtSurface = `ext:${string}`;

export function isExtSurface(key: string): key is ExtSurface {
  return key.startsWith("ext:");
}

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

/** 描述符里同一段文字的三种语言，按当前界面语言取；缺这一种时依次退到中文、英文、法文。 */
export function localizedText(value: LocalizedText | undefined, fallback = ""): string {
  if (!value) return fallback;
  return value[currentLocale()] || value.zh || value.en || value.fr || fallback;
}

/** 运行时注册的类型在界面上的名字；描述符还没到时用键里的 id 顶上。 */
export function extSurfaceLabel(key: string): string {
  return localizedText(surfaceDescriptor(key)?.label, key.slice("ext:".length));
}

/** 描述符给的图标名不在图标集里时用记忆库图标（与任务中心对未知类型的写法一致）。 */
export function extSurfaceIcon(key: string): IconName {
  const name = surfaceDescriptor(key)?.icon ?? "";
  return (ICON_NAMES as readonly string[]).includes(name) ? (name as IconName) : "book";
}

/** 设置里某个运行时注册类型的那一份（`extensions` 的键是不带 `ext:` 的 id）；没存过时为空对象。 */
export function extSettingsEntry(extensions: Record<string, unknown>, key: string): { pinned?: unknown; order?: unknown } {
  const raw = extensions[key.slice("ext:".length)];
  return raw && typeof raw === "object" ? (raw as { pinned?: unknown; order?: unknown }) : {};
}

/** 全部运行时注册的类型，按设置里的 order 排，同序按描述符顺序。「扩展」页和侧栏都按这个顺序。 */
export function orderedExtDescriptors(extensions: Record<string, unknown>): SurfaceDescriptor[] {
  const order = (item: SurfaceDescriptor) => {
    const value = extSettingsEntry(extensions, item.key).order;
    return typeof value === "number" && Number.isFinite(value) ? value : 0;
  };
  return descriptors
    .filter((item) => !item.builtin && isExtSurface(item.key))
    .map((item, index) => ({ item, index }))
    .sort((a, b) => order(a.item) - order(b.item) || a.index - b.index)
    .map(({ item }) => item);
}

/** 显示在侧栏「翻译」组里的运行时注册类型：设置里没取消「显示在侧栏」的，顺序同上。 */
export function pinnedExtDescriptors(extensions: Record<string, unknown>): SurfaceDescriptor[] {
  return orderedExtDescriptors(extensions).filter((item) => extSettingsEntry(extensions, item.key).pinned !== false);
}

/** 按设置重排侧栏「翻译」组里的运行时注册类型；启动时和「扩展」页改了开关、顺序之后调用。 */
export function refreshExtensionSideItems(extensions: Record<string, unknown>): void {
  setExtensionSideItems(pinnedExtDescriptors(extensions).flatMap((item) => (
    isExtSurface(item.key)
      ? [{ id: item.key, label: () => extSurfaceLabel(item.key), icon: extSurfaceIcon(item.key) }]
      : []
  )));
}
