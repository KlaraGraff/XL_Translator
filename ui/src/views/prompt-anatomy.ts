// 设置 · 翻译参数 · 提示词设置标题旁的「?」：弹窗说明提示词由哪几段组成。
//
// 左边是处理流程，右边按实际发送顺序逐段列出发给模型的内容；这次不发送的段落用虚线框。
// 内容跟着当前设置走（补充要求选了哪条、「自动识别场景」开没开、PDF 的翻译方式）。
// 只写已经上线的部分；拼装逻辑见 core/engine_dispatcher.get_system_prompt、
// core/document_profile.append_profile、core/term_engine（本批涉及的词条、改正说明）、
// core/image_generation.build_pdf_image_translation_prompt、core/pdf_vector/classify.PROMPT
// 和 core/pdf_word_route（转成 Word 后按 Word 流程翻译）。

import { createButton } from "../components";
import { t } from "../i18n";
import { el } from "./workspace-util";
import "./prompt-anatomy.css";

export type PromptAnatomySurface = "excel" | "word" | "pdf" | "cad";

interface AnatomyBlock {
  title: string;
  state: string;
  content: string;
  sent: boolean;
  mono?: boolean;
}

interface AnatomyGroup {
  /** PDF 按翻译方式分组；Excel、Word、CAD 只有一组，不写标题。 */
  title?: string;
  state?: string;
  used: boolean;
  blocks: AnatomyBlock[];
  note?: string;
}

export interface PromptAnatomy {
  steps: string[];
  heading: string;
  groups: AnatomyGroup[];
  notes: string[];
}

export interface DocAnatomyInput {
  surface: "excel" | "word" | "cad";
  corePrompt: string;
  /** 当前选用的补充要求名称和正文；未选用时为空。 */
  extraName: string;
  extraText: string;
  smartContext: boolean;
  /** 记忆库「未采用设定译法时重试次数」。 */
  termRetries: number;
}

export interface PdfAnatomyInput {
  smartContext: boolean;
  reviewEnabled: boolean;
  route: "image" | "auto";
  scanRoute: string;
  textRoute: string;
  drawingRoute: string;
  fallback: string;
}

/** 取开头一段，超出时以「……」结尾：内置规则全文就在卡片里，这里只示意。 */
function excerpt(text: string, maxChars: number): string {
  const flat = text.trim().split(/\n+/).join("");
  return flat.length > maxChars ? `${flat.slice(0, maxChars)}……` : flat;
}

function profileExample(): string {
  return t("例：建筑消防设计说明，甲/乙/丙指防火门和防火分区的耐火等级。");
}

function profileBlock(on: boolean, state = t("由「自动识别场景」生成")): AnatomyBlock {
  return { title: t("文档说明"), state: on ? state : t("未开启，不发送"), sent: on, content: profileExample() };
}

function formatBlock(): AnatomyBlock {
  return { title: t("返回格式"), state: t("程序附上"), sent: true, content: t("只返回译文，按 JSON 数组输出，条数和顺序与原文一致。") };
}

