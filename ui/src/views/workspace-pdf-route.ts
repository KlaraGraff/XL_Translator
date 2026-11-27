/**
 * 工作台：PDF 页的「翻译方式」区（图像还原 / 其他）和本机条件查询。
 */
import { type PdfRouteCapabilities } from "../api-client";
import { createDropdown, createHintBadge, createSwitchRow, type SelectFieldOption } from "../components";
import { openGuide } from "../guide";
import { t } from "../i18n";
import { navigate } from "../router";
import { el } from "./workspace-util";
import { type PdfRoute, type SurfaceState, client, handlePdfRouteChange, rerender } from "./workspace";

/** 本机条件：后端 /api/pdf/route-capabilities 给；查不到时不提示。每次进 PDF 页重查一次
 *  （在插件页装好组件、在设置里配好图像模型再回来，提示要跟着消失），重查期间沿用旧值。 */
let pdfRouteCaps: PdfRouteCapabilities | null = null;
let pdfRouteCapsLoading = false;
let pdfRouteCapsStale = true;

/** 下次进 PDF 页时重查本机条件。 */
export function markPdfRouteCapsStale(): void {
  pdfRouteCapsStale = true;
}

function ensurePdfRouteCaps(): void {
  if (pdfRouteCapsLoading || (pdfRouteCaps && !pdfRouteCapsStale)) return;
  pdfRouteCapsLoading = true;
  pdfRouteCapsStale = false;
  client.pdfRouteCapabilities()
    .then((caps) => {
      const changed = JSON.stringify(caps) !== JSON.stringify(pdfRouteCaps);
      pdfRouteCaps = caps;
      if (changed) rerender("pdf");
    })
    .catch(() => undefined)
    .finally(() => {
      pdfRouteCapsLoading = false;
    });
}

function buildRouteRow(opts: {
  label: string;
  hint: string;
  /** 提示末尾的蓝色链接（「翻译方式」的「查看图文说明」）。 */
  hintAction?: { label: string; onClick: () => void };
  options: SelectFieldOption[];
  value: string;
  disabled: boolean;
  sub?: boolean;
  wide?: boolean;
  onChange: (value: string) => void;
}): HTMLElement {
  const row = el("div", opts.sub ? "selrow sub" : "selrow");
  const name = el("span", "selrow-label");
  name.textContent = opts.label;
  row.append(name, createHintBadge(opts.hint, "hint", opts.hintAction));
  const dropdown = createDropdown({
    options: opts.options,
    value: opts.value,
    disabled: opts.disabled,
    // className 还会拼成菜单的类名（compact-menu），只能是一个词；宽版另加类。
    className: "compact",
    onChange: opts.onChange,
  });
  if (opts.wide) dropdown.root.classList.add("wide");
  dropdown.input.setAttribute("aria-label", opts.label);
  row.append(dropdown.root);
  return row;
}

