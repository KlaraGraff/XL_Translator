// V9 翻译工作区 —— Excel / Word / PDF / CAD 四个视图共用的布局模板 + 任务生命周期状态机。
// 四页「同构」：col-l/col-r 的板块顺序、位置、命名完全一致，只有内容（开关列表、统计
// 字段、文件表列）按 surface 不同。excel.ts / word.ts / pdf.ts / cad.ts 只是薄封装，
// 具体渲染、扫描、启动/停止/暂停、SSE 订阅、设置持久化全部在这里实现一次。
//
// 新增一个翻译板块时，不要另写页面：把它加进 Surface，补齐下面各张按 surface 索引的表
// （TS 会把漏掉的表全部报出来），再在 computeStats / buildTableHeadRow / buildTableRow /
// buildPayload 里补它自己的那一支。布局、文案、扫描→预检→开始→监听→收尾的流程都自动一致。
//
// 状态机（每个 surface 独立一份，模块级 Map 持久化，跨 mount/unmount 保留）：
//   setup（未扫描 / 已扫描待开始） --scan--> setup（文件已填充）
//   setup --start（可能经 xls/doc 兼容确认、并行风险确认）--> running
//   running --stop--> running（等终态事件）--终态事件--> setup（横幅显示）
//   running --pause（PDF；CAD 在下一批请求之前暂停）--> paused
//   paused --resume（PDF / CAD）--> running
//   paused --end-paused（仅 PDF）--> running（等终态事件，同上落回 setup+横幅）
// 「right column 运行时锁定」「完成横幅 + 复位」等细节见下方实现与文件尾的架构说明。

import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";

import { ApiClient, apiErrorReason, type PdfPagesSnapshot, type SseEvent, type TaskStatus } from "../api-client";
import { consumePendingRestartWarning } from "../update-controller";
import { openRestartBeforeTaskModal } from "../update-toast";
import {
  createButton,
  createChip,
  createEmptyState,
  createField,
  createFold,
  createLanguageField,
  createProgressBar,
  createDropdown,
  createSelectField,
  createSwitchRow,
  createTextField,
  closeLanguagePopover,
  closeMenu,
  hideHint,
  openMenu,
  openModal,
  showToast,
  type ChipTone,
  type LanguageOption,
  type ModalAction,
  type ModalHandle,
  type StatusTone,
} from "../components";
import { icon, type IconName } from "../icons";
import { navigate, type ViewParams } from "../router";
import { setTopbar } from "../shell";
import { stopConfirmBody, taskStateWord } from "../task-state-labels";
// 任务中心是活动任务徽标的权威来源；新任务刚提交时要主动通知它，别等它自己巡检。
import { noteTaskStarted } from "./tasks";
import { fileProgressLabel, fileProgressTone, reduceFileProgress, type FileProgress } from "../file-progress";
import { createTaskControlFoot, createTaskListPanel, createTaskLogPanel, createTaskProgressPanel, createTaskTerminalBanner } from "../task-panels";
import { normalizeUserPath } from "../path-input";

import {
  buildPdfRecoveryCard,
  buildPdfReviewCard,
  fetchPdfPagesSnapshot,
  schedulePdfSnapshotRefresh,
  stopPdfRerunTicker,
} from "./workspace-pdf-pages";
import { el, formatSizeKb, formatTime, hasOwn, nestedPatch, num, record, redactedText, text, type JsonObject } from "./workspace-util";
import { currentLocale, fmtNum, t, tx, localized } from "../i18n";
import "./workspace.css";

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------

export type Surface = "excel" | "word" | "pdf" | "cad";


/** 扫描接口返回的文件条目；字段是后端各 surface 联合，具体用到哪些看 surface。 */
type FileItem = JsonObject & {
  path: string;
  file_id?: string;
  name?: string;
  relative_path?: string;
  size_kb?: number;
  format?: string;
  sheet_count?: number;
  sheets?: string[];
  text_cell_count?: number;
  paragraph_count?: number;
  table_count?: number;
  page_count?: number;
  /** PDF「大幅面页」计数（两个方向都超过 A4 约 15%）。null 是后端明确表达的「这个文件数不出来」
   *  （加密、结构损坏），不是 0；字段缺失表示这个 surface / 这版后端不产出。三态含义见
   *  oversizedPageStats() 上方的整段说明，处理方式与 Excel 的 image_count 完全一致。 */
  oversized_page_count?: number | null;
  source_type?: string;
  /** CAD：图纸里的文字实体总数 / 其中需要翻译的候选数（已是目标语言的不算）。
   *  DWG 扫描时不转换（转换工具一张图要几秒到几分钟），这两个数是 null、counts_pending
   *  为 true：开始翻译后由任务转换并统计，界面写「开始后统计」，不写 0。 */
  text_count?: number | null;
  candidate_count?: number | null;
  counts_pending?: boolean;
  needs_conversion?: boolean;
  // 以下几个是 Excel 的「单元格外内容」计数，见下方 outsideCellStats() 的整段说明。
  // null 是后端明确表达的「这个文件数不出来」，不是 0；字段缺失则是「这一版后端不产出」。
  image_count?: number | null;
  shape_text_count?: number | null;
  comment_count?: number | null;
  anchor_frozen_count?: number;
};

type ScanSkippedItem = { path?: string; relative_path?: string; name?: string; reason?: string };

// --- 续译：扫描响应里的 previous_output ------------------------------------
// 后端在源目录旁（或自定义输出根目录下）找 `{文件夹名}_翻译输出_{时间戳}`，按当前目标
// 语言的产物命名规则逐个文件比对。没有命中时整个字段是 null，前端一个字都不提。

type ResumeFileStatus = "full" | "partial" | "none" | "found" | "diverged";

type ResumeCandidate = { dir: string; label: string; timestamp: string };

type ResumeFileInfo = {
  /** 与 items[].path 对应的源文件绝对路径，逐行标注靠它连接。 */
  path: string;
  status: ResumeFileStatus;
  matched_output: string | null;
  /** excel = 待补格数，word = 待补段数；PDF 不给这个字段，用 page_done / page_total。 */
  untranslated_count: number | null;
  page_done: number | null;
  page_total: number | null;
};

type ResumeSummary = {
  full: number;
  partial: number;
  none: number;
  /** 找到产物但判定不出完整度：按「已有译文」展示，但不算进「上次已全部翻完」。 */
  found: number;
  /** 源文件在上次翻译后有改动：底稿当不了续译起点，续译时该文件整份重翻。 */
  diverged: number;
  page_done: number | null;
  page_total: number | null;
  last_task_stopped: boolean | null;
  last_time_label: string | null;
};

interface PreviousOutput {
  target_lang: string;
  candidates: ResumeCandidate[];
  selected_dir: string | null;
  files: ResumeFileInfo[];
  summary: ResumeSummary;
}

type TogglePathKind = "none" | "flat" | "output";

interface ToggleDef {
  key: string;
  label: string;
  hint: string;
  default: boolean;
  pathKind: TogglePathKind;
  /** flat: 完整点路径（如 "excel_review.mark_review_items"）；output: 只给 key 本身，前缀由 outputSettingPath() 解析。 */
  path?: string;
  /** 与另一个开关互斥：本开关打开时另一个自动关闭，反之亦然（Excel 的「用 Excel 调整行高」/「保持行高，缩小字号」）。 */
  exclusiveWith?: string;
}

const EXCEL_TOGGLES: ToggleDef[] = localized(() => ([
  { key: "untranslated", label: t("仅补译未翻译内容"), hint: t("仅翻译尚无译文的内容，已翻译部分保持不变。"), default: false, pathKind: "none" },
  { key: "translationOnly", label: t("仅显示译文"), hint: t("开启后工作表中仅保留译文。是否另存原文，由下方「另存原文工作表」决定。"), default: false, pathKind: "output", path: "output_translation_only" },
  { key: "translateOutputFilename", label: t("翻译文件名"), hint: t("将输出文件名翻译为目标语言；关闭时沿用原文件名。"), default: false, pathKind: "output", path: "translate_output_filename" },
  { key: "translateSheetNames", label: t("翻译工作表名称"), hint: t("同时翻译工作表标签名，并同步更新公式中引用的表名；无法安全重命名的工作表保留原名。"), default: true, pathKind: "output", path: "translate_sheet_names" },
  { key: "keepOriginal", label: t("另存原文工作表"), hint: t("在每张工作表旁保留一份未翻译的原文副本，表名后加「_原文」。"), default: true, pathKind: "output", path: "keep_original_sheets" },
  { key: "formulaBackfill", label: t("翻译公式单元格"), hint: t("公式单元格按当前显示内容写为双语文本，此后不再自动计算。关闭时公式单元格保持原样，不予翻译。"), default: true, pathKind: "output", path: "formula_display_value_backfill" },
  { key: "excelAutofit", label: t("用 Excel 调整行高"), hint: t("需要本机安装 Excel。开启后由 Excel 按译文重新计算行高，结果更准确；Excel 不可用时保留估算行高，并在结果中提示。"), default: false, pathKind: "output", path: "enable_excel_autofit", exclusiveWith: "lockRowHeight" },
  { key: "lockRowHeight", label: t("保持行高，缩小字号"), hint: t("保持行高不变，译文超出时缩小字号；缩至最小字号仍无法容纳的单元格标记为需复核。不能与「用 Excel 调整行高」同时使用。"), default: false, pathKind: "output", path: "lock_row_height", exclusiveWith: "excelAutofit" },
  { key: "reviewMark", label: t("标记需复核内容"), hint: t("为保留原文或原文疑似夹杂错误外文的单元格添加底色，便于人工检查。已确认无问题的内容仅记入报告，不添加底色。"), default: true, pathKind: "flat", path: "excel_review.mark_review_items" },
]));

const WORD_TOGGLES: ToggleDef[] = localized(() => ([
  { key: "untranslated", label: t("仅补译未翻译内容"), hint: t("仅翻译尚无译文的内容，已翻译部分保持不变。"), default: false, pathKind: "none" },
  { key: "translationOnly", label: t("仅显示译文"), hint: t("开启后输出文件只保留译文；关闭时保留原文与译文。"), default: false, pathKind: "output", path: "output_translation_only" },
  { key: "translateOutputFilename", label: t("翻译文件名"), hint: t("将输出文件名翻译为目标语言；关闭时沿用原文件名。"), default: false, pathKind: "output", path: "translate_output_filename" },
  { key: "wordNativePreprocessing", label: t("用 Word / LibreOffice 处理编号"), hint: t("翻译前将自动编号（如 1.1、（一））转换为普通文本，以免译后编号错乱。开启时优先使用本机的 Word 或 LibreOffice，效果更准确；两者均未安装时自动改用内置方式。关闭时始终使用内置方式。仅处理临时副本，源文件保持不变。"), default: true, pathKind: "flat", path: "word_conversion.use_native_preprocessing" },
  { key: "wordHighlight", label: t("标记需复核内容"), hint: t("为保留原文或质量校验未通过的段落加高亮，便于人工复核。"), default: true, pathKind: "flat", path: "word_review.highlight_unresolved" },
  { key: "protectFrontMatter", label: t("保护封面和目录"), hint: t("从文档开头到正文第一个章节标题之前的内容（封面、批准页、目录、前言）不翻译。章节标题按「第一章」「1 概述」「1.1」「（一）」和 Word 标题样式识别；未找到正文起点时不启用，并在日志中说明。"), default: true, pathKind: "none" },
  { key: "translateHeadersFooters", label: t("翻译页眉页脚"), hint: t("开启后同时翻译页眉页脚，译文以「 / 」接在原文同一行，以免页眉增高导致正文下移。页码和目录类页眉页脚不翻译。"), default: false, pathKind: "none" },
]));

const PDF_TOGGLES: ToggleDef[] = localized(() => ([
  { key: "translateOutputFilename", label: t("翻译文件名"), hint: t("将输出文件名翻译为目标语言；关闭时沿用原文件名。"), default: false, pathKind: "output", path: "translate_output_filename" },
  { key: "pdfReview", label: t("逐页审核"), hint: t("每页翻译完成后由审核模型检查版式和文字是否完整，并提示复核有问题的页面。每页将额外调用一次模型，产生额外费用。"), default: false, pathKind: "flat", path: "pdf.review_enabled" },
  { key: "pdfCompressed", label: t("生成压缩 PDF"), hint: t("在常规输出之外，另生成一份体积更小的 PDF。"), default: true, pathKind: "flat", path: "pdf.generate_compressed_pdf" },
  { key: "pdfImages", label: t("也翻译图片文件"), hint: t("开启后，扫描时将 PNG、JPG、WebP、BMP、TIFF 图片一并纳入翻译；关闭时仅处理 PDF。"), default: true, pathKind: "flat", path: "pdf.include_images" },
  // 判定条件只看纸张尺寸，和内容是不是图纸无关，所以开关名必须是尺寸口径——叫「跳过图纸」
  // 会让人以为程序在识别图纸内容，遇到 A3 的宣传册被跳过时只会当成程序出错。
  { key: "skipOversizedPages", label: t("跳过 A3 及更大的页面"), hint: t("长、宽均超出 A4 约 15% 以上的页面（A3 及更大，横向、纵向均适用）不翻译，按原样写入输出文件，清晰度不变。工程图纸常用此类幅面。略大于 A4 的页面（如带扫描白边）不受影响。"), default: true, pathKind: "flat", path: "pdf.skip_oversized_pages" },
]));

// CAD 的开关全部写在 cad_output 下（outputSettingPathPrefix 解析到 cad_output）。
// 回读校验、乱码扫描、实体数核对、残留外文扫描是质量保护，始终开启，不做成开关。
const CAD_TOGGLES: ToggleDef[] = localized(() => ([
  { key: "translateOutputFilename", label: t("翻译文件名"), hint: t("将输出文件名翻译为目标语言；关闭时沿用原文件名。"), default: false, pathKind: "output", path: "translate_output_filename" },
  { key: "cadBlockText", label: t("翻译图块内文字"), hint: t("同时翻译图块中的文字（含属性文字）；关闭时仅翻译直接绘制在图纸上的文字。"), default: true, pathKind: "output", path: "include_block_text" },
  { key: "cadKeepWorkDxf", label: t("另存一份 DXF"), hint: t("在输出图纸之外，另存一份翻译后的 DXF，便于在其他软件中核对。"), default: false, pathKind: "output", path: "keep_work_dxf" },
  { key: "cadCopyRelated", label: t("复制同名附属文件"), hint: t("把与图纸同名的其他文件（如 .ctb、.pdf 说明）一并复制到输出目录。"), default: false, pathKind: "output", path: "copy_related_files" },
]));

const TOGGLES: Record<Surface, ToggleDef[]> = { excel: EXCEL_TOGGLES, word: WORD_TOGGLES, pdf: PDF_TOGGLES, cad: CAD_TOGGLES };

export const SURFACE_LABEL: Record<Surface, string> = { excel: "Excel", word: "Word", pdf: "PDF", cad: "CAD" };
const SURFACE_ICON: Record<Surface, IconName> = { excel: "excel", word: "word", pdf: "pdf", cad: "cad" };
const SURFACE_PAGE_TITLE: Record<Surface, string> = localized(() => ({ excel: t("Excel 表格翻译"), word: t("Word 文档翻译"), pdf: t("PDF 与图片翻译"), cad: t("CAD 图纸翻译") }));
const SURFACE_FILE_NOUN: Record<Surface, string> = localized(() => ({ excel: t("表格文件"), word: t("文档"), pdf: t("PDF / 图片文件"), cad: t("DWG / DXF 图纸") }));

/** 「5 个 PDF / 图片文件」——名词以西文字母开头时补一个空格，否则会挤成「5 个PDF」。 */
function fileNounPhrase(surface: Surface, count: number): string {
  switch (surface) {
    case "excel": return t("{0} 个表格文件", count);
    case "word": return t("{0} 个文档", count);
    case "pdf": return t("{0} 个 PDF / 图片文件", count);
    default: return t("{0} 个 DWG / DXF 图纸", count);
  }
}
const SURFACE_EMPTY_SUBTITLE: Record<Surface, string> = localized(() => ({
  excel: t("选择文件或文件夹，扫描后勾选要翻译的表格"),
  word: t("选择文件或文件夹，扫描后勾选要翻译的文档"),
  pdf: t("选择文件或文件夹，扫描后勾选要翻译的 PDF 与图片"),
  cad: t("选择文件或文件夹，扫描后勾选要翻译的 DWG / DXF 图纸"),
}));
const SURFACE_FIRST_EMPTY: Record<Surface, { title: string; description: string }> = localized(() => ({
  excel: { title: t("尚无可翻译的文件"), description: t("通过「浏览」选择文件或文件夹后将自动扫描，找到的表格将列于此处，可逐个勾选。") },
  word: { title: t("尚无可翻译的文件"), description: t("通过「浏览」选择文件或文件夹后将自动扫描，找到的文档将列于此处，可逐个勾选。") },
  pdf: { title: t("尚无可翻译的文件"), description: t("通过「浏览」选择文件或文件夹后将自动扫描，找到的 PDF 与图片将列于此处，可逐个勾选。") },
  cad: { title: t("尚无可翻译的文件"), description: t("通过「浏览」选择文件或文件夹后将自动扫描，找到的 DWG / DXF 图纸将列于此处，可逐个勾选。") },
}));
const SURFACE_BANNER_EMPTY: Record<Surface, { title: string; description: string }> = localized(() => ({
  excel: { title: t("可开始下一批"), description: t("上一任务的结果已归档，选择新来源即可继续。") },
  word: { title: t("可开始下一批"), description: t("上一任务的结果已归档，选择新来源即可继续。") },
  pdf: { title: t("可开始下一批"), description: t("上一任务的结果已归档，选择新来源即可继续。") },
  cad: { title: t("可开始下一批"), description: t("上一任务的结果已归档，选择新来源即可继续。") },
}));

// ---------------------------------------------------------------------------
// 每个 surface 的本地状态（模块级，跨 mount/unmount 存活，与 tasks.ts 的后台徽标循环
// 各自独立——见文件尾「架构偏离说明」）。
// ---------------------------------------------------------------------------

export interface LocalTask {
  task: TaskStatus;
  /** seq 是全局单调递增的行号，只用来在整页重建后认出「重建前顶在视口口沿的是哪一行」
   *  （见 captureLogScroll / restoreLogScroll）。数组索引不行——满 200 条后每来一条
   *  就从头部丢一条，同一条日志的索引会一直变小。 */
  logs: Array<{ seq: number; time: string; level: string; message: string }>;
  /** 进度卡标题：这一阶段里具体在做什么（状态句，或 CAD 进度事件的后半句）。 */
  phaseName: string;
  /** 当前阶段名（读取文件 / 翻译 / 调整行高 / 生成文件），进度卡标题前的小标签。 */
  stageName: string;
  percent: number;
  /** 当前阶段序号 / 总阶段数（progress 事件的 phase_index / phase_total）。只用来把
   *  阶段内进度折算成整条进度；进度卡上不再写「阶段 2 / 4」，各页阶段总数不一样。 */
  phaseIndex: number;
  phaseTotal: number;
  streamState: "connected" | "reconnecting";
  watcherActive: boolean;
  /** 最后一次收到任何事件的时刻。一批请求发出去到回来之间引擎不说话，界面得自己
   *  报一句「还在等」，否则十几秒没动静看起来和卡死一模一样。 */
  lastEventAt: number;
  fileProgress: Map<string, FileProgress>;
  wordRecovery?: JsonObject;
  pdfPageRecovery?: JsonObject;
  pdfReview?: JsonObject;
  /** 逐页拉取的快照（GET /pdf-pages）。没有逐页 SSE——收到聚合事件或任务状态变化时重新拉取一次，
   *  见 fetchPdfPagesSnapshot() 与它的调用点清单。终态后不清空，保留最后一次结果供「查看对比」使用。 */
  pdfPagesSnapshot?: PdfPagesSnapshot;
  pdfPagesUnavailable?: boolean;
}

/** 结果的三种口气：全部产出、产出了但有事要说、没能产出。 */
type ResultTone = "ok" | "warn" | "fail";

interface BannerInfo {
  title: string;
  subtitle: string;
  tone: ResultTone;
  /** 顶栏那颗徽章的字。和横幅同一份判断，不再靠对副标题做正则猜结论。 */
  statusLabel: string;
  /** 没有任何产出时不给「打开输出目录」——那个目录里没有用户要的东西。 */
  hasOutput: boolean;
}

interface FileOutcome {
  produced: boolean;
  label: string;
  detail: string;
  /** 不给就按 produced 决定（绿 / 红）。停止后只翻了一部分的文件给 warn。 */
  tone?: "warn";
}

interface SurfaceState {
  sourcePath: string;
  /** 「浏览 → 选择文件」一次挑了多个文件时的原始清单；sourcePath 存它们共同的上级
   *  目录（任务的 source_path 要能被后端重扫，selected_paths 再收窄到这几个文件）。
   *  手输路径、选文件夹、只选一个文件时都是空数组。 */
  sourcePaths: string[];
  files: FileItem[];
  skipped: ScanSkippedItem[];
  scanSummary: JsonObject;
  selected: Set<string>;
  toggles: Map<string, boolean>;
  targetLang: string;
  sourceLang: string;
  domainPreset: string;
  useCustomOutputDir: boolean;
  customOutputDir: string;
  task: LocalTask | null;
  /** Older persisted tasks omit source paths; their results remain viewable but cannot start a new run. */
  historyOnly: boolean;
  hasEverCompleted: boolean;
  showBanner: boolean;
  bannerInfo: BannerInfo | null;
  /** 上一次任务的逐文件结果，键是文件名（扫描项的 `name` 与 file_results[].name 同源，
   *  都来自 FileItem.name）。任务结束后清单不再清空，这份结果用来在「状态」列上标出
   *  哪几个文件真的产出了、哪几个没有。
   *
   *  绝对不能用 source_path 做键：它在 api/task_manager.py 的隐私过滤里被置空，
   *  经过 API 回来永远是 null，映射会是空的（9.2.6 就是这么错的）。 */
  fileOutcomes: Map<string, FileOutcome>;
  /** Excel 完成汇总里那句「哪些内容没被翻译」。finishTask 会清空 st.files，
   *  所以必须在清空前把要说的数据快照下来，跟着完成横幅一起展示。 */
  excelDoneNotice: ExcelDoneNotice | null;
  allowXlsFallback: boolean;
  allowDocFallback: boolean;
  /** 本机是否装了 LibreOffice（扫描时探测一次）。只用来切换兼容转换弹窗的文案——
   *  装了之后兼容转换也能保留公式/样式，不再是「必然变纯数值」。 */
  libreofficeAvailable: boolean;
  /** 「更多设置」折叠区的开合。每次开关变化都会整块重绘右栏，不记下来的话用户刚收起就被弹回展开。 */
  moreSettingsOpen: boolean;
  /** 本次扫描检测到的历史产物；没有检测结果时为 null。每次扫描整体重置。 */
  resumeInfo: PreviousOutput | null;
  /** 用户对这份检测结果的决定。null = 弹窗还没弹或还没回应。 */
  resumeDecision: "resume" | "fresh" | null;
  /** 检测发生时的目标语言。产物文件名里带语言名，换了目标语言这份检测就对不上号了——
   *  与当前 targetLang 不一致时整套续译状态作废（见 resumeState()）。 */
  resumeDetectedLang: string;
  /** 提示条上的「×」：本次扫描内不再出现这条提示（重新弹窗也不恢复）。 */
  resumeNoticeDismissed: boolean;
  /** 进入续译前「仅补译未翻译内容」开关的原值，「改为全部重译」时还原回去。 */
  resumeToggleRestore: boolean | null;
  renderer: (() => void) | null;
  lastTaskId?: string;
  lastOutputPath?: string;
  /** CAD：图纸转换工具（ODA）的连接状态，挂载时和每次扫描时刷新。DWG 必须先转成 DXF
   *  才能读写，没连上时 DXF 照常可翻，DWG 在清单里标「需连接转换工具」。 */
  cadCapability: JsonObject | null;
  /** CAD：「从断点继续」选中的上一次输出目录。CAD 的续译不经过扫描检测，而是从
   *  已停止任务的完成横幅进入；开始翻译时作为 resume_output_dir 发出，任务开始后清空。 */
  cadResumeDir: string;
}

function freshToggles(surface: Surface): Map<string, boolean> {
  const map = new Map<string, boolean>();
  for (const toggle of TOGGLES[surface]) map.set(toggle.key, toggle.default);
  return map;
}

function freshState(surface: Surface): SurfaceState {
  return {
    sourcePath: "",
    sourcePaths: [],
    files: [],
    skipped: [],
    scanSummary: {},
    selected: new Set(),
    toggles: freshToggles(surface),
    targetLang: surface === "pdf" || surface === "cad" ? "zh" : surface === "word" ? "fr" : "en",
    sourceLang: "auto",
    domainPreset: surface === "excel" ? "无" : "同步工程场景", // i18n-ignore 发给后端的专业领域预设值
    useCustomOutputDir: false,
    customOutputDir: "",
    task: null,
    historyOnly: false,
    hasEverCompleted: false,
    showBanner: false,
    bannerInfo: null,
    fileOutcomes: new Map(),
    excelDoneNotice: null,
    allowXlsFallback: false,
    allowDocFallback: false,
    libreofficeAvailable: false,
    moreSettingsOpen: true,
    resumeInfo: null,
    resumeDecision: null,
    resumeDetectedLang: "",
    resumeNoticeDismissed: false,
    resumeToggleRestore: null,
    renderer: null,
    cadCapability: null,
    cadResumeDir: "",
  };
}

export const states: Record<Surface, SurfaceState> = {
  excel: freshState("excel"),
  word: freshState("word"),
  pdf: freshState("pdf"),
  cad: freshState("cad"),
};

type WorkspaceContext = Pick<SurfaceState, "sourcePath" | "sourcePaths" | "files" | "skipped" | "scanSummary" | "selected">;
// Source paths stay in this window, outside sanitized task history/diagnostics.
const taskContexts = new Map<string, WorkspaceContext>();

function rememberTaskContext(st: SurfaceState, taskId: string): void {
  if (st.historyOnly || !st.sourcePath || !st.files.length) return;
  taskContexts.set(taskId, {
    sourcePath: st.sourcePath,
    sourcePaths: [...st.sourcePaths],
    files: [...st.files],
    skipped: [...st.skipped],
    scanSummary: { ...st.scanSummary },
    selected: new Set(st.selected),
  });
  if (taskContexts.size > 200) taskContexts.delete(taskContexts.keys().next().value!);
}

/** New input must not reuse a completed task's progress, pages, banner or results. */
function clearWorkspaceInput(surface: Surface, invalidate = true): void {
  const st = states[surface];
  if (invalidate) ++mountTokens[surface];
  ++scanTokens[surface];
  scanBusy[surface] = false;
  stopSilenceTicker(surface);
  stopPdfRerunTicker(surface);
  st.task = null;
  st.files = [];
  st.skipped = [];
  st.scanSummary = {};
  st.selected = new Set();
  st.fileOutcomes = new Map();
  st.showBanner = false;
  st.bannerInfo = null;
  st.excelDoneNotice = null;
  st.lastTaskId = undefined;
  st.lastOutputPath = undefined;
  st.historyOnly = false;
  st.cadResumeDir = "";
  resetResumeState(st);
}

/**
 * 来源路径只记在本窗口内存里（任务中心和历史记录不带路径）。窗口重载后接管一个还在
 * 跑或暂停着的任务，内存里的那份没了：以前来源栏是空的，还提示「未保留原始来源路径」，
 * 跑完想再翻一次得重新选。后端还握着这个任务时，向它要回来源，再按原路径静默重扫一遍
 * 把清单和统计补齐，勾选只保留这个任务实际处理的那几份。拿不到就照旧按历史任务显示。
 */
