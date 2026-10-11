// 界面语言（中文 / English / Français）。
//
// 词条以中文原文为键：代码里照旧写中文，外面套一层 t("…")，英法译文放在
// ./catalog/*.json（同一条中文的 en / fr 放在一起，方便对照）。没有译文的词条
// 原样显示中文，不会空白。
//
// 两个入口：
//   t(中文, ...参数)  界面自己写的文字。中文里用 {0} {1} 占位，译文可以调换顺序；
//                     译文里写 {0|file|files} 按参数取单复数（英语 1 为单数，法语 0、1 为单数）。
//   tx(文字)          后端送来的文字（运行日志、状态行、报错、复核说明……）。后端仍用中文
//                     生成，这里按目录里的「模板」整句匹配后换成当前语言，占位处取到的
//                     片段再递归翻一次。已经存下来的旧日志切换语言后也会跟着变。
//
// 切换语言会重新加载页面：模块级的文案常量（侧栏、状态词表等）在加载时就算好了，
// 重载是让全部文字一次换齐的最简单办法。

import { createTranslator, type Catalog, type Locale } from "./engine";

export type { Locale } from "./engine";

export const LOCALES: { value: Locale; label: string }[] = [
  { value: "zh", label: "中文" },
  { value: "en", label: "English" },
  { value: "fr", label: "Français" },
];

export const LOCALE_STORAGE_KEY = "translator.locale";

function readStoredLocale(): Locale {
  try {
    const stored = window.localStorage.getItem(LOCALE_STORAGE_KEY);
    return stored === "en" || stored === "fr" || stored === "zh" ? stored : "zh";
  } catch {
    return "zh";
  }
}

const locale: Locale = readStoredLocale();

export function currentLocale(): Locale {
  return locale;
}

/** 给 Intl / toLocaleString 用的语言标签。 */
export function localeTag(): string {
  return locale === "en" ? "en-US" : locale === "fr" ? "fr-FR" : "zh-CN";
}

/** 把选择写进 localStorage（首屏不等后端）。后端那份由设置页写。 */
export function storeLocale(value: Locale): void {
  try {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, value);
  } catch {
    // localStorage 不可用时只是下次启动先按中文画一帧，再由后端值纠正。
  }
}

export function isLocale(value: unknown): value is Locale {
  return value === "zh" || value === "en" || value === "fr";
}

const catalogs = locale === "zh"
  ? []
  : Object.values(import.meta.glob<Catalog>("./catalog/*.json", { eager: true, import: "default" }));
const translator = createTranslator(locale, catalogs);

/** 界面文字。key 是中文原文，{0} {1} 占位。 */
export function t(source: string, ...args: unknown[]): string {
  return translator.t(source, ...args);
}

/** 后端送来的文字：整句或按模板换成当前语言，认不出来就原样返回。 */
export function tx(text: unknown): string {
  return translator.tx(text);
}

/** 数字按当前语言分组（1,234 / 1 234）。 */
export function fmtNum(value: number): string {
  return value.toLocaleString(localeTag());
}