export function docPromptAnatomy(input: DocAnatomyInput): PromptAnatomy {
  const withTerms = input.surface !== "cad";
  const hasExtra = Boolean(input.extraText.trim());
  const retries = Math.max(0, Math.trunc(input.termRetries));
  const steps = [
    t("任务开始时查找记忆库：整段与记忆库词条相同的内容直接套用，不发送给模型。"),
    t("开启「自动识别场景」时，先由文档翻译模型通读整份文件，写一段文档说明，每份文件一次。"),
  ];
  if (withTerms) {
    steps.push(
      t("开启「自动识别场景」且内容较多时，再找出文中反复出现的词，由记忆库清洗模型（未配置时由文档翻译模型）结合文档说明定下统一译法，每份文件一次。"),
      t("其余内容分批发送。每批发送前，查找本批实际出现的记忆库词条，随本批一起发送。"),
      retries > 0
        ? t("译文返回后核对「始终采用」的词条；未采用时附上改正说明重新翻译，最多 {0} 次，仍未采用的位置标记为需复核。", retries)
        : t("译文返回后核对「始终采用」的词条；未采用的位置直接标记为需复核。"),
    );
    if (retries > 0) {
      steps.push(t("本文档统一用词同样核对：未采用时随第一次重新翻译一并改正，仍未采用的不再重译，也不标记。"));
    }
  } else {
    steps.push(t("其余内容分批发送给文档翻译模型。"));
  }
  steps.push(input.surface === "word"
    ? t("译文为空或不完整时，按「单段最多重试次数」重新翻译；仍失败的段落保留原文，并标记为需复核。")
    : t("请求失败或回答中断时，按「失败重试次数」重新发送；仍失败的内容保留原文，并标记为需复核。"));

  const blocks: AnatomyBlock[] = [
    {
      title: t("内置规则"),
      state: t("每次发送"),
      sent: true,
      content: input.corePrompt ? excerpt(input.corePrompt, 90) : t("内置规则读取失败，请稍后重新打开此页。"),
    },
    {
      title: t("补充要求"),
      state: hasExtra ? t("当前选用：{0}", input.extraName) : t("未选用，不发送"),
      sent: hasExtra,
      content: hasExtra ? excerpt(input.extraText, 120) : t("无"),
    },
    // 发送顺序同 core/prompt_cache：固定内容在前，每份文档、每批不同的在后。
    formatBlock(),
    profileBlock(input.smartContext),
  ];
  if (withTerms) {
    blocks.push(
      {
        title: t("本批涉及的词条"),
        state: t("仅含本批出现的词条，没有时不发送"),
        sent: true,
        mono: true,
        content: t("必须采用：\n- [第 1、17 条] 甲级防火门 → porte coupe-feu de classe A\n按场景选用：\n- [第 12 条] 甲：a. A（配电回路编号）；b. 原样保留（属于编号的一部分）\n本文档统一用词：\n- [第 5、9 条] 防火分区 → compartiment coupe-feu"),
      },
      {
        title: t("改正说明"),
        state: retries > 0 ? t("仅在重新翻译时发送") : t("重试次数为 0，不发送"),
        sent: retries > 0,
        mono: true,
        content: t("- [第 17 条] 上次译文：porte coupe-feu A；须采用：甲级防火门 → porte coupe-feu de classe A"),
      },
    );
  }
  blocks.push({
    title: t("待翻译内容"),
    state: t("本批原文"),
    sent: true,
    mono: true,
    content: t("[\"1. 本工程防火门均采用甲级防火门\", … 本批若干条]"),
  });

  const notes = [t("目标语言为自定义语言时，另附该语言的说明。")];
  if (withTerms) notes.push(t("记忆库中未在本批出现的词条不发送。"));
  return {
    steps,
    heading: t("每批发送给文档翻译模型的内容"),
    groups: [{ used: true, blocks }],
    notes,
  };
}

export function pdfPromptAnatomy(input: PdfAnatomyInput): PromptAnatomy {
  const auto = input.route === "auto";
  const imagePrimary = !auto || input.scanRoute === "image" || input.textRoute === "image" || input.drawingRoute === "image";
  const imageFallback = auto && !imagePrimary && input.fallback === "image";
  const wordUsed = auto && input.textRoute === "word";
  const inplaceUsed = auto && (input.textRoute === "inplace" || input.drawingRoute === "inplace");

  const steps = [t("开启「自动识别场景」时，先读取 PDF 中的文字，由文档翻译模型写一段文档说明；扫描件没有可读文字时跳过。")];
  if (auto) {
    steps.push(t("按页面类型分别处理：扫描页、可提取文字的页、图纸和大图各按 PDF 页面「翻译方式」中的设定；某页无法使用所选方式时改用保底方式。"));
  }
  if (imagePrimary || imageFallback) {
    steps.push(auto
      ? t("整页图像翻译的页：每页转成图片发送给图像模型；开启「逐页审核」时，未通过的页面附上审核意见重新生成。")
      : t("每页转成图片发送给图像模型，生成翻译后的页面；开启「逐页审核」时，未通过的页面附上审核意见重新生成。"));
    steps.push(t("单页生成失败或审核未通过时，最多按「单页重试次数」重新生成。"));
  }
  if (wordUsed) steps.push(t("转成 Word 的页：连续几页合成一份 Word，按 Word 的提示词设置和记忆库翻译，再转回 PDF。"));
  if (inplaceUsed) steps.push(t("原位替换的页：取出页面上的文字发送给文档翻译模型，译文按原字号和颜色写回原位置。"));
  if (auto) steps.push(t("全部完成后，按原页序合成一份 PDF。"));

  const groupState = (used: boolean, fallback = false): string =>
    used ? t("当前使用") : fallback ? t("用作保底方式") : t("当前未使用");
  const groups: AnatomyGroup[] = [
    {
      title: t("整页图像翻译 · 发送给图像模型"),
      state: groupState(imagePrimary, imageFallback),
      used: imagePrimary || imageFallback,
      blocks: [
        { title: t("内置规则"), state: t("每次发送"), sent: true, content: t("页面翻译的通用要求和目标语言的书写规则，由程序固定，不能修改。") },
        profileBlock(input.smartContext),
        { title: t("原页面图片"), state: t("每页一张"), sent: true, content: t("当前页面的图片。") },
        {
          title: t("审核意见"),
          state: input.reviewEnabled ? t("仅在审核未通过、重新生成时发送") : t("未开启逐页审核，不发送"),
          sent: input.reviewEnabled,
          content: t("审核模型指出的问题，要求只改正这些问题，其余保持不变。"),
        },
      ],
    },
    {
      title: t("转成 Word · 发送给文档翻译模型"),
      state: groupState(wordUsed),
      used: wordUsed,
      blocks: [{
        title: t("与 Word 文档相同"),
        state: t("按「Word」页签的提示词设置"),
        sent: true,
        content: t("依次为内置规则、补充要求、返回格式、文档说明、本批涉及的词条和待翻译内容。"),
      }],
    },
    {
      title: t("原位替换 · 发送给文档翻译模型"),
      state: groupState(inplaceUsed),
      used: inplaceUsed,
      blocks: [
        { title: t("内置规则"), state: t("每次发送"), sent: true, content: t("工程图纸标注的翻译要求：逐条译成简体中文，保留数字、单位、编号和缩写，由程序固定。") },
        formatBlock(),
        profileBlock(input.smartContext, t("由「自动识别场景」按本页文字生成")),
        { title: t("待翻译内容"), state: t("本页取出的文字"), sent: true, mono: true, content: t("[\"Porte coupe-feu\", \"Local technique\", … 本页若干条]") },
      ],
      note: t("数字、编号等不需要翻译的文字不发送。"),
    },
  ];
  return {
    steps,
    heading: t("各翻译方式发送给模型的内容"),
    groups,
    notes: [],
  };
}

