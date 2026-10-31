// 界面语言（简体中文 / 繁體中文 / English / Français）。
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
// 切换语言不重新加载页面：setLocale() 换掉词典，通知订阅者（app.ts 重画侧栏和
// 当前页面）。模块级的文案常量（侧栏、状态词表等）一律用 localized() 包起来，
// 换语言后第一次读取时按新语言重新生成；直接写成 const X = t(...) 会停留在旧语言。
//
// 繁體中文没有自己的词条：显示前把简体词条转成繁体（./zh-hant.ts，转换表按需加载）。
// 启动时（app.ts）和切换时都先等转换表加载好再画，不会先闪一帧简体。

import { createTranslator, type Catalog, type Locale } from "./engine";
import { loadTraditionalConverter, traditionalConverter } from "./zh-hant";

export type { Locale } from "./engine";

export const LOCALES: { value: Locale; label: string }[] = [
  { value: "zh", label: "简体中文" }, // i18n-ignore 各语言用自己的原名
  { value: "zh-Hant", label: "繁體中文" }, // i18n-ignore 各语言用自己的原名
  { value: "en", label: "English" },
  { value: "fr", label: "Français" },
];

export const LOCALE_STORAGE_KEY = "translator.locale";

function readStoredLocale(): Locale {
  try {
    const stored = window.localStorage.getItem(LOCALE_STORAGE_KEY);
    return isLocale(stored) ? stored : "zh";
  } catch {
    return "zh";
  }
}

let locale: Locale = readStoredLocale();

export function currentLocale(): Locale {
  return locale;
}

/** 给 Intl / toLocaleString 用的语言标签。 */
export function localeTag(): string {
  return locale === "en" ? "en-US" : locale === "fr" ? "fr-FR" : locale === "zh-Hant" ? "zh-TW" : "zh-CN";
}

/** 简体、繁体中文界面（标点、句子之间不加空格等按中文习惯）。 */
export function isChineseLocale(value: Locale = locale): boolean {
  return value === "zh" || value === "zh-Hant";
}

/** 后端给的 {zh, en, fr} 三语文字取当前语言；繁体由简体转换。 */
export function pickLocalized(value: Partial<Record<"zh" | "en" | "fr", string>> | null | undefined): string {
  if (!value) return "";
  if (locale === "zh-Hant") return value.zh ? t(value.zh) : value.en || value.fr || "";
  return value[locale] || value.zh || value.en || value.fr || "";
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
  return value === "zh" || value === "zh-Hant" || value === "en" || value === "fr";
}

function buildTranslator(value: Locale) {
  if (value === "zh") return createTranslator(value, []);
  if (value === "zh-Hant") {
    const convert = traditionalConverter();
    // 转换表还没加载好（只会在 ensureLocaleReady 之前出现）：先按简体显示。
    return convert ? createTranslator(value, catalogs, convert) : createTranslator("zh", []);
  }
  return createTranslator(value, catalogs);
}

/** 当前语言需要的资源（繁体转换表）加载好。启动时 app.ts 先等它，再画界面。 */
export async function ensureLocaleReady(value: Locale = locale): Promise<void> {
  if (value !== "zh-Hant") return;
  try {
    await loadTraditionalConverter();
  } catch {
    // 加载失败（不应发生：转换表打包在应用里）：界面按简体显示，不挡启动。
    return;
  }
  if (value === locale) {
    translator = buildTranslator(locale);
    generation += 1;
  }
}

const catalogs = Object.values(import.meta.glob<Catalog>("./catalog/*.json", { eager: true, import: "default" }));
let translator = buildTranslator(locale);
// 每换一次语言加一，localized() 据此判断缓存是否过期。
let generation = 0;
const localeListeners = new Set<(value: Locale) => void>();

/** 换界面语言，不重新加载页面。已经画好的界面由订阅者（app.ts）负责重画。
 *  繁體中文先等转换表加载好（第一次切换约几十毫秒）再换。 */
export async function setLocale(value: Locale): Promise<void> {
  if (value === locale) return;
  await ensureLocaleReady(value);
  locale = value;
  translator = buildTranslator(locale);
  generation += 1;
  document.documentElement.lang = localeTag();
  for (const listener of localeListeners) listener(value);
}

/** 订阅语言切换。返回取消订阅函数。 */
export function onLocaleChange(listener: (value: Locale) => void): () => void {
  localeListeners.add(listener);
  return () => localeListeners.delete(listener);
}

/**
 * 模块级文案常量的写法：const LABELS = localized(() => ({ a: t("…") }))。
 * 返回的对象读起来和原来的对象、数组、Map、Set 一样，换语言后第一次读取时
 * 按新语言重新生成一份。只适合只读的常量。
 */
export function localized<T extends object>(build: () => T): T {
  let cached = build();
  let builtAt = generation;
  const current = (): T => {
    if (builtAt !== generation) {
      cached = build();
      builtAt = generation;
    }
    return cached;
  };
  return new Proxy({} as T, {
    get(_target, key) {
      const real = current();
      const value: unknown = Reflect.get(real, key, real);
      return typeof value === "function" ? (value as (...args: unknown[]) => unknown).bind(real) : value;
    },
    has(_target, key) {
      return Reflect.has(current(), key);
    },
    ownKeys() {
      return Reflect.ownKeys(current()).filter((key) => key !== "length" || !Array.isArray(current()));
    },
    getOwnPropertyDescriptor(_target, key) {
      const descriptor = Reflect.getOwnPropertyDescriptor(current(), key);
      return descriptor ? { ...descriptor, configurable: true } : undefined;
    },
  });
}

// 「法文加长 30%」假语言：只在浏览器开发模式里、localStorage 的 translator.pseudo-long 为 1 时生效，
// 法文界面里每个词加长 30%，看以后加德语这类更长的语言时哪里会放不下（scripts/ui_text_fit_check.mjs
// 的报告项）。import.meta.env.DEV 在生产构建里是 false，整段剔除。
const pseudoLong = import.meta.env.DEV && (() => {
  try {
    return window.localStorage.getItem("translator.pseudo-long") === "1";
  } catch {
    return false;
  }
})();

function stretch(text: string): string {
  return pseudoLong && locale === "fr" ? text.replace(/\p{L}{2,}/gu, (word) => word + "ë".repeat(Math.ceil(word.length * 0.3))) : text;
}

/** 界面文字。key 是中文原文，{0} {1} 占位。 */
export function t(source: string, ...args: unknown[]): string {
  return stretch(translator.t(source, ...args));
}

/** 后端送来的文字：整句或按模板换成当前语言，认不出来就原样返回。 */
export function tx(text: unknown): string {
  return stretch(translator.tx(text));
}

/** 这个数量在当前语言里用单数吗（英文只有 1；法文 0 和 1）。和词条里的 {0|单数|复数} 同一规则。 */
export function isSingularCount(value: number): boolean {
  return translator.isSingular(value);
}

/** 数字按当前语言分组（1,234 / 1 234）。 */
export function fmtNum(value: number): string {
  return value.toLocaleString(localeTag());
}
