// 词条匹配引擎：不碰 window、不碰 Vite，只吃「语言 + 词条表」，方便在 node 里单测
// （tests/i18n_engine.test.mjs）。界面代码请用 ./index.ts 导出的 t / tx。

export type Locale = "zh" | "en" | "fr";
export type CatalogEntry = { en?: string; fr?: string };
export type Catalog = Record<string, CatalogEntry>;

interface TemplateEntry {
  regex: RegExp;
  target: string;
  /** 模板里字面文字的长度：多个模板都能匹配时取字面最长的，最具体。 */
  weight: number;
}

export interface Translator {
  t(source: string, ...args: unknown[]): string;
  tx(text: unknown): string;
}

const PLACEHOLDER_SPLIT_RE = /\{(\d+)\}/;
const PLACEHOLDER_TEST_RE = /\{\d+\}/;
const INTERPOLATE_RE = /\{(\d+)(?:\|([^|}]*)\|([^}]*))?\}/g;
const CJK_RE = /[㐀-鿿]/;
// 纯数字、时间、百分比之类的捕获片段不用再翻。
const NUMERIC_RE = /^[\s\d.,:%+\-/×x()]*$/;

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function createTranslator(locale: Locale, catalogs: Iterable<Catalog>): Translator {
  const exact = new Map<string, string>();
  const templatesByHead = new Map<string, TemplateEntry[]>();
  const templatesAnyHead: TemplateEntry[] = [];
  const runtimeCache = new Map<string, string>();

  function addTemplate(source: string, target: string): void {
    const pieces = source.split(new RegExp(PLACEHOLDER_SPLIT_RE.source, "g"));
    // split 带捕获组：偶数位是字面文字，奇数位是占位序号。
    let pattern = "^";
    const order: number[] = [];
    let weight = 0;
    for (let i = 0; i < pieces.length; i += 1) {
      if (i % 2 === 0) {
        pattern += escapeRegExp(pieces[i]);
        weight += pieces[i].length;
      } else {
        order.push(Number(pieces[i]));
        // 开头的占位通常是「[文件] 第 3 页 」这类前缀，里面可能含有模板后面的字面字，
        // 贪婪匹配才能切在最后一处；其余占位取最短。
        pattern += i === 1 && pieces[0] === "" ? "([\\s\\S]*)" : "([\\s\\S]*?)";
      }
    }
    pattern += "$";
    // 只有占位、没有字面文字的模板什么都能匹配，留着只会误伤。
    if (weight === 0) return;
    // 捕获组按出现顺序编号，模板里的占位序号未必从 0 递增：把译文里的 {n} 改写成捕获组序号。
    let rewritten = target;
    if (order.some((value, index) => value !== index)) {
      rewritten = target.replace(/\{(\d+)(\|[^}]*)?\}/g, (whole, num: string, rest: string | undefined) => {
        const position = order.indexOf(Number(num));
        return position < 0 ? whole : `{${position}${rest ?? ""}}`;
      });
    }
    const entry: TemplateEntry = { regex: new RegExp(pattern), target: rewritten, weight };
    const head = pieces[0] ? pieces[0].charAt(0) : "";
    if (head) {
      const bucket = templatesByHead.get(head) ?? [];
      bucket.push(entry);
      templatesByHead.set(head, bucket);
    } else {
      templatesAnyHead.push(entry);
    }
  }

  if (locale !== "zh") {
    for (const catalog of catalogs) {
      for (const [source, entry] of Object.entries(catalog)) {
        const target = entry?.[locale];
        if (typeof target !== "string" || !target) continue;
        exact.set(source, target);
        if (PLACEHOLDER_TEST_RE.test(source)) addTemplate(source, target);
        // 状态行以「状态：」开头，界面显示前会去掉这个前缀（workspace / tasks 的
        // statusPhaseText）：去掉前缀的说法也认，译文同样去掉 “Status:” 一类前缀。
        const STATUS_PREFIX = /^状态[:：]\s*/;
        if (STATUS_PREFIX.test(source)) {
          const bareSource = source.replace(STATUS_PREFIX, "");
          const bareTarget = target.replace(/^(Status|État|Statut)\s*:\s*/u, "");
          const capitalized = bareTarget.charAt(0).toUpperCase() + bareTarget.slice(1);
          if (bareSource && !exact.has(bareSource)) {
            exact.set(bareSource, capitalized);
            if (PLACEHOLDER_TEST_RE.test(bareSource)) addTemplate(bareSource, capitalized);
          }
        }
        // 后端常用「  → …」这类带缩进的行，界面显示前可能已经去掉了首尾空白：两种都认。
        const bare = source.trim();
        if (bare !== source && bare && !exact.has(bare)) {
          const bareTarget = target.trim();
          exact.set(bare, bareTarget);
          if (PLACEHOLDER_TEST_RE.test(bare)) addTemplate(bare, bareTarget);
        }
      }
    }
    const byWeight = (a: TemplateEntry, b: TemplateEntry) => b.weight - a.weight;
    for (const bucket of templatesByHead.values()) bucket.sort(byWeight);
    templatesAnyHead.sort(byWeight);
  }

  function pluralIndex(value: unknown): 0 | 1 {
    const n = typeof value === "number" ? value : Number(String(value ?? "").replace(/[^\d.-]/g, ""));
    if (!Number.isFinite(n)) return 1;
    if (locale === "fr") return Math.abs(n) < 2 ? 0 : 1;
    return Math.abs(n) === 1 ? 0 : 1;
  }

  function interpolate(template: string, args: readonly unknown[]): string {
    return template.replace(INTERPOLATE_RE, (whole, num: string, one?: string, many?: string) => {
      const index = Number(num);
      if (index >= args.length) return whole;
      if (one !== undefined && many !== undefined) {
        return pluralIndex(args[index]) === 0 ? one : many;
      }
      const value = args[index];
      return value === null || value === undefined ? "" : String(value);
    });
  }

  // 字面文字很短的模板（「{0}、{1}」「{0} · {1}」）几乎什么都能套上，只在整句、
  // 具体模板、按句拆分都失败之后才用。
  const GENERIC_WEIGHT = 3;

  function matchTemplate(text: string, depth: number, generic: boolean): string | null {
    let best: { entry: TemplateEntry; match: RegExpExecArray } | null = null;
    for (const list of [templatesByHead.get(text.charAt(0)) ?? [], templatesAnyHead]) {
      for (const entry of list) {
        if (best && best.entry.weight >= entry.weight) break;
        if ((entry.weight < GENERIC_WEIGHT) !== generic) continue;
        const match = entry.regex.exec(text);
        if (match) {
          best = { entry, match };
          break;
        }
      }
    }
    if (!best) return null;
    const captures = best.match.slice(1).map((part) => translateRuntime(part, depth + 1));
    return interpolate(best.entry.target, captures);
  }

  function translateSentences(text: string, depth: number): string | null {
    // 「A。B。」：逐句翻，每句先带句号找，再不带句号找。
    const sentences = text.match(/[^。]+。?/g);
    if (!sentences || sentences.length < 2) return null;
    let changed = false;
    const out = sentences.map((sentence) => {
      const withStop = translateRuntime(sentence, depth + 1);
      if (withStop !== sentence) {
        changed = true;
        return withStop;
      }
      if (sentence.endsWith("。")) {
        const bare = sentence.slice(0, -1);
        const translated = translateRuntime(bare, depth + 1);
        if (translated !== bare) {
          changed = true;
          return /[.!?]$/.test(translated) ? translated : `${translated}.`;
        }
      }
      return sentence;
    });
    return changed ? out.join(" ").replace(/\s+$/, "") : null;
  }

  function translateRuntime(text: string, depth: number): string {
    if (!text || depth > 4 || NUMERIC_RE.test(text) || !CJK_RE.test(text)) return text;
    const direct = exact.get(text);
    if (direct !== undefined) return direct;
    const templated = matchTemplate(text, depth, false);
    if (templated !== null) return templated;
    const trimmed = text.trim();
    if (trimmed !== text) {
      const inner = translateRuntime(trimmed, depth);
      return inner === trimmed ? text : text.replace(trimmed, inner);
    }
    // 整句对不上时，按行、按中文分号、再按句号拆开逐段翻：后端有些说明是几句拼起来的。
    if (text.includes("\n")) {
      const lines = text.split("\n");
      const translated = lines.map((line) => translateRuntime(line, depth + 1));
      if (translated.some((line, index) => line !== lines[index])) return translated.join("\n");
    }
    const bySentence = translateSentences(text, depth);
    if (bySentence !== null) return bySentence;
    if (text.includes("；")) {
      const parts = text.split("；");
      const translated = parts.map((part) => translateRuntime(part, depth + 1));
      if (translated.some((part, index) => part !== parts[index])) return translated.join("; ");
    }
    return matchTemplate(text, depth, true) ?? text;
  }

  return {
    t(source: string, ...args: unknown[]): string {
      const target = locale === "zh" ? source : exact.get(source) ?? source;
      return interpolate(target, args);
    },
    tx(text: unknown): string {
      const value = text === null || text === undefined ? "" : String(text);
      if (locale === "zh" || !value || !CJK_RE.test(value)) return value;
      const cached = runtimeCache.get(value);
      if (cached !== undefined) return cached;
      const result = translateRuntime(value, 0);
      if (runtimeCache.size > 5000) runtimeCache.clear();
      runtimeCache.set(value, result);
      return result;
    },
  };
}