function renderBlock(block: AnatomyBlock): HTMLDivElement {
  const item = el("div", block.sent ? "pa-blk" : "pa-blk off");
  const tag = el("div", "pa-tag");
  const name = el("b");
  name.textContent = block.title;
  const state = el("span");
  state.textContent = block.state;
  tag.append(name, state);
  const body = el("div", block.mono ? "pa-body mono" : "pa-body");
  body.textContent = block.content;
  item.append(tag, body);
  return item;
}

function renderGroup(group: AnatomyGroup): HTMLDivElement {
  const box = el("div", group.used ? "pa-group" : "pa-group off");
  if (group.title) {
    const head = el("div", "pa-group-head");
    const name = el("b");
    name.textContent = group.title;
    head.append(name);
    if (group.state) {
      const state = el("span", group.used ? "pa-chip on" : "pa-chip");
      state.textContent = group.state;
      head.append(state);
    }
    box.append(head);
  }
  // 当前没用到的翻译方式只留标题行：要看它的组成，先在 PDF 页把它选上。
  if (!group.used) return box;
  for (const block of group.blocks) box.append(renderBlock(block));
  if (group.note) {
    const note = el("p", "pa-note");
    note.textContent = group.note;
    box.append(note);
  }
  return box;
}

/** 打开组成说明弹窗：居中、比普通弹窗宽；Esc、点遮罩或「关闭」都能关上。 */
export function openPromptAnatomy(surfaceLabel: string, anatomy: PromptAnatomy): void {
  const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const overlay = el("div", "overlay");
  const modal = el("div", "modal prompt-anatomy-modal");
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");
  modal.setAttribute("aria-labelledby", "prompt-anatomy-title");

  const label = el("span", "mlabel");
  label.textContent = t("设置 · 翻译参数 · {0}", surfaceLabel);
  const heading = el("h3");
  heading.id = "prompt-anatomy-title";
  heading.textContent = t("提示词的组成");
  const intro = el("p", "pa-intro");
  intro.textContent = t("按当前设置列出处理顺序和发送给模型的内容；虚线框表示当前不发送。");

  const grid = el("div", "pa-grid");
  const flow = el("div", "pa-flow");
  const flowTitle = el("b", "pa-title");
  flowTitle.textContent = t("处理流程");
  const steps = el("ol");
  for (const step of anatomy.steps) {
    const li = el("li");
    li.textContent = step;
    steps.append(li);
  }
  flow.append(flowTitle, steps);

  const sent = el("div", "pa-sent");
  const sentTitle = el("b", "pa-title");
  sentTitle.textContent = anatomy.heading;
  sent.append(sentTitle);
  for (const group of anatomy.groups) sent.append(renderGroup(group));
  for (const text of anatomy.notes) {
    const note = el("p", "pa-note");
    note.textContent = text;
    sent.append(note);
  }
  grid.append(flow, sent);

  const acts = el("div", "macts");
  const closeBtn = createButton({ label: t("关闭"), variant: "primary", onClick: () => close() });
  acts.append(closeBtn);

  modal.append(label, heading, intro, grid, acts);
  overlay.append(modal);
  document.body.append(overlay);
  closeBtn.focus();

  function close(): void {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
    opener?.focus();
  }
  function onKey(event: KeyboardEvent): void {
    const overlays = document.querySelectorAll(".overlay");
    if (event.key !== "Escape" || overlays[overlays.length - 1] !== overlay) return;
    close();
  }
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });
  document.addEventListener("keydown", onKey);
}

/** 提示词设置的卡片标题旁的「?」按钮。 */
export function promptAnatomyButton(onOpen: () => void): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "swrow-hint prompt-anatomy-toggle";
  button.textContent = "?";
  button.title = t("提示词的组成");
  button.setAttribute("aria-label", t("提示词的组成"));
  button.setAttribute("aria-haspopup", "dialog");
  button.addEventListener("click", onOpen);
  return button;
}
