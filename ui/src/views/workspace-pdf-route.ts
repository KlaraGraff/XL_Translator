/**
 * 工作台：PDF 页的「翻译方式」区（按页自动选择 / 整页图像翻译）和本机条件、所选文件页面类型的查询。
 */
import { type PdfPageKindCounts, type PdfRouteCapabilities } from "../api-client";
import { createDropdown, createHintBadge, createSwitchRow, type SelectFieldOption } from "../components";
import { openGuide } from "../guide";
import { t } from "../i18n";
import { navigate } from "../router";
import { el } from "./workspace-util";
import { type FileItem, type PdfRoute, type SurfaceState, client, handlePdfRouteChange, rerender } from "./workspace";

/** 本机条件：后端 /api/pdf/route-capabilities 给；查不到时不提示。每次进 PDF 页重查一次
 *  （在插件页装好组件、在设置里配好图像模型再回来，提示要跟着消失），重查期间沿用旧值。 */
let pdfRouteCaps: PdfRouteCapabilities | null = null;
let pdfRouteCapsLoading = false;
let pdfRouteCapsStale = true;

/** 所选 PDF 各有几页文字页、图纸、扫描页（后端 /api/pdf/page-kinds，按路径记）。null = 数不出来。 */
const pageKinds = new Map<string, PdfPageKindCounts | null>();
const pageKindsLoading = new Set<string>();