export function buildPdfRouteSection(st: SurfaceState, active: boolean): HTMLElement {
  // 紧接在上面的开关后面，不另起小标题：小标题和下面这一行的名称都会是「翻译方式」。
  // 只让用户选两种（maple 2026-11-27）：「图像还原」整页交给图像模型；「其他」由程序逐页
  // 在段落重排、转 Word、原位替换里选（core/pdf_routes.py page_route），不再让用户挑。
  const wrap = el("div", "route-block");
  const imageLabel = t("图像还原");
  const keepLabel = t("原样保留");

  wrap.append(buildRouteRow({
    label: t("翻译方式"),
    // 细节交给图文指引（guide.ts），问号里只留一句话。
    hint: t("图像还原把每页交给图像模型重新生成；「其他」由程序逐页选用段落重排、转成 Word 或原位替换。"),
    hintAction: { label: t("查看图文说明"), onClick: () => openGuide("pdf") },
    options: [
      { value: "image", label: imageLabel, description: t("每页由图像模型重新生成，适合扫描件和照片") },
      { value: "auto", label: t("其他"), tag: t("Beta"), description: t("程序逐页选用段落重排、转成 Word 或原位替换，合成一份 PDF") },
    ],
    value: st.pdfRoute,
    disabled: active,
    wide: true,
    onChange: (value) => void handlePdfRouteChange(st, "pdfRoute", "pdf.translation_route", value as PdfRoute),
  }));
  if (st.pdfRoute !== "auto") return wrap;

  ensurePdfRouteCaps();
  wrap.append(buildRouteRow({
    label: t("扫描页"),
    hint: t("提取不到文字的页，比如签字页、盖章的评审意见、拍照件；扫描的大幅面图纸也算扫描页。只有图像模型能翻。"),
    options: [
      { value: "image", label: imageLabel, description: t("各类页面均可处理，费用较高；大页面受像素限制，清晰度下降") },
      { value: "skip", label: keepLabel, description: t("不翻译，完成后列出来") },
    ],
    value: st.pdfAutoScanRoute,
    disabled: active,
    sub: true,
    onChange: (value) => void handlePdfRouteChange(st, "pdfAutoScanRoute", "pdf.auto_scan_route", value as SurfaceState["pdfAutoScanRoute"]),
  }));
  wrap.append(buildRouteRow({
    label: t("保底方式"),
    hint: t("程序选的方式在某一页用不了时改用它，比如没装「PDF 处理组件」、电脑上没有 Word、原位替换暂不支持目标语言（如阿拉伯文）。A3 及以上的大幅面页不会改用图像还原（会不够清晰），用不了就保留原样。换了方式的页完成后都会列出来。"),
    options: [{ value: "image", label: imageLabel }, { value: "skip", label: keepLabel }],
    value: st.pdfAutoFallback,
    disabled: active,
    sub: true,
    onChange: (value) => void handlePdfRouteChange(st, "pdfAutoFallback", "pdf.auto_fallback", value as SurfaceState["pdfAutoFallback"]),
  }));
  const sw = createSwitchRow({
    label: t("同时输出 Word"),
    hint: t("整份文件都转成 Word 翻译时，除了 PDF 再给一份可以修改的 Word。夹有扫描页、图纸或在原 PDF 上排版的页时不另出 Word。"),
    checked: st.pdfAlsoSaveWord,
    disabled: active,
    onChange: (checked) => void handlePdfRouteChange(st, "pdfAlsoSaveWord", "pdf.also_save_word", checked),
  });
  sw.classList.add("sub");
  wrap.append(sw);

  // 缺组件的提示右下角带一个「安装 ↗ 组件」，直接跳到「扩展 → 组件」并定位到那张卡片。
  const note = (text: string, warn = false, link?: string): HTMLElement => {
    const node = el("div", warn ? "route-note warn" : "route-note");
    node.textContent = text;
    if (link) {
      const go = el("button", "linklike route-note-link");
      go.type = "button";
      go.textContent = link;
      go.addEventListener("click", () => navigate("plugins", { focus: "pdf" }));
      node.append(go);
    }
    return node;
  };
  // 按本机条件写这次每类页实际会用的方式（规则见 core/pdf_routes.py page_route）。
  const caps = pdfRouteCaps;
  const complexTarget = !inplaceTargetSupported(st.targetLang);
  const textRoute = caps?.babeldoc_component && !complexTarget ? t("段落重排（不过检查的页转成 Word）") : t("转成 Word");
  wrap.append(note(t("可提取文字的页 → {0} · 图纸和大图 → 原位替换 · 扫描页 → {1}；用不了时{2}",
    textRoute,
    st.pdfAutoScanRoute === "image" ? imageLabel : keepLabel,
    st.pdfAutoFallback === "image" ? t("改用图像还原") : t("保留原样"),
  )));
  const usesImage = st.pdfAutoScanRoute === "image" || st.pdfAutoFallback === "image";
  if (caps && usesImage && !caps.image_model_ready) {
    wrap.append(note(t("没有可用的图像翻译模型：扫描页和需要保底的页会保留原样。"), true));
  }
  if (caps && !caps.word_component) {
    wrap.append(note(t("还没有安装「PDF 处理组件」：除扫描页外的页都改用保底方式，大幅面图纸保留原样。可在「扩展 → 组件」一键安装。"), true, t("安装 ↗ 组件")));
  } else if (caps) {
    if (caps.inplace_component === false) {
      wrap.append(note(t("「PDF 处理组件」里的 PyMuPDF 无法加载：原位替换的页改用保底方式，大幅面图纸保留原样。可在「扩展 → 组件」重新安装。"), true, t("重新安装 ↗ 组件")));
    }
    if (caps.babeldoc_component === false && !complexTarget) {
      wrap.append(note(t("装上「段落重排组件」后，可提取文字的页会先在原 PDF 上排版，公式、图和表格线原样保留。"), false, t("安装 ↗ 组件")));
    }
    if (!caps.office_engine) {
      wrap.append(note(t("电脑上没有 Microsoft Word 或 LibreOffice：要转成 Word 的页改用原位替换。"), true));
    }
  }
  if (complexTarget) {
    wrap.append(note(t("当前的目标语言需要连写排版（如阿拉伯文、泰文、印地文）：段落重排和原位替换用不了，可提取文字的页转成 Word，图纸改用保底方式。"), true));
  }
  if (!usesImage) wrap.append(note(t("这次不会调用图像模型。")));
  return wrap;
}

/** 当前目标语言能不能原位替换。名单来自后端（core/pdf_vector/fonts.py COMPLEX_SCRIPT_TARGETS）；
 *  还没取到时按能处理算，免得一闪而过的误报。 */
function inplaceTargetSupported(targetLang: string): boolean {
  const base = targetLang.trim().toLowerCase().replace("_", "-").split("-")[0];
  return !(pdfRouteCaps?.inplace_unsupported_targets ?? []).includes(base);
}