async function recoverTaskContext(surface: Surface, taskId: string): Promise<WorkspaceContext | undefined> {
  const st = states[surface];
  try {
    const c = await getClient();
    const source = await c.request<{ source_path?: string; files?: { path?: string }[] }>(
      `/api/tasks/${encodeURIComponent(taskId)}/source`,
    );
    const path = normalizeUserPath(text(source.source_path));
    if (!path) return undefined;
    const taskFiles = new Set((source.files ?? []).map((item) => normalizeUserPath(text(item.path))).filter(Boolean));
    const scanState = { ...st, sourcePaths: [] } as SurfaceState;
    const response = await c.request<JsonObject>("/api/sources/scan", {
      method: "POST",
      body: JSON.stringify(scanPayload(surface, scanState, path)),
    });
    const result = record(response.result) && Object.keys(record(response.result)).length ? record(response.result) : response;
    const items = Array.isArray(result.items) ? (result.items as FileItem[]) : [];
    if (!items.length) return undefined;
    const selected = items.filter((item) => !taskFiles.size || taskFiles.has(normalizeUserPath(item.path)));
    const context: WorkspaceContext = {
      sourcePath: path,
      sourcePaths: [],
      files: items,
      skipped: Array.isArray(result.skipped) ? (result.skipped as ScanSkippedItem[]) : [],
      scanSummary: record(result.summary),
      selected: new Set((selected.length ? selected : items).map((item) => item.path)),
    };
    taskContexts.set(taskId, context);
    return context;
  } catch {
    // 任务早已不在内存里（后端重启过、记录被回收）或来源已被移走：按历史任务显示即可。
    return undefined;
  }
}

async function openWorkspaceTask(surface: Surface, taskId: string, isCurrent: () => boolean): Promise<void> {
  // Hide the previous task immediately, including on lookup failure.
  const st = states[surface];
  clearWorkspaceInput(surface, false);
  st.sourcePath = "";
  st.sourcePaths = [];
  const c = await getClient();
  const task = await c.getTask(taskId);
  if (!isCurrent()) return;
  if (task.surface !== surface) throw new Error(t("此任务不属于当前工作区。"));
  const context = taskContexts.get(taskId) ?? await recoverTaskContext(surface, taskId);
  if (!isCurrent()) return;
  st.sourcePath = context?.sourcePath ?? "";
  st.sourcePaths = [...(context?.sourcePaths ?? [])];
  st.skipped = [...(context?.skipped ?? [])];
  st.scanSummary = { ...(context?.scanSummary ?? {}) };
  st.historyOnly = !context;
  // Old records deliberately exclude original paths. Show their own file results,
  // never borrow the current source list or invent paths for a new translation.
  const progress = [...reduceFileProgress(new Map(), task.file_progress).values()];
  const entries = progress.length ? progress : terminalFileResults(record(task.result)).map(record);
  st.files = context ? [...context.files] : entries.map((entry, index) => {
    const name = text(entry.name, text(entry.relative_path, t("文件 {0}", index + 1)));
    const format = name.split(".").pop()?.toLowerCase();
    return {
      path: `history:${taskId}:${index}`,
      file_id: text(entry.file_id),
      name,
      relative_path: text(entry.relative_path),
      format: format && ["xlsx", "xls", "docx", "doc", "pdf", "png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "dwg", "dxf"].includes(format)
        ? format : "",
    };
  });
  st.selected = context ? new Set(context.selected) : new Set(st.files.map((file) => file.path));
  focusTask(surface, task);
  st.task!.logs = (task.logs ?? []).map((log, index) => ({
    seq: num(log.event_id, index), time: typeof log.timestamp === "number" ? formatTime(log.timestamp) : "",
    level: text(log.level, "INFO"), message: redactedText(log.message),
  }));
  bootstrapAdopted.add(surface);
  if (task.terminal) finishTask(surface, task);
  else watchTask(surface);
  if (surface === "pdf") await fetchPdfPagesSnapshot(surface, taskId);
}

// ---------------------------------------------------------------------------
// API 客户端 + 全局设置/语言目录（本视图独立持有一份，不复用 tasks.ts 的实例——
// 各视图自包含，减少跨模块耦合，见文件尾说明）。
// ---------------------------------------------------------------------------

const client = new ApiClient();
let connectPromise: Promise<void> | null = null;
export async function getClient(): Promise<ApiClient> {
  if (!connectPromise) {
    const attempt = client.connect();
    // 与 ensureBootstrap 同理，而且这一层更致命：connect() 里那记 /health 撞上还没起来的
    // 后端就会 reject，缓存住之后**整个工作区的每一个请求**都会立刻失败，连「重试」也修不好
    // ——重试第一步就是 getClient()。失败即置空，让下一次调用真的重连。
    attempt.catch(() => {
      if (connectPromise === attempt) connectPromise = null;
    });
    connectPromise = attempt;
  }
  await connectPromise;
  return client;
}

let settings: JsonObject = {};
// 语言目录整条留着（含后端给的 aliases），可搜索选择器要靠 aliases 匹配英文名。
let languageOptions: { source: LanguageOption[]; target: LanguageOption[] } = { source: [], target: [] };
let bootstrapPromise: Promise<void> | null = null;
let bootstrapAdopted = new Set<Surface>();

async function ensureBootstrap(): Promise<void> {
  if (!bootstrapPromise) {
    const attempt = (async () => {
      const c = await getClient();
      const [settingsPayload, languagesPayload] = await Promise.all([
        c.request<JsonObject>("/api/settings"),
        c.request<{ source_options: LanguageOption[]; target_options: LanguageOption[] }>("/api/languages"),
      ]);
      settings = settingsPayload;
      languageOptions = { source: languagesPayload.source_options ?? [], target: languagesPayload.target_options ?? [] };
      applySettingsToStates();
    })();
    // 失败的 promise 绝不能留在缓存里。设置和语言目录都靠这一趟拿，缓存住一个 rejected
    // promise，等于把「后端冷启动慢了一秒」变成「这个窗口的语言选择器永久瘫痪」——而且
    // 瘫得很像功能本身有毛病：按钮回落成裸代码（en 而不是「英文」），浮层过滤空目录后
    // 显示「没有匹配的语言，换个说法试试」，把加载失败说成用户搜错了词。
    // 置空之后，下一次挂载（切个页面再切回来）或空态里的「重试」会重新拉。
    attempt.catch(() => {
      if (bootstrapPromise === attempt) bootstrapPromise = null;
    });
    bootstrapPromise = attempt;
  }
  await bootstrapPromise;
}

/** 丢掉已缓存的引导结果，下一次 ensureBootstrap 重新拉设置与语言目录。 */
function resetBootstrap(): void {
  bootstrapPromise = null;
}

/** 语言目录空态里「重试」的动作：重新引导，成功就把这一片重画出来。 */
async function reloadBootstrap(surface: Surface): Promise<void> {
  resetBootstrap();
  try {
    await ensureBootstrap();
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, t("重新加载语言列表失败。")), error: true });
    return;
  }
  states[surface].renderer?.();
}

function applySettingsToStates(): void {
  for (const surface of ["excel", "word", "pdf", "cad"] as Surface[]) {
    const st = states[surface];
    st.sourcePath = normalizeUserPath(st.sourcePath || text(settings[`last_${surface}_source_folder`]));
    if (surface === "pdf") {
      const pdf = record(settings.pdf);
      st.targetLang = text(pdf.target_lang, "zh");
    } else {
      const fallbackTarget = surface === "word" ? "fr" : surface === "cad" ? "zh" : text(settings.target_lang, "en");
      st.targetLang = text(settings[`${surface}_target_lang`], fallbackTarget);
      st.sourceLang = text(settings[`${surface}_source_lang`], "auto");
    }
    if (surface !== "pdf") {
      st.domainPreset = text(settings[`${surface}_domain_preset`], surface === "excel" ? "无" : "同步工程场景"); // i18n-ignore 专业领域预设值，存进设置
    }
    for (const toggle of TOGGLES[surface]) {
      if (toggle.pathKind === "flat" && toggle.path) {
        st.toggles.set(toggle.key, Boolean(readPath(settings, toggle.path)));
      } else if (toggle.pathKind === "output") {
        const configured = readOutputPath(surface, toggle.path as string);
        st.toggles.set(toggle.key, configured === undefined ? toggle.default : Boolean(configured));
      }
    }
    // 互斥对两边都为真：老版本切换互斥开关时只落盘自己那一半，磁盘上会留下
    // 两个都是 true 的脏数据。照后端的实际裁决显示——task_runner 里
    // need_autofit = enable_excel_autofit and not lock_row_height，锁定行高赢。
    // 界面必须跟裁决一致，不能两个都亮着让人以为设置被改乱了。
    if (st.toggles.get("excelAutofit") && st.toggles.get("lockRowHeight")) {
      st.toggles.set("excelAutofit", false);
    }
    const outputSettings = outputRecord(surface);
    st.useCustomOutputDir = Boolean(outputSettings.use_custom_output_dir);
    st.customOutputDir = text(outputSettings.custom_output_dir);
  }
}

function readPath(obj: JsonObject, path: string): unknown {
  let cursor: unknown = obj;
  for (const part of path.split(".")) {
    if (!cursor || typeof cursor !== "object") return undefined;
    cursor = (cursor as JsonObject)[part];
  }
  return cursor;
}

/** excel_output/word_output 有旧版 output 兜底（main.ts 的 excelOutputSettings 同款逻辑）；pdf_output、cad_output 没有。 */
function outputRecord(surface: Surface): JsonObject {
  if (surface === "pdf" || surface === "cad") return record(settings[`${surface}_output`]);
  const isolated = record(settings[`${surface}_output`]);
  return Object.keys(isolated).length ? isolated : record(settings.output);
}

function outputSettingPathPrefix(surface: Surface): string {
  if (surface === "pdf" || surface === "cad") return `${surface}_output`;
  return hasOwn(settings, `${surface}_output`) ? `${surface}_output` : "output";
}

function readOutputPath(surface: Surface, key: string): unknown {
  return outputRecord(surface)[key];
}

async function persistSettings(patch: JsonObject): Promise<void> {
  const c = await getClient();
  settings = await c.request<JsonObject>("/api/settings", { method: "PUT", body: JSON.stringify(patch) });
}

/** 开关/领域/输出目录这几处设置都是「先改界面再落盘」，落盘发射后不管：PUT 失败时
 *  界面已经显示了新值，磁盘上还是旧值，之后任务读的是磁盘（比如「锁定行高时缩字号」
 *  这一类互斥开关），于是出现「界面显示关、实际按开跑」的假象，用户全程不知道保存
 *  失败过。这里统一收口：失败就把 revert 传回来的旧值改回界面并重绘，同时报一句
 *  尽量简短的事实性 toast，不替用户下「这意味着什么」的结论。 */
async function persistSettingsOrRevert(surface: Surface, patch: JsonObject, revert: () => void, what: string): Promise<void> {
  try {
    await persistSettings(patch);
  } catch (error) {
    revert();
    rerender(surface);
    showToast({ message: redactedText((error as Error)?.message, t("{0}未能保存，已恢复为原设置。", what)), error: true });
  }
}

// ---------------------------------------------------------------------------
// mount / unmount
// ---------------------------------------------------------------------------

const mountTokens: Record<Surface, number> = { excel: 0, word: 0, pdf: 0, cad: 0 };

export function mountWorkspace(container: HTMLElement, params: ViewParams, surface: Surface): void {
  const st = states[surface];
  const token = ++mountTokens[surface];
  const isCurrent = () => token === mountTokens[surface] && st.renderer !== null;
  const taskId = text(params.taskId);
  st.renderer = () => renderInto(container, surface);
  // 中途切走再回来时任务还在跑（st.task 是模块级状态），静默计时器得跟着这一屏重新起。
  if (st.task && !st.task.task.terminal) startSilenceTicker(surface);
  renderLoading(container, surface);
  ensureBootstrap()
    .then(() => {
      if (!isCurrent()) return;
      return taskId ? openWorkspaceTask(surface, taskId, isCurrent) : adoptExistingTask(surface, isCurrent);
    })
    .then(() => {
      if (!isCurrent()) return;
      // 「停」配对的「起」：unmountWorkspace 会停掉 PDF 逐页重跑的轮询计时器（高-10），
      // 但 adoptExistingTask 只在首次发现任务时拉一次快照——st.task 已经存在的情形（切走
      // 再切回这一屏）它会直接早返回，不会重新拉快照，于是重跑期间切走再切回来，面板会
      // 永远停在离开那一刻的页码上、逐页按钮全灰，直到重启应用（B1-1）。这里补上：只要是
      // PDF 且已经有任务，每次挂载都重新拉一次快照；fetchPdfPagesSnapshot 内部看到
      // rerun.active 会自己把计时器重新起起来，跟 adoptExistingTask 里的调用是同一套逻辑。
      if (surface === "pdf" && st.task) void fetchPdfPagesSnapshot(surface, st.task.task.task_id);
      // 用户可能刚在插件页连好转换工具再切回来，所以每次挂载都重新查一次。
      if (surface === "cad") void refreshCadCapability(isCurrent);
      if (st.renderer) renderInto(container, surface);
    })
    .catch((error) => {
      if (!isCurrent()) return;
      showToast({ message: redactedText((error as Error)?.message, t("初始化工作区失败。")), error: true });
      if (st.renderer) renderInto(container, surface);
    });
}

/** CAD：查询图纸转换工具的连接状态。失败不打断页面——没有结果时按「未知」处理，
 *  扫描时后端会再回一次最新状态。 */
async function refreshCadCapability(isCurrent: () => boolean): Promise<void> {
  try {
    const c = await getClient();
    const capability = await c.getCadCapability();
    if (!isCurrent()) return;
    states.cad.cadCapability = record(capability);
    rerender("cad");
  } catch {
    // 查询失败时保持原状；扫描接口会带回 capability。
  }
}

function cadConverterReady(st: SurfaceState): boolean {
  return text(record(st.cadCapability).oda) === "connected";
}

export function unmountWorkspace(surface: Surface): void {
  ++mountTokens[surface];
  states[surface].renderer = null;
  // 离开这一屏后没人看那句「已等待 N 秒」，计时器再走就是白转（rerender 也已经是空操作）。
  // 任务本身不受影响：事件流由 watchTask 维护，回到这一屏时 startSilenceTicker 会重新起。
  stopSilenceTicker(surface);
  // PDF 逐页重跑的轮询计时器同理必须在这里清掉：它是 setTimeout 链而不是随 DOM 走的
  // 东西，不主动停就会在用户切到设置、记忆库等完全不相干的页面之后继续每隔几秒拉一次
  // 快照——拉失败时那条报错 toast 会跟着用户跨页面弹，直到应用重启（高-10）。回到这一屏
  // 时 fetchPdfPagesSnapshot 看到 snapshot.rerun.active 仍会自己把计时器重新起起来。
  stopPdfRerunTicker(surface);
  // 提示气泡、语言选择器浮层、「浏览」按钮的锚定菜单都挂在 document.body 上，不随 container
  // 一起被清掉。不主动关就会留下一个悬在半空的面板，而且模块级的「当前展开项」指针还指着已死的闭包。
  hideHint();
  closeLanguagePopover();
  closeMenu();
}

function renderLoading(container: HTMLElement, surface: Surface): void {
  setTopbar({ title: SURFACE_PAGE_TITLE[surface], status: { label: t("加载中"), tone: "idle" }, subtitle: t("正在连接后台服务…") });
  const card = createEmptyState({ title: t("正在加载工作区"), description: t("首次连接后台可能需要几秒。"), icon: SURFACE_ICON[surface] });
  card.style.flex = "1";
  container.append(card);
}


/** 首次打开某 surface 时，如果后端已有一个仍在跑的该类任务且本地还没聚焦任何任务，
 *  自动接管并订阅——对应 main.ts 的 workspaceTask() 在无显式聚焦时退回"最近一个活动任务"。
 *
 *  bootstrapAdopted 只在这次 listTasks() 真的问出结果（不管有没有活动任务）之后才写入，
 *  绝不在发起网络调用之前就标记。原来的写法是「先标记、再请求、请求失败就静默吞掉」——
 *  一次网络抖动就把这个 surface 永久钉在「没有任务」的假设上：bootstrapAdopted 是
 *  模块级、跨挂载持续存在的 Set，之后不管切页面多少次再切回来，看到的都只是空的工作区，
 *  实际后端还在跑的任务再也不会被自动接管。跟 ensureBootstrap() 一个道理：失败的尝试
 *  不能留在缓存里，得让下一次挂载能够重试。 */
async function adoptExistingTask(surface: Surface, isCurrent: () => boolean): Promise<void> {
  if (bootstrapAdopted.has(surface)) return;
  const st = states[surface];
  if (st.task) {
    bootstrapAdopted.add(surface);
    return;
  }
  try {
    // Each mount owns its lookup. A rapid remount must not reuse an obsolete
    // promise whose guard belongs to the previous mount.
    const c = await getClient();
    const list = await c.listTasks();
    if (!isCurrent()) return;
    const candidate = list.active.find((t) => t.surface === surface);
    if (candidate) await openWorkspaceTask(surface, candidate.task_id, isCurrent);
    if (isCurrent()) bootstrapAdopted.add(surface);
  } catch {
    // 接管失败不影响正常使用——用户可以照常扫描发起新任务；下次挂载这个 surface 会重试。
  }
}

// ---------------------------------------------------------------------------
// 顶部渲染入口：整页按当前 phase 重建（DOM 规模很小，重建比增量 patch 更简单可靠）。
// ---------------------------------------------------------------------------

// 运行日志的滚动位置：旧版本没有这套逻辑，这里是新写的。整页每次重建都会把 .log
// 连同其它 DOM 一起扔掉重建，浏览器不会替你记滚动条——不记就记，不然用户往上翻看历史时
// 一来新日志就被强制拽回底部。只有「用户本来就停在底部」（容差 8px）才在重建后继续贴底，
// 否则原样恢复重建前的位置。
const LOG_SCROLL_STICK_TOLERANCE = 8;

/**
 * 还原滚动位置不能直接照抄 scrollTop。日志攒到 LOG_VIEW_LIMIT 条以后，每来一条新日志
 * 就同时丢掉最旧一条、追加最新一条：scrollHeight 一点没变，可整段内容却往上挪了一行。
 * 这时把 scrollTop 原样写回去，用户看到的内容就一行一行往上漂——他往上翻历史，日志
 * 每来一条就把他顶走一行，等于「不抢滚动位置」这条规矩在满 200 条之后彻底失效。
 * 距离底部（scrollHeight - scrollTop - clientHeight）同样救不了，因为它在这种情况下
 * 也完全不变。
 *
 * 所以改成锚点还原：记下重建前顶在视口口沿的那一行是第几条（seq 全局单调递增）、以及
 * 它的上沿相对视口上沿差多少像素；重建后把同一条日志摆回同样的位置。行高不一致
 * （长消息会折行）也不影响，因为量的是这一行本身的实际位置，不是「几行 × 行高」。
 */
interface LogScrollState {
  atBottom: boolean;
  scrollTop: number;
  /** 视口顶部那一行的日志序号；日志为空时为 null。 */
  anchorSeq: number | null;
  /** 锚点行上沿减去视口上沿，通常是 0 或负数（这一行被视口切掉了一截）。 */
  anchorOffset: number;
}

function captureLogScroll(container: HTMLElement): LogScrollState | null {
  const prev = container.querySelector<HTMLElement>(".log");
  if (!prev) return null;
  const distanceFromBottom = prev.scrollHeight - prev.scrollTop - prev.clientHeight;
  let anchorSeq: number | null = null;
  let anchorOffset = 0;
  const viewTop = prev.getBoundingClientRect().top;
  for (const line of Array.from(prev.children) as HTMLElement[]) {
    const seq = line.dataset.seq;
    if (seq === undefined) continue;
    const rect = line.getBoundingClientRect();
    // 第一条下沿还在视口里的行，就是用户眼下看到的最上面那行。
    if (rect.bottom > viewTop) {
      anchorSeq = Number(seq);
      anchorOffset = rect.top - viewTop;
      break;
    }
  }
  return { atBottom: distanceFromBottom <= LOG_SCROLL_STICK_TOLERANCE, scrollTop: prev.scrollTop, anchorSeq, anchorOffset };
}

function restoreLogScroll(container: HTMLElement, prevScroll: LogScrollState | null): void {
  logResizeObserver?.disconnect();
  const next = container.querySelector<HTMLElement>(".log");
  if (!next) return;
  // 没有旧状态（比如第一次渲染日志卡片）按贴底处理，最新的日志本来就该先看到。
  if (!prevScroll || prevScroll.atBottom) {
    stickLogToBottom(next);
    return;
  }
  if (prevScroll.anchorSeq !== null) {
    const anchor = next.querySelector<HTMLElement>(`[data-seq="${prevScroll.anchorSeq}"]`);
    if (anchor) {
      next.scrollTop += anchor.getBoundingClientRect().top - next.getBoundingClientRect().top - prevScroll.anchorOffset;
      return;
    }
    // 锚点行已经被 200 条上限挤掉了：用户正在看的那段日志已经不在缓冲区里，
    // 退到现存最旧的一条，而不是沿用旧 scrollTop——那会把他丢到一段没看过的位置。
    next.scrollTop = 0;
    return;
  }
  next.scrollTop = prevScroll.scrollTop;
}

let logResizeObserver: ResizeObserver | null = null;

/** 贴底不能只在重建那一刻设一次 scrollTop：页面插进去以后，下面的文件状态条换了文案
 *  （比如暂停时变成两行）或者逐页表又长了几行，日志区会再被压矮一截。这时 scrollTop
 *  没变、可视高度变小，最后一行就被底下的文件状态条盖住半截；下一次重建量到的离底
 *  距离又超过了容差，从此就不再贴底。所以用户没主动往上翻之前，日志区每次变高变矮都
 *  重新贴一次底。 */
function stickLogToBottom(log: HTMLElement): void {
  log.scrollTop = log.scrollHeight;
  logResizeObserver?.disconnect();
  if (typeof ResizeObserver === "undefined") return;
  let stick = true;
  log.addEventListener("scroll", () => {
    stick = log.scrollHeight - log.scrollTop - log.clientHeight <= LOG_SCROLL_STICK_TOLERANCE;
  }, { passive: true });
  logResizeObserver = new ResizeObserver(() => {
    if (stick && log.isConnected) log.scrollTop = log.scrollHeight;
  });
  logResizeObserver.observe(log);
}

/** 整页重建前记下各滚动区的位置，重建后放回去。
 *
 *  每来一个任务事件页面都会整体重画一次；没有这一步，用户往下翻到第 30 页的逐页表、
 *  页恢复列表、右栏设置，下一个事件一到就被弹回顶部。要保位置的滚动区在创建时打上
 *  `data-keep-scroll="<名字>"`，名字在页面内唯一即可。日志区另有「贴底跟随」的规则，
 *  由 captureLogScroll / restoreLogScroll 单独处理。 */
function captureKeptScrolls(container: HTMLElement): Map<string, number> {
  const positions = new Map<string, number>();
  for (const node of container.querySelectorAll<HTMLElement>("[data-keep-scroll]")) {
    if (node.scrollTop > 0) positions.set(node.dataset.keepScroll ?? "", node.scrollTop);
  }
  return positions;
}

function restoreKeptScrolls(container: HTMLElement, positions: Map<string, number>): void {
  if (!positions.size) return;
  for (const node of container.querySelectorAll<HTMLElement>("[data-keep-scroll]")) {
    const top = positions.get(node.dataset.keepScroll ?? "");
    if (top !== undefined) node.scrollTop = top;
  }
}

function renderInto(container: HTMLElement, surface: Surface): void {
  const logScroll = captureLogScroll(container);
  const keptScrolls = captureKeptScrolls(container);
  while (container.firstChild) container.removeChild(container.firstChild);
  container.removeAttribute("style");
  const st = states[surface];
  const active = Boolean(st.task && !st.task.task.terminal);

  updateTopbar(surface, st, active);

  if (st.showBanner && st.bannerInfo) {
    container.style.flexDirection = "column";
    container.append(buildBanner(surface, st));
    // 完成横幅之下再补一句「哪些内容没被翻译」——横幅只说做了什么，这句说没做什么。
    if (surface === "excel" && st.excelDoneNotice) container.append(buildExcelDoneCard(st.excelDoneNotice));
    const row = el("div");
    row.style.cssText = "flex:1;display:flex;gap:16px;min-height:0";
    row.append(buildColLeft(surface, st, active), buildColRight(surface, st, active));
    container.append(row);
  } else {
    container.append(buildColLeft(surface, st, active), buildColRight(surface, st, active));
  }
  restoreLogScroll(container, logScroll);
  restoreKeptScrolls(container, keptScrolls);
}

export function rerender(surface: Surface): void {
  const st = states[surface];
  st.renderer?.();
}

function updateTopbar(surface: Surface, st: SurfaceState, active: boolean): void {
  if (st.showBanner && st.bannerInfo) {
    // 状态直接来自任务终态和逐文件结果（见 finishTask 的 resultTone）。以前这里靠
    // 对副标题正则匹配「需复核」来决定颜色，于是失败的任务也顶着一个绿色「已完成」。
    const info = st.bannerInfo;
    setTopbar({
      title: SURFACE_PAGE_TITLE[surface],
      status: { label: info.statusLabel, tone: info.tone === "fail" ? "danger" : info.tone },
      subtitle: info.tone === "fail"
        ? t("任务已归档到任务中心，可在完整报告里看失败原因")
        : t("任务已归档到任务中心，可随时回看完整报告"),
    });
    return;
  }
  if (scanBusy[surface]) {
    setTopbar({
      title: SURFACE_PAGE_TITLE[surface],
      status: { label: t("扫描中…"), tone: "run" },
      subtitle: t("正在读取文件，请稍候"),
    });
    return;
  }
  if (active && st.task) {
    const state = st.task.task.state;
    const meta = runningStatusMeta(state);
    const fileCount = st.selected.size || st.files.length;
    // 专业领域选「无」时不写：单独一个「无」挂在语言后面，看不出指的是什么。
    const domainSuffix = surface !== "pdf" && st.domainPreset && st.domainPreset !== "无" // i18n-ignore 与后端预设值比较
      ? " · " + t("专业领域：{0}", tx(st.domainPreset))
      : "";
    // PDF 没有源语言选择，只说目标语言；其余三页同一句式。
    const subtitle = surface === "pdf"
      ? t("{0} · 译成{1}", fileNounPhrase(surface, fileCount), targetLabel(st))
      : `${fileNounPhrase(surface, fileCount)} · ${sourceLabel(st)} → ${targetLabel(st)}${domainSuffix}`;
    setTopbar({ title: SURFACE_PAGE_TITLE[surface], status: meta, subtitle });
    return;
  }
  // 点了开始、任务还没建好：后端要把选中的文件重新读一遍再核对连接，大文档要好几秒。
  // 这段时间顶栏还写「待开始」，看上去像没点上，用户会再点一次。
  if (submittingSurfaces.has(surface) && st.files.length > 0) {
    setTopbar({
      title: SURFACE_PAGE_TITLE[surface],
      status: { label: t("正在启动"), tone: "run" },
      subtitle: t("正在读取 {0}并检查模型连接，大文件需要几秒钟", fileNounPhrase(surface, st.selected.size || st.files.length)),
    });
    return;
  }
  if (st.files.length > 0) {
    setTopbar({
      title: SURFACE_PAGE_TITLE[surface],
      status: { label: t("已扫描 · 待开始"), tone: "run" },
      subtitle: t("已找到 {0}，勾选后开始翻译", fileNounPhrase(surface, st.files.length)),
    });
    return;
  }
  setTopbar({ title: SURFACE_PAGE_TITLE[surface], status: { label: t("待选择来源"), tone: "idle" }, subtitle: SURFACE_EMPTY_SUBTITLE[surface] });
}

function runningStatusMeta(state: TaskStatus["state"]): { label: string; tone: StatusTone } {
  switch (state) {
    case "paused":
      return { label: t("已暂停"), tone: "pause" };
    case "pausing":
      return { label: t("正在暂停"), tone: "pause" };
    case "stopping":
      return { label: t("正在停止"), tone: "warn" };
    case "finalizing":
      return { label: t("正在收尾"), tone: "warn" };
    default:
      return { label: t("翻译中"), tone: "run" };
  }
}

function sourceLabel(st: SurfaceState): string {
  if (st.sourceLang === "auto") return t("自动检测");
  return tx(languageOptions.source.find((o) => o.code === st.sourceLang)?.display_name ?? st.sourceLang);
}
function targetLabel(st: SurfaceState): string {
  return tx(languageOptions.target.find((o) => o.code === st.targetLang)?.display_name ?? st.targetLang);
}

/** 语言名是后端给的中文显示名：显示时换成当前界面语言，中文原名补进搜索别名，照样能用中文搜到。 */
function localizedLanguageOptions(options: LanguageOption[]): LanguageOption[] {
  return options.map((option) => {
    const display = tx(option.display_name);
    if (display === option.display_name && !option.description) return option;
    return {
      ...option,
      display_name: display,
      aliases: display === option.display_name ? option.aliases : [...(option.aliases ?? []), option.display_name],
      description: option.description ? tx(option.description) : option.description,
    };
  });
}