/** 下次进 PDF 页时重查本机条件和页面类型（文件可能在外面改过；后端按修改时间缓存，重查很快）。 */
export function markPdfRouteCapsStale(): void {
  pdfRouteCapsStale = true;
  pageKinds.clear();
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

function ensurePageKinds(files: FileItem[]): void {
  const missing = files
    .filter((file) => file.source_type !== "image")
    .map((file) => file.path)
    .filter((path) => !pageKinds.has(path) && !pageKindsLoading.has(path));
  if (!missing.length) return;
  for (const path of missing) pageKindsLoading.add(path);
  client.pdfPageKinds(missing)
    .then((counts) => {
      for (const path of missing) pageKinds.set(path, counts[path] ?? null);
      rerender("pdf");
    })
    .catch(() => undefined)
    .finally(() => {
      for (const path of missing) pageKindsLoading.delete(path);
    });
}

/** 所选文件合起来的页面类型；还在数时 loading 为 true。独立图片按扫描页算（交给图像模型）。 */
function selectedPageKinds(files: FileItem[]): { counts: PdfPageKindCounts; loading: boolean; unknown: number } {
  const counts: PdfPageKindCounts = { text: 0, drawing: 0, scan: 0 };
  let loading = false;
  let unknown = 0;
  for (const file of files) {
    if (file.source_type === "image") {
      counts.scan += 1;
      continue;
    }
    if (!pageKinds.has(file.path)) {
      loading = true;
      continue;
    }
    const found = pageKinds.get(file.path);
    if (!found) {
      unknown += 1;
      continue;
    }
    counts.text += found.text;
    counts.drawing += found.drawing;
    counts.scan += found.scan;
  }
  return { counts, loading, unknown };
}

function buildRouteRow(opts: {
  label: string;
  hint: string;
  /** 提示末尾的蓝色链接（「翻译方式」的「查看图文说明」）。 */
  hintAction?: { label: string; onClick: () => void };
  options: SelectFieldOption[];
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}): HTMLElement {
  const row = el("div", "selrow");
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
  dropdown.root.classList.add("wide");
  dropdown.input.setAttribute("aria-label", opts.label);
  row.append(dropdown.root);
  return row;
}

// 缺组件的提示右下角带一个「安装 ↗ 组件」，直接跳到「扩展 → 组件」并定位到那张卡片。
function note(text: string, warn = false, link?: { label: string; onClick: () => void }): HTMLElement {
  const node = el("div", warn ? "route-note warn" : "route-note");
  node.textContent = text;
  if (link) {
    const go = el("button", "linklike route-note-link");
    go.type = "button";
    go.textContent = link.label;
    go.addEventListener("click", link.onClick);
    node.append(go);
  }
  return node;
}

const installComponents = (label: string) => ({ label, onClick: () => navigate("plugins", { focus: "pdf" }) });

export function buildPdfRouteSection(st: SurfaceState, active: boolean): HTMLElement {
  // 紧接在上面的开关后面，不另起小标题：小标题和下面这一行的名称都会是「翻译方式」。
  // 只让用户选两种（maple 2026-11-28）：按页自动选择由程序逐页选方式、用不了时自己选保底，
  // 没有子选项（core/pdf_routes.py page_route / fallback_route）；整页图像翻译每页交给图像模型。
  const wrap = el("div", "route-block");
  wrap.append(buildRouteRow({
    label: t("翻译方式"),
    // 细节交给图文指引（guide.ts），问号里只留一句话。
    hint: t("按页自动选择：程序看每一页是什么、选合适的方式，最后拼回一份 PDF。全部用图像翻译：每页交给图像模型重新生成。"),
    hintAction: { label: t("查看图文说明"), onClick: () => openGuide("pdf") },
    options: [
      { value: "auto", label: t("按页自动选择"), tag: t("推荐"), description: t("文字和表格重新排版，图纸只换文字，扫描页用图像翻译，最后拼回一份 PDF") },
      { value: "image", label: t("全部用图像翻译"), description: t("每一页都交给图像模型重新生成，适合整份扫描件和照片；费用较高") },
    ],
    value: st.pdfRoute,
    disabled: active,
    onChange: (value) => void handlePdfRouteChange(st, "pdfRoute", "pdf.translation_route", value as PdfRoute),
  }));
  if (st.pdfRoute !== "auto") return wrap;

  ensurePdfRouteCaps();
  const files = st.files.filter((file) => st.selected.has(file.path));
  ensurePageKinds(files);
  wrap.append(buildPagePlan(st, files));

  const sw = createSwitchRow({
    label: t("同时输出 Word"),
    hint: t("除了 PDF 再给一份可以修改的 Word。文件里有扫描页、图纸，或文字页直接在原 PDF 上排版（装了「段落重排组件」）时不另出 Word。"),
    checked: st.pdfAlsoSaveWord,
    disabled: active,
    onChange: (checked) => void handlePdfRouteChange(st, "pdfAlsoSaveWord", "pdf.also_save_word", checked),
  });
  sw.classList.add("sub");
  wrap.append(sw);
  return wrap;
}

/** 「这批文件逐页怎么处理」：每类页一行（有几页、用什么），下面是本机条件不够时的后果。
 *  规则同 core/pdf_routes.py page_route；没选文件时只写规则。 */
function buildPagePlan(st: SurfaceState, files: FileItem[]): HTMLElement {
  const caps = pdfRouteCaps;
  const complexTarget = !inplaceTargetSupported(st.targetLang);
  const imageReady = caps?.image_model_ready !== false;
  const { counts, loading, unknown } = selectedPageKinds(files);
  const known = files.length > 0 && !loading && unknown < files.length;

  const plan = el("div", "route-plan");
  const head = el("div", "route-plan-head");
  head.textContent = !files.length ? t("程序逐页这样选") : loading ? t("正在看所选文件的页面…") : t("所选文件逐页这样处理");
  plan.append(head);

  // 本机条件不够时，这一行直接写程序会换成的方式（同 runner 的保底：能用图像模型就整页图像翻译，
  // 大幅面页和没有图像模型时原样保留），橙色标出；下面的提示再说为什么、怎么补。
  const component = caps?.word_component !== false;
  const inplaceOk = component && caps?.inplace_component !== false && !complexTarget;
  const fallback = { route: imageReady ? t("图像翻译") : t("原样保留"), warn: true };
  const drawingFallback = { route: imageReady ? t("图像翻译，大幅面原样保留") : t("原样保留"), warn: true };
  const text = !component ? fallback
    : caps?.babeldoc_component && !complexTarget ? { route: t("重新排版"), warn: false }
      : caps?.office_engine !== "" ? { route: t("重新排版"), warn: false }
        : inplaceOk ? { route: t("原位替换"), warn: true } : fallback;
  const rows: { kind: keyof PdfPageKindCounts; name: string; route: string; warn: boolean }[] = [
    { kind: "text", name: t("文字和表格页"), ...text },
    { kind: "drawing", name: t("图纸和大图"), ...(inplaceOk ? { route: t("原位替换"), warn: false } : drawingFallback) },
    { kind: "scan", name: t("扫描页"), route: imageReady ? t("图像翻译") : t("原样保留"), warn: !imageReady },
  ];
  for (const row of rows) {
    if (known && counts[row.kind] === 0) continue;
    const name = el("span", "route-plan-kind");
    name.textContent = known ? t("{0} · {1} 页", row.name, counts[row.kind]) : row.name;
    const route = el("span", row.warn ? "route-plan-route warn" : "route-plan-route");
    route.textContent = row.route;
    plan.append(name, route);
  }
  if (known && counts.text + counts.drawing + counts.scan === 0) {
    const empty = el("span", "route-plan-kind");
    empty.textContent = t("全是空白页，原样保留");
    plan.append(empty);
  }
  const foot = el("div", "route-plan-foot");
  foot.textContent = t("某一页用不了这种方式时，程序自动换一种，完成后单独列出。");
  plan.append(foot);

  const notes: HTMLElement[] = [];
  const scanPages = known ? counts.scan : -1;
  if (caps && !caps.image_model_ready && scanPages !== 0) {
    notes.push(note(scanPages > 0
      ? t("没有可用的图像翻译模型：{0} 页扫描页会原样保留，其他页照常翻译。", scanPages)
      : t("没有可用的图像翻译模型：扫描页会原样保留，其他页照常翻译。"), true,
    { label: t("去设置 ↗"), onClick: () => navigate("settings", { page: "models", role: "image" }) }));
  }
  if (caps && !caps.word_component) {
    notes.push(note(t("还没有安装「PDF 处理组件」：文字页和图纸改用图像翻译，没有图像模型或页面太大时原样保留。"), true, installComponents(t("安装 ↗ 组件"))));
  } else if (caps) {
    if (caps.inplace_component === false) {
      notes.push(note(t("「PDF 处理组件」里的 PyMuPDF 无法加载：图纸改用图像翻译，大幅面图纸原样保留。"), true, installComponents(t("重新安装 ↗ 组件"))));
    }
    if (!caps.office_engine && !(caps.babeldoc_component && !complexTarget)) {
      notes.push(note(t("电脑上没有 Microsoft Word 或 LibreOffice：文字页改用原位替换。"), true));
    }
    if (caps.babeldoc_component === false && !complexTarget) {
      notes.push(note(t("装上「段落重排组件」后，文字页直接在原 PDF 上排版，更接近原文。"), false,
        { label: t("安装 ↗ 组件"), onClick: () => navigate("plugins", { focus: "babeldoc" }) }));
    }
  }
  if (complexTarget) {
    notes.push(note(t("当前的目标语言需要连写排版（如阿拉伯文、泰文、印地文）：图纸改用图像翻译。"), true));
  }
  if (known && unknown === 0 && counts.scan === 0) notes.push(note(t("这批文件不会用到图像模型。")));
  const block = el("div");
  block.append(plan, ...notes);
  return block;
}

/** 当前目标语言能不能原位替换。名单来自后端（core/pdf_vector/fonts.py COMPLEX_SCRIPT_TARGETS）；
 *  还没取到时按能处理算，免得一闪而过的误报。 */
function inplaceTargetSupported(targetLang: string): boolean {
  const base = targetLang.trim().toLowerCase().replace("_", "-").split("-")[0];
  return !(pdfRouteCaps?.inplace_unsupported_targets ?? []).includes(base);
}
