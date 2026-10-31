// 板块类型描述符（GET /api/surfaces）的内存缓存：启动时拉一次，之后同步读取。
//
// 四个内置页面仍按 workspace.ts / shell.ts / tasks.ts 里编译期的表来画；描述符给的是
// 运行时才注册的页面（以后的格式扩展，键为 `ext:<id>`）用的名称、图标、文件名词、开关。
// 拉取失败不影响启动，只是那些页面暂时不显示。

import { ApiClient, type LocalizedText, type SurfaceDescriptor } from "./api-client";
import { pickLocalized } from "./i18n";
import { ICON_NAMES, type IconName } from "./icons";
import type { ViewId } from "./router";
import { builtinTranslateSideItem, setTranslateSideItems } from "./shell";

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

/** 安装或卸载扩展之后重新拉一次（原来的缓存作废）。 */
export function reloadSurfaceDescriptors(): Promise<SurfaceDescriptor[]> {
  pending = null;
  return loadSurfaceDescriptors();
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
  return pickLocalized(value) || fallback;
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

/** 内置四种翻译类型的键，按默认顺序。 */
export const BUILTIN_TYPE_KEYS = ["excel", "word", "pdf", "cad"] as const;

/** settings.translation_types（v40）：全部翻译类型的顺序与「显示在侧栏」，「扩展」页里改。 */
export interface TranslationTypesLayout {
  order: string[];
  /** 拨过开关的类型；没拨过的见 isTypeVisible。 */
  visible: Record<string, boolean>;
}

export function translationTypesLayout(settings: Record<string, unknown> | null | undefined): TranslationTypesLayout {
  const raw = settings?.translation_types;
  const table = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const order = Array.isArray(table.order) ? table.order.filter((item): item is string => typeof item === "string") : [];
  const visible: Record<string, boolean> = {};
  if (table.visible && typeof table.visible === "object") {
    for (const [key, flag] of Object.entries(table.visible as Record<string, unknown>)) {
      if (typeof flag === "boolean") visible[key] = flag;
    }
  }
  return { order, visible };
}

/**
 * 是否显示在侧栏：拨过的按拨的；没拨过的内置四种显示，扩展类型按描述符的 visible_by_default
 * （随应用提供、清单要求的才显示，如 PPT），与后端 shows 一致。
 */
export function isTypeVisible(settings: Record<string, unknown> | null | undefined, key: string): boolean {
  const flag = translationTypesLayout(settings).visible[key];
  if (typeof flag === "boolean") return flag;
  return isExtSurface(key) ? surfaceDescriptor(key)?.visible_by_default === true : true;
}

/**
 * 全部翻译类型（内置四种 + 已拉到描述符的运行时注册类型），按「扩展」页排的顺序；
 * 没排到的（新装的类型）按描述符顺序接在后面，描述符写了 sidebar_after 的（随应用提供的 PPT
 * 紧跟 Word）插在那个类型后面。用户排过的顺序只增不改：已排的类型之间的先后不动。
 * 「扩展」页、侧栏和翻译参数的页签都按这个顺序。
 */
export function orderedTypeKeys(settings: Record<string, unknown> | null | undefined): string[] {
  const known: string[] = [
    ...BUILTIN_TYPE_KEYS,
    ...descriptors.filter((item) => !item.builtin && isExtSurface(item.key)).map((item) => item.key),
  ];
  const ranked = translationTypesLayout(settings).order.filter((key) => known.includes(key));
  const anchorOf = (key: string): string => (ranked.includes(key) ? "" : surfaceDescriptor(key)?.sidebar_after ?? "");
  const keys = [...ranked, ...known.filter((key) => !ranked.includes(key) && !anchorOf(key))];
  // 有锚点的插在锚点后面（同一个锚点的按描述符顺序排）；锚点不在列表里的接在最后。
  const anchored = known.filter((key) => anchorOf(key));
  let pending = anchored;
  while (pending.length) {
    const rest = pending.filter((key) => {
      const anchor = keys.indexOf(anchorOf(key));
      if (anchor < 0) return true;
      let at = anchor + 1;
      while (at < keys.length && anchorOf(keys[at]) === anchorOf(key)) at += 1;
      keys.splice(at, 0, key);
      return false;
    });
    if (rest.length === pending.length) {
      keys.push(...rest);
      break;
    }
    pending = rest;
  }
  return keys;
}

/** 显示在侧栏的翻译类型，顺序同上。 */
export function visibleTypeKeys(settings: Record<string, unknown> | null | undefined): string[] {
  return orderedTypeKeys(settings).filter((key) => isTypeVisible(settings, key));
}

/** 按设置重画侧栏「翻译」组；启动时和「扩展」页改了显示、顺序之后调用。 */
export function refreshTranslateSideItems(settings: Record<string, unknown> | null | undefined): void {
  setTranslateSideItems(visibleTypeKeys(settings).flatMap((key) => {
    if (isExtSurface(key)) return [{ id: key, label: () => extSurfaceLabel(key), icon: extSurfaceIcon(key) }];
    const item = builtinTranslateSideItem(key as ViewId);
    return item ? [item] : [];
  }));
}