// ---------------------------------------------------------------------------
// 完成横幅
// ---------------------------------------------------------------------------

function buildBanner(surface: Surface, st: SurfaceState): HTMLElement {
  const info = st.bannerInfo!;
  const actions: HTMLElement[] = [];
  // 一个文件都没产出时不给「打开输出目录」：点进去只有空目录，等于骗他跑一趟。
  if (info.hasOutput) {
    actions.push(createButton({
      label: t("打开输出目录"),
      icon: "folder",
      onClick: () => openTaskLocalFile(st.lastOutputPath, true),
    }));
  }
  // CAD 停止后可以从断点继续：已验证的文件直接复用，不用整批重跑。
  const lastTask = st.task?.task;
  const lastOutputDir = text(record(lastTask?.result).output_dir);
  if (surface === "cad" && lastTask?.state === "stopped" && lastOutputDir && st.files.length) {
    actions.push(createButton({
      label: t("从断点继续"),
      icon: "play",
      onClick: () => {
        st.cadResumeDir = lastOutputDir;
        st.showBanner = false;
        rerender(surface);
        showToast({ message: t("已选择断点，点击「开始翻译」继续。") });
      },
    }));
  }
  // Excel / Word 停止后已经写出了一部分：直接从这里接着翻，不用先去「重新扫描」才能
  // 看到续译入口。
  // 后端数不出剩余内容时不给 unstarted_text_count：那不是「全部翻完」，照样给入口。
  const unfinishedRaw = record(record(lastTask?.result).kpi).unstarted_text_count;
  const unfinishedTexts = num(unfinishedRaw);
  const unfinishedUnknown = typeof unfinishedRaw !== "number";
  if ((surface === "excel" || surface === "word") && lastTask?.state === "stopped" && info.hasOutput && (unfinishedTexts > 0 || unfinishedUnknown) && st.files.length) {
    actions.unshift(createButton({
      label: t("继续翻译"),
      icon: "play",
      variant: "primary",
      disabled: submittingSurfaces.has(surface),
      onClick: () => void resumeAfterStop(surface, st, lastOutputDir),
    }));
  }
  actions.push(createButton({
    label: info.tone === "fail" ? t("查看失败原因") : t("查看完整报告"),
    icon: "ext",
    onClick: () => navigate("tasks", { taskId: st.lastTaskId }),
  }));
  return createTaskTerminalBanner({
    title: info.title,
    subtitle: info.subtitle,
    icon: info.tone === "ok" ? "check" : "warn",
    tone: info.tone,
    actions,
    onClose: () => {
      st.showBanner = false;
      rerender(surface);
    },
  });
}

// ---------------------------------------------------------------------------
// 左栏
// ---------------------------------------------------------------------------

function buildColLeft(surface: Surface, st: SurfaceState, active: boolean): HTMLElement {
  const col = el("div", "col-l");
  const local = st.task;
  if (active && local) {
    col.append(buildProgressCard(surface, st));
    if (surface === "word") {
      const recoveryCard = buildWordRecoveryCard(local);
      if (recoveryCard) col.append(recoveryCard);
    }
    let sharesHeight = false;
    if (surface === "pdf") {
      const recoveryCard = buildPdfRecoveryCard(surface, local);
      if (recoveryCard) col.append(recoveryCard);
      const reviewCard = buildPdfReviewCard(surface, local);
      if (reviewCard) col.append(reviewCard);
      sharesHeight = Boolean(reviewCard);
    }
    col.append(buildLogCard(local, sharesHeight));
  } else {
    col.append(buildSrcBar(surface, st));
    if (st.historyOnly) {
      const note = el("p", "ws-note");
      note.textContent = t("此历史任务未保留来源路径。结果仍可查看；如需重新翻译，请先选择来源并扫描。");
      col.append(note);
    }
    col.append(buildStatsRow(surface, st));
    if (surface === "cad") {
      const notice = buildCadConverterBanner(st);
      if (notice) col.append(notice);
      const resumeNote = buildCadResumeNote(st);
      if (resumeNote) col.append(resumeNote);
    }
    // 单元格外内容的说明只属于 Excel：Word / PDF 没有这条限制，不该出现这段提示。
    if (surface === "excel") {
      const notice = buildOutsideCellBanner(st);
      if (notice) col.append(notice);
    }
    // 续译的两种「已决定」态都贴在文件表正上方：横幅说底稿从哪来，提示条说这次不用底稿。
    const resumeBanner = buildResumeBanner(surface, st);
    if (resumeBanner) col.append(resumeBanner);
    const resumeDismiss = buildResumeDismissBar(surface, st);
    if (resumeDismiss) col.append(resumeDismiss);
    const tableCard = buildTableCard(surface, st);
    col.append(tableCard);
    // 逐页表格以前只在运行中的那半边存在，任务一结束整块就消失了。结果页恰恰是用户
    // 看出某一页翻坏了的地方，看出来了却连表格都回不去，只能整份重翻。终态照样把它
    // 渲染出来——终态的可操作项只有「重新生成某一页」，由卡片自己按快照判断。
    if (surface === "pdf" && local?.task.terminal) {
      if (local.pdfPagesUnavailable) {
        const note = el("p", "ws-note");
        note.textContent = t("此历史任务的逐页记录已不可用。文件结果仍可在完整报告中查看。");
        col.append(note);
      }
      const reviewCard = buildPdfReviewCard(surface, local);
      if (reviewCard) {
        // 两张卡片对半分这一栏的高度。任务清单默认 min-height:0，逐页表一展开就把它
        // 挤得只剩标题，所以这里给它留一个能看到几行文件的下限。
        tableCard.style.minHeight = "200px";
        col.append(reviewCard);
      }
    }
  }
  return col;
}

function buildSrcBar(surface: Surface, st: SurfaceState): HTMLElement {
  const bar = el("div", "card srcbar");
  const scanning = scanBusy[surface];
  const scanBtn = createButton({
    // 扫描在飞时两个入口都锁住：再点一次只会多一个在途请求，
    // 而先发后到的那个会把清单换成上一个路径的内容（见 runScan 的请求序号）。
    label: scanning ? t("扫描中…") : st.files.length > 0 ? t("重新扫描") : t("扫描"),
    variant: "primary",
    disabled: scanning || !st.sourcePath.trim(),
    onClick: () => void runScan(surface),
  });
  const { root, input } = createTextField({
    label: "",
    value: st.sourcePath,
    placeholder: t("选择或粘贴文件、文件夹路径…"),
    onInput: (value) => {
      const normalized = normalizeUserPath(value);
      if (normalized !== value) input.value = normalized;
      if (normalized === st.sourcePath && !st.sourcePaths.length) return;
      const needsRender = !!st.task || !!st.files.length || st.showBanner || scanBusy[surface];
      const caret = input.selectionStart;
      clearWorkspaceInput(surface);
      st.sourcePath = normalized;
      // 手输/粘贴就不再是"刚才多选的那几个文件"了，多选清单必须一起作废。
      st.sourcePaths = [];
      // 手输/粘贴路径时同步解锁「扫描」——这一栏不整页重建，按钮得自己更新。
      // 扫描在飞时仍然保持锁定，光有路径不算能点。
      scanBtn.disabled = scanBusy[surface] || !normalized;
      if (needsRender) {
        rerender(surface);
        const next = document.querySelector<HTMLInputElement>(".srcbar input");
        next?.focus();
        if (caret !== null) next?.setSelectionRange(caret, caret);
      }
    },
  });
  root.style.margin = "0";
  root.style.flex = "1";
  input.addEventListener("change", () => {
    void persistSettings({ [`last_${surface}_source_folder`]: st.sourcePath });
  });
  // 粘贴路径后按回车直接扫描，和点「扫描」等价；输入法组字时的回车不算。
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing || scanBusy[surface] || !st.sourcePath.trim()) return;
    event.preventDefault();
    void runScan(surface);
  });
  bar.append(input);
  const browseBtn = createButton({
    label: t("浏览"),
    icon: "folder",
    disabled: scanning,
    title: scanning ? t("正在扫描当前路径，完成后方可选择新来源。") : undefined,
    onClick: () => {
      openMenu(browseBtn, [
        { label: t("选择文件夹…"), description: t("递归扫描目录下所有可翻译文件"), onSelect: () => void pickSource(surface, st, input, true) },
        { label: t("选择{0}…", SURFACE_FILE_NOUN[surface]), description: t("可多选，只扫描并翻译选中的文件"), onSelect: () => void pickSource(surface, st, input, false) },
      ]);
    },
  });
  bar.append(browseBtn);
  bar.append(scanBtn);
  return bar;
}

/** 单文件选择时的扩展名过滤，与后端 scanner 的 SUPPORTED_*_SUFFIXES 一一对应。 */
function sourceFileFilter(surface: Surface, st: SurfaceState): { name: string; extensions: string[] } {
  if (surface === "excel") return { name: t("Excel 表格"), extensions: ["xlsx", "xls"] };
  if (surface === "word") return { name: t("Word 文档"), extensions: ["docx", "doc"] };
  if (surface === "cad") return { name: t("CAD 图纸"), extensions: ["dwg", "dxf"] };
  const images = st.toggles.get("pdfImages") ? ["png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"] : [];
  return { name: images.length ? t("PDF 与图片") : "PDF", extensions: ["pdf", ...images] };
}

/** 取路径的上级目录。系统选择框一次只能在同一个文件夹里多选，取第一个的父目录即可。 */
function parentDirOf(path: string): string {
  const normalized = path.replace(/[\\/]+$/, "");
  const cut = Math.max(normalized.lastIndexOf("/"), normalized.lastIndexOf("\\"));
  return cut > 0 ? normalized.slice(0, cut) : normalized;
}

/**
 * 打开系统选择框。Tauri 的 dialog 插件把「选目录」和「选文件」做成两个互斥模式
 * （directory:true 时 filters 直接被忽略），所以这里由「浏览」菜单先定模式再调用；
 * 后端 scan 两种路径都支持，单文件走 Path.is_file() 分支；选文件时可以一次挑多个，
 * 后端按每个路径各扫一次再合并成一份清单。
 */
async function pickSource(surface: Surface, st: SurfaceState, input: HTMLInputElement, directory: boolean): Promise<void> {
  let picked: unknown;
  try {
    picked = directory
      ? await open({ title: t("选择来源文件夹"), directory: true, multiple: false })
      // 选文件不限数量，只限类型：filters 管类型，multiple 让用户一次挑几份。
      : await open({ title: t("选择{0}", SURFACE_FILE_NOUN[surface]), directory: false, multiple: true, filters: [sourceFileFilter(surface, st)] });
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, t("选择来源失败。")), error: true });
    return;
  }
  const pickedList = (Array.isArray(picked) ? picked : [picked]).filter(
    (item): item is string => typeof item === "string" && item.trim().length > 0,
  );
  if (!pickedList.length) return;
  clearWorkspaceInput(surface);
  // 多选时 sourcePath 记它们共同的上级目录：任务启动要用它重扫，selected_paths 再收窄
  // 回这几个文件；清单和统计仍然只显示选中的这几份（扫描只扫这几个路径）。
  st.sourcePaths = pickedList.length > 1 ? pickedList : [];
  const displayPath = pickedList.length > 1 ? parentDirOf(pickedList[0]) : pickedList[0];
  // 先落地到界面（含解锁「扫描」），再去写设置：记住上次目录失败不该把刚选好的路径丢掉。
  st.sourcePath = displayPath;
  input.value = displayPath;
  rerender(surface);
  try {
    await persistSettings({ [`last_${surface}_source_folder`]: displayPath });
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, t("已选择来源，但未能记住上次使用的目录。")), error: true });
  }
  // 走「浏览」选完路径就直接扫描，不用用户再点一次——手输/粘贴路径的场景不走这个函数，
  // 不受影响。复用 runScan 本身，扫描失败的提示和点按钮时完全一样。
  await runScan(surface);
}

interface StatCell {
  label: string;
  value: string;
  /** 数字标红（既有用法：旧版 .xls 计数）。 */
  warn?: boolean;
  /** 整格变黄（样张 .stat.attn）：这一格里的东西需要用户知道。 */
  attn?: boolean;
  /** 数字压成弱色（样张 .v.dash）：取值为 0 或「—」，没有信息量。 */
  dim?: boolean;
  /** 数字下面的一行小字（样张 .stat .sub）。 */
  sub?: string;
}

function buildStatsRow(surface: Surface, st: SurfaceState): HTMLElement {
  const stats = computeStats(surface, st);
  // 四格以内沿用固定 4 列；Excel 固定五格（见 computeStats），五格同排放不下时整齐地
  // 折成 3 + 2 两排，不让第 5 格孤零零掉到第二行（workspace.css 的 .stats-wrap）。
  const outer = el("div", "stats-wrap");
  const wrap = el("div", stats.length > 4 ? "stats five" : "stats");
  outer.append(wrap);
  for (const [index, stat] of stats.entries()) {
    const classes = ["stat"];
    if (!st.files.length) classes.push("dim");
    else if (stat.attn) classes.push("attn");
    const cell = el("div", classes.join(" "));
    const span = el("span");
    span.textContent = stat.label;
    const b = el("b");
    b.textContent = st.files.length && (!st.historyOnly || index === 0) ? stat.value : "—";
    if (stat.warn && st.files.length) b.style.color = "var(--warn)";
    if (stat.dim && st.files.length) b.className = "dash";
    cell.append(span, b);
    if (stat.sub && st.files.length) {
      const sub = el("div", "sub");
      sub.textContent = stat.sub;
      cell.append(sub);
    }
    wrap.append(cell);
  }
  return outer;
}

function computeStats(surface: Surface, st: SurfaceState): StatCell[] {
  const files = st.files;
  if (surface === "excel") {
    const cells = files.reduce((sum, f) => sum + num(f.text_cell_count), 0);
    const sheets = files.reduce((sum, f) => sum + num(f.sheet_count, f.sheets?.length ?? 0), 0);
    const xls = files.filter((f) => isRisky(surface, f)).length;
    // 固定五格：扫描前、扫描后、扫描失败都是这五格，数不出来的写「—」，页面不会跳。
    return [
      { label: t("已扫描文件"), value: String(files.length) },
      { label: t("文本单元格"), value: fmtNum(cells) },
      { label: t("工作表"), value: String(sheets) },
      outsideCellStat(outsideCellStats(files)),
      { label: t("旧版 .xls"), value: String(xls), warn: xls > 0 },
    ];
  }
  if (surface === "word") {
    const paragraphs = files.reduce((sum, f) => sum + num(f.paragraph_count), 0);
    const tables = files.reduce((sum, f) => sum + num(f.table_count), 0);
    const doc = files.filter((f) => isRisky(surface, f)).length;
    return [
      { label: t("已扫描文件"), value: String(files.length) },
      { label: t("段落"), value: fmtNum(paragraphs) },
      { label: t("表格"), value: String(tables) },
      { label: t("旧版 .doc"), value: String(doc), warn: doc > 0 },
    ];
  }
  if (surface === "cad") {
    // 合计只加扫描时数出来的（DXF）；DWG 开始后才统计，另起一行小字说明还有几张没算进来。
    // 没连转换工具的 DWG 归「需转换工具」那一格，不算进「开始后统计」。
    const counted = files.filter((f) => !cadCountsPending(f));
    const pending = files.filter((f) => cadCountsPending(f) && !isRisky(surface, f)).length;
    const texts = counted.reduce((sum, f) => sum + num(f.text_count), 0);
    const candidates = counted.reduce((sum, f) => sum + num(f.candidate_count, num(f.text_count)), 0);
    const blocked = files.filter((f) => isRisky(surface, f)).length;
    const noneCounted = counted.length === 0;
    const pendingNote = pending
      ? noneCounted
        ? t("{0} 张 DWG 开始后统计", pending)
        : t("另有 {0} 张 DWG 开始后统计", pending)
      : undefined;
    return [
      { label: t("已扫描文件"), value: String(files.length) },
      { label: t("文字实体"), value: noneCounted ? "—" : fmtNum(texts), dim: noneCounted, sub: pendingNote },
      {
        label: t("待翻译"),
        value: noneCounted ? "—" : fmtNum(candidates),
        dim: noneCounted,
        sub: texts > candidates ? t("不含纯数字、编号和已是目标语言的文字") : undefined,
      },
      { label: t("需转换工具"), value: String(blocked), warn: blocked > 0, dim: blocked === 0 },
      { label: t("跳过项"), value: String(st.skipped.length), dim: st.skipped.length === 0 },
    ];
  }
  const pages = files.reduce((sum, f) => sum + num(f.page_count), 0);
  const images = files.filter((f) => f.source_type === "image").length;
  // 与 Excel 那格同样的约定：oversizedPageStat 在整批文件都不产出该字段时返回 null，
  // 过滤掉即可——.five 修饰类靠的就是过滤后的实际格数。
  const pdfStats: (StatCell | null)[] = [
    { label: t("已扫描文件"), value: String(files.length) },
    { label: t("总页数"), value: String(pages) },
    oversizedPageStat(oversizedPageStats(files)),
    { label: t("独立图片"), value: String(images) },
    { label: t("跳过项"), value: String(st.skipped.length) },
  ];
  return pdfStats.filter((stat): stat is StatCell => stat !== null);
}

function isRisky(surface: Surface, f: FileItem): boolean {
  if (surface === "excel") return f.format === "xls" || Boolean(f.needs_conversion);
  if (surface === "word") return f.format === "doc" || Boolean(f.needs_conversion);
  // DWG 要先经转换工具转成 DXF；转换工具没连上时这些文件开不了工。
  if (surface === "cad") return Boolean(f.needs_conversion) && !cadConverterReady(states.cad);
  return false;
}

/** CAD：这张图纸的文字数扫描时没数（DWG），开始翻译后由任务统计。老后端不带 counts_pending，按计数是否为 null 判断。 */
function cadCountsPending(f: FileItem): boolean {
  return Boolean(f.counts_pending) || (f.format === "dwg" && f.text_count === null);
}

function fileLabel(f: FileItem): string {
  return text(f.name, f.relative_path ?? f.path.split("/").pop() ?? f.path);
}

// ---------------------------------------------------------------------------
// Excel「单元格外内容」——嵌入图片里的字、文本框/形状里的字
//
// 这些内容目前一律不翻译：写入器逐字节原样保留它们。本节不改变这个行为，只负责把它
// 说出来——用户看到「翻译完成」时，默认理解是「整个文件都翻了」，而实际上图片里的字
// 一个都没动，不说清楚就是误导。
//
// 三种状态必须区分开，绝不能合并：
//   数字   —— 扫描阶段数清楚了，例如 6 张图片；
//   null   —— 后端明说「数不出来」。目前只有 .xls：这些部件要先经 Excel COM 转成
//             .xlsx 才看得见，扫描阶段够不着。显示成「0」或「没有图片」是撒谎。
//   字段缺失 —— 这一版后端/这个 surface 根本不产出该字段，按「不适用」处理：不显示，
//             也不计入「未知」，免得在 Word/PDF 或老后端上凭空冒出一片「未知」。
// ---------------------------------------------------------------------------

type MaybeCount = number | "unknown" | "n/a";

function maybeCount(file: FileItem, key: "image_count" | "shape_text_count" | "comment_count" | "oversized_page_count"): MaybeCount {
  if (!hasOwn(file, key)) return "n/a";
  const value = file[key];
  return typeof value === "number" && Number.isFinite(value) ? value : "unknown";
}

interface OutsideCellStats {
  /** 已数清的图片总数（未知的文件不参与求和）。 */
  images: number;
  /** 已数清的含文字文本框/形状总数。 */
  shapes: number;
  /** 已数清的带批注单元格总数。批注文字同样不翻译，原样保留。 */
  comments: number;
  /** 至少有一项数不出来的文件数——对应后端 summary 的 *_unknown_files，不能吞掉。 */
  unknownFiles: number;
  /** 两个字段里至少有一个不是 n/a 的文件数。全批次都是 0，说明这批文件根本不产出这两个
   *  字段（不是「数出来是 0」），outsideCellStat 靠它把「字段缺失」跟「确认为 0」分开，
   *  不能像现在这样合并成同一个分支。 */
  presentFiles: number;
}

function outsideCellStats(files: FileItem[]): OutsideCellStats {
  const stats: OutsideCellStats = { images: 0, shapes: 0, comments: 0, unknownFiles: 0, presentFiles: 0 };
  for (const file of files) {
    const images = maybeCount(file, "image_count");
    const shapes = maybeCount(file, "shape_text_count");
    const comments = maybeCount(file, "comment_count");
    if (typeof images === "number") stats.images += images;
    if (typeof shapes === "number") stats.shapes += shapes;
    if (typeof comments === "number") stats.comments += comments;
    if (images === "unknown" || shapes === "unknown" || comments === "unknown") stats.unknownFiles += 1;
    if (images !== "n/a" || shapes !== "n/a" || comments !== "n/a") stats.presentFiles += 1;
  }
  return stats;
}

/** 有没有确凿数出来的东西。为 false 时可能是「确认没有」，也可能是「全都没数出来」。 */
function hasKnownOutside(stats: OutsideCellStats): boolean {
  return stats.images > 0 || stats.shapes > 0 || stats.comments > 0;
}

/** 「9 张图片、4 个文本框、3 条批注」；全为 0 时返回空串。 */
function outsideCellPhrase(stats: Pick<OutsideCellStats, "images" | "shapes" | "comments">): string {
  const parts: string[] = [];
  if (stats.images > 0) parts.push(t("{0} 张图片", fmtNum(stats.images)));
  if (stats.shapes > 0) parts.push(t("{0} 个文本框", fmtNum(stats.shapes)));
  if (stats.comments > 0) parts.push(t("{0} 条批注", fmtNum(stats.comments)));
  return parts.join(t("、"));
}

/** 接入点 1 · 扫描统计条里的「图片 / 文本框」格。这一格始终在（Excel 统计条固定五格，
 *  以前字段缺失时整格消失，扫描前后格数在 4 和 5 之间跳）；数不出来就写「—」。 */
function outsideCellStat(stats: OutsideCellStats): StatCell {
  const label = t("图片 / 文本框 / 批注");
  const unknownNote = stats.unknownFiles > 0 ? t("{0} 个 .xls 未统计", stats.unknownFiles) : "";
  const n = (value: number) => fmtNum(value);
  if (hasKnownOutside(stats)) {
    return {
      label,
      value: `${n(stats.images)} / ${n(stats.shapes)} / ${n(stats.comments)}`,
      attn: true,
      sub: unknownNote ? t("不翻译，原样保留 · {0}", unknownNote) : t("不翻译，原样保留"),
    };
  }
  // 一个都没数出来时不能写「0 / 0 / 0」——那是「确认没有」的说法。留「—」并说明原因。
  if (stats.unknownFiles > 0) {
    return { label, value: "—", dim: true, sub: t("{0} 个 .xls 需转换后才能统计", stats.unknownFiles) };
  }
  // 这几个字段在整批文件上都不存在（不是数出来是 0，例如还没扫描、扫描失败）——不该显示
  // 「0 / 0 / 0」把「不知道」说成「没有」，写「—」。
  if (stats.presentFiles === 0) return { label, value: "—", dim: true };
  return { label, value: "0 / 0 / 0", dim: true, sub: t("无") };
}

// ---------------------------------------------------------------------------
// CAD 专属提示：转换工具状态、从断点继续。样式沿用 Excel「单元格外内容」那条横幅。
// ---------------------------------------------------------------------------

/** 转换工具没连上时的提示。只有 DXF 也照常可翻，所以只在清单里真有 DWG、或还没扫描时出现。 */
function buildCadConverterBanner(st: SurfaceState): HTMLElement | null {
  if (!st.cadCapability || cadConverterReady(st)) return null;
  const blocked = st.files.filter((f) => isRisky("cad", f)).length;
  if (st.files.length && blocked === 0) return null;
  const banner = el("div", "banner warn");
  banner.append(icon("warn", { className: "ico" }));
  const tx = el("div", "tx");
  const title = el("div", "tt");
  title.textContent = blocked
    ? t("有 {0} 个 DWG 图纸需要先连接图纸转换工具", blocked)
    : t("尚未连接图纸转换工具");
  const body = el("div", "bd");
  body.textContent = text(record(st.cadCapability).oda) === "incompatible"
    ? t("检测到的转换工具无法使用。DWG 需要转换工具才能读写；DXF 不受影响，可直接翻译。")
    : t("DWG 需要转换工具才能读写；DXF 不受影响，可直接翻译。连接后请返回此处重新扫描。");
  tx.append(title, body);
  banner.append(tx);
  banner.append(createButton({ label: t("打开插件"), onClick: () => navigate("plugins") }));
  return banner;
}

/** 「从断点继续」已选好时，在清单上方说清这次会接着哪一次的输出继续，并给一个撤销入口。 */
function buildCadResumeNote(st: SurfaceState): HTMLElement | null {
  if (!st.cadResumeDir) return null;
  const banner = el("div", "banner");
  banner.append(icon("folder", { className: "ico" }));
  const tx = el("div", "tx");
  const title = el("div", "tt");
  title.textContent = t("将从上次停止的位置继续");
  const body = el("div", "bd");
  body.textContent = t("已验证的文件直接复用，其余文件重新翻译。上次输出：{0}", st.cadResumeDir);
  tx.append(title, body);
  banner.append(tx);
  banner.append(createButton({
    label: t("改为全部重译"),
    onClick: () => {
      st.cadResumeDir = "";
      rerender("cad");
    },
  }));
  return banner;
}

/** 接入点 2 · 扫描后、任务清单上方的说明横幅。没有可说的就返回 null，不占版面。 */
function buildOutsideCellBanner(st: SurfaceState): HTMLElement | null {
  if (!st.files.length) return null;
  const stats = outsideCellStats(st.files);
  const known = hasKnownOutside(stats);
  if (!known && stats.unknownFiles === 0) return null;

  const banner = el("div", "banner warn");
  banner.append(icon("warn", { className: "ico" }));
  const tx = el("div", "tx");
  const title = el("div", "tt");
  title.textContent = known
    ? t("此批文件中有 {0}不会被翻译", outsideCellPhrase(stats))
    : t("有 {0} 个 .xls 文件暂时无法统计图片、文本框和批注", stats.unknownFiles);
  const body = el("div", "bd");
  body.textContent = known
    ? t("仅翻译单元格中的文字。图片中的文字需要 OCR，暂不支持；文本框、形状和批注中的文字暂不处理。这些内容将按原样保留，不会丢失或变形。")
    : t("仅翻译单元格中的文字，图片、文本框和批注中的文字将按原样保留。");
  if (stats.unknownFiles > 0) {
    body.textContent += t("另有 {0} 个 .xls 文件需先转换为 .xlsx 才能统计；转换后如有此类内容，同样不会被翻译。", stats.unknownFiles);
  }
  tx.append(title, body);
  banner.append(tx);
  banner.append(createButton({ label: t("查看是哪些文件"), onClick: () => showOutsideCellModal(st) }));
  return banner;
}

function showOutsideCellModal(st: SurfaceState): void {
  const lines: string[] = [];
  for (const file of st.files) {
    const images = maybeCount(file, "image_count");
    const shapes = maybeCount(file, "shape_text_count");
    const comments = maybeCount(file, "comment_count");
    if (images === "unknown" || shapes === "unknown" || comments === "unknown") {
      lines.push(fileLabel(file) + " — " + t("未知（.xls 需转换后才能统计）"));
      continue;
    }
    const phrase = outsideCellPhrase({
      images: typeof images === "number" ? images : 0,
      shapes: typeof shapes === "number" ? shapes : 0,
      comments: typeof comments === "number" ? comments : 0,
    });
    if (phrase) lines.push(`${fileLabel(file)} — ${phrase}`);
  }
  openModal({
    tone: "warn",
    icon: "warn",
    sourceLabel: t("扫描结果 · 单元格外内容"),
    title: t("这些文件里有不参与翻译的内容"),
    body: lines.length ? lines : [t("没有检测到图片或文本框。")],
    actions: [{ label: t("关闭") }],
  });
}

