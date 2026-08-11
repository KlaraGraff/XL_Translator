// 更新说明的极简 Markdown 渲染器。
//
// 只认发布说明实际会用到的五种语法：二/三级标题、无序列表、段落、**加粗**、`行内代码`。
// 刻意不支持 HTML 透传、图片、表格——发布说明来自 GitHub Release 正文（网络内容），
// 走 innerHTML 等于把远端字符串当代码执行。这里全程用 createElement / createTextNode
// 拼 DOM，任何尖括号都只会以字面文本出现在界面上。
//
// 目标样式见 docs/mockups/2026-08-11_in-app-update.html 的 `.rn` 规则组。

/**
 * 「下载」「校验」这类小节在应用内没有意义：更新器自己下载、自己验签，
 * 把安装包链接和 64 位十六进制摆给用户看只会把真正的变更说明挤出视野。
 * 命中这些标题的小节整段丢弃，直到下一个同级或更高级标题为止。
 */
// 中文标题必须整行相等再丢（`\b` 在汉字后面永远不成立，中英文不能写进同一个分支：
// `下载\b` 是死规则，命不中任何东西）。英文标题保留词边界，好接住
// "Downloads and checksums" 这类带后缀的写法。
const DROPPED_SECTION =
  /^(?:(?:下载|下载与校验|安装包|校验|校验和|文件校验)$|(?:downloads?|checksums?|verification|assets)\b)/i;

/**
 * `---` / `***` / `___` 分隔线。在发布说明里它一直只有一个用途：把「这一版改了
 * 什么」和写给下载页的页脚分开——签名与 SmartScreen 提示、安装包清单、sha256
 * 校验说明。这些话是讲给「还没装上这个软件的人」听的，而看这一页的人早就装好了，
 * 而且更新器自己会下载、自己验签。所以分隔线不是「画一条线」，是正文到此为止。
 *
 * 因此发布说明的写法约定为：应用内要看到的内容写在第一条 `---` 之前。
 */
const THEMATIC_BREAK = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;

/**
 * 整行只有一段加粗时当作小标题。发布说明的既定写法里，「下载」这一节的标题就是
 * `**下载**` 而不是 `## 下载`——不认它的话，安装包链接和校验说明会原样留在界面上。
 */
const BOLD_ONLY_LINE = /^\s*(?:\*\*|__)(.+?)(?:\*\*|__)\s*$/;

/**
 * setext 标题的下划线：紧跟在一段文字下面的一行 `===` 或 `---`，在 Markdown 里
 * 是「把上面那行变成标题」，不是分隔线。必须先于 THEMATIC_BREAK 判定——否则谁把
 * 标题写成下划线式，从那一行往后的整篇正文都会被当成页脚丢掉。
 */
const SETEXT_UNDERLINE = /^\s{0,3}(?:=+|-+)\s*$/;

interface Block {
  kind: "heading" | "paragraph" | "list";
  lines: string[];
}

/**
 * 返回标题级别，0 表示不是标题。整行加粗算三级标题——比 `##` 低一级，这样
 * `**下载**` 开启的小节会被后面任何一个 `##` 正常收尾。
 */
