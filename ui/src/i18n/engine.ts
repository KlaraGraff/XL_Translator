// 词条匹配引擎：不碰 window、不碰 Vite，只吃「语言 + 词条表」，方便在 node 里单测
// （tests/i18n_engine.test.mjs）。界面代码请用 ./index.ts 导出的 t / tx。

// zh-Hant：繁體中文。词条表里没有繁体译文，显示时由调用方给的转换函数把简体换成繁体。
export type Locale = "zh" | "zh-Hant" | "en" | "fr";
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
  /** 这个数在该语言里取 {0|单数|复数} 的单数分支吗。 */
  isSingular(value: unknown): boolean;
}

const PLACEHOLDER_SPLIT_RE = /\{(\d+)\}/;
const PLACEHOLDER_TEST_RE = /\{\d+\}/;
const INTERPOLATE_RE = /\{(\d+)(?:\|([^|}]*)\|([^}]*))?\}/g;
const CJK_RE = /[㐀-鿿]/;
// 纯数字、时间、百分比之类的捕获片段不用再翻。
const NUMERIC_RE = /^[\s\d.,:%+\-/×x()]*$/;
// 繁体界面里围住占位片段的标记（控制字符，不会出现在正常文字里）。
const CAPTURE_OPEN = "\u0001";
const CAPTURE_CLOSE = "\u0002";
const CAPTURE_SPAN_RE = /\u0001[^\u0001\u0002]*\u0002/g;

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** 去掉一部分占位符后，把剩下的按原文出现顺序重新编号成 {0} {1} …，原文译文同步改。 */
function renumberPlaceholders(source: string, target: string): [string, string] {
  const order: string[] = [];
  for (const match of source.matchAll(/\{(\d+)(?:\|[^}]*)?\}/g)) if (!order.includes(match[1])) order.push(match[1]);
  const rename = (text: string) => text.replace(/\{(\d+)((?:\|[^}]*)?)\}/g, (all, index: string, rest: string) => {
    const position = order.indexOf(index);
    return position < 0 ? all : `{${position}${rest}}`;
  });
  return [rename(source), rename(target)];
}