/** 接入点 3 · 任务清单每行的「单元格外内容」标记。 */
function buildOutsideCellChips(file: FileItem): HTMLElement[] {
  const images = maybeCount(file, "image_count");
  const shapes = maybeCount(file, "shape_text_count");
  const comments = maybeCount(file, "comment_count");
  const chips: HTMLElement[] = [];
  if (typeof images === "number" && images > 0) {
    chips.push(createChip({ label: "🖼 " + t("{0} 张图片", fmtNum(images)), className: "oob" }));
  }
  if (typeof shapes === "number" && shapes > 0) {
    chips.push(createChip({ label: "▭ " + t("{0} 个文本框", fmtNum(shapes)), className: "oob" }));
  }
  if (typeof comments === "number" && comments > 0) {
    chips.push(createChip({ label: "💬 " + t("{0} 条批注", fmtNum(comments)), className: "oob" }));
  }
  // 未知和已知可以并存（真出现时两条都要说），所以是追加而不是二选一。
  if (images === "unknown" || shapes === "unknown" || comments === "unknown") {
    chips.push(createChip({ label: "? " + t("未知（.xls 需转换后才能统计）"), className: "oob mut" }));
  }
  if (chips.length) return chips;
  const dash = el("span", "dash");
  dash.textContent = "—";
  return [dash];
}

// ---------------------------------------------------------------------------
// PDF「大幅面页」——两个方向都超过 A4 约 15% 的页面（A3 及以上）
//
// 「跳过 A3 及更大的页面」开着时这些页不送翻译模型，原始矢量内容整页直传到输出 PDF。
// 判定只看纸张尺寸，跟页面内容是不是图纸无关——界面上的措辞一律用尺寸口径，不要写成
// 「图纸」，否则 A3 的宣传册被跳过时用户只会当成程序判错了。
//
// 三态和 Excel 的 image_count 完全同一套规矩，不再重复解释：数字 / null（数不出来，
// 例如加密或结构损坏的 PDF）/ 字段缺失（不适用）。「数不出来」永远不能渲染成 0。
// ---------------------------------------------------------------------------

interface OversizedPageStats {
  /** 已数清的大幅面页总数（数不出来的文件不参与求和）。 */
  pages: number;
  /** 幅面数不出来的文件数——对应后端 summary 的 oversized_page_count_unknown_files。 */
  unknownFiles: number;
  /** 产出了该字段的文件数。为 0 说明这批文件根本不产出（不是「数出来是 0」），整格不渲染。 */
  presentFiles: number;
}

/** 独立图片输入没有「页」的概念，后端给的 oversized_page_count 恒为 null（就是字段默认值，
 *  不是「数不出来」）。不排掉的话每张图片都会被记成一个「幅面未知的文件」。后端 summary
 *  里 pdf_items 那层过滤就是同一件事，两边必须一致。 */
function hasOversizedPageInfo(file: FileItem): boolean {
  return file.source_type !== "image" && hasOwn(file, "oversized_page_count");
}

function oversizedPageStats(files: FileItem[]): OversizedPageStats {
  const stats: OversizedPageStats = { pages: 0, unknownFiles: 0, presentFiles: 0 };
  for (const file of files) {
    if (!hasOversizedPageInfo(file)) continue;
    const count = maybeCount(file, "oversized_page_count");
    if (count === "n/a") continue;
    stats.presentFiles += 1;
    if (count === "unknown") stats.unknownFiles += 1;
    else stats.pages += count;
  }
  return stats;
}

/** 扫描统计条里的「大幅面页」格。字段整批缺失时返回 null，由 computeStats 过滤掉。 */
function oversizedPageStat(stats: OversizedPageStats): StatCell | null {
  if (stats.presentFiles === 0) return null;
  const label = t("大幅面页");
  const unknownNote = stats.unknownFiles > 0 ? t("其中 {0} 个文件无法确认幅面", stats.unknownFiles) : "";
  if (stats.pages > 0) {
    return {
      label,
      value: fmtNum(stats.pages),
      attn: true,
      sub: unknownNote || t("开启跳过后不翻译，原样保留"),
    };
  }
  // 一页都没数出来时不能写 0——那是「确认没有」的说法。
  if (stats.unknownFiles > 0) return { label, value: "—", dim: true, sub: unknownNote };
  return { label, value: "0", dim: true, sub: t("全部为 A4 及以下") };
}

/** 文件表「页数 / 尺寸」列里跟在页数后面的那半句；没什么可说时返回空串。 */
function oversizedPageNote(file: FileItem): { text: string; warn: boolean } | null {
  if (!hasOversizedPageInfo(file)) return null;
  const count = maybeCount(file, "oversized_page_count");
  if (count === "n/a") return null;
  if (count === "unknown") return { text: t("幅面未知"), warn: false };
  if (count <= 0) return null;
  // 「1 页 · 1 页 A3+」读起来像两件事。整份都是大幅面时直接说「整份都是 A3+」，
  // 部分时说「其中 N 页 A3+」，都跟在总页数后面，语义才连得上。
  const pages = num(file.page_count);
  if (pages > 0 && count >= pages) return { text: t("整份都是 A3+"), warn: true };
  return { text: t("其中 {0} 页 A3+", count), warn: true };
}

/** 整份文件都是大幅面页——开着跳过开关选它等于什么都不会翻，扫描阶段就得说。 */
function isAllOversized(file: FileItem): boolean {
  if (!hasOversizedPageInfo(file)) return false;
  const count = maybeCount(file, "oversized_page_count");
  const pages = num(file.page_count);
  return typeof count === "number" && pages > 0 && count >= pages;
}

/** 接入点 4 · 翻译完成后的结果说明所需的数据快照。 */
interface ExcelDoneNotice {
  fileCount: number;
  cellCount: number;
  stats: OutsideCellStats;
  anchorFrozen: number;
}

/**
 * anchor_frozen_count（调行高时被冻结锚点、改成固定尺寸的悬浮图片数）只有一个真实来源：
 * file_results[].anchor_frozen_count（core/task_runner.py 约 1235 行产出，经
 * _sanitize_task_data 后 int 0 也完整保留)。不对不存在的异形 payload 形状（summary/kpi/
 * 结果根/files/results/items）做兜底——分发前不写没有对应真实用户的兼容代码，这些位置
 * 在这个仓库里本来就不产出该字段；真缺失时按 0 处理（那句话不出现),不要猜别的字段名,
 * 免得字段哪天改名时 UI 悄悄挪到别处继续显示旧值、把改名的问题藏起来。
 */
function anchorFrozenTotal(result: JsonObject): number {
  const entries = result.file_results;
  if (!Array.isArray(entries)) return 0;
  return entries.reduce((sum: number, entry) => sum + num(record(entry).anchor_frozen_count), 0);
}

function buildExcelDoneNotice(st: SurfaceState, result: JsonObject): ExcelDoneNotice | null {
  // 只统计真正送去翻译的那些文件；用户取消勾选的不该出现在完成汇总里。
  const files = st.files.filter((f) => st.selected.has(f.path));
  const stats = outsideCellStats(files);
  const anchorFrozen = anchorFrozenTotal(result);
  if (!hasKnownOutside(stats) && stats.unknownFiles === 0 && anchorFrozen === 0) return null;
  return {
    fileCount: files.length,
    cellCount: files.reduce((sum, f) => sum + num(f.text_cell_count), 0),
    stats,
    anchorFrozen,
  };
}

function buildExcelDoneCard(notice: ExcelDoneNotice): HTMLElement {
  const box = el("div", "done");
  box.append(icon("warn", { className: "ico" }));
  const tx = el("div");
  const title = el("div", "tt");
  // 「翻译完成 · N 个文件」以前写在这里，和上面那张横幅的标题说的是同一件事。这张卡
  // 只负责一件事：说清哪些内容按设定没有翻译。
  title.textContent = t("按设定未翻译的内容 · 本批 {0} 个文件、{1} 个文本单元格", notice.fileCount, fmtNum(notice.cellCount));
  const body = el("div", "bd");
  const phrase = outsideCellPhrase(notice.stats);
  if (phrase) {
    body.append(document.createTextNode(t("另有 ")));
    const em = el("em");
    em.textContent = phrase;
    body.append(em, document.createTextNode(t("中的文字未翻译，已原样保留。")));
  }
  if (notice.stats.unknownFiles > 0) {
    body.append(document.createTextNode(t("{0} 个 .xls 文件的图片、文本框和批注未能统计，其中的文字同样未翻译。", notice.stats.unknownFiles)));
  }
  if (notice.anchorFrozen > 0) {
    body.append(document.createTextNode(t("{0} 张悬浮图片已固定尺寸，避免行高变化时被拉伸。", fmtNum(notice.anchorFrozen))));
  }
  tx.append(title, body);
  box.append(tx);
  return box;
}

function buildFmtBadge(label: string, warn: boolean): HTMLElement {
  const span = el("span", warn ? "fmt warn" : "fmt");
  span.textContent = label;
  return span;
}

function buildTableCard(surface: Surface, st: SurfaceState): HTMLElement {
  const card = createTaskListPanel({
    header: (head) => {
      if (!st.files.length) return;
      const countSpan = el("span");
      countSpan.textContent = t("已选 {0} / {1}", st.selected.size, st.files.length);
      const tools = el("div", "tc-tools");
      const selectAll = el("span", "linklike");
      selectAll.textContent = t("全选");
      selectAll.addEventListener("click", () => {
        st.selected = new Set(st.files.map((f) => f.path));
        rerender(surface);
      });
      const selectNone = el("span", "linklike");
      selectNone.textContent = t("全不选");
      selectNone.addEventListener("click", () => {
        st.selected = new Set();
        rerender(surface);
      });
      tools.append(selectAll, selectNone);
      head.append(countSpan, tools);
    },
  });
  if (scanBusy[surface]) {
    card.append(createEmptyState({
      title: t("扫描中…"),
      description: t("正在读取文件，请稍候。"),
      icon: SURFACE_ICON[surface],
    }));
    return card;
  }
  if (!st.files.length) {
    // 全部输入都被跳过时，清单是空的但原因是明摆着的——扫描到了文件，只是一个都不能翻。
    // 这时候还说「拖入文件开始」等于把刚发生的事抹掉，人会以为路径选错了反复重扫。
    const allSkipped = st.skipped.length > 0;
    // 只跳过一项（路径写错、文件损坏最常见）时把原因直接写出来，不用再点一次报告。
    const onlySkipped = st.skipped.length === 1 ? st.skipped[0] : null;
    const copy = allSkipped
      ? {
          title: onlySkipped
            ? t("「{0}」未能加入清单", text(onlySkipped.name, text(onlySkipped.relative_path, onlySkipped.path)))
            : t("扫描到的 {0} 项均未能加入清单", st.skipped.length),
          description: onlySkipped
            ? t("{0}。处理后请重新扫描。", tx(redactedText(onlySkipped.reason, t("原因未知")).replace(/[。.]?$/, "")))
            : t("点击下方「查看扫描报告」可查看各项原因；处理后请重新扫描。"),
        }
      : !st.hasEverCompleted
        ? SURFACE_FIRST_EMPTY[surface]
        : st.bannerInfo?.tone === "fail"
          ? { title: t("上一个任务未能完成"), description: t("失败原因见任务中心的完整报告；也可重新选择来源后重试。") }
          : SURFACE_BANNER_EMPTY[surface];
    const empty = createEmptyState({ title: copy.title, description: copy.description, icon: SURFACE_ICON[surface] });
    card.append(empty);
    const skipRow = buildSkipNoticeRow(surface, st);
    if (skipRow) card.append(skipRow);
    return card;
  }

  const tableWrap = el("div");
  tableWrap.style.cssText = "flex:1;overflow:auto";
  tableWrap.dataset.keepScroll = "files";
  const table = el("table", "tbl ws-files");
  table.append(buildTableHeadRow(surface));
  const resumeFiles = resumeFileMap(st);
  for (const file of st.files) {
    table.append(buildTableRow(surface, st, file, resumeFiles.get(file.path)));
  }
  tableWrap.append(table);
  card.append(tableWrap);

  const skipRow = buildSkipNoticeRow(surface, st);
  if (skipRow) card.append(skipRow);

  return card;
}

/** 「扫描时跳过 N 项」那一条。清单有内容和一个都没纳入时都要出现，所以单独拿出来。 */
function buildSkipNoticeRow(surface: Surface, st: SurfaceState): HTMLElement | null {
  if (!st.skipped.length) return null;
  const skipRow = el("div", "tc-head");
  skipRow.style.cssText = "border-top:1px solid var(--line);border-bottom:0";
  const warnIcon = icon("warn", { size: "sm" });
  warnIcon.style.color = "var(--warn)";
  const label = el("span");
  const names = st.skipped.slice(0, 2).map((s) => text(s.name, text(s.relative_path, s.path))).filter(Boolean);
  const extra = st.skipped.length > names.length;
  const nameList = names.join(t("、"));
  label.textContent = !names.length
    ? t("扫描时跳过 {0} 项", st.skipped.length)
    : extra
      ? t("扫描时跳过 {0} 项：{1} 等", st.skipped.length, nameList)
      : t("扫描时跳过 {0} 项：{1}", st.skipped.length, nameList);
  const skipTools = el("div", "tc-tools");
  const reportLink = el("span", "linklike");
  reportLink.textContent = t("查看扫描报告");
  reportLink.addEventListener("click", () => showSkipReportModal(surface, st));
  skipTools.append(reportLink);
  skipRow.append(warnIcon, label, skipTools);
  return skipRow;
}

function buildTableHeadRow(surface: Surface): HTMLTableRowElement {
  const row = el("tr");
  const check = el("th");
  check.style.width = "34px";
  row.append(check);
  const th = (label: string, numeric = false) => {
    const cell = el("th");
    if (numeric) cell.className = "num";
    cell.textContent = label;
    return cell;
  };
  if (surface === "excel") {
    // 「​」是窄窗口下表头唯一允许折行的位置（workspace.css 的 .ws-files th 用 keep-all）。
    row.append(th(t("文件")), th(t("格式")), th(t("工作表"), true), th(t("文本​单元格"), true), th(t("单元格外​内容")), th(t("状态")));
  } else if (surface === "word") {
    row.append(th(t("文件")), th(t("格式")), th(t("段落"), true), th(t("表格"), true), th(t("状态")));
  } else if (surface === "cad") {
    row.append(th(t("文件")), th(t("格式")), th(t("文字实体"), true), th(t("待翻译"), true), th(t("状态")));
  } else {
    row.append(th(t("文件")), th(t("类型")), th(t("大小")), th(t("页数 / ​尺寸")), th(t("状态")));
  }
  return row;
}

function buildTableRow(surface: Surface, st: SurfaceState, file: FileItem, resume?: ResumeFileInfo): HTMLTableRowElement {
  const row = el("tr");
  const checkCell = el("td");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.className = "ck";
  checkbox.checked = st.selected.has(file.path);
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) st.selected.add(file.path);
    else st.selected.delete(file.path);
    rerender(surface);
  });
  checkCell.append(checkbox);
  row.append(checkCell);

  // 文件名单行显示、放不下就省略号，悬停看全名——窄窗口下以前折成两行，整行被撑高。
  const nameCell = el("td", "fname");
  nameCell.textContent = fileLabel(file);
  nameCell.title = file.relative_path || fileLabel(file);
  row.append(nameCell);

  if (surface === "cad") {
    const fmtCell = el("td");
    fmtCell.append(buildFmtBadge((file.format ?? "").toUpperCase(), isRisky(surface, file)));
    // 没连转换工具的 DWG 读不出内容，计数是 0 不代表图纸里没字——写「—」，不写 0。
    const unreadable = st.historyOnly || isRisky(surface, file);
    // DWG 扫描时不转换，文字数在开始翻译后由任务统计（运行日志里有每张图纸的转换用时）。
    const pending = !unreadable && cadCountsPending(file);
    const textCell = el("td", pending ? "num pending" : "num");
    const candidateCell = el("td", pending ? "num pending" : "num");
    if (pending) {
      textCell.textContent = t("开始后统计");
      candidateCell.textContent = t("开始后统计");
      textCell.title = t("DWG 图纸在开始翻译后转换并统计文字");
      candidateCell.title = textCell.title;
    } else {
      textCell.textContent = unreadable ? "—" : fmtNum(num(file.text_count));
      candidateCell.textContent = unreadable ? "—" : fmtNum(num(file.candidate_count, num(file.text_count)));
    }
    row.append(fmtCell, textCell, candidateCell);
  } else if (surface === "excel" || surface === "word") {
    const fmtCell = el("td");
    fmtCell.append(buildFmtBadge((file.format ?? "").toUpperCase(), isRisky(surface, file)));
    row.append(fmtCell);
    const numCell1 = el("td", "num");
    numCell1.textContent = st.historyOnly ? "—" : surface === "excel" ? String(num(file.sheet_count, file.sheets?.length ?? 0)) : String(num(file.paragraph_count));
    const numCell2 = el("td", "num");
    numCell2.textContent = st.historyOnly ? "—" : surface === "excel" ? fmtNum(num(file.text_cell_count)) : String(num(file.table_count));
    row.append(numCell1, numCell2);
    if (surface === "excel") {
      const outsideCell = el("td");
      for (const chip of buildOutsideCellChips(file)) outsideCell.append(chip);
      row.append(outsideCell);
    }
  } else {
    const typeCell = el("td");
    typeCell.append(buildFmtBadge((file.format ?? file.source_type ?? "").toUpperCase(), false));
    row.append(typeCell);
    const sizeCell = el("td");
    sizeCell.textContent = st.historyOnly ? "—" : formatSizeKb(file.size_kb);
    row.append(sizeCell);
    const dimCell = el("td");
    if (file.source_type === "image") {
      dimCell.textContent = "—";
    } else {
      dimCell.append(document.createTextNode(file.page_count === undefined && st.historyOnly ? "—" : t("{0} 页", num(file.page_count))));
      const note = oversizedPageNote(file);
      if (note) {
        dimCell.append(document.createTextNode(" · "));
        const span = el("span");
        span.style.color = note.warn ? "var(--warn)" : "var(--ink-3)";
        if (note.warn) span.style.fontWeight = "600";
        span.textContent = note.text;
        dimCell.append(span);
      }
    }
    row.append(dimCell);
  }

  const statusCell = el("td", "fstatus");
  const liveProgress = file.file_id ? st.task?.fileProgress.get(file.file_id) : undefined;
  const outcome = (file.file_id ? st.fileOutcomes.get(file.file_id) : undefined) ?? st.fileOutcomes.get(fileOutcomeKey(fileLabel(file)));
  if (!st.selected.has(file.path)) {
    statusCell.append(createChip({ label: t("未勾选"), tone: "mute" }));
  } else if (liveProgress) {
    statusCell.append(createChip({ label: fileProgressLabel(liveProgress, st.task?.task.state), tone: fileProgressTone(liveProgress, st.task?.task.state) }));
  } else if (outcome) {
    // 跑过之后这一列说的是结果，不再说开跑前的预判——那时候的「需先转换」已经成了旧闻。
    const chip = createChip({
      label: outcome.label,
      tone: outcome.tone ?? (outcome.produced ? "ok" : "dgr"),
      icon: outcome.produced && !outcome.tone ? undefined : "warn",
    });
    if (outcome.detail) chip.title = outcome.detail;
    statusCell.append(chip);
  } else if (resume) {
    // 选了「接着上次继续」之后，这一列说的是「这份文件本次会怎么处理」，比开跑前的
    // 格式预判更贴近用户此刻要确认的事。
    statusCell.append(resumeChip(surface, resume));
  } else if (isRisky(surface, file)) {
    statusCell.append(createChip({ label: surface === "cad" ? t("需连接转换工具") : t("需先转换"), tone: "warn", icon: "warn" }));
  } else if (surface === "pdf" && isAllOversized(file)) {
    // 不看开关状态：开关在右栏，用户可能来回切，而「这份文件整份都是大幅面」是文件本身的
    // 事实。开着跳过时它一页都不会翻，提前说出来比跑完再发现强。
    statusCell.append(createChip({ label: t("整份均为大幅面"), tone: "warn", icon: "warn" }));
  } else {
    // 这一列以前对「一切正常、等着开跑」的文件是空白的，看上去像信息没加载出来。
    statusCell.append(createChip({ label: t("未开始"), tone: "mute" }));
  }
  row.append(statusCell);
  return row;
}

function showSkipReportModal(surface: Surface, st: SurfaceState): void {
  const body: (string | HTMLElement)[] = st.skipped.length
    ? st.skipped.map((s) => text(s.name, text(s.relative_path, s.path)) + " — " + tx(redactedText(s.reason, t("原因未知"))))
    : [t("没有被跳过的项目。")];
  openModal({
    tone: "warn",
    icon: "warn",
    title: t("扫描报告 · {0}", SURFACE_LABEL[surface]),
    body,
    actions: [{ label: t("关闭") }],
  });
}

// ---------------------------------------------------------------------------
// 左栏 · 运行中：进度卡 / 恢复卡（聚合数据，PDF 见下方数据缺口说明）/ 日志+逐文件卡
// ---------------------------------------------------------------------------

function buildProgressCard(surface: Surface, st: SurfaceState): HTMLElement {
  const local = st.task!;
  let note: string | undefined;
  if (local.streamState === "reconnecting") {
    note = t("与后台服务的连接暂时中断，正在自动重新连接；已有进度不会丢失，也不会重复处理。");
  } else {
    const silence = silenceSeconds(local);
    const waiting = !local.task.terminal
      && local.task.state !== "paused"
      && local.task.state !== "pausing"
      && silence >= SILENCE_NOTICE_SECONDS;
    // 安全停止时不再发新请求，安静的这段多半是在写文件，只说「等接口」会误导。
    if (waiting) {
      note = local.task.state === "stopping"
        ? t("已等待 {0} 秒：正在收尾，待进行中的请求返回后将已翻译的内容写入文件。大文档耗时较长，程序仍在正常运行。", silence)
        : t("已等待 {0} 秒：此批内容已发送至模型，正在等待返回译文，程序仍在正常运行。", silence);
    }
  }
  return createTaskProgressPanel({
    stage: local.stageName,
    phase: redactedText(local.phaseName, ""),
    percent: local.percent,
    state: local.task.state,
    monitor: buildMonChips(surface, local),
    note,
  });
}

function buildMonChips(surface: Surface, local: LocalTask): HTMLElement[] {
  const chip = (label: string, tone: ChipTone) => createChip({ label, tone });
  // 计数为 0 的临时状态不占位：一排「重试中 0 · 已恢复 0 · 未恢复 0」既说明不了
  // 任何事，又让人以为程序正在做这三件事。只有真的发生过才出现。
  const chipIfAny = (chips: HTMLElement[], value: number, label: string, tone: ChipTone) => {
    if (value > 0) chips.push(chip(label, tone));
  };
  if (surface === "word" && local.wordRecovery) {
    const r = local.wordRecovery;
    const chips: HTMLElement[] = [];
    chipIfAny(chips, num(r.retry_round), t("第 {0} 轮重译", num(r.retry_round)), "tint");
    chipIfAny(chips, num(r.semantic_processing_count), t("复查中 {0}", num(r.semantic_processing_count)), "tint");
    chipIfAny(chips, num(r.semantic_accepted_count), t("复查后采用 {0}", num(r.semantic_accepted_count)), "ok");
    const unresolved = num(r.retry_unresolved_count, num(r.unresolved_count));
    chipIfAny(chips, unresolved, t("仍未完成 {0}", unresolved), "warn");
    return chips;
  }
  if (surface === "pdf" && local.pdfPageRecovery) {
    const r = local.pdfPageRecovery;
    const chips: HTMLElement[] = [];
    // completed_pages 是「跑完的页」，占位页（生成失败后塞回原页）也算在里面。
    // 直接拿它当「已生成」会把失败页说成生成成功，所以先扣掉占位页，失败数另开一格。
    // 大幅面页同理：跳过翻译、原样保留，也算「跑完」，但不是「已生成」。
    const failed = num(r.placeholder_page_count);
    const skipped = num(r.skipped_oversize_page_count);
    const generated = Math.max(0, num(r.completed_pages) - failed - skipped);
    const toGenerate = Math.max(0, num(r.total_pages) - skipped);
    if (hasOwn(r, "completed_pages")) chips.push(chip(t("已生成 {0} / {1} 页", generated, toGenerate), "ok"));
    chipIfAny(chips, skipped, t("原样保留 {0} 页", skipped), "mute");
    chipIfAny(chips, failed, t("生成失败 {0} 页", failed), "dgr");
    chipIfAny(chips, num(r.retrying_page_count), t("重试中 {0}", num(r.retrying_page_count)), "warn");
    chipIfAny(chips, num(r.recovered_page_count), t("已恢复 {0}", num(r.recovered_page_count)), "mute");
    return chips;
  }
  return [];
}

function buildWordRecoveryCard(local: LocalTask): HTMLElement | null {
  const r = local.wordRecovery;
  if (!r || !Object.keys(r).length) return null;
  const card = el("div", "card");
  card.style.padding = "13px 16px";
  const head = el("div", "tc-head");
  head.style.cssText = "padding:0 0 8px;border:0";
  const b = el("b");
  b.textContent = t("自动重译与复查");
  head.append(b);
  card.append(head);
  const note = el("p", "ws-note");
  // ws-note 默认居中（给空态用的），这张卡和其他卡片一样左对齐。
  note.style.textAlign = "left";
  note.textContent = t("译文为空、明显不完整或未通过质量检查的段落将自动重译；重译后仍存疑、经模型复查后才采用的译文不会写入记忆库。");
  card.append(note);
  return card;
}

// ---------------------------------------------------------------------------
// 运行卡片文件列表上的「这个文件到底跳过了什么」标签
//
// 两个功能都在悄悄地少翻内容，用户看到「已完成」时默认理解是「整个文件都翻了」。跳了
// 多少必须逐文件说出来，而且失败要跟成功长得不一样——「开了保护但没找到正文起点」和
// 「开了保护跳过了 23 段」都表现为「已完成」，含义却相反。
//
// 不按 surface 分支，改看 payload 里有没有对应字段：Word 的条目带 front_matter，PDF 的
// 带 skipped_oversize_page_count。字段哪天没了标签自己消失，不会显示旧值。
// ---------------------------------------------------------------------------

/** file_results 里对应某个源文件的那一条。Word 给 source_path（全路径），PDF 只给 name。 */
function fileResultFor(result: JsonObject, path: string): JsonObject | null {
  const entries = result.file_results;
  if (!Array.isArray(entries)) return null;
  const base = path.split("/").pop() ?? path;
  for (const entry of entries) {
    const item = record(entry);
    if (text(item.source_path) === path || text(item.name) === base) return item;
  }
  return null;
}

/** file_results 上某个数值字段的合计。字段不存在的条目按 0 计——不猜别的字段名。 */
/** 终态结果里的逐文件行。正常完成走 `file_results`，中止和失败走 `files`——这是终态
 *  契约的键名（core/task_runner.py 的 StoppedMsg / ErrorMsg 都只给 `files`）。只认前者
 *  的话，一个中途停下来的任务在清单和小结里都看不出哪几个文件已经写出来、有几页是占位。 */
function terminalFileResults(result: JsonObject): JsonObject[] {
  for (const key of ["file_results", "files"]) {
    const entries = result[key];
    if (Array.isArray(entries) && entries.length) return entries.map(record);
  }
  return [];
}

/** 逐文件行上某个数值字段的合计，只算真的写出了文档的文件。页级小结说的是「打开输出能看到什么」：一个整份都失败的
 *  文件根本没有输出 PDF，它那几页占位图只作为失败素材留在报告里。把它们也算进「N 页已放失败
 *  占位页」，用户按这个数去输出目录里数，永远差几页——这一句本来就是为了让数字对得上才改的。
 *  没生成的文件另有「N 个文件没能生成」那一句交代，逐文件那一行还写着失败原因。 */
function producedFileResultTotal(result: JsonObject, key: string): number {
  return terminalFileResults(result)
    .filter((entry) => fileResultProduced(entry))
    .reduce((sum: number, entry) => sum + num(entry[key]), 0);
}

/** 所有文件实际被「保护封面和目录」跳过的段落合计；没找到正文起点的文件本来就是 0。
 *  同样只算写出了文档的文件——没生成的文件里「保护了几段」没有任何意义。 */
function frontMatterTotal(result: JsonObject): number {
  const entries = terminalFileResults(result).filter((entry) => fileResultProduced(entry));
  return entries.reduce((sum: number, entry) => {
    const fm = record(entry.front_matter);
    return fm.requested && fm.found ? sum + num(fm.protected_paragraph_count) : sum;
  }, 0);
}