function headingLevel(line: string): number {
  const match = /^(#{1,6})\s+/.exec(line);
  if (match) return match[1].length;
  return BOLD_ONLY_LINE.test(line) ? 3 : 0;
}

function headingText(line: string): string {
  const bold = BOLD_ONLY_LINE.exec(line);
  if (bold) return bold[1].trim();
  return line.replace(/^#{1,6}\s+/, "").trim();
}

function listItemText(line: string): string | null {
  const match = /^\s*(?:[-*+]|\d+[.)])\s+(.*)$/.exec(line);
  return match ? match[1].trim() : null;
}

/** 把原文切成块，并顺带丢掉 DROPPED_SECTION 命中的整个小节。 */
function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  // > 0 时表示正处在一个被丢弃的小节里，值是该小节标题的级别。
  let droppingAtLevel = 0;
  // 当前是否有一个还没被空行收尾的段落。只看 blocks 末尾是不是段落不够：空行
  // 之后的下一段会接着写进上一段，整篇变成一坨。
  let paragraphOpen = false;

  for (const raw of lines) {
    const line = raw.trimEnd();
    const openParagraph = paragraphOpen ? blocks[blocks.length - 1] : undefined;
    if (openParagraph?.kind === "paragraph" && SETEXT_UNDERLINE.test(line)) {
      // 上一段其实是个标题。就地改成标题块，标题名照样过一遍丢弃规则。
      const title = openParagraph.lines.join(" ");
      blocks.pop();
      paragraphOpen = false;
      if (DROPPED_SECTION.test(title)) {
        droppingAtLevel = 2;
        continue;
      }
      blocks.push({ kind: "heading", lines: [title] });
      continue;
    }
    if (THEMATIC_BREAK.test(line)) {
      // 正文到此为止。开头就是分隔线的情况按「还没有正文」处理，否则整篇会被
      // 一条排在最前面的横线吃光。
      if (blocks.length) break;
      continue;
    }
    const level = headingLevel(line);

    if (level > 0) {
      paragraphOpen = false;
      const title = headingText(line);
      if (DROPPED_SECTION.test(title)) {
        droppingAtLevel = level;
        continue;
      }
      // 同级或更高级的标题结束上一个被丢弃的小节。
      if (droppingAtLevel > 0 && level <= droppingAtLevel) {
        droppingAtLevel = 0;
      }
      if (droppingAtLevel > 0) continue;
      blocks.push({ kind: "heading", lines: [title] });
      continue;
    }

    if (droppingAtLevel > 0) continue;

    if (!line.trim()) {
      paragraphOpen = false;
      continue;
    }

    const item = listItemText(line);
    const last = blocks[blocks.length - 1];
    if (item !== null) {
      paragraphOpen = false;
      if (last?.kind === "list") {
        last.lines.push(item);
      } else {
        blocks.push({ kind: "list", lines: [item] });
      }
      continue;
    }

    // 连续的非空行合并成一个段落（Markdown 的软换行语义），空行则另起一段。
    if (paragraphOpen && last?.kind === "paragraph") {
      last.lines.push(line.trim());
    } else {
      blocks.push({ kind: "paragraph", lines: [line.trim()] });
      paragraphOpen = true;
    }
  }

  return blocks;
}

/**
 * 渲染行内语法到 target。识别顺序固定：行内代码 > 加粗 > 链接文本 > 纯文本，
 * 反引号内部不再做加粗解析（否则 `**kwargs` 这类代码会被吃掉星号）。
 */
function appendInline(target: HTMLElement, source: string): void {
  // 先按反引号切开，奇数段（索引为奇）就是代码。
  const codeSplit = source.split("`");
  codeSplit.forEach((segment, index) => {
    if (index % 2 === 1) {
      const code = document.createElement("code");
      code.textContent = segment;
      target.append(code);
      return;
    }
    appendEmphasis(target, segment);
  });
}

function appendEmphasis(target: HTMLElement, source: string): void {
  // [文字](链接) 只保留文字：应用内没有浏览器上下文，渲染成不可点的蓝字更误导。
  // 链接地址允许含一层配对括号（维基词条那种 `..._(disambiguation)` 就是），否则
  // 会停在第一个右括号上，把剩下的半截地址当正文留在界面里。
  const flattened = source.replace(/\[([^\]]*)\]\((?:[^()]|\([^()]*\))*\)/g, "$1");
  const parts = flattened.split(/(\*\*[^*]+\*\*|__[^_]+__)/g);
  for (const part of parts) {
    if (!part) continue;
    const bold = /^(\*\*|__)([\s\S]+)\1$/.exec(part);
    if (bold) {
      const strong = document.createElement("strong");
      strong.textContent = bold[2];
      target.append(strong);
    } else {
      target.append(document.createTextNode(part));
    }
  }
}

/**
 * 渲染发布说明。返回一个 `.rn` 容器；source 为空或全部小节都被丢弃时返回 null，
 * 由调用方决定显示什么占位文案（不同状态下的兜底措辞不一样）。
 */
export function renderReleaseNotes(source: string): HTMLDivElement | null {
  const blocks = parseBlocks(source ?? "");
  if (!blocks.length) return null;

  const root = document.createElement("div");
  root.className = "rn";
  for (const block of blocks) {
    if (block.kind === "heading") {
      const h = document.createElement("h3");
      appendInline(h, block.lines[0]);
      root.append(h);
    } else if (block.kind === "list") {
      const ul = document.createElement("ul");
      for (const item of block.lines) {
        const li = document.createElement("li");
        appendInline(li, item);
        ul.append(li);
      }
      root.append(ul);
    } else {
      const p = document.createElement("p");
      appendInline(p, block.lines.join(" "));
      root.append(p);
    }
  }
  return root;
}

/** 折叠判定用：渲染后大致有多少「行」内容，超过阈值默认收起。 */
export function releaseNotesLineCount(source: string): number {
  return parseBlocks(source ?? "").reduce(
    (total, block) => total + (block.kind === "list" ? block.lines.length : 1),
    0,
  );
}