export function createTranslator(
  locale: Locale,
  catalogs: Iterable<Catalog>,
  convert: (text: string) => string = (text) => text,
): Translator {
  const hant = locale === "zh-Hant";
  // 中文界面（简体、繁体）：句子之间不加空格，分号、句号用全角。
  const chinese = locale === "zh" || hant;
  const convertCache = new Map<string, string>();
  const toHant = (text: string): string => {
    const cached = convertCache.get(text);
    if (cached !== undefined) return cached;
    const result = convert(text);
    if (convertCache.size > 20000) convertCache.clear();
    convertCache.set(text, result);
    return result;
  };
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
        const target = hant ? toHant(source) : entry?.[locale as "en" | "fr"];
        if (typeof target !== "string" || !target) continue;
        exact.set(source, target);
        if (PLACEHOLDER_TEST_RE.test(source)) addTemplate(source, target);
        // 状态行以「状态：」开头，界面显示前会去掉这个前缀（workspace / tasks 的
        // statusPhaseText）：去掉前缀的说法也认，译文同样去掉 “Status:” 一类前缀。
        const STATUS_PREFIX = /^状态[:：]\s*/;
        if (STATUS_PREFIX.test(source)) {
          const bareSource = source.replace(STATUS_PREFIX, "");
          const bareTarget = hant ? toHant(bareSource) : target.replace(/^(Status|État|Statut)\s*:\s*/u, "");
          const capitalized = bareTarget.charAt(0).toUpperCase() + bareTarget.slice(1);
          if (bareSource && !exact.has(bareSource)) {
            exact.set(bareSource, capitalized);
            if (PLACEHOLDER_TEST_RE.test(bareSource)) addTemplate(bareSource, capitalized);
          }
          // 进度条上阶段名单独显示，状态行连「[阶段 2/3]」「[收尾中]」也一起去掉了。
          const STAGE_MARK = /^\[(?:阶段\s*[^\]]*|收尾中)\]\s*/;
          if (STAGE_MARK.test(bareSource)) {
            const [stageSource, stageTarget] = renumberPlaceholders(
              bareSource.replace(STAGE_MARK, ""),
              hant ? toHant(bareSource.replace(STAGE_MARK, "")) : bareTarget.replace(/^\[[^\]]*\]\s*/, ""),
            );
            const stageCapitalized = stageTarget.charAt(0).toUpperCase() + stageTarget.slice(1);
            if (stageSource && !exact.has(stageSource)) {
              exact.set(stageSource, stageCapitalized);
              if (PLACEHOLDER_TEST_RE.test(stageSource)) addTemplate(stageSource, stageCapitalized);
            }
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
    // 中文没有单复数，词条里写了 {0|…|…} 时都取第二种（与简体界面原样显示的写法一致）。
    return Math.abs(n) === 1 ? 0 : 1;
  }

  function interpolate(template: string, args: readonly unknown[]): string {
    return template.replace(INTERPOLATE_RE, (whole, num: string, one?: string, many?: string) => {
      const index = Number(num);
      if (index >= args.length) return whole;
      if (one !== undefined && many !== undefined) {
        // 分支里的「#」换成这个数：「{1|cet élément|ces # éléments}」。
        return (pluralIndex(args[index]) === 0 ? one : many).replace(/#/g, () => String(args[index] ?? ""));
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
    // 繁体界面：占位处取到的片段（文件名、路径、用户写的内容）用标记围起来，最后整句转繁体时跳过。
    const captures = best.match.slice(1).map((part) => {
      const translated = translateRuntime(part, depth + 1);
      return hant ? `${CAPTURE_OPEN}${translated}${CAPTURE_CLOSE}` : translated;
    });
    return interpolate(best.entry.target, captures);
  }

  function cjkCount(text: string): number {
    return (text.match(new RegExp(CJK_RE.source, "g")) ?? []).length;
  }

  function translateSemicolonParts(text: string, depth: number): string | null {
    const parts = text.split("；");
    const translated = parts.map((part) => translateRuntime(part, depth + 1));
    if (!translated.some((part, index) => part !== parts[index])) return null;
    // 前一段已经以句号结尾（整句翻出来的）就只空一格，不再接分号。
    if (chinese) return translated.join("；");
    const semicolon = locale === "fr" ? "\u00a0; " : "; ";
    return translated.reduce((acc, part) => (/[.!?]$/.test(acc) ? `${acc} ${part}` : `${acc}${semicolon}${part}`));
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
          if (chinese) return /[。！？]$/.test(translated) ? translated : `${translated}。`;
          return /[.!?]$/.test(translated) ? translated : `${translated}.`;
        }
      }
      return sentence;
    });
    if (!changed) return null;
    return chinese ? out.join("") : out.join(" ").replace(/\s+$/, "");
  }

  // 繁体界面：标记之外、还没换成繁体的字数（没认出来的部分）；其余界面：剩下的中文字数。
  function leftover(text: string): number {
    if (!hant) return cjkCount(text);
    const outside = text.replace(CAPTURE_SPAN_RE, "");
    const converted = toHant(outside);
    let count = 0;
    for (let i = 0; i < outside.length; i += 1) if (outside[i] !== converted[i]) count += 1;
    return count;
  }

  // 去掉占位标记；标记之外的文字转成繁体（词条认不出来的句子也显示繁体），标记之内原样。
  function finishHant(text: string): string {
    let out = "";
    let plain = "";
    let level = 0;
    for (const ch of text) {
      if (ch === CAPTURE_OPEN) {
        if (level === 0 && plain) {
          out += toHant(plain);
          plain = "";
        }
        level += 1;
      } else if (ch === CAPTURE_CLOSE) {
        level = Math.max(0, level - 1);
      } else if (level === 0) {
        plain += ch;
      } else {
        out += ch;
      }
    }
    return out + (plain ? toHant(plain) : "");
  }

  function translateRuntime(text: string, depth: number): string {
    if (!text || depth > 4 || NUMERIC_RE.test(text) || !CJK_RE.test(text)) return text;
    const direct = exact.get(text);
    if (direct !== undefined) return direct;
    const templated = matchTemplate(text, depth, false);
    if (templated !== null) {
      // 模板的占位可能吞进「；」另一侧的整条说明：拆开逐条翻剩的中文更少时用拆开的。
      const split = text.includes("；") ? translateSemicolonParts(text, depth) : null;
      return split !== null && leftover(split) < leftover(templated) ? split : templated;
    }
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
    // 几条说明用「；」拼成一行时（任务摘要），按句拆会把分号两侧切进同一句里。
    // 两种拆法都试，取剩下中文最少的那个。
    const candidates: string[] = [];
    const bySentence = translateSentences(text, depth);
    if (bySentence !== null) candidates.push(bySentence);
    const bySemicolon = text.includes("；") ? translateSemicolonParts(text, depth) : null;
    if (bySemicolon !== null) candidates.push(bySemicolon);
    if (candidates.length) {
      return candidates.reduce((a, b) => (leftover(b) < leftover(a) ? b : a));
    }
    return matchTemplate(text, depth, true) ?? text;
  }

  return {
    t(source: string, ...args: unknown[]): string {
      const target = locale === "zh" ? source : exact.get(source) ?? (hant ? toHant(source) : source);
      return interpolate(target, args);
    },
    tx(text: unknown): string {
      const value = text === null || text === undefined ? "" : String(text);
      if (locale === "zh" || !value || !CJK_RE.test(value)) return value;
      const cached = runtimeCache.get(value);
      if (cached !== undefined) return cached;
      const result = hant ? finishHant(translateRuntime(value, 0)) : translateRuntime(value, 0);
      if (runtimeCache.size > 5000) runtimeCache.clear();
      runtimeCache.set(value, result);
      return result;
    },
    isSingular(value: unknown): boolean {
      return pluralIndex(value) === 0;
    },
  };
}