function buildFileSkipChips(result: JsonObject, path: string): HTMLElement[] {
  const item = fileResultFor(result, path);
  if (!item) return [];
  const chips: HTMLElement[] = [];

  const frontMatter = record(item.front_matter);
  if (frontMatter.requested) {
    if (frontMatter.found) {
      chips.push(createChip({ label: t("跳过开头 {0} 段", num(frontMatter.protected_paragraph_count)), tone: "tint" }));
    } else {
      // 一段都没保护，全文照常翻译了。这不是错误，但和用户开这个开关的预期不符，必须显眼。
      chips.push(createChip({ label: t("未识别到正文起点"), tone: "warn", icon: "warn" }));
    }
  }

  const oversized = num(item.skipped_oversize_page_count);
  if (oversized > 0) chips.push(createChip({ label: t("跳过 {0} 页 A3+", oversized), tone: "tint" }));

  return chips;
}

// 界面上只需要滚动查看，不需要把整条历史都塞进 DOM——200 条封顶，够回溯最近的操作，
// 完整记录另外随诊断归档，不靠这里兜底。
const LOG_VIEW_LIMIT = 200;

/** 日志行号发号器（模块级、跨 surface 共用，只要不重号就够用）。 */
let nextLogSeq = 1;

/** `sharesHeight`：同一栏里还有逐页审核表（PDF 运行中）。日志卡片默认按内容撑高，
 *  在 860px 高的窗口里会把逐页表挤得只剩表头；这时两张卡片对半分高度，日志区和文件
 *  列表各自滚动。 */
function buildLogCard(local: LocalTask, sharesHeight = false): HTMLElement {
  const card = createTaskLogPanel({
    entries: local.logs.map((entry) => ({ seq: entry.seq, time: entry.time, message: entry.message, level: entry.level })),
    limit: LOG_VIEW_LIMIT,
  });
  if (sharesHeight) {
    card.style.flex = "3 1 0%"; // 与逐页审核卡片（运行中 flex:3 1 0%）对半分
    const log = card.querySelector<HTMLElement>(".log");
    if (log) {
      log.style.flex = "1 1 auto";
      log.style.maxHeight = "none";
    }
  }
  // 文件很多时这一栏不能无限往下长：日志卡片会被挤没，整页还会跟着出现滚动条。
  const fileList = el("div");
  fileList.style.cssText = `flex:0 0 auto;max-height:${sharesHeight ? 120 : 184}px;overflow-y:auto`;
  fileList.dataset.keepScroll = "log-files";
  const result = record(local.task.result);
  const rows = [...local.fileProgress.values()];
  if (!rows.length) {
    const state = states[local.task.surface as Surface];
    for (const file of state.files) {
      if (!state.selected.has(file.path) || !file.file_id) continue;
      rows.push({ file_id: file.file_id, name: file.name ?? file.path.split(/[\\/]/).pop() ?? file.path, relative_path: file.relative_path ?? "", state: "waiting", phase: "", completed: 0, total: 0, result: {}, revision: 0 });
    }
  }
  for (const progress of rows) {
    const row = el("div", "filerow");
    row.append(createChip({ label: fileProgressLabel(progress, local.task.state), tone: fileProgressTone(progress, local.task.state) }));
    const nm = el("span", "nm");
    nm.textContent = progress.name || progress.relative_path;
    // 名字太长时单行省略，鼠标停上去看全名（带相对路径，同名文件分得清）。
    nm.title = progress.relative_path || progress.name;
    row.append(nm);
    for (const chip of buildFileSkipChips(result, progress.relative_path || progress.name)) row.append(chip);
    fileList.append(row);
  }
  card.append(fileList);
  return card;
}

// ---------------------------------------------------------------------------
// 右栏
// ---------------------------------------------------------------------------

function buildColRight(surface: Surface, st: SurfaceState, active: boolean): HTMLElement {
  const col = el("div", "col-r");
  // 只有「运行中」才锁定右栏。未扫描不是锁定理由：这些开关写的是长期设置，
  // 用户本来就该在挑文件之前先把它们调好。
  const card = el("div", "card runpanel");
  const scroll = el("div", active ? "rp-scroll dis" : "rp-scroll");
  scroll.dataset.keepScroll = "run-settings";

  const title = el("div", "rp-title");
  title.textContent = t("运行设置");
  if (active) {
    const lockChip = createChip({ label: t("任务中锁定"), tone: "mute" });
    lockChip.style.marginLeft = "auto";
    title.append(lockChip);
  }
  scroll.append(title);

  const langSec = el("div", "rp-sec");
  langSec.textContent = t("语言");
  scroll.append(langSec);

  // recentKey 按 surface + 方向分开：Excel 的目标语言和 Word 的目标语言是两套习惯，
  // 混在一起会让「最近使用」总是显示另一个页面刚选过的语种。
  const targetField = createLanguageField({
    label: t("目标语言"),
    options: localizedLanguageOptions(languageOptions.target),
    value: st.targetLang,
    disabled: active,
    recentKey: `${surface}-target`,
    onReload: () => void reloadBootstrap(surface),
    onChange: (value) => {
      st.targetLang = value;
      void persistSettings(surface === "pdf" ? nestedPatch("pdf.target_lang", value) : { [`${surface}_target_lang`]: value });
      // 历史产物的文件名里带目标语言，换了语言这份检测就对不上号了。静默作废，不弹提示：
      // 用户此刻在做的是选语言，不是在处理续译。
      if (st.resumeInfo && st.resumeDetectedLang && st.resumeDetectedLang !== value) {
        resetResumeState(st);
        rerender(surface);
      }
      if (surface === "cad") rescanAfterScanInputChange(surface);
    },
  });
  scroll.append(targetField.root);

  if (surface !== "pdf") {
    // /api/languages 的 source_options 头一项本来就是「自动识别」，只有后端没给时才补。
    // （旧的原生 select 无条件在前面插一条「自动检测」，结果两条 auto 并排出现。）
    const sourceLangOptions: LanguageOption[] = languageOptions.source.some((o) => o.code === "auto")
      ? languageOptions.source
      : [{ code: "auto", display_name: t("自动识别"), aliases: ["auto", "自动识别", "自动检测", t("自动识别")] }, ...languageOptions.source]; // i18n-ignore 中文别名供搜索匹配
    const sourceField = createLanguageField({
      label: t("源语言"),
      options: localizedLanguageOptions(sourceLangOptions),
      value: st.sourceLang,
      disabled: active,
      recentKey: `${surface}-source`,
      onReload: () => void reloadBootstrap(surface),
      onChange: (value) => {
        st.sourceLang = value;
        void persistSettings({ [`${surface}_source_lang`]: value });
        if (surface === "cad") rescanAfterScanInputChange(surface);
      },
    });
    scroll.append(sourceField.root);
  }

  const typeSec = el("div", "rp-sec");
  typeSec.textContent = t("{0} 选项", SURFACE_LABEL[surface]);
  scroll.append(typeSec);

  const resumeAuto = Boolean(activeResumeDir(st));
  for (const toggle of TOGGLES[surface]) {
    // 「接着上次继续」时补译是这次任务的定义，不是一个可选项：payload 无论如何都按补译发
    // （见 buildPayload），开关再放开就会出现「界面显示关、实际按开跑」的假象。锁住并给
    // 角标说明，想全译走横幅上的「改为全部重译」。
    const lockedByResume = resumeAuto && toggle.key === "untranslated";
    const row = createSwitchRow({
      label: toggle.label,
      hint: lockedByResume
        ? t("{0}选择「继续上次翻译」时自动开启；如需全部重译，请点击文件列表上方横幅中的「改为全部重译」。", toggle.hint)
        : toggle.hint,
      checked: Boolean(st.toggles.get(toggle.key)),
      disabled: active || lockedByResume,
      onChange: (checked) => void handleToggleChange(surface, st, toggle, checked),
    });
    if (lockedByResume) {
      const badge = el("span", "resume-auto");
      badge.textContent = t("已自动开启");
      row.insertBefore(badge, row.querySelector(".sw"));
    }
    scroll.append(row);
  }

  if (!active) {
    const foldContent = buildTaskFold(surface, st);
    // 名字不能叫「本次任务」：里面的专业领域和输出位置都是改了就存的长期设置，
    // 「本次」会让人以为下次要重设一遍（尤其是把输出位置固定到自定义文件夹的人）。
    // 默认展开——它不是高级选项，是每次开工前会看一眼的东西。
    const fold = createFold({
      title: t("更多设置"),
      content: foldContent,
      open: st.moreSettingsOpen,
      onToggle: (open) => { st.moreSettingsOpen = open; },
    });
    scroll.append(fold.root);
  }

  card.append(scroll);
  card.append(buildRightFoot(surface, st, active));
  col.append(card);
  return col;
}

/** 这个开关落盘要写的补丁；pathKind "none" 不落盘，返回 null。 */
function togglePatch(surface: Surface, toggle: ToggleDef, checked: boolean): JsonObject | null {
  if (toggle.pathKind === "flat" && toggle.path) return nestedPatch(toggle.path, checked);
  if (toggle.pathKind === "output" && toggle.path) return nestedPatch(`${outputSettingPathPrefix(surface)}.${toggle.path}`, checked);
  return null;
}

/** 两份 nestedPatch 合并成一份（同前缀的嵌套对象拼在一起），一次 PUT 写完。 */
function mergePatches(base: JsonObject, extra: JsonObject): JsonObject {
  const out: JsonObject = { ...base };
  for (const [key, value] of Object.entries(extra)) {
    const existing = out[key];
    if (existing && typeof existing === "object" && !Array.isArray(existing) && value && typeof value === "object" && !Array.isArray(value)) {
      out[key] = mergePatches(existing as JsonObject, value as JsonObject);
    } else {
      out[key] = value;
    }
  }
  return out;
}

async function handleToggleChange(surface: Surface, st: SurfaceState, toggle: ToggleDef, checked: boolean): Promise<void> {
  const prevChecked = Boolean(st.toggles.get(toggle.key));
  const prevPartner = toggle.exclusiveWith ? Boolean(st.toggles.get(toggle.exclusiveWith)) : undefined;
  st.toggles.set(toggle.key, checked);
  let patch = togglePatch(surface, toggle, checked);
  if (toggle.exclusiveWith) {
    if (checked) st.toggles.set(toggle.exclusiveWith, false);
    // 互斥对每次都两半一起落盘，开、关都写。只写自己那一半的话，磁盘上会留下
    // 「两个都开」的脏数据，下次启动水合出来就是「设置被默认替代掉」的样子——
    // 正是用户报的那个 bug。老版本已经留下的脏数据也靠这一步顺手治好：水合时
    // 界面按后端裁决只亮一个，用户随便动一下这对开关，写盘的就是界面上的两个值。
    const partner = TOGGLES[surface].find((t) => t.key === toggle.exclusiveWith);
    const partnerPatch = partner ? togglePatch(surface, partner, Boolean(st.toggles.get(partner.key))) : null;
    if (partnerPatch) patch = patch ? mergePatches(patch, partnerPatch) : partnerPatch;
  }
  if (patch) {
    await persistSettingsOrRevert(surface, patch, () => {
      st.toggles.set(toggle.key, prevChecked);
      if (toggle.exclusiveWith && prevPartner !== undefined) st.toggles.set(toggle.exclusiveWith, prevPartner);
    }, toggle.label);
  }
  rerender(surface);
  if (SCAN_INPUT_TOGGLES.has(toggle.key)) rescanAfterScanInputChange(surface);
}

/** 这几个开关会改变扫描结果本身（PDF 收不收独立图片、CAD 数不数图块里的字）。 */
const SCAN_INPUT_TOGGLES = new Set(["pdfImages", "cadBlockText"]);

/** 扫描结果依赖的输入变了，就按「选完来源自动扫描」的同一规矩自动重扫一次。
 *  以前 CAD 改了目标语言，「待翻译」还停在旧语言的数字，要用户自己点「重新扫描」。
 *  还没选来源、或任务正在跑时什么都不做。 */
function rescanAfterScanInputChange(surface: Surface): void {
  const st = states[surface];
  if (!normalizeUserPath(st.sourcePath)) return;
  if (st.task && !st.task.task.terminal) return;
  void runScan(surface);
}

function buildTaskFold(surface: Surface, st: SurfaceState): HTMLElement {
  const wrap = el("div");
  if (surface !== "pdf") {
    const domainField = el("div", "field");
    const label = el("label");
    label.textContent = t("专业领域") + " ";
    // 即使当前选的是「无」也保留入口：进入设置后会落到「＋新增提示词」，
    // 让用户可以从空配置直接建立自己的提示词。
    const link = el("span", "linklike");
    link.style.fontSize = "11px";
    link.textContent = t("编辑提示词 ↗ 设置");
    link.addEventListener("click", () => navigate("settings", {
      page: "params",
      paramsSurface: surface,
      domainPreset: st.domainPreset,
    }));
    label.append(link);
    domainField.append(label);
    const select = createSelectField({
      label: "",
      // 「无」放在第一项，让「不加任何领域限定」一眼可见。
      options: domainOptionsFor(surface, st.domainPreset).map((v) => ({ value: v, label: tx(v) })),
      value: st.domainPreset,
      onChange: (value) => {
        const prev = st.domainPreset;
        st.domainPreset = value;
        rerender(surface);
        void persistSettingsOrRevert(surface, { [`${surface}_domain_preset`]: value }, () => { st.domainPreset = prev; }, t("专业领域"));
      },
    });
    domainField.append(select.root);
    wrap.append(domainField);
  }

  const outputField = createField(t("输出位置"), buildOutputRadioRow(surface, st));
  wrap.append(outputField);
  return wrap;
}

function domainOptionsFor(surface: Exclude<Surface, "pdf">, current: string): string[] {
  const builtIns = ["无", "同步工程场景", "资料管理场景", "行政生活化场景"]; // i18n-ignore 发给后端的专业领域预设值，显示时 tx()
  const disabled = Array.isArray(settings[`${surface}_domain_disabled_presets`])
    ? (settings[`${surface}_domain_disabled_presets`] as unknown[]).filter((value): value is string => typeof value === "string")
    : [];
  const custom = settings[`${surface}_domain_custom_prompts`];
  const customNames = custom && typeof custom === "object" && !Array.isArray(custom)
    ? Object.keys(custom as JsonObject)
    : [];
  const options = builtIns.filter((name) => name === "无" || !disabled.includes(name)); // i18n-ignore 与预设值比较
  for (const name of customNames) if (!options.includes(name)) options.push(name);
  if (current && !options.includes(current)) options.push(current);
  return options;
}

function buildOutputRadioRow(surface: Surface, st: SurfaceState): HTMLElement {
  const row = el("div", "radio-row");
  const name = `out-${surface}`;
  const makeOption = (value: boolean, label: string) => {
    const wrapLabel = el("label");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = name;
    radio.checked = st.useCustomOutputDir === value;
    radio.addEventListener("change", () => {
      const prev = st.useCustomOutputDir;
      st.useCustomOutputDir = value;
      rerender(surface);
      void persistSettingsOrRevert(
        surface,
        nestedPatch(`${outputSettingPathPrefix(surface)}.use_custom_output_dir`, value),
        () => { st.useCustomOutputDir = prev; },
        t("输出位置"),
      );
    });
    wrapLabel.append(radio, document.createTextNode(` ${label}`));
    return wrapLabel;
  };
  row.append(makeOption(false, t("源目录内")), makeOption(true, t("自定义")));
  const container = el("div");
  container.append(row);
  if (st.useCustomOutputDir) {
    const { root, input } = createTextField({
      label: "",
      value: st.customOutputDir,
      placeholder: t("自定义输出目录路径…"),
      onInput: (value) => {
        st.customOutputDir = value;
      },
    });
    const saveCustomDir = (): void => {
      // 落盘前的旧值要从「上一次成功落盘的值」取，不能取 st.customOutputDir 自己——
      // 打字过程中每敲一下 onInput 都已经把它改成了正在输入的新值，到这里已经晚了。
      // outputRecord() 读的是 settings 模块变量，只在 persistSettings 成功时才会更新，
      // 是这一刻唯一还留着「磁盘上真实值」的地方。
      const prevDir = text(outputRecord(surface).custom_output_dir);
      void persistSettingsOrRevert(
        surface,
        nestedPatch(`${outputSettingPathPrefix(surface)}.custom_output_dir`, st.customOutputDir),
        () => { st.customOutputDir = prevDir; },
        t("自定义输出目录"),
      );
    };
    input.addEventListener("change", saveCustomDir);
    // 和来源一栏一样：输入框后面跟一个「浏览」，在系统选择框里挑文件夹，不必手动粘贴路径。
    const browseBtn = createButton({
      label: t("浏览"),
      icon: "folder",
      onClick: () => void (async () => {
        let picked: unknown;
        try {
          picked = await open({ title: t("选择输出文件夹"), directory: true, multiple: false, defaultPath: st.customOutputDir.trim() || undefined });
        } catch (error) {
          showToast({ message: redactedText((error as Error)?.message, t("选择输出文件夹失败。")), error: true });
          return;
        }
        if (typeof picked !== "string" || !picked.trim()) return;
        st.customOutputDir = picked;
        input.value = picked;
        saveCustomDir();
      })(),
    });
    root.style.margin = "0";
    root.style.flex = "1";
    root.style.minWidth = "0";
    const line = el("div");
    line.style.display = "flex";
    line.style.gap = "8px";
    line.style.alignItems = "center";
    line.style.marginTop = "6px";
    line.append(root, browseBtn);
    container.append(line);
  }
  return container;
}

function buildRightFoot(surface: Surface, st: SurfaceState, active: boolean): HTMLElement {
  if (!active) {
    const busy = submittingSurfaces.has(surface);
    const disabled = st.selected.size === 0 || !st.sourcePath.trim() || scanBusy[surface] || busy;
    return createTaskControlFoot({ idle: {
      label: busy
        ? t("正在启动…")
        : submitFailures.has(surface) ? t("重试启动") : disabled ? t("开始翻译") : t("开始翻译（{0} 个文件）", st.selected.size),
      disabled,
      onClick: () => void startTask(surface, st),
    } });
  }

  const task = st.task!;
  // 只有 PDF 和 CAD 支持暂停：PDF 停止提交新页面，CAD 停止发出新的一批翻译请求（单张图纸也能停）。
  // Excel / Word 不给暂停按钮——后端会直接拒绝，按了只会弹一条报错。
  const pausable = surface === "pdf" || surface === "cad";
  return createTaskControlFoot({
    idle: { label: t("开始翻译"), disabled: true, onClick: () => undefined },
    active: {
      paused: pausable && task.task.state === "paused",
      pausing: pausable && task.task.state === "pausing",
      onResume: () => void resumeTask(surface, st),
      onPause: pausable ? () => void pauseTask(surface, st) : undefined,
      onStop: () => confirmStopTask(surface, st),
      pauseLabel: t("暂停"),
      finishPaused: surface === "pdf" && task.task.state === "paused"
        ? { label: t("结束任务"), onClick: () => void confirmEndPaused(st), note: t("结束后将保存已完成的页面，并生成相应报告") }
        : undefined,
      stopLabel: t("停止"),
    },
  });
}

// ---------------------------------------------------------------------------
// 续译（接着上次继续）
//
// 扫描完成 → 后端给出 previous_output → 弹窗二选一 → 决定写进 SurfaceState，
// 由文件表上方的横幅/提示条、状态列的芯片、右栏开关的角标一起把决定说清楚，
// 最后在 buildPayload() 里变成 resume_output_dir（+ excel/word 的 untranslated_only）。
// 两种选择都写进新的时间戳输出目录，历史产物只读不改。
// ---------------------------------------------------------------------------

function numOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function parsePreviousOutput(raw: unknown): PreviousOutput | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const obj = raw as JsonObject;
  const summaryRaw = record(obj.summary);
  const candidates: ResumeCandidate[] = (Array.isArray(obj.candidates) ? obj.candidates : [])
    .map((item) => record(item))
    .filter((item) => text(item.dir))
    .map((item) => ({ dir: text(item.dir), label: text(item.label, text(item.timestamp)), timestamp: text(item.timestamp) }));
  const files: ResumeFileInfo[] = (Array.isArray(obj.files) ? obj.files : [])
    .map((item) => record(item))
    .filter((item) => text(item.path))
    .map((item) => ({
      path: text(item.path),
      status: (["full", "partial", "none", "found", "diverged"] as const).includes(item.status as ResumeFileStatus)
        ? (item.status as ResumeFileStatus)
        : "found",
      matched_output: text(item.matched_output) || null,
      untranslated_count: numOrNull(item.untranslated_count),
      page_done: numOrNull(item.page_done),
      page_total: numOrNull(item.page_total),
    }));
  return {
    target_lang: text(obj.target_lang),
    candidates,
    selected_dir: text(obj.selected_dir) || null,
    files,
    summary: {
      full: num(summaryRaw.full),
      partial: num(summaryRaw.partial),
      none: num(summaryRaw.none),
      found: num(summaryRaw.found),
      diverged: num(summaryRaw.diverged),
      page_done: numOrNull(summaryRaw.page_done),
      page_total: numOrNull(summaryRaw.page_total),
      last_task_stopped: typeof summaryRaw.last_task_stopped === "boolean" ? summaryRaw.last_task_stopped : null,
      last_time_label: text(summaryRaw.last_time_label) || null,
    },
  };
}

/** 检测结果里有没有值得开口的命中。none 不算——那只是「这个文件没翻过」。 */
function resumeHitCount(info: PreviousOutput): number {
  return info.summary.full + info.summary.partial + info.summary.found + info.summary.diverged;
}

/**
 * 当前仍然有效的检测结果。产物文件名里带目标语言，用户改了目标语言之后这份检测就
 * 对不上号了：这里直接判空，横幅、芯片、角标、payload 全都跟着一起消失，不额外弹提示。
 */
function resumeState(st: SurfaceState): PreviousOutput | null {
  const info = st.resumeInfo;
  if (!info) return null;
  if (st.resumeDetectedLang && st.resumeDetectedLang !== st.targetLang) return null;
  return info;
}

/** 真的要按「接着上次继续」跑这一次任务吗。没有底稿目录就不算数。 */
function activeResumeDir(st: SurfaceState): string | null {
  if (st.resumeDecision !== "resume") return null;
  const info = resumeState(st);
  return info?.selected_dir ?? null;
}

/** 每次扫描（含切换历史目录的重扫）都把续译状态整体归零，不留上一次的残留。 */
function resetResumeState(st: SurfaceState): void {
  restoreUntranslatedToggle(st);
  st.resumeInfo = null;
  st.resumeDecision = null;
  st.resumeDetectedLang = "";
  st.resumeNoticeDismissed = false;
}

/** 进入续译时把「仅补译未翻译内容」自动打开，原值留着还原用（这个开关不落盘，只影响本次）。 */
function forceUntranslatedToggle(surface: Surface, st: SurfaceState): void {
  if (surface === "pdf") return;
  if (st.resumeToggleRestore === null) st.resumeToggleRestore = Boolean(st.toggles.get("untranslated"));
  st.toggles.set("untranslated", true);
}

function restoreUntranslatedToggle(st: SurfaceState): void {
  if (st.resumeToggleRestore === null) return;
  st.toggles.set("untranslated", st.resumeToggleRestore);
  st.resumeToggleRestore = null;
}

function chooseResume(surface: Surface, st: SurfaceState): void {
  st.resumeDecision = "resume";
  st.resumeNoticeDismissed = false;
  forceUntranslatedToggle(surface, st);
  rerender(surface);
}

function chooseFresh(surface: Surface, st: SurfaceState): void {
  st.resumeDecision = "fresh";
  restoreUntranslatedToggle(st);
  rerender(surface);
}

function basename(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

/** 补译数量的量词按 surface 换：Excel 数格、Word 数段、PDF 数页。 */
function resumePendingLabel(surface: Surface, file: ResumeFileInfo): string {
  if (surface === "pdf") {
    const total = file.page_total;
    const done = file.page_done;
    if (total !== null && done !== null && total > done) return t("部分翻译 · 补 {0} 页", total - done);
    return t("部分翻译 · 补剩余页");
  }
  const count = file.untranslated_count;
  if (count === null || count <= 0) return t("部分翻译 · 补缺失内容");
  return surface === "excel" ? t("部分翻译 · 补 {0} 个单元格", fmtNum(count)) : t("部分翻译 · 补 {0} 段", count);
}

function resumeChip(surface: Surface, file: ResumeFileInfo): HTMLElement {
  // found（找到产物但判定不出完整度）按「已有译文」说：底稿照样接得上，补译由后端逐项判。
  if (file.status === "full" || file.status === "found") return createChip({ label: t("已有译文 · 原样保留"), tone: "ok" });
  if (file.status === "partial") return createChip({ label: resumePendingLabel(surface, file), tone: "warn" });
  // 源文件在上次翻译后有改动：上次产物当不了底稿，后端会整份重翻这个文件。
  if (file.status === "diverged") return createChip({ label: t("源文件已变化 · 整份重译"), tone: "warn" });
  // 「首次翻译」是状态不是承诺：整个任务按补译方式跑，这个文件因为没有任何已有
  // 译文，事实上就是从头翻——但不能写「全量翻译」，那跟右栏亮着的「仅补译未翻译
  // 内容」开关自相矛盾。
  return createChip({ label: t("无译文 · 首次翻译"), tone: "tint" });
}

/** 状态列查表用；决策不是「继续」时返回空表，状态列照旧走原来的分支。 */
function resumeFileMap(st: SurfaceState): Map<string, ResumeFileInfo> {
  const map = new Map<string, ResumeFileInfo>();
  if (st.resumeDecision !== "resume") return map;
  const info = resumeState(st);
  if (!info) return map;
  for (const file of info.files) map.set(file.path, file);
  return map;
}

// --- 状态 C：文件表上方的续译横幅 ------------------------------------------

function buildResumeBanner(surface: Surface, st: SurfaceState): HTMLElement | null {
  const dir = activeResumeDir(st);
  if (!dir) return null;
  const info = resumeState(st)!;
  const banner = el("div", "resume-banner");
  const badge = el("div", "bi");
  badge.append(icon("restart", { size: "sm" }));
  const copy = el("div", "copy");
  const title = el("b");
  title.textContent = t("继续上次翻译");
  const sub = el("div", "sub");
  const code = el("code");
  code.textContent = basename(dir);
  const done = info.summary.page_done;
  if (surface === "pdf" && done !== null && done > 0) {
    sub.append(document.createTextNode(t("以 ")), code, document.createTextNode(t(" 里已完成的 {0} 页为底稿，只生成剩下的页；结果存入新的输出文件夹。", done)));
  } else {
    sub.append(document.createTextNode(t("以 ")), code, document.createTextNode(t(" 里的译文为底稿，只补缺失的部分；结果存入新的输出文件夹。")));
  }
  if (info.summary.diverged > 0) {
    sub.append(document.createTextNode(t("（{0} 个源文件已变化的文件将整份重译）", info.summary.diverged)));
  }
  copy.append(title, sub);
  const acts = el("div", "acts");
  const revert = el("button", "resume-link");
  revert.type = "button";
  revert.textContent = t("改为全部重译");
  revert.addEventListener("click", () => chooseFresh(surface, st));
  acts.append(revert);
  banner.append(badge, copy, acts);
  return banner;
}

// --- 状态 D：「稍后再说」之后的提示条 --------------------------------------

function buildResumeDismissBar(surface: Surface, st: SurfaceState): HTMLElement | null {
  if (st.resumeDecision !== "fresh" || st.resumeNoticeDismissed) return null;
  const info = resumeState(st);
  if (!info || resumeHitCount(info) === 0) return null;
  const bar = el("div", "resume-dismiss");
  const mark = icon("help", { size: "sm" });
  const label = el("span");
  const withText = info.summary.full + info.summary.found;
  label.textContent = t("检测到上次的翻译结果（{0}/{1} 个文件已有译文）。本次将全部重新翻译。", withText, info.files.length);
  const acts = el("div", "acts");
  const again = el("button", "resume-link");
  again.type = "button";
  again.textContent = t("重新选择");
  // 重新弹的是缓存下来的这一份检测结果，不重扫——用户只是想改主意，没换过路径。
  again.addEventListener("click", () => void showResumeModal(surface, st));
  const close = el("button", "resume-link mute");
  close.type = "button";
  close.textContent = "×";
  close.setAttribute("aria-label", t("不再提示"));
  close.addEventListener("click", () => {
    st.resumeNoticeDismissed = true;
    rerender(surface);
  });
  acts.append(again, close);
  bar.append(mark, label, acts);
  return bar;
}

// --- 状态 A / B / E：扫描完成后的弹窗 ---------------------------------------

function resumeStatChips(info: PreviousOutput): HTMLElement {
  const row = el("div", "rf-stats");
  const s = info.summary;
  if (s.full > 0) row.append(createChip({ label: t("{0} 个已有完整译文", s.full), tone: "ok" }));
  if (s.found > 0) row.append(createChip({ label: t("{0} 个已有译文", s.found), tone: "ok" }));
  if (s.partial > 0) row.append(createChip({ label: t("{0} 个已部分翻译", s.partial), tone: "warn" }));
  if (s.diverged > 0) row.append(createChip({ label: t("{0} 个源文件已变化", s.diverged), tone: "warn" }));
  if (s.none > 0) row.append(createChip({ label: t("{0} 个尚无译文", s.none), tone: "mute" }));
  return row;
}

/** PDF 专用：page_total 缺失（null 或 0）的文件数。summary.page_done/page_total 只统计
 *  有清单记录的 PDF，这批文件不在里面——续译对它们形同摆设，只能整份重新生成。少了这个
 *  数，「剩余页数」会被当成整批的工作量，漏掉这几个文件的真实成本。
 *  diverged（源文件已变化）单独有自己的文案，不混进「没有页记录」里数两遍。 */
function pdfFilesMissingPageData(info: PreviousOutput): number {
  return info.files.filter((file) => file.status !== "diverged" && !file.page_total).length;
}

/** 弹窗里那个灰底方框：底稿目录 + 统计（或页进度）+ 历史目录下拉。 */
function buildResumeFoundBox(surface: Surface, info: PreviousOutput, handle: () => ModalHandle | null): HTMLElement {
  const box = el("div", "resume-found");
  const pathRow = el("div", "rf-path");
  pathRow.append(icon(surface === "pdf" ? "pdf" : "folder", { size: "sm" }));
  const code = el("code");
  code.textContent = info.selected_dir ? basename(info.selected_dir) : t("（未找到可用的历史目录）");
  pathRow.append(code);
  box.append(pathRow);

  const total = info.summary.page_total;
  const done = info.summary.page_done;
  if (surface === "pdf" && total !== null && total > 0) {
    const missing = pdfFilesMissingPageData(info);
    const line = el("div", "rf-page");
    line.append(createProgressBar({ percent: ((done ?? 0) / total) * 100 }).root);
    const span = el("span");
    // 页进度只覆盖有清单记录的文件；还有 missing 个文件完全没有页记录，不能让这条进度条
    // 看起来像是「整批」的进度——那会把这几个文件的工作量凭空抹掉。
    span.textContent = missing > 0
      ? t("已完成 {0} / {1} 页（另有 {2} 个文件没有页记录，需整份生成）", done ?? 0, total, missing)
      : t("已完成 {0} / {1} 页", done ?? 0, total);
    line.append(span);
    box.append(line);
  } else {
    box.append(resumeStatChips(info));
  }

  if (info.summary.last_task_stopped) {
    const stoppedRow = el("div", "rf-stats");
    const when = info.summary.last_time_label;
    stoppedRow.append(createChip({ label: when ? t("上次任务 {0} 被手动停止", tx(when)) : t("上次任务被手动停止"), tone: "warn" }));
    box.append(stoppedRow);
  }

  // 一份历史输出没什么可选的，下拉只在有第二份时出现。
  if (info.candidates.length >= 2) {
    const selRow = el("div", "rf-sel");
    const isLatest = info.selected_dir === info.candidates[0].dir;
    const hint = el("span");
    hint.textContent = isLatest
      ? t("找到 {0} 次历史输出，已选最近一次", info.candidates.length)
      : t("找到 {0} 次历史输出", info.candidates.length);
    const select = createDropdown({
      className: "rf-history-select",
      options: info.candidates.map((candidate, index) => ({
        value: candidate.dir,
        label: index === 0 ? t("{0}（最近）", tx(candidate.label)) : tx(candidate.label),
      })),
      value: info.selected_dir ?? undefined,
      onChange: (next) => {
      if (!next || next === info.selected_dir) return;
      // 换底稿要重新逐个文件比对，只有后端算得出来：带着 preferred_resume_dir 重扫一次，
      // 新结果回来会重新弹这个弹窗（走 runScan 原有的请求序号守卫，旧结果不会落地）。
      handle()?.close();
      void runScan(surface, next);
      },
    });
    selRow.append(hint, select.root);
    box.append(selRow);
  }
  return box;
}

/** 「**加粗词**：正文」这种一句话说明；openModal 会把它包进 <p>。 */
function resumeLead(lead: string, body: string): HTMLElement {
  const span = el("span");
  const bold = el("b");
  bold.textContent = lead;
  span.append(bold, document.createTextNode(t("：{0}", body)));
  return span;
}

/** 几句说明接在一起：中文直接相连，英法文句子之间补一个空格。 */
function joinNotes(...notes: string[]): string {
  const parts = notes.filter(Boolean);
  if (!parts.length) return "";
  const glue = currentLocale() === "zh" ? "" : " ";
  return glue + parts.join(glue);
}

function resumeFootnote(body: string): HTMLElement {
  const span = el("span");
  span.style.cssText = "color:var(--ink-3);font-size:12.5px";
  span.textContent = body;
  return span;
}

/** onDecided：从「开始翻译」进来时，选了「接着上次继续」或「全部重新翻译」就接着启动。
 *  「稍后再说」只关弹窗，不启动。 */
/** 返回弹窗是否真的打开了。目标语言和上次不一致（resumeState 为空）或没有可续的
 *  产物时不弹，调用方要自己往下走，不能把这次点击吞掉。 */
function showResumeModal(surface: Surface, st: SurfaceState, onDecided?: () => void): boolean {
  const info = resumeState(st);
  if (!info || resumeHitCount(info) === 0) return false;
  const fileCount = st.files.length || info.files.length;
  const single = fileCount === 1;
  const s = info.summary;
  // 「上次已全部翻完」只认 full：partial 显然还有活，found 是判定不出完整度的，
  // diverged 是源文件已经变了的——都不能拿来当「翻完了」，那会把唯一能补译的
  // 入口从界面上拿掉，或者拿旧版本的产物冒充新版本的成品。
  const allDone = s.partial === 0 && s.none === 0 && s.found === 0 && s.diverged === 0;
  const pdfPages = surface === "pdf" && s.page_total !== null && s.page_total > 0;
  const remaining = pdfPages ? (s.page_total as number) - (s.page_done ?? 0) : 0;
  // pdfPages 为假说明整批 PDF 一份可用的页存档都没有——续译完全靠这份存档才能只补差额，
  // 存档不在，「接着上次继续」在后端会整份重新生成，跟「全部重新翻译」没有区别，
  // 却顶着「不再重复花钱」的许诺，是在骗用户点它。
  const pdfNoPageData = surface === "pdf" && !pdfPages && !allDone;
  // pdfPages 为真时页汇总也可能没盖住整批：还有几个文件完全没有页记录（新加进来的、
  // 或找到产物但读不出页存档），这些文件的工作量不在 s.page_total 里，两个选项的文案
  // 都得把它们单独点出来，不然「10 页」听起来就是这一批的全部代价。
  const pdfMissingPages = pdfPages ? pdfFilesMissingPageData(info) : 0;
  const pdfMissingNote = pdfMissingPages > 0
    ? t("另有 {0} 个文件没有页面记录，无论选择哪种方式都将整份重新生成，不计入此处页数。", pdfMissingPages)
    : "";
  const pdfDivergedNote = surface === "pdf" && s.diverged > 0
    ? t("另有 {0} 个 PDF 的源文件在上次翻译后已修改，上次的页面无法复用，将整份重新生成。", s.diverged)
    : "";

  let handle: ModalHandle | null = null;
  const foundBox = buildResumeFoundBox(surface, info, () => handle);
  const body: (string | HTMLElement)[] = [foundBox];

  if (allDone) {
    body.push(t("此批文件上次已全部翻译完成，无需补译；结果位于上方文件夹中。"));
    body.push(resumeLead(t("全部重新翻译"), t("忽略上次的结果，将 {0} 个文件从头重新翻译。", fileCount)));
    body.push(resumeFootnote(t("结果将保存到新的输出文件夹，上次的输出不会被覆盖或删除。")));
  } else if (pdfPages) {
    body.push(resumeLead(
      t("继续上次翻译"),
      t("跳过已完成的 {0} 页，仅生成剩余的 {1} 页，最后合成为完整的 PDF。已生成的页面直接复用，不会重复产生费用。", s.page_done ?? 0, remaining) + joinNotes(pdfMissingNote, pdfDivergedNote),
    ));
    const warnBox = el("div", "resume-warn");
    warnBox.append(icon("warn", { size: "sm" }));
    const warnText = el("span");
    warnText.textContent = t("中途停止的任务，最后几页可能未完整生成。继续前将逐页核对，占位页或损坏页面将自动重新生成。");
    warnBox.append(warnText);
    body.push(warnBox);
    body.push(resumeLead(t("全部重新翻译"), t("从头重新生成全部 {0} 页。", s.page_total) + joinNotes(pdfMissingNote, pdfDivergedNote)));
    body.push(resumeFootnote(t("无论选择哪种方式，结果都将保存到新的输出文件夹，上次的输出不会被覆盖或删除。")));
  } else if (pdfNoPageData) {
    // 整批都是「源文件已改动」时按真实原因说话，别拿「读不到页存档」搪塞。
    const allChanged = s.diverged > 0 && s.partial + s.found + s.full === 0;
    body.push(allChanged
      ? resumeLead(t("找到上次的翻译结果"), t("但源文件在上次翻译后已修改，上次的页面无法复用，继续翻译也将整份重新生成。"))
      : resumeLead(t("找到上次的翻译结果"), t("但无法读取上次的页面存档，继续翻译也将整份重新生成，无法仅补译缺失部分。")));
    body.push(resumeLead(t("全部重新翻译"), t("忽略上次的结果，将 {0} 个文件从头重新翻译。", fileCount)));
    body.push(resumeFootnote(t("结果将保存到新的输出文件夹，上次的输出不会被覆盖或删除。")));
  } else {
    // diverged 文件的「继续」承诺不了省钱：底稿被拒后整份重翻，只有翻译记忆兜底。
    // 有一个说一个，不能让「一个字不动」的许诺盖到它们头上。
    const resumables = s.partial + s.found + s.full;
    const divergedNote = s.diverged > 0
      ? t("其中 {0} 个文件的源文件在上次翻译后已修改，上次的输出无法作为底稿，将整份重新翻译（已翻译的内容尽量由记忆库复用）。", s.diverged)
      : "";
    body.push(s.diverged > 0 && resumables === 0
      ? resumeLead(t("继续上次翻译"), t("源文件在上次翻译后已修改，上次的输出无法作为底稿，本次将整份重新翻译；已翻译的内容尽量由记忆库复用。"))
      : resumeLead(t("继续上次翻译"), t("以上次的译文为底稿，仅翻译缺少译文的部分；已翻译的内容保持不变，不会重复产生费用。") + joinNotes(divergedNote)));
    body.push(resumeLead(t("全部重新翻译"), t("忽略上次的结果，将 {0} 个文件从头重新翻译。", fileCount)));
    body.push(resumeFootnote(t("无论选择哪种方式，结果都将保存到新的输出文件夹，上次的输出不会被覆盖或删除。")));
  }

  const title = allDone
    ? (single ? t("此文件上次已翻译完成") : t("这批文件上次已全部翻译完成"))
    : pdfNoPageData
      ? (single ? t("此 PDF 无可用的续译记录") : t("这些 PDF 无可用的续译记录"))
      : pdfPages
        ? (single ? t("此 PDF 上次未翻译完成") : t("这些 PDF 上次未翻译完成"))
        : (single ? t("此文件已翻译过") : t("此文件夹已部分翻译"));

  const actions: ModalAction[] = [
    { label: t("稍后再说"), onClick: () => chooseFresh(surface, st) },
    {
      label: t("全部重新翻译"),
      variant: allDone || pdfNoPageData ? "primary" : undefined,
      onClick: () => {
        chooseFresh(surface, st);
        onDecided?.();
      },
    },
  ];
  // pdfNoPageData 时不给「接着上次继续」——那个按钮在后端会整份重新生成，跟「全部重新
  // 翻译」结果一样，留着它只会让用户以为还有一条能省钱的路。
  if (!allDone && !pdfNoPageData) {
    actions.push({
      label: t("继续上次翻译"),
      variant: "primary",
      onClick: () => {
        chooseResume(surface, st);
        onDecided?.();
      },
    });
  }

  handle = openModal({
    tone: (pdfPages || pdfNoPageData) && !allDone ? "warn" : "tint",
    icon: surface === "pdf" ? "pdf" : "folder",
    sourceLabel: onDecided ? t("{0} · 开始前核对", SURFACE_LABEL[surface]) : t("{0} · 扫描完成", SURFACE_LABEL[surface]),
    title,
    body,
    actions,
  });
  return true;
}

// ---------------------------------------------------------------------------
// 扫描
// ---------------------------------------------------------------------------

/**
 * 扫描请求序号。换目录时上一次扫描往往还在路上（「浏览」选完就自动扫，用户又点了一次
 * 「扫描」也一样），两个响应谁后到谁覆盖 st.files；先发的那个后到，界面就变成
 * 「路径栏显示 B、文件清单是 A」。用户点开始翻译，后端按 B 重扫再和前端交上来的选中
 * 路径取交集，结果只翻了两边都有的那一部分，界面上不报任何错。
 * 只有序号仍等于最新一次的响应才允许写回状态。
 */
const scanTokens: Record<Surface, number> = { excel: 0, word: 0, pdf: 0, cad: 0 };
/** 有扫描在飞时置灰「扫描」「浏览」，避免用户连点又多造一个在途请求。 */
const scanBusy: Record<Surface, boolean> = { excel: false, word: false, pdf: false, cad: false };

function scanPayload(surface: Surface, st: SurfaceState, path: string): JsonObject {
  const payload: JsonObject = {
    surface,
    path,
    // 多选文件时按这几个路径各扫一次再合并；清单里就只有用户挑的那几份。
    paths: st.sourcePaths.map(normalizeUserPath),
    include_images: surface === "pdf" && Boolean(st.toggles.get("pdfImages")),
  };
  if (surface === "cad") {
    // CAD 的「待翻译」数取决于语言对：已经是目标语言的文字不算候选。
    payload.source_language = st.sourceLang;
    payload.target_language = st.targetLang;
    payload.include_block_text = Boolean(st.toggles.get("cadBlockText"));
  }
  return payload;
}

/** preferredResumeDir：用户在弹窗里换了历史输出目录，带着它重扫一遍拿新的逐文件比对结果。 */
async function runScan(surface: Surface, preferredResumeDir?: string): Promise<void> {
  const st = states[surface];
  const path = normalizeUserPath(st.sourcePath);
  if (!path) return;
  st.sourcePath = path;
  if (st.task && !st.task.task.terminal) return;
  clearWorkspaceInput(surface);
  const token = ++scanTokens[surface];
  scanBusy[surface] = true;
  rerender(surface);
  try {
    const c = await getClient();
    const payload = scanPayload(surface, st, path);
    if (preferredResumeDir) payload.preferred_resume_dir = preferredResumeDir;
    const response = await c.request<JsonObject>("/api/sources/scan", { method: "POST", body: JSON.stringify(payload) });
    if (token !== scanTokens[surface]) return; // 已被更晚的扫描取代，这份结果一个字都不能落地
    const result = record(response.result) && Object.keys(record(response.result)).length ? record(response.result) : response;
    const items = Array.isArray(result.items) ? (result.items as FileItem[]) : [];
    const skipped = Array.isArray(result.skipped) ? (result.skipped as ScanSkippedItem[]) : [];
    st.files = items;
    st.skipped = skipped;
    st.scanSummary = record(result.summary);
    // 只有 Excel 扫描才带 risk.libreoffice_available；Word/PDF 没这个字段，record()
    // 在缺字段时给回 {}，取值自然落回 false，不用按 surface 分支特判。
    st.libreofficeAvailable = Boolean(record(result.risk).libreoffice_available);
    if (surface === "cad" && Object.keys(record(response.capability)).length) st.cadCapability = record(response.capability);
    st.selected = new Set(items.map((f) => f.path));
    st.showBanner = false;
    // 新清单配旧结果没有意义：上一次跑的是别的文件。
    st.fileOutcomes = new Map();
    // 续译状态整体跟着这一次扫描重置，再按新的检测结果重建。
    resetResumeState(st);
    const detected = parsePreviousOutput(result.previous_output);
    // selected_dir 为空说明后端定不出唯一的底稿目录（比如跨多个文件夹多选）。任务只认
    // 一个 resume_output_dir，没有它「接着上次继续」根本兑现不了——这种检测结果连弹窗
    // 都不该弹，弹了就是在承诺一件做不到的事。
    if (detected && detected.selected_dir && resumeHitCount(detected) > 0) {
      st.resumeInfo = detected;
      st.resumeDetectedLang = detected.target_lang || st.targetLang;
    }
    // 「记住上次目录」失败不能牵连扫描结果：清单已经拿到了，没道理因为写设置出错就清空它。
    try {
      await persistSettings({ [`last_${surface}_source_folder`]: path });
    } catch {
      // 下次进入这个界面得重新选路径而已，不值得打断当前流程。
    }
    if (token !== scanTokens[surface]) return;
    // 一个都没跳过时不提「跳过 0 个」；西文名词前后都要留空格。
    const skipCount = skipped.length;
    const toastMessage = surface === "pdf"
      ? (skipCount > 0 ? t("已扫描到 {0} 个 PDF / 图片输入，跳过 {1} 个。", items.length, skipCount) : t("已扫描到 {0} 个 PDF / 图片输入。", items.length))
      : surface === "cad"
        ? (skipCount > 0 ? t("已扫描到 {0} 个 DWG / DXF 图纸，跳过 {1} 个。", items.length, skipCount) : t("已扫描到 {0} 个 DWG / DXF 图纸。", items.length))
        : (skipCount > 0
          ? t("已扫描到 {0} 个 {1} 文件，跳过 {2} 个。", items.length, SURFACE_LABEL[surface], skipCount)
          : t("已扫描到 {0} 个 {1} 文件。", items.length, SURFACE_LABEL[surface]));
    showToast({ message: toastMessage });
    // 弹窗不阻塞扫描结果展示：清单已经写进状态了，finally 里的 rerender 会把它铺在弹窗背后。
    if (st.resumeInfo) void showResumeModal(surface, st);
  } catch (error) {
    if (token !== scanTokens[surface]) return;
    // 扫描失败必须把上一次的清单清掉：留着旧结果，用户看到的是「路径 B + B 之外的文件」，
    // 会当成 B 目录的内容直接开翻。宁可空着让他重扫，也不能拿上一次的结果冒充这一次。
    st.files = [];
    st.skipped = [];
    st.scanSummary = {};
    st.selected = new Set();
    resetResumeState(st);
    showToast({ message: redactedText((error as Error)?.message, t("扫描失败。")), error: true });
  } finally {
    if (token === scanTokens[surface]) {
      scanBusy[surface] = false;
      rerender(surface);
    }
  }
}

/**
 * 只做续译检测、不动清单：和「重新扫描」走同一个接口、同一套比对，但不重置文件清单、
 * 勾选和横幅。preferredResumeDir 给了就以它为底稿目录（停止横幅上的「接着翻译」用
 * 刚停下那次任务的输出目录）。检测不到或请求失败都返回 null。
 */
async function detectPreviousOutputForStart(
  surface: Surface,
  st: SurfaceState,
  preferredResumeDir?: string,
): Promise<PreviousOutput | null> {
  const path = normalizeUserPath(st.sourcePath);
  if (!path) return null;
  try {
    const c = await getClient();
    const payload = scanPayload(surface, st, path);
    if (preferredResumeDir) payload.preferred_resume_dir = preferredResumeDir;
    const response = await c.request<JsonObject>("/api/sources/scan", { method: "POST", body: JSON.stringify(payload) });
    const result = record(response.result) && Object.keys(record(response.result)).length ? record(response.result) : response;
    const detected = parsePreviousOutput(result.previous_output);
    if (!detected || !detected.selected_dir || resumeHitCount(detected) === 0) return null;
    return detected;
  } catch {
    return null;
  }
}

/** 停止横幅上的「接着翻译」：以刚停下那次的输出为底稿，直接按「接着上次继续」启动。 */
async function resumeAfterStop(surface: Surface, st: SurfaceState, outputDir: string): Promise<void> {
  // 扫描要跑一个来回：整段占住提交锁，连点「接着翻译」或这时再点「开始翻译」都不会
  // 再起一条流程。锁在启动前放开，startTask 里的预检提交会自己再拿。
  if (submittingSurfaces.has(surface)) return;
  submittingSurfaces.add(surface);
  rerender(surface);
  let detected: PreviousOutput | null = null;
  try {
    detected = await detectPreviousOutputForStart(surface, st, outputDir || undefined);
  } finally {
    submittingSurfaces.delete(surface);
    rerender(surface);
  }
  if (!detected) {
    showToast({ message: t("未找到上次停止时写出的文件，请点击「重新扫描」后重试。"), error: true });
    return;
  }
  st.resumeInfo = detected;
  st.resumeDetectedLang = detected.target_lang || st.targetLang;
  st.showBanner = false;
  chooseResume(surface, st);
  await startTask(surface, st);
}

// ---------------------------------------------------------------------------
// 启动任务（含 xls/doc 兼容确认、并行风险确认）
// ---------------------------------------------------------------------------

function buildPayload(surface: Surface, st: SurfaceState): JsonObject {
  const payload: JsonObject = {
    surface,
    source_path: normalizeUserPath(st.sourcePath),
    selected_paths: st.files.filter((f) => st.selected.has(f.path)).map((f) => normalizeUserPath(f.path)),
    untranslated_only: Boolean(st.toggles.get("untranslated")),
    target_lang: st.targetLang,
  };
  // 「接着上次继续」：底稿目录跟着请求走，Excel / Word 一律按补译跑。这里不读开关的 UI
  // 状态——开关只是把这个决定显示出来，真正保证补译的是这一行。
  const resumeDir = activeResumeDir(st);
  if (resumeDir) {
    payload.resume_output_dir = resumeDir;
    if (surface !== "pdf") payload.untranslated_only = true;
  }
  if (surface !== "pdf") payload.source_lang = st.sourceLang;
  if (surface === "excel") payload.allow_xls_fallback = st.allowXlsFallback;
  if (surface === "word") {
    payload.allow_doc_fallback = st.allowDocFallback;
    payload.protect_front_matter = Boolean(st.toggles.get("protectFrontMatter"));
    payload.translate_headers_footers = Boolean(st.toggles.get("translateHeadersFooters"));
  }
  if (surface === "pdf") payload.include_images = Boolean(st.toggles.get("pdfImages"));
  if (surface === "cad") {
    // 已经是目标语言的文字不再送翻译——与扫描时「待翻译」的口径一致。
    payload.untranslated_only = true;
    payload.cad_translate_output_filename = Boolean(st.toggles.get("translateOutputFilename"));
    payload.cad_include_block_text = Boolean(st.toggles.get("cadBlockText"));
    payload.cad_keep_work_dxf = Boolean(st.toggles.get("cadKeepWorkDxf"));
    payload.cad_copy_related_files = Boolean(st.toggles.get("cadCopyRelated"));
    if (st.useCustomOutputDir && st.customOutputDir.trim()) payload.cad_output_dir = normalizeUserPath(st.customOutputDir);
    if (st.cadResumeDir) payload.resume_output_dir = normalizeUserPath(st.cadResumeDir);
  }
  return payload;
}

async function startTask(surface: Surface, st: SurfaceState): Promise<void> {
  if (!st.sourcePath.trim() || !st.selected.size) return;
  // 更新已装好但还没重启时,新任务的组件加载可能按旧偏移读到新二进制而失败,
  // 报错文案还会带偏方向(高-12)——第一次新建任务先拦一下,提醒重启。
  // 「仍要开始」重新走 startTask:警告已消费,这次会直接放行,后续守卫照常跑。
  if (consumePendingRestartWarning()) {
    openRestartBeforeTaskModal(() => void startTask(surface, st));
    return;
  }
  if (surface !== "pdf" && st.sourceLang !== "auto" && st.sourceLang === st.targetLang) {
    showToast({ message: t("源语言与目标语言相同，请重新选择。"), error: true });
    return;
  }
  if (surface === "cad") {
    const blocked = st.files.filter((f) => st.selected.has(f.path) && isRisky("cad", f)).length;
    if (blocked > 0) {
      openModal({
        tone: "warn",
        icon: "warn",
        title: t("需要先连接图纸转换工具"),
        body: [
          t("所选文件中有 {0} 个 DWG 图纸。DWG 需先通过转换工具转换为 DXF，才能翻译和写回。", blocked),
          t("可在「插件」中连接图纸转换工具后重新扫描，或取消勾选这些 DWG，仅翻译 DXF。"),
        ],
        actions: [{ label: t("取消") }, { label: t("打开插件"), variant: "primary", onClick: () => navigate("plugins") }],
      });
      return;
    }
  }
  if (surface === "excel") {
    const riskyCount = st.files.filter((f) => st.selected.has(f.path) && isRisky("excel", f)).length;
    if (riskyCount > 0 && !st.allowXlsFallback) {
      showCompatibilityModal(surface, st, riskyCount);
      return;
    }
  }
  if (surface === "word") {
    const riskyCount = st.files.filter((f) => st.selected.has(f.path) && isRisky("word", f)).length;
    if (riskyCount > 0 && !st.allowDocFallback) {
      showCompatibilityModal(surface, st, riskyCount);
      return;
    }
  }
  await submitAfterResumeCheck(surface, st);
}

/** 开始前的续译检测，再预检提交。
 *  上一次任务没有干净地跑完（停止、失败、中断、有问题）又直接点「开始翻译」：先做一遍
 *  「重新扫描」那套续译检测。以前续译弹窗只在重新扫描后出现，停止后点开始会整份从头
 *  重翻，已经付过钱的部分再花一遍。确实有翻了一半的文件才弹窗，其余照常开始。
 *  .xls / .doc 转换方式弹窗的按钮也走这里——直接预检提交会把这道检测绕过去。 */
async function submitAfterResumeCheck(surface: Surface, st: SurfaceState): Promise<void> {
  const lastState = st.task?.task.terminal ? st.task.task.state : null;
  if ((surface === "excel" || surface === "word") && st.resumeDecision === null && lastState && lastState !== "done") {
    if (submittingSurfaces.has(surface)) return;
    submittingSurfaces.add(surface);
    rerender(surface);
    let detected: PreviousOutput | null = null;
    try {
      detected = await detectPreviousOutputForStart(surface, st);
    } finally {
      submittingSurfaces.delete(surface);
      rerender(surface);
    }
    if (detected && detected.summary.partial > 0) {
      st.resumeInfo = detected;
      st.resumeDetectedLang = detected.target_lang || st.targetLang;
      // 选完之后 resumeDecision 已定，再进这里会直接预检提交；前面的检查（含转换方式）
      // 都已经过了，不再重走 startTask，免得转换方式弹窗再问一遍。
      if (showResumeModal(surface, st, () => void submitAfterResumeCheck(surface, st))) return;
    }
  }
  await preflightAndSubmit(surface, st);
}

function showCompatibilityModal(surface: Surface, st: SurfaceState, count: number): void {
  const legacyExt = surface === "excel" ? ".xls" : ".doc";
  const finalExt = surface === "excel" ? ".xlsx" : ".docx";
  const appName = surface === "excel" ? "Excel" : "Word";
  openModal({
    tone: "warn",
    icon: "warn",
    title: t("{0} 转换方式确认", legacyExt),
    body:
      surface === "excel"
        ? [
            t("已选择 {0} 个旧版 .xls 文件，最终结果统一输出为 .xlsx。", count),
            t("源文件不会被修改：公式、宏和样式均完整保留在原 .xls 文件中，可随时打开对照。以下选择仅影响翻译输出的新文件。"),
            t("优先高保真：使用本机 Excel 转换，样式、合并单元格、图片和图表完整带入输出；若未安装 Excel、未允许 Translator 控制 Excel 或某个文件转换失败，该文件将标记为失败，其他文件照常处理，不会自动改用兼容转换。"),
            st.libreofficeAvailable
              ? t("允许兼容转换：未安装 Excel 时也可翻译。已在本机检测到 LibreOffice，将使用其进行转换，公式、样式和合并单元格通常可以保留（图表、图片可能有差异）。此选择仅对本次任务生效。")
              : t("允许兼容转换：未安装 Excel 时也可翻译，但输出文件中的公式将变为计算结果，样式、合并单元格、图片和图表不会保留。此选择仅对本次任务生效。安装免费的 LibreOffice 后，兼容转换也可保留公式与样式。"),
          ]
        : [
            t("已选择 {0} 个旧版 {1} 文件。最终结果统一输出为 {2}，源文件不会被改写。", count, legacyExt, finalExt),
            t("优先高保真：使用本机 {0} 转换，排版完整保留；若未安装 {0}、未允许 Translator 控制 {0} 或某个文件转换失败，该文件将标记为失败，其他文件照常处理，不会自动改用兼容转换。", appName),
            t("允许兼容转换：高保真转换不可用时继续翻译，但复杂样式、合并单元格、图片、图表和宏可能无法完整保留。此选择仅对本次任务生效。"),
          ],
    // 主按钮给「优先高保真」：它是不会损失内容的那一个。默认高亮有损选项，等于
    // 替用户默认接受丢样式、丢图片、丢宏——回车一按就丢了，而且事后看不出丢了什么。
    actions: [
      { label: t("取消") },
      {
        label: t("允许兼容转换"),
        onClick: () => {
          if (surface === "excel") st.allowXlsFallback = true;
          else st.allowDocFallback = true;
          void submitAfterResumeCheck(surface, st);
        },
      },
      {
        label: t("优先高保真"),
        variant: "primary",
        onClick: () => {
          if (surface === "excel") st.allowXlsFallback = false;
          else st.allowDocFallback = false;
          void submitAfterResumeCheck(surface, st);
        },
      },
    ],
  });
}

/** 预检加提交合起来要跑好几个来回，这段时间里按钮还是可点的。第二次点击最终会被
 *  后端的串行化挡下来返回 409，用户看到的是一条看不懂的报错——所以在这里就拦住。 */
const submittingSurfaces = new Set<Surface>();
const submitFailures = new Set<Surface>();

async function preflightAndSubmit(
  surface: Surface,
  st: SurfaceState,
  overrides: JsonObject = {},
): Promise<void> {
  if (submittingSurfaces.has(surface)) return;
  submittingSurfaces.add(surface);
  submitFailures.delete(surface);
  rerender(surface);
  const payload = { ...buildPayload(surface, st), ...overrides };
  try {
    const c = await getClient();
    const preflight = await c.preflightTask(payload);
    if (preflight.requires_confirmation) {
      // 风险确认弹窗是一个决策点：这里必须放开，否则用户在弹窗上点「仍要并行启动」
      // 会被自己的这把锁挡住。弹窗那条路径由 submitTaskStart 自己守。
      showTaskRiskModal(surface, st, payload, preflight.confirmation_token ?? "");
      return;
    }
    await sendTaskStart(surface, st, payload);
  } catch (error) {
    noteStartFailure(surface, error);
    showStartBlockedModal(surface, st, payload, error);
  } finally {
    submittingSurfaces.delete(surface);
    rerender(surface);
  }
}

/** 模型还没配好时后端给的拦截原因（api/task_contracts.py 的 REASON_*_MODEL_MISSING），
 *  值是「设置 › 模型服务」里对应的角色。 */
const MODEL_MISSING_ROLE: Record<string, string> = {
  translation_model_missing: "translation",
  image_model_missing: "image",
  pdf_review_model_missing: "pdf_review",
};

/** 启动没成功时记一笔，开始按钮会改叫「重试启动」。没配模型不算：那一次什么都还没
 *  试过，按钮照旧叫「开始翻译」，出路在弹窗的「去设置」里。 */
function noteStartFailure(surface: Surface, error: unknown): void {
  if (MODEL_MISSING_ROLE[apiErrorReason(error)]) submitFailures.delete(surface);
  else submitFailures.add(surface);
}

/** 前置校验把任务拦下来时给一个弹窗，不给两秒就消失的提示：任务根本没开始这件事得留在
 *  屏幕上。更要紧的是有些拦截本身是有出路的——审核模型上次测试失败那条就写着「或明确
 *  确认继续」，而 9.2.6 只在提示文字里说了这句话，界面上没有任何按钮能确认，看起来就
 *  像开始按钮坏了。 */
function showStartBlockedModal(surface: Surface, st: SurfaceState, payload: JsonObject, error: unknown): void {
  if (apiErrorReason(error) === "pdf_review_model_unavailable") {
    openModal({
      tone: "warn",
      icon: "warn",
      title: t("审核模型上次测试未通过"),
      body: [
        t("逐页审核已开启，但本机最近一次测试「PDF 与图片审核」模型时失败。如现在开始，页面将正常翻译，但审核步骤很可能在每一页都出错。"),
        t("建议先在「设置 → 模型服务 → PDF 与图片审核」中重新测试，或关闭逐页审核后再开始。"),
        t("也可忽略测试结果直接开始：仅影响本次任务，设置不会被修改。"),
      ],
      actions: [
        { label: t("取消") },
        { label: t("前往设置重新测试"), onClick: () => navigate("settings", { page: "models" }) },
        {
          label: t("忽略并开始"),
          variant: "primary",
          onClick: () => void preflightAndSubmit(surface, st, { ...payload, allow_known_review_failure: true }),
        },
      ],
    });
    return;
  }
  const missingRole = MODEL_MISSING_ROLE[apiErrorReason(error)];
  if (missingRole) {
    // 以前这里和其他拦截一样只有「知道了」：用户读完「尚未填写模型名称」还得自己去找
    // 设置在哪。直接给一个去「设置 › 模型服务」对应角色的主按钮。
    openModal({
      tone: "warn",
      icon: "warn",
      title: missingRole === "pdf_review" ? t("尚未配置审核模型") : t("尚未配置翻译模型"),
      body: [
        redactedText((error as Error)?.message, t("尚未填写翻译所用的模型。")),
        t("填写模型名称和 API 密钥后，返回此处点击「开始翻译」。"),
      ],
      actions: [
        { label: t("取消") },
        { label: t("前往设置"), variant: "primary", onClick: () => navigate("settings", { page: "models", role: missingRole }) },
      ],
    });
    return;
  }
  openModal({
    tone: "warn",
    icon: "warn",
    title: t("本次任务未能开始"),
    body: [redactedText((error as Error)?.message, t("任务准备失败，无更多信息。"))],
    actions: [{ label: t("知道了"), variant: "primary" }],
  });
}

/** 简化版并行风险确认：main.ts 原版还会展示共享连接明细表、活动任务列表、候选任务快照——
 *  这里按品牌要求的措辞保留决策要点，略去表格化明细，见文件尾「已确认简化」说明。 */
function showTaskRiskModal(surface: Surface, st: SurfaceState, payload: JsonObject, token: string): void {
  openModal({
    tone: "warn",
    icon: "warn",
    title: t("与正在运行的任务共用同一条连接"),
    // 原文写的是「按新任务自己的默认吞吐启动」「一次性令牌原子复检」「降低共享组的
    // 运行时容量」——这些是实现细节，读的人只需要知道三件事：共用什么、可能出什么事、
    // 自己的设置会不会被改。
    body: [
      t("此任务将与正在运行的任务共用同一条 API 连接。两个任务各自按设定速度发送请求，服务端收到的是两者叠加的请求量。"),
      t("因此处理可能变慢或需要排队，个别文件可能超时失败；如该连接按用量计费，两个任务将同时产生费用。如遇服务限速，程序将自动降低本次任务的发送速度，设置中的速度不会被修改。"),
      t("如不急于处理，建议待前一个任务完成后再开始。"),
    ],
    actions: [
      { label: t("取消") },
      { label: t("仍要并行启动"), variant: "primary", onClick: () => void submitTaskStart(surface, st, payload, token) },
    ],
  });
}

/** 弹窗按钮走这条：自己守一次，避免连点弹窗上的确认键。 */
async function submitTaskStart(surface: Surface, st: SurfaceState, payload: JsonObject, confirmationToken = ""): Promise<void> {
  if (submittingSurfaces.has(surface)) return;
  submittingSurfaces.add(surface);
  rerender(surface);
  try {
    await sendTaskStart(surface, st, payload, confirmationToken);
  } finally {
    submittingSurfaces.delete(surface);
    rerender(surface);
  }
}

async function sendTaskStart(surface: Surface, st: SurfaceState, payload: JsonObject, confirmationToken = ""): Promise<void> {
  try {
    const c = await getClient();
    const body = confirmationToken ? { ...payload, confirmation_token: confirmationToken } : payload;
    const task = await c.request<TaskStatus>("/api/tasks", { method: "POST", body: JSON.stringify(body) });
    submitFailures.delete(surface);
    focusTask(surface, task);
    noteTaskStarted(task);
    st.showBanner = false;
    st.fileOutcomes = new Map();
    st.cadResumeDir = "";
    initFileStages(st);
    rerender(surface);
    watchTask(surface);
    if (surface === "pdf") void fetchPdfPagesSnapshot(surface, task.task_id);
  } catch (error) {
    noteStartFailure(surface, error);
    // 真正启动这一步会把前置校验再跑一遍（设置可能在弹窗开着的时候被改了），所以同一批
    // 「有出路的拦截」也会从这里出来，走同一个弹窗。
    showStartBlockedModal(surface, st, payload, error);
  }
}

function focusTask(surface: Surface, task: TaskStatus): void {
  const st = states[surface];
  rememberTaskContext(st, task.task_id);
  // 切回页面或重开程序接管一个跑到一半的任务时，进度从任务快照里接上，而不是从 0 重新爬。
  const progress = record(task.progress);
  const phase = splitPhaseName(redactedText(progress.phase_name, ""));
  st.task = {
    task,
    logs: [],
    phaseName: phase.detail,
    stageName: phase.stage,
    percent: Math.min(100, Math.max(0, num(progress.overall_percent, 0))),
    phaseIndex: num(progress.phase_index, 0),
    phaseTotal: num(progress.phase_total, 0),
    streamState: "connected",
    watcherActive: false,
    lastEventAt: Date.now(),
    fileProgress: reduceFileProgress(new Map(), task.file_progress),
  };
  st.lastTaskId = task.task_id;
  startSilenceTicker(surface);
}

/** 静默计时器：任务在跑但没有事件进来时，每 5 秒重画一次，让「已等待 N 秒」这句话
 *  自己走动。任务一到终态就停——不留一个空转的 interval。 */
const silenceTickers: Record<Surface, number | undefined> = { excel: undefined, word: undefined, pdf: undefined, cad: undefined };

function startSilenceTicker(surface: Surface): void {
  if (silenceTickers[surface] !== undefined) return;
  silenceTickers[surface] = window.setInterval(() => {
    const local = states[surface].task;
    if (!local || local.task.terminal) {
      stopSilenceTicker(surface);
      return;
    }
    // 只有真的静默下来才重画；有事件在流动时 rerender 已经被事件驱动了。
    if (silenceSeconds(local) >= SILENCE_NOTICE_SECONDS) rerender(surface);
  }, 5000);
}

function stopSilenceTicker(surface: Surface): void {
  const handle = silenceTickers[surface];
  if (handle !== undefined) {
    window.clearInterval(handle);
    silenceTickers[surface] = undefined;
  }
}

/** 多久没消息才值得说一句。低于这个数的空白属于正常节奏，报出来只是噪音。 */
const SILENCE_NOTICE_SECONDS = 20;

function silenceSeconds(local: LocalTask): number {
  return Math.max(0, Math.round((Date.now() - local.lastEventAt) / 1000));
}

function initFileStages(st: SurfaceState): void {
  const local = st.task;
  if (!local) return;
  for (const file of st.files) {
    if (st.selected.has(file.path) && file.file_id && !local.fileProgress.has(file.file_id)) {
      local.fileProgress.set(file.file_id, { file_id: file.file_id, name: file.name ?? "", relative_path: file.relative_path ?? "", state: "waiting", phase: "", completed: 0, total: 0, result: {}, revision: 0 });
    }
  }
}

// ---------------------------------------------------------------------------
// SSE 订阅
// ---------------------------------------------------------------------------

function watchTask(surface: Surface): void {
  const st = states[surface];
  const local = st.task;
  if (!local || local.watcherActive || local.task.terminal) return;
  local.watcherActive = true;
  const taskId = local.task.task_id;
  void (async () => {
    try {
      const c = await getClient();
      await c.streamTask(taskId, (event) => {
        if (states[surface].task === local) handleTaskEvent(surface, taskId, event);
      }, {
        onConnectionState: (state) => {
          if (states[surface].task !== local) return;
          const wasReconnecting = states[surface].task!.streamState === "reconnecting";
          states[surface].task!.streamState = state;
          rerender(surface);
          if (wasReconnecting && state === "connected") void refetchTask(surface, taskId).catch(() => undefined);
        },
      });
      if (states[surface].task === local && !local.task.terminal) {
        await refetchTask(surface, taskId);
      }
    } catch {
      if (states[surface].task !== local) return;
      await refetchTask(surface, taskId).catch(() => {
        if (states[surface].task === local) markTaskInterrupted(surface, taskId);
      });
    } finally {
      if (states[surface].task === local) local.watcherActive = false;
    }
  })();
}

async function refetchTask(surface: Surface, taskId: string): Promise<void> {
  const local = states[surface].task;
  const c = await getClient();
  const task = await c.getTask(taskId);
  if (!local || states[surface].task !== local || local.task.task_id !== taskId) return;
  if (states[surface].task!.task.terminal && !task.terminal && states[surface].task!.task.state !== "interrupted") return;
  states[surface].task!.task = task;
  states[surface].task!.fileProgress = reduceFileProgress(states[surface].task!.fileProgress, task.file_progress);
  // 终态前把最后一次逐页快照拉齐，保证「查看对比」在任务结束后仍能用上最新数据。
  if (surface === "pdf") await fetchPdfPagesSnapshot(surface, taskId);
  if (task.terminal) finishTask(surface, task);
  else rerender(surface);
}

function markTaskInterrupted(surface: Surface, taskId: string): void {
  const st = states[surface];
  const local = st.task;
  if (!local || local.task.task_id !== taskId) return;
  local.task = { ...local.task, state: "interrupted", terminal: true };
  local.phaseName = t("翻译服务意外重启或应用异常退出，此任务无法继续。已生成的文件仍保留在输出目录中；如有需要，请重新开始翻译。");
  showToast({ message: t("与后台服务的连接已断开，此任务已标记为应用中断。"), error: true });
  finishTask(surface, local.task);
}

function handleTaskEvent(surface: Surface, taskId: string, event: SseEvent): void {
  const st = states[surface];
  const local = st.task;
  if (!local || local.task.task_id !== taskId) return;
  local.lastEventAt = Date.now();
  const data = event.data;
  switch (event.type) {
    case "log": {
      local.logs.push({ seq: nextLogSeq++, time: formatTime(num(data.ts, 0) || undefined), level: text(data.level, "info"), message: redactedText(data.message ?? data.text, "") });
      // 数组本身也要封顶，不然长任务跑几个小时会攒出一条越来越长的内存记录——界面只展示
      // 最近 LOG_VIEW_LIMIT 条，这里索性同步截断，省得两处上限不一致。
      if (local.logs.length > LOG_VIEW_LIMIT) local.logs.splice(0, local.logs.length - LOG_VIEW_LIMIT);
      break;
    }
    case "progress": {
      if (data.phase_name !== undefined) {
        // 阶段名只进标签；标题留给状态句（「正在查找记忆库里已有的译文…」），不再被
        // 光秃秃的「翻译」两个字来回覆盖。CAD 没有状态句，后半句随进度事件一起来。
        const phase = splitPhaseName(redactedText(data.phase_name, ""));
        if (phase.stage) local.stageName = phase.stage;
        if (phase.detail && !holdsStatePhase(local)) local.phaseName = phase.detail;
      }
      const done = num(data.step_done);
      const total = num(data.step_total);
      if (local.task.state === "stopping") {
        // 停止时 Excel / Word 会把没翻完的部分跳过、直接进入最后的写文件阶段，后端按阶段
        // 权重折算，进度条会从 29% 一下跳到 80%，然后在那儿停十几秒——看上去像快跑完了。
        // 停止期间进度条留在点停止时的位置，标题改说正在做什么。
        // 认阶段名而不是「最后一个阶段」：Excel 开了调整行高时共 4 个阶段，写文件是第 3 个。
        const writing = splitPhaseName(redactedText(data.phase_name, "")).stage === "生成文件"; // i18n-ignore 比较后端原文阶段名
        if ((surface === "excel" || surface === "word") && writing && total > 0) {
          local.phaseName = t("正在停止：写入已翻译的部分（{0}/{1} 个文件）", Math.min(done, total), total);
        }
        break;
      }
      // step_done/step_total 是**当前阶段**的步数。直接拿它当总进度，扫描一结束进度条
      // 就满格，后面还有翻译和生成两个阶段要跑——用户看到 100% 却还在等，只能理解成卡住。
      // 事件里带着 phase_index/phase_total，用它把阶段内进度折算到整条进度上。
      const phaseIndex = num(data.phase_index);
      const phaseTotal = num(data.phase_total);
      if (phaseIndex > 0) local.phaseIndex = phaseIndex;
      if (phaseTotal > 0) local.phaseTotal = phaseTotal;
      const withinPhase = total > 0 ? Math.min(1, done / total) : 0;
      const overall = Number(data.overall_percent);
      if (Number.isFinite(overall)) {
        // 后端按各页面每个阶段的实际耗时比例折算好的总进度（api/task_manager.py
        // 的 _PHASE_WEIGHTS）。按阶段平均分的话，PDF 整个翻页过程都卡在 25%~50%。
        local.percent = Math.min(100, Math.max(0, overall));
      } else if (phaseTotal > 0 && phaseIndex > 0) {
        local.percent = Math.min(100, ((phaseIndex - 1 + withinPhase) / phaseTotal) * 100);
      } else if (total > 0) {
        local.percent = withinPhase * 100;
      }
      break;
    }
    case "status": {
      const phase = statusPhaseText(redactedText(data.phase_desc, local.phaseName));
      // 说的正是当前暂停/停止状态的句子（「正在停止任务：等待 2 页完成…」）照常显示。
      if (!holdsStatePhase(local) || STATE_PHASE_KEYWORD[local.task.state]?.test(phase)) local.phaseName = phase;
      break;
    }
    case "file_progress": {
      local.fileProgress = reduceFileProgress(local.fileProgress, data as unknown as FileProgress);
      if (surface === "pdf") schedulePdfSnapshotRefresh(surface, taskId);
      break;
    }
    // 这几个事件都带着后端已经改好的任务状态（data.state），必须同步到本地：
    // 底栏按钮、进度条颜色、顶栏、文件状态全都看 local.task.state。
    case "stopping":
      syncEventState(local, data, "stopping");
      local.phaseName = t("正在停止…");
      break;
    case "pausing":
      syncEventState(local, data, "pausing");
      local.phaseName = surface === "cad" ? t("正在暂停：待已发送的请求返回后暂停") : t("正在暂停：已开始的页面完成后暂停");
      break;
    case "paused":
      syncEventState(local, data, "paused");
      local.phaseName = surface === "cad" ? t("已暂停，不再发出新的翻译请求") : t("已暂停，不再开始新的页面");
      if (surface === "pdf") void fetchPdfPagesSnapshot(surface, taskId);
      break;
    case "resumed":
      syncEventState(local, data, "running");
      local.phaseName = surface === "cad" ? t("正在继续翻译") : t("正在继续提交页面");
      if (surface === "pdf") void fetchPdfPagesSnapshot(surface, taskId);
      break;
    case "word_recovery":
      local.wordRecovery = data;
      break;
    case "pdf_page_recovery":
      local.pdfPageRecovery = data;
      // 跑起来之后每处理完一页都会来一条，合并刷新，别一条一个请求。
      schedulePdfSnapshotRefresh(surface, taskId);
      break;
    case "pdf_review":
      local.pdfReview = data;
      // 跑起来之后每处理完一页都会来一条，合并刷新，别一条一个请求。
      schedulePdfSnapshotRefresh(surface, taskId);
      break;
    case "pdf_page_action":
      // 没有聚合本地状态需要更新——这个事件纯粹是「去拉一次逐页快照」的信号。
      if (surface === "pdf") void fetchPdfPagesSnapshot(surface, taskId);
      break;
    case "done":
    case "completed_with_issues":
    case "error":
    case "stopped":
    case "interrupted": {
      local.task = { ...local.task, state: event.type as TaskStatus["state"], terminal: true, result: (data as JsonObject) ?? local.task.result };
      local.fileProgress = reduceFileProgress(local.fileProgress, (data as JsonObject).file_progress as never);
      // Per-file terminal state remains authoritative; task completion never promotes every file.
      if (surface === "pdf") void fetchPdfPagesSnapshot(surface, taskId);
      finishTask(surface, local.task);
      return;
    }
    default:
      break;
  }
  rerender(surface);
}

/** 暂停中/已暂停/正在停止时，进度卡片标题说的是这个状态本身（「已暂停提交新页面」）。
 *  这期间已提交的页还会陆续完成、继续发进度和状态句，不能让它们把标题改回「正在翻译…」，
 *  否则顶栏写着已暂停，卡片却像还在跑。 */
const STATE_PHASE_KEYWORD: Partial<Record<TaskStatus["state"], RegExp>> = {
  pausing: /暂停/, // i18n-ignore 对后端原文判断
  paused: /暂停/, // i18n-ignore 对后端原文判断
  stopping: /停止/, // i18n-ignore 对后端原文判断
};

function holdsStatePhase(local: LocalTask): boolean {
  const state = local.task.state;
  return state === "pausing" || state === "paused" || state === "stopping";
}

/** 后端的状态句都带着「状态：」前缀和「[阶段 2/3]」「[收尾中]」这样的标记（给命令行
 *  日志用的）。进度卡片的标题本身就是状态，阶段名另有标签，这些前缀在卡片上全是重复。 */
function statusPhaseText(value: string): string {
  return value.replace(/^\s*状态[:：]\s*/, "").replace(/^\s*\[(?:阶段\s*\d+\s*\/\s*\d+|收尾中)\]\s*/, ""); // i18n-ignore 去掉后端原文的前缀和阶段标记
}

/** progress 事件的 phase_name：Excel / Word / PDF 只有阶段名（「翻译」），CAD 是
 *  「阶段名 · 具体一步：第 i / N 个文件」（core/cad_task_runner.py 的 _progress）。 */
function splitPhaseName(value: string): { stage: string; detail: string } {
  const text = value.trim();
  const cut = text.indexOf(" · ");
  if (cut < 0) return { stage: text, detail: "" };
  return { stage: text.slice(0, cut).trim(), detail: text.slice(cut + 3).trim() };
}

function syncEventState(local: LocalTask, data: JsonObject, fallback: TaskStatus["state"]): void {
  if (local.task.terminal) return;
  const state = text(data.state, fallback) as TaskStatus["state"];
  local.task = { ...local.task, state };
}

function finishTask(surface: Surface, task: TaskStatus): void {
  const st = states[surface];
  st.hasEverCompleted = true;
  closeStopModal(surface);
  // 任务一到终态就把续译状态整体清零：resume_output_dir 指向的是这次任务开始前的产物
  // 目录，这次任务自己刚写出的新产物根本不在那个目录里。不清空的话续译横幅还挂着
  // 「接着上次继续」，用户点「开始翻译」会把 buildPayload() 里的旧 resume_output_dir
  // 原样带上，把刚生成的内容当成「还没翻」重新翻一遍——重复花钱；而且文件表这时已经在
  // 显示这次任务的完成芯片，续译横幅还说着上一次的话，两处对不上。
  resetResumeState(st);
  const result = record(task.result);
  // 终态结果契约（core/*_task_runner.py 的 DoneMsg/ErrorMsg/StoppedMsg）没有 `summary`
  // 对象，也没有顶层 generated_count/review_count/auto_fixed_count——这些字段名在后端
  // 从未存在过。真实字段：`file_results[].success`（逐文件是否成功写出，三个 surface
  // 都有）、`review.total_count`（Excel/Word 才有；PDF 的 review 契约字段留空）、
  // `kpi.auto_recovered_text_count`（仅 Word 的严格重试/语义复核有这个概念）。缺失的
  // 字段一律按 0 处理，不用占位数字冒充「全部通过」。
  const fileResults = terminalFileResults(result);
  // 「产出了」不等于 success===true：需复核的文件同样写出了双语文件（后端给的
  // status 是 needs_review、output 是真实路径），把它算成失败会让横幅报出比输出
  // 目录里少的数字。反过来，失败的条目 output 是空字符串——这才是判断依据。
  st.fileOutcomes = new Map();
  let produced = 0;
  let failed = 0;
  // 同名（不同后缀）的文件会让「哪一行是哪个结果」无法判断，例如 report.xls 和
  // report.xlsx 的 name 都是 report。这种情况下宁可让那一行留在「未开始」，也不能
  // 在状态列上蒙一个可能张冠李戴的结论。
  const ambiguousNames = new Set<string>();
  const seenNames = new Set<string>();
  for (const file of st.files) {
    const key = fileOutcomeKey(fileLabel(file));
    if (!key) continue;
    if (seenNames.has(key)) ambiguousNames.add(key);
    seenNames.add(key);
  }
  for (const entry of fileResults) {
    const item = record(entry);
    const ok = fileResultProduced(item);
    if (ok) produced += 1;
    // 用户按停止时还没做完的文件不是失败，算在「还有 N 个没跑到」里：CAD 记为
    // status=stopped，Excel / Word 记为 status=unstarted。以前只认前者，Excel 停止后
    // 同一批文件会被说两遍——「3 个文件没能生成 · 还有 3 个文件没跑到」。
    else if (!fileResultNotReached(item)) failed += 1;
    const key = fileOutcomeKey(text(item.name));
    const fileId = text(item.file_id);
    if (key && !ambiguousNames.has(key)) {
      const pending = fileResultPendingReview(item);
      // 整份未翻译的文件不能只写「已生成」：文档确实写出来了，可里面一个字都没翻。
      // 这一行的说明沿用后端给的原话（`fully_skipped_oversize_message`）。
      const untranslated = ok && fileResultUntranslated(item);
      const notReached = !ok && fileResultNotReached(item);
      // 停止后写出的文件只翻了一部分（后端按续译检测同一口径数的格/段数）。绿色「已生成」
      // 会让人以为翻完了。
      const unfinished = ok && !untranslated ? num(item.unfinished_text_count) : 0;
      const label = notReached
        ? t("停止前未处理")
        : !ok
        ? t("未生成")
        : untranslated
          ? t("已生成 · 整份未翻译")
          : unfinished > 0
            ? (surface === "excel" ? t("部分翻译 · 剩余 {0} 个单元格", fmtNum(unfinished)) : t("部分翻译 · 剩余 {0} 段", fmtNum(unfinished)))
            : pending > 0
              ? t("已生成 · {0} 处需复核", pending)
              : t("已生成");
      st.fileOutcomes.set(key, {
        produced: ok,
        label,
        tone: unfinished > 0 ? "warn" : undefined,
        detail: unfinished > 0
          ? t("停止时该文件尚未翻译完成，未翻译的部分保留原文；点击上方「继续翻译」即可仅补译这些部分。")
          : (ok && !untranslated) || notReached
            ? ""
            : tx(redactedText(text(item.error, text(item.detail)), t("没有说明原因。"))),
      });
    }
    if (fileId) {
      if (key && !ambiguousNames.has(key)) {
        const outcome = st.fileOutcomes.get(key);
        if (outcome) st.fileOutcomes.set(fileId, outcome);
      }
    }
  }
  // stopped 不能跟 error/interrupted 分开对待：PDF 任务停在预处理阶段（还没真正开始
  // 翻译第一页）时，file_results 会是空数组——一个字节都没写出来过。Excel/Word 的停止
  // 路径无论如何都会给每个文件补一条 unstarted 记录，fileResults 从不为空，不会走到
  // 这个兜底分支；只有 PDF 这条路会。此时唯一诚实的数字是 0，不是「选中了多少个文件」
  // ——选中只代表打算翻，不代表已经产出，退回 st.selected.size 会把「已停止」说成
  // 「已生成 N 个文件、全部通过」，实际一个文件都没写出来。
  const stateNotProduced = task.state === "error" || task.state === "interrupted" || task.state === "stopped";
  const generated = fileResults.length > 0 ? produced : stateNotProduced ? 0 : st.selected.size;
  // total_count 数的是全部结果项，里面混着用户不用管的两类：severity=resolved（后端
  // 已经自动修好）和 severity=info（按设计如此、去 Word 里刷一下就行的提示，比如自动
  // 目录）。照抄总数，一份只有目录提示的正常文档会在这一屏顶着黄横幅写「1 处需复核」，
  // 而同一个任务在任务中心已经显示成「提示 1」——同一件事两屏两种说法。
  // counts 只有 Word 那一路按 severity 分桶，Excel 按 category 分桶；认不出分桶就按
  // 总数算，宁可多报也不漏报。
  const reviewCounts = record(record(result.review).counts);
  const countsBucketed = ["needs_review", "resolved", "info"].some((key) => key in reviewCounts);
  // counts 只有 Word 那一路按 severity 分桶（Excel 按 category 分桶，PDF 不填）。
  // 分不出桶就退回逐条 issue 自己带的 severity——Excel 的 quality_issues 同样带
  // severity，任务中心正是照它把「提示」从待办里摘出去的（tasks.ts 的 reviewRows）。
  // 这一步不做的话，一次只触发了「残留序号已自动修复」的 Excel 任务，在任务中心是
  // 中性的「提示 1」，切到这一屏却是黄底的「完成但有问题」——同一件事两屏两种说法，
  // 正是这次要消灭的那类矛盾。
  const issues = Array.isArray(result.issues) ? result.issues.map((raw) => text(record(raw).severity)) : [];
  const issuesBucketed = issues.length > 0 && issues.every((severity) => severity !== "");
  const severityBucketed = countsBucketed || issuesBucketed;
  const hints = countsBucketed
    ? num(reviewCounts.info)
    : issues.filter((severity) => severity === "info").length;
  // 待办数只认一个来源，不许把 issues 加进来：Excel 里判「同格译文不是这一格的译文」
  // 既写一条 issue、又给那一格上底色（core/task_runner.py 的 _set_excel_review_mark），
  // 两边一相加，一格问题会被说成两处。issues 只用来判「还有没有待办」，不用来计数。
  const openIssues = countsBucketed ? 0 : issues.filter(
    (severity) => severity !== "resolved" && severity !== "info",
  ).length;
  const review = countsBucketed
    ? Math.max(0, num(record(result.review).total_count) - num(reviewCounts.resolved) - hints)
    : num(record(result.review).total_count);
  const autoFixed = num(record(result.kpi).auto_recovered_text_count);
  const outputPath = text(result.output_dir, st.sourcePath);
  st.lastOutputPath = outputPath;
  const clauses: string[] = [];
  // 「按设定没翻的内容」要跟在生成结果后面一起说。横幅只给总数，逐文件的明细在运行卡片
  // 的文件列表上（buildFileSkipChips），完整边界在任务日志和报告里。
  const skippedOversizePages = producedFileResultTotal(result, "skipped_oversize_page_count");
  const protectedParagraphs = frontMatterTotal(result);
  // PDF 的问题是按页记的，而且那一路不填 review.total_count（那是 Excel / Word 的字段）。
  // 只看 review 的话，一份「4 页全是占位页」的任务小结照样会写「全部通过」，而下面的
  // 清单同时写着「已生成 · 4 处需复核」——同一屏上两句话互相打脸。
  // 相加的两个数必须互不重叠。review_failed_page_count 和 quality_flagged_page_count 都会
  // 和 placeholder_page_count 撞在同一页上（审核没通过的页有一半是直接退回占位页的，质检
  // 标记也留在退回前的最后一次尝试上），三个一相加就会把一页坏页说成两三页。后端为此单独
  // 给了 suspect_adopted_page_count——「有疑点但仍然采用」的页，与占位页严格互斥。
  const placeholderPages = producedFileResultTotal(result, "placeholder_page_count");
  const suspectPages = producedFileResultTotal(result, "suspect_adopted_page_count");
  const pageProblems = placeholderPages + suspectPages;
  // 同样只算真的写出了文档的文件：横幅那句是「其中 N 个文件整份都是 A3+」，「其中」指的是
  // 已生成的那几个。一份全是 A3+ 却在合成输出时失败的文件既没生成、又满足「整份未翻译」，
  // 不过滤就会出现「1 个文件没能生成 · 其中 1 个文件整份都是 A3+」——没有「其中」可言。
  const untranslatedFiles = terminalFileResults(result).filter(
    (entry) => fileResultProduced(entry) && fileResultUntranslated(entry),
  ).length;
  // 没能生成的文件排在最前面：其余小结说的都是「做到了什么」，这一句说的是
  // 「有东西没拿到」，它决定用户下一步要不要重跑。
  if (failed > 0) clauses.push(t("{0} 个文件未能生成", failed));
  // 接口一直限流时后端会先把翻好的部分写出去、其余保留原文（rate_limited_partial）。
  // 只说「N 处需复核」，用户会以为是译文质量问题挨格去看；要直说是限流没翻完、等会儿续译。
  const rateLimited = Array.isArray(result.issues)
    ? result.issues.map(record).find((issue) => text(issue.type) === "rate_limited_partial")
    : undefined;
  if (rateLimited) clauses.push(t("服务持续限流，{0} 处尚未翻译，请稍后使用「继续上次翻译」补译", num(rateLimited.count)));
  // CAD 的待复核是按文件记的（逐文件 status=needs_review），没有 review.total_count。
  const reviewFiles = surface === "cad"
    ? fileResults.filter((entry) => fileResultProduced(entry) && fileResultPendingReview(entry) > 0).length
    : 0;
  if (reviewFiles > 0) clauses.push(t("{0} 个文件需复核", reviewFiles));
  // 占位页不是原页：那是程序自己画的一张失败占位页（白底红框、写着这一页没能生成译图），
  // 源页内容一个像素都不在上面。说成「已保留原页」会让用户去输出 PDF 里找原文。
  // 真正原样保留的只有「跳过 A3+」那一路——它从源 PDF 矢量直传，说法在下面那句。
  if (placeholderPages > 0) clauses.push(t("{0} 页未能生成译文，已插入占位页", placeholderPages));
  if (suspectPages > 0) clauses.push(t("{0} 页译文有疑点，已采用但建议复核", suspectPages));
  if (skippedOversizePages > 0) clauses.push(t("跳过 {0} 页 A3+，原样保留", skippedOversizePages));
  // 整份都被跳过的文件必须单独点名：它的输出文档跟源文件逐字节一样，一个字都没翻。
  // 后端为此把这种文件的 success 判成 false（见 _file_record_to_result），也在
  // _record_needs_review 里把它算作需复核；界面只说「跳过 N 页」的话，用户会以为
  // 那是一份翻好的文件里少翻了几页，而实际上整份都没动。
  if (untranslatedFiles > 0) {
    clauses.push(t("其中 {0} 个文件全部为 A3+ 页面，未翻译任何内容", untranslatedFiles));
  }
  if (protectedParagraphs > 0) clauses.push(t("保护开头 {0} 段未翻译", protectedParagraphs));
  if (review > 0) clauses.push(t("{0} 处需复核", review));
  // 提示不是待办，但也不能不说：自动目录那条要用户自己回 Word 按一下 F9，不说他不会做。
  if (hints > 0) clauses.push(t("{0} 处提示需手动处理", hints));
  // 中途换过连接就明说：这一半译文出自另一家服务商，用户回头比质量、查账单都要知道。
  // 详细的切换时刻和原因在任务中心的快照行里。
  const switchCount = num(record(result.connections).switch_count);
  if (switchCount > 0) {
    const finalLabel = text(record(result.connections).final_label);
    clauses.push(finalLabel ? t("中途切换了连接，后续部分由「{0}」完成", finalLabel) : t("中途切换连接 {0} 次", switchCount));
  }
  if (autoFixed > 0) clauses.push(t("{0} 处已自动处理", autoFixed));
  // 停止且真有内容没跑到时，横幅要报出「还剩多少」——不然用户只知道自己点了停止，
  // 不知道停在了哪、值不值得重新跑。字段名照抄任务中心已经在用的口径（tasks.ts）：
  // PDF 按页算（kpi.unstarted_page_count，core/pdf_image_translation.py 的 _done_kpi），
  // Excel/Word 按文件算（kpi.unstarted_file_count，core/task_runner.py 的
  // _build_result_contract）。两个字段不会同时非零，谁非空就报谁；旧任务记录没有这
  // 两个字段时 num() 按 0 处理，不会显示这一句——不是新字段就必须报数。
  // Word 是先整批翻完再逐个写文件，停止时每个文件都会写出来，「没跑到的文件」恒为 0；
  // 没翻到的是段落（kpi.unstarted_text_count），它们保留原文留给续译。不报这个数，
  // 横幅会在三分之二段落还是原文时写「其余全部通过」。
  const unfinishedRaw = record(result.kpi).unstarted_text_count;
  const unfinishedTexts = task.state === "stopped" ? num(unfinishedRaw) : 0;
  // Excel / Word 停止后数不出产物里还剩多少原文时，后端不写这个键（不是 0）。
  // 不知道就不能说「全部翻完」「全部通过」。
  const unfinishedUnknown = task.state === "stopped"
    && (surface === "excel" || surface === "word")
    && typeof unfinishedRaw !== "number";
  if (task.state === "stopped") {
    const unfinishedPages = num(record(result.kpi).unstarted_page_count);
    const unfinishedFiles = num(record(result.kpi).unstarted_file_count);
    if (unfinishedPages > 0) clauses.push(t("尚有 {0} 页未处理", unfinishedPages));
    else if (unfinishedFiles > 0) clauses.push(t("尚有 {0} 个文件未处理", unfinishedFiles));
    if (unfinishedTexts > 0) clauses.push(t("尚有 {0} 段未翻译，已保留原文，可继续翻译", unfinishedTexts));
  }
  // 按了「安全停止」、但在飞的页刚好全部跑完时，任务照常走完成分支。不点这一句的话，
  // 屏幕上只剩「已完成 · 全部通过」，用户不知道自己那一下有没有截掉内容（只有运行日志
  // 里有一条 WARN）。
  const stopInfo = record(result.stop);
  if (stopInfo.requested === true && stopInfo.truncated === false) {
    clauses.push(t("请求停止时所有页面均已完成，没有内容被截断"));
  }
  // 「全部通过」是一句承诺，只有真的一个问题都没有才能说。有文件没生成时一个字都不提，
  // 有需复核/占位页/已自动处理时说「其余」。
  // 「其余」也得真的有其余：4 页 PDF 四页全是占位页时，前面几句已经把每一页都点了名，
  // 再补一句「其余全部通过」是在给一个不存在的剩余部分背书。
  // 分母跟分子取同一批文件（只算写出了文档的），否则一个整份失败的文件会把总页数抬高，
  // 「问题页是否已覆盖全部页」就永远判不成立。
  // 跳过的 A3+ 页也算「这一页没翻」。少了它，一份 4 页里 2 页跳过、2 页退回占位页的文件
  // （每一页都没翻）仍会落到「其余全部通过」——正是上面这句注释要防的那件事。
  const totalPages = producedFileResultTotal(result, "page_count");
  const untranslatedPages = pageProblems + skippedOversizePages;
  const everyPageHasProblem = totalPages > 0 && untranslatedPages >= totalPages;
  // PDF 没开逐页审核时谁也没判过「通过」：逐文件的 review_enabled 一个都不为真（旧记录
  // 没有这个键，也按没开算——说「已生成」不会错，说「通过」可能是错的）。
  const pdfUnreviewed = surface === "pdf"
    && !fileResults.some((entry) => record(entry).review_enabled === true);
  if (failed === 0 && generated > 0 && !everyPageHasProblem && untranslatedFiles === 0 && reviewFiles < generated && unfinishedTexts === 0 && !unfinishedUnknown) {
    const hasOther = review > 0 || autoFixed > 0 || pageProblems > 0 || hints > 0 || openIssues > 0 || reviewFiles > 0;
    clauses.push(
      pdfUnreviewed
        ? hasOther ? t("其余页面已生成") : task.state === "stopped" ? t("已处理到的页面均已生成") : t("全部页面已生成")
        : hasOther
          ? t("其余全部通过")
          // 停止时还有文件没跑到，光说「全部通过」会让人以为整批都好了。
          : task.state === "stopped" ? t("已生成的全部通过") : t("全部通过"),
    );
  }
  if (generated > 0) clauses.push(t("输出至 {0}", outputPath));
  const tone = resultTone(
    task.state,
    generated,
    failed,
    review + pageProblems + untranslatedFiles + openIssues + unfinishedTexts,
    autoFixed,
    severityBucketed,
  );
  // 完成汇总要用扫描期的图片/文本框计数。任务失败时不出这张卡：那种情况下
  // 「哪些没翻」根本说不准，横幅已经说了任务未完成。
  st.excelDoneNotice = surface === "excel" && tone !== "fail" ? buildExcelDoneNotice(st, result) : null;
  // 横幅已经判成绿色（零待办，最多剩几条「回 Word 按 F9」这类提示），状态词还写
  // 「完成但有问题」的话，同一行里绿勾和「有问题」互相打脸，后缀又是空的，用户只能
  // 去猜到底哪里有问题。有没有问题以 tone 为准。
  const stateWord = tone === "ok" && task.state === "completed_with_issues"
    ? terminalStateWord("done")
    : terminalStateWord(task.state);
  const reason = tx(redactedText(result.message, ""));
  if (task.state === "stopped") {
    // 停止是用户自己的操作，不是失败：不报红、不弹错误 toast、按钮也不叫「查看失败原因」。
    // 后端的停止说明（如 CAD 的「已完成文件和断点均已保留」）只在这里出现一次。
    // Excel / Word 只说一句话：后端说明、「N 段保留原文待续译」和输出路径以前一起堆在
    // 横幅上，同一个剩余数说两遍，后面还跟着三行路径（「打开输出目录」按钮就在右边）。
    // 不是用户按的停止（Word 持续限流时自行收尾）：后端说明里是原因，必须原样给出，
    // 换成「已翻好的内容写进了文件」那句会让人以为是自己点了停止。
    const userStopped = task.stop_requested !== false;
    const condensed = userStopped && (surface === "excel" || surface === "word")
      ? stoppedSummarySentence(surface, generated, failed, unfinishedUnknown ? null : unfinishedTexts, review)
      : "";
    st.bannerInfo = {
      title: generated > 0 ? t("{0} · 已生成 {1} 个文件", stateWord, generated) : t("{0} · 没有生成文件", stateWord),
      subtitle: condensed || [reason, ...clauses].filter(Boolean).join(" · "),
      tone,
      statusLabel: stateWord,
      hasOutput: generated > 0,
    };
  } else if (tone === "fail") {
    const detail = reason || (generated > 0
      ? t("任务在结束前中断，已生成的文件仍保留在输出目录。")
      : t("任务未生成任何文件，原因请查看下方日志或完整报告。"));
    st.bannerInfo = {
      title: generated > 0 ? t("{0} · 已生成 {1} 个文件", stateWord, generated) : t("{0} · 没有生成文件", stateWord),
      // 零产出也要拼 clauses：停止在阶段 2 之前时 generated 为 0，而「还有 N 页/
      // 个文件没跑到」恰恰在 clauses 里——那正是最需要报数的一次（其余 clauses
      // 在零产出时本来就都是空的，拼上无害）。
      subtitle: [detail, ...clauses].join(" · "),
      tone,
      statusLabel: generated > 0 ? t("{0} · 部分生成", stateWord) : stateWord,
      hasOutput: generated > 0,
    };
  } else {
    // 后缀要指着「让这条横幅变黄的那件事」说。提示排在自动处理前面的话，一份既有
    // 目录提示、又有自动恢复的文档，横幅写「有提示」而真正让它变黄的是自动处理——
    // 用户照着提示去 Word 按了 F9，回来发现还是黄的。所以按报黄的顺序排：需复核 →
    // 自动处理 → 提示（提示本身不报黄，排最后）。
    const suffix = failed > 0
      ? t("有文件未生成")
      : review > 0 || pageProblems > 0 || openIssues > 0
        ? t("需复核")
        : autoFixed > 0
          ? t("有自动处理")
          : hints > 0
            ? t("有提示")
            : "";
    st.bannerInfo = {
      title: stateWord === t("已完成")
        ? t("已生成 {0} 个文件", generated)
        : generated > 0
          ? t("{0} · 已生成 {1} 个文件", stateWord, generated)
          : t("{0} · 尚未生成文件", stateWord),
      subtitle: clauses.join(" · "),
      tone,
      statusLabel: suffix ? `${stateWord} · ${suffix}` : stateWord,
      // 停止或失败时一个文件都没写出，后端会把空的输出文件夹删掉、output_dir 置空；
      // 这时再给「打开输出目录」，点进去要么是空文件夹，要么是源文件夹。
      hasOutput: generated > 0 || text(result.output_dir) !== "",
    };
  }
  st.showBanner = true;
  // 清单、勾选、来源路径都保留：任务结束正是用户要重跑失败文件、或换个设置再来一遍的
  // 时候。以前这里全部清空，等于把「再来一次」的入口一起删了，他只能重新选路径重扫。
  // 页面开着时横幅已经把原因原话写出来了，再弹一条同样的提示就是一句话说两遍。
  // 只有用户在别的页面时才用提示告诉他任务失败了。
  if (tone === "fail" && !st.renderer) {
    showToast({ message: reason || t("任务未能顺利完成。"), error: true });
  }
  rerender(surface);
}

/** Excel / Word 停止横幅的那一句话：写出了什么、还剩多少、下一步点哪里。 */
function stoppedSummarySentence(
  surface: Surface,
  generated: number,
  failed: number,
  unfinished: number | null,
  review: number,
): string {
  if (generated === 0) return t("停止时尚无已翻译的内容，未生成文件。");
  const excel = surface === "excel";
  // 整句做词条（英法语序和中文不同，不能拼半句）：正文三种 × 有没有复核尾巴。
  // null：后端没能数出剩余多少，不报数，也不说「全部翻完」。
  const body = unfinished === null
    ? review > 0
      ? t("已翻译内容已写入文件，未翻译部分保留原文；点击「继续翻译」可从停止处继续。另有 {0} 处需复核。", review)
      : t("已翻译内容已写入文件，未翻译部分保留原文；点击「继续翻译」可从停止处继续。")
    : unfinished > 0
      ? excel
        ? review > 0
          ? t("已翻译内容已写入文件，尚有 {0} 个单元格未翻译、保留原文；点击「继续翻译」可从停止处继续。另有 {1} 处需复核。", fmtNum(unfinished), review)
          : t("已翻译内容已写入文件，尚有 {0} 个单元格未翻译、保留原文；点击「继续翻译」可从停止处继续。", fmtNum(unfinished))
        : review > 0
          ? t("已翻译内容已写入文件，尚有 {0} 段未翻译、保留原文；点击「继续翻译」可从停止处继续。另有 {1} 处需复核。", fmtNum(unfinished), review)
          : t("已翻译内容已写入文件，尚有 {0} 段未翻译、保留原文；点击「继续翻译」可从停止处继续。", fmtNum(unfinished))
      : review > 0
        ? t("停止前已全部翻译完成，另有 {0} 处需复核。", review)
        : t("停止前已全部翻译完成。");
  return failed > 0 ? t("{0} 个文件未能生成；其余{1}", failed, body) : body;
}

/** 这个文件是因为用户停止才没做（CAD：stopped；Excel / Word：unstarted），而不是失败。 */
function fileResultNotReached(entry: unknown): boolean {
  const status = text(record(entry).status);
  return status === "stopped" || status === "unstarted";
}

/** 逐文件结果是否真的写出了文件。 */
/** 状态列的连接键：扫描项与 file_results 两边字段不一致——PDF 的 `name` 是带扩展名的
 *  文件名，Excel / Word 的 `name` 是不含扩展名的主干名，而 Word 那一路除了 `name` 什么
 *  都没有（source_path 会被隐私过滤置空）。所以两边统一归一成「去掉已知文档扩展名的
 *  文件名」，只要算法一致就能对上。
 *
 *  只削掉认识的扩展名：`报价.v2` 这种带点的主干名不能被当成扩展名削掉，否则同一个文件
 *  两边算出来的键会不一样。 */
const KNOWN_DOC_EXT_RE = /\.(?:xlsx|xlsm|xls|docx|doc|pdf|png|jpe?g|webp|bmp|tiff?|dwg|dxf)$/i;

function fileOutcomeKey(raw: unknown): string {
  const value = text(raw).trim();
  if (!value) return "";
  let base = value.replace(/\\/g, "/").split("/").pop() ?? "";
  // 反复削到削不动为止。两边给的字符串本来就差一层扩展名（扫描项是主干名、逐文件结果是
  // 带扩展名的文件名），只削一次的话 `合同.doc.pdf` 一边算出「合同」、另一边算出「合同.doc」，
  // 键对不上，那一行跑完还写着「未开始」——正是这一版要修的老毛病本身。
  while (KNOWN_DOC_EXT_RE.test(base)) base = base.replace(KNOWN_DOC_EXT_RE, "");
  return base.trim();
}

/** 这个文件里还有多少处需要人去看。三条路的字段各不相同：
 *  - Excel：后端逐文件给 review_count（位置计数，见 core/task_runner.py）
 *  - Word：issues[] 里只有 severity=needs_review 才是待办，resolved 是已经自动恢复好的
 *  - PDF：退回占位页的页数，加上「有疑点但仍然采用」的页数（后端算好的互斥计数，
 *    见 core/pdf_image_translation.py 的 suspect_adopted_page_count）。口径跟后端判
 *    needs_review 的那套一致，否则一份有占位页的文件在这一行是干干净净的「已生成」，
 *    而任务中心的同一份文件挂着「需复核」徽章。
 *  9.2.6 判的是 `status === "needs_review"`——Excel 实际给 `succeeded`，Word 根本没有
 *  这个字段，所以这个标签从来没出现过。 */
function fileResultPendingReview(item: JsonObject): number {
  const direct = num(item.review_count);
  if (direct > 0) return direct;
  const issues = Array.isArray(item.issues) ? item.issues : [];
  let pending = 0;
  for (const raw of issues) {
    if (text(record(raw).severity) === "needs_review") pending += 1;
  }
  if (pending > 0) return pending;
  // CAD：逐条未解决的文字 + 写回后仍像外文的残留文字。
  if (item.status === "needs_review") {
    const stats = record(item.stats);
    const unresolved = Array.isArray(item.unresolved) ? item.unresolved.length : 0;
    return Math.max(1, unresolved + num(stats.residual_foreign_text_count) + num(stats.replacement_characters));
  }
  return num(item.placeholder_page_count) + num(item.suspect_adopted_page_count);
}

function fileResultProduced(item: JsonObject): boolean {
  if (item.success === true) return true;
  if (item.status === "failed" || item.status === "stopped") return false;
  return Boolean(text(item.output) || text(item.compressed_output) || text(item.output_path));
}

/** 整份都被「跳过 A3+」跳过的文件：输出文档写出来了，但内容跟源文件逐字节一样，一个字都没翻。
 *  后端把它的 success 判成 false 并算进 needs_review（`_file_record_to_result` /
 *  `_record_is_fully_skipped_oversize`），可它有输出路径，`fileResultProduced` 仍会算「已生成」。
 *  不单独认出来的话，这一行就是干干净净一句「已生成」，而任务中心给同一份文件挂着「需复核」。 */
function fileResultUntranslated(item: JsonObject): boolean {
  if (item.all_pages_skipped_oversize === true) return true;
  const pages = num(item.page_count);
  return pages > 0 && num(item.skipped_oversize_page_count) >= pages;
}

/** 状态词只有一份，跟任务中心共用（见 ui/src/task-state-labels.ts）。这里以前另存了一份
 *  switch，四个终态里没有一个跟任务中心写的一样，同一个任务在两屏上是两种说法。 */
function terminalStateWord(state: TaskStatus["state"]): string {
  return taskStateWord(state);
}

/** 终态 + 逐文件结果一起决定横幅的口气；只看 state 会把「一个文件都没生成」画成绿色。 */
function resultTone(
  state: TaskStatus["state"],
  generated: number,
  failed: number,
  review: number,
  autoFixed: number,
  settled: boolean,
): ResultTone {
  if (state === "error" || state === "interrupted") return "fail";
  // 停止是用户主动的，哪怕一个文件都还没写出来也不算失败。
  if (state === "stopped") return "warn";
  if (generated === 0) return "fail";
  if (failed > 0) return "warn";
  if (review > 0 || autoFixed > 0) return "warn";
  // 后端只要给出任何一条结果项，任务终态就是 completed_with_issues，这一行过去无条件
  // 报黄——包括一条待办都没有、只剩「去 Word 里按 F9 刷新目录」这类提示的任务。真正
  // 该报黄的情况上面几行已经全部拦下。
  // 放行的条件是「后端按 severity 分了桶」，不是「有提示」：一份问题全被自动修好
  // （severity=resolved）、连提示都没有的任务同样零待办，只认 hints>0 会让它顶着
  // 黄横幅写「全部通过」，颜色和文字互相打脸。分不出桶（Excel / PDF 那两路）说明
  // 「零待办」这个结论不可信，照旧报黄。
  if (state === "completed_with_issues") return settled ? "ok" : "warn";
  return "ok";
}

// ---------------------------------------------------------------------------
// 停止 / 暂停 / 继续 / 结束暂停
// ---------------------------------------------------------------------------

/** 正在展示的「安全停止」确认框。任务自己走到终态时要把它关掉——见 closeStopModal。 */
const stopModals: Record<Surface, ModalHandle | null> = { excel: null, word: null, pdf: null, cad: null };

function closeStopModal(surface: Surface): void {
  // 任务已经结束了，再问「要不要停止当前任务」没有意义：这个框以前会一直浮在结果横幅
  // 上面，用户只能自己去点「继续执行」把它关掉，而那个按钮的字面意思正好相反。
  stopModals[surface]?.close();
  stopModals[surface] = null;
}

function confirmStopTask(surface: Surface, st: SurfaceState): void {
  const taskId = st.task?.task.task_id;
  if (!taskId) return;
  closeStopModal(surface);
  stopModals[surface] = openModal({
    tone: "warn",
    icon: "stop",
    sourceLabel: t("停止运行中的任务"),
    title: t("停止当前任务？"),
    body: [stopConfirmBody(surface)],
    actions: [
      { label: t("继续执行"), onClick: () => { stopModals[surface] = null; } },
      { label: t("停止"), variant: "danger-solid", onClick: () => { stopModals[surface] = null; void stopTask(taskId); } },
    ],
  });
}

async function stopTask(taskId: string): Promise<void> {
  try {
    const c = await getClient();
    await c.request(`/api/tasks/${taskId}/stop`, { method: "POST" });
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, t("停止任务失败。")), error: true });
  }
}

/** 暂停 / 继续的接口会回传最新任务状态，直接用它更新界面——不能只等事件流：
 *  以前这里把返回值丢掉，而事件处理又只改了提示文字，任务状态一直停在 running，
 *  暂停后底栏还是「暂停」按钮，「继续」永远不出现。 */
function applyControlResponse(surface: Surface, taskId: string, status: TaskStatus): void {
  const local = states[surface].task;
  if (!local || local.task.task_id !== taskId || local.task.terminal) return;
  local.task = { ...local.task, state: status.state };
  rerender(surface);
}

async function pauseTask(surface: Surface, st: SurfaceState): Promise<void> {
  const taskId = st.task?.task.task_id;
  if (!taskId) return;
  try {
    const c = await getClient();
    applyControlResponse(surface, taskId, await c.controlTask(taskId, "pause"));
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, t("暂停失败。")), error: true });
  }
}

async function resumeTask(surface: Surface, st: SurfaceState): Promise<void> {
  const taskId = st.task?.task.task_id;
  if (!taskId) return;
  try {
    const c = await getClient();
    applyControlResponse(surface, taskId, await c.controlTask(taskId, "resume"));
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, t("继续任务失败。")), error: true });
  }
}

/** 与 main.ts / tasks.ts 一致：结束暂停用原生 window.confirm，而不是 openModal——两处既有实现
 *  都这么做，这里保持同一先例，不额外造一种新的确认样式。 */
function confirmEndPaused(st: SurfaceState): void {
  const taskId = st.task?.task.task_id;
  if (!taskId) return;
  const ok = window.confirm(t("结束后将不再翻译剩余页面，已完成的页面和报告将保留。确定要结束此任务吗？"));
  if (!ok) return;
  void (async () => {
    try {
      const c = await getClient();
      await c.request(`/api/tasks/${taskId}/end-paused`, { method: "POST" });
    } catch (error) {
      showToast({ message: redactedText((error as Error)?.message, t("无法结束任务，请重试。")), error: true });
    }
  })();
}

async function openTaskLocalFile(path: string | undefined, reveal: boolean): Promise<void> {
  if (!path) {
    showToast({ message: t("没有可打开的输出路径。"), error: true });
    return;
  }
  try {
    await invoke("open_local_path", { path, reveal });
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, t("打开路径失败。")), error: true });
  }
}
