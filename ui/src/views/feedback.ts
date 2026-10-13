// 反馈视图 —— 用户写一段话，自己勾选要附上的内容，然后发送或保存为 ZIP。
//
// 隐私约定（改这个页面时必须守住）：
//   - 什么都不会自动发出；只有点了「发送」才上传。
//   - 「附带内容」是一组按类别的勾选项，勾了哪些就上传哪些：系统与版本信息、任务日志
//     默认勾选；原文件、译文默认不勾（文件可能含客户资料）。「其他文件」单列，
//     用户自己添加了才上传。
//   - 日志、原文件、译文取自下面「选择任务」里选中的那几次；任务默认一个都不选，
//     只有从「反馈此任务」弹窗转过来时预先选上那一次。
//   - 日志里的文件名由后端换成「文件1」「文件2」，预览里看到的就是实际发送的内容。
// 草稿存在模块变量里，切到别的页面再回来不会丢；发送成功后才清空。

import { open } from "@tauri-apps/plugin-dialog";
import type { ViewParams } from "../router";
import { navigate } from "../router";
import { setTopbar } from "../shell";
import { ApiClient, ApiError } from "../api-client";
import { createButton, createCard, createDropdown, createHintBadge, openModal, showToast } from "../components";
import { icon } from "../icons";
import { currentLocale, localeTag, localized, t, tx } from "../i18n";
import { taskStateWord } from "../task-state-labels";
import {
  SEND_FAILURES,
  confirmSendWithoutEmail,
  createPrivacyNote,
  createSendProgress,
  emailLooksValid,
  formatBytes,
  uploadFeedback,
} from "../feedback-shared";
import "./feedback.css";

type Category = "problem" | "suggestion" | "other";

interface FeedbackTask {
  task_id: string;
  surface: string;
  state: string;
  source_label: string;
  updated_at: number;
}

interface FeedbackContext {
  endpoint_configured: boolean;
  tasks: FeedbackTask[];
  max_attachment_bytes: number;
  /** 截图合计上限，和文件分开算。 */
  max_images_bytes?: number;
  max_files: number;
  max_tasks: number;
  /** 上次成功发送时填的邮箱（只存在本机），新反馈默认填上。 */
  saved_email?: string;
  /** 没填邮箱发送时要不要问一次。 */
  ask_email?: boolean;
}

interface PickedFile {
  path: string;
  name: string;
  bytes: number;
  exists: boolean;
}

type FileKind = "source" | "output" | "compressed";

interface TaskFile extends PickedFile {
  kind: FileKind;
}

interface PreviewPayload {
  system: Record<string, unknown> | null;
  tasks: { task_id: string; surface: string; state: string; log: string }[];
  files: { name: string; bytes: number }[];
  images: { name: string; bytes: number }[];
  package_bytes: number;
}

interface PastedImage {
  /** base64，不带 data: 前缀，原样发给后端。 */
  data: string;
  bytes: number;
  url: string;
}

interface Draft {
  category: Category;
  description: string;
  contact: string;
  /** 用户动过邮箱框就不再自动填（包括有意清空）。 */
  contactEdited: boolean;
  includeSystem: boolean;
  includeLogs: boolean;
  includeSources: boolean;
  includeOutputs: boolean;
  taskIds: Set<string>;
  /** 「其他文件」里自己添加的文件；添加了就上传，移除即不上传。 */
  files: PickedFile[];
  /** 粘贴到描述框里的截图，按粘贴顺序。 */
  images: PastedImage[];
}

function emptyDraft(): Draft {
  return {
    category: "problem",
    description: "",
    contact: rememberedEmail,
    contactEdited: false,
    includeSystem: true,
    includeLogs: true,
    includeSources: false,
    includeOutputs: false,
    taskIds: new Set(),
    files: [],
    images: [],
  };
}

const CATEGORIES: { value: Category; label: string; hint: string }[] = localized(() => ([
  { value: "problem", label: t("问题"), hint: t("功能异常、报错或结果不正确") },
  { value: "suggestion", label: t("建议"), hint: t("希望新增或改进的功能") },
  { value: "other", label: t("其他"), hint: t("其他意见或想法") },
]));

const SURFACE_LABEL: Record<string, string> = localized(() => ({
  excel: "Excel",
  word: "Word",
  pdf: "PDF",
  cad: "CAD",
  cleaner: t("记忆库清洗"),
  tm_clean: t("记忆库清洗"),
}));

/** 上次成功发送时留的邮箱；新写的反馈默认填它，省得每次重输。 */
let rememberedEmail = "";
let draft: Draft = emptyDraft();
let context: FeedbackContext | null = null;
let contextError = "";
let busy: "" | "preview" | "save" | "send" = "";
let sentId = "";
/** 发出时填的邮箱，「已发送」页据此说明会发邮件到哪里。 */
let sentEmail = "";
let sendFailure = "";
/** 正在上传时后端报回的已发字节；null 表示还在打包。 */
let sendProgress: { sent: number; total: number } | null = null;
// 进度每 300 ms 更新一次，只改进度这一块，不重画整页（否则正在输入的内容和滚动位置会丢）。
let progressView: ReturnType<typeof createSendProgress> | null = null;
let mounted = false;
let rootEl: HTMLElement | null = null;
let clientPromise: Promise<ApiClient> | null = null;
// 每次任务能附上的文件，勾选任务后才去读；"loading" 时显示占位，读失败显示原因。
const taskFiles = new Map<string, TaskFile[] | "loading" | { error: string }>();

function getClient(): Promise<ApiClient> {
  if (!clientPromise) {
    const instance = new ApiClient();
    clientPromise = instance.connect().then(() => instance).catch((error) => {
      clientPromise = null;
      throw error;
    });
  }
  return clientPromise;
}

export function mount(container: HTMLElement, params: ViewParams): void {
  mounted = true;
  rootEl = container;
  const taskId = typeof params.taskId === "string" ? params.taskId : "";
  if (taskId) {
    // 从「反馈此任务」弹窗转过来：这是一个具体的问题，预先勾上那次任务的日志。
    if (sentId) {
      draft = emptyDraft();
      sentId = "";
    }
    draft.category = "problem";
    setTaskChecked(taskId, true);
    // 从「反馈此任务」弹窗转过来：弹窗里已经写的描述和邮箱接着用。
    if (typeof params.description === "string" && params.description && !draft.description) draft.description = params.description;
    if (typeof params.contact === "string" && params.contact) {
      draft.contact = params.contact;
      draft.contactEdited = true;
    }
    const include = params.include as Partial<Record<"system" | "logs" | "sources" | "outputs", boolean>> | undefined;
    if (include && typeof include === "object") {
      if (typeof include.system === "boolean") draft.includeSystem = include.system;
      if (typeof include.logs === "boolean") draft.includeLogs = include.logs;
      if (typeof include.sources === "boolean") draft.includeSources = include.sources;
      if (typeof include.outputs === "boolean") draft.includeOutputs = include.outputs;
    }
  }
  render();
  void loadContext();
}

export function unmount(): void {
  mounted = false;
  rootEl = null;
}

async function loadContext(): Promise<void> {
  try {
    const client = await getClient();
    context = await client.request<FeedbackContext>("/api/feedback/context");
    contextError = "";
    rememberedEmail = context.saved_email ?? "";
    if (!draft.contactEdited) draft.contact = rememberedEmail;
    // 任务记录被删掉了就不再勾着它，免得预览时报「记录已不存在」。
    const known = new Set(context.tasks.map((task) => task.task_id));
    for (const id of [...draft.taskIds]) if (!known.has(id)) setTaskChecked(id, false);
    for (const id of draft.taskIds) void loadTaskFiles(id);
  } catch (error) {
    contextError = errorText(error);
  }
  if (mounted) render();
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function errorReason(error: unknown): string {
  return error instanceof ApiError ? error.reason : "";
}

function formatWhen(epoch: number): string {
  if (!epoch) return "";
  return new Date(epoch * 1000).toLocaleString(localeTag(), { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

function requestBody(): Record<string, unknown> {
  return {
    category: draft.category,
    description: draft.description,
    contact: draft.contact,
    include_system: draft.includeSystem,
    include_logs: draft.includeLogs,
    include_source_files: draft.includeSources,
    include_output_files: draft.includeOutputs,
    ui_language: currentLocale(),
    task_ids: [...draft.taskIds],
    file_paths: draft.files.map((file) => file.path),
    images: draft.images.map((image) => ({ data: image.data })),
  };
}

/** 所选任务里某一类文件（已读到的部分）。 */
function selectedTaskFiles(kinds: FileKind[]): TaskFile[] {
  const result: TaskFile[] = [];
  for (const id of draft.taskIds) {
    const files = taskFiles.get(id);
    if (Array.isArray(files)) result.push(...files.filter((file) => kinds.includes(file.kind)));
  }
  return result;
}

/** 实际会上传的文件，跟后端按同样的规则取。 */
function checkedFiles(): PickedFile[] {
  const kinds: FileKind[] = [];
  if (draft.includeSources) kinds.push("source");
  if (draft.includeOutputs) kinds.push("output", "compressed");
  const result: PickedFile[] = [];
  const seen = new Set<string>();
  for (const file of [...selectedTaskFiles(kinds), ...draft.files]) {
    if (!seen.has(file.path)) {
      seen.add(file.path);
      result.push(file);
    }
  }
  return result;
}

function tasksLoading(): boolean {
  return [...draft.taskIds].some((id) => taskFiles.get(id) === "loading");
}

function setTaskChecked(taskId: string, checked: boolean): void {
  if (checked) {
    draft.taskIds.add(taskId);
    void loadTaskFiles(taskId);
    return;
  }
  draft.taskIds.delete(taskId);
}

async function loadTaskFiles(taskId: string): Promise<void> {
  const cached = taskFiles.get(taskId);
  if (cached === "loading" || Array.isArray(cached)) return;
  taskFiles.set(taskId, "loading");
  try {
    const client = await getClient();
    const result = await client.request<{ files: TaskFile[] }>(`/api/feedback/tasks/${encodeURIComponent(taskId)}/files`);
    taskFiles.set(taskId, result.files);
  } catch (error) {
    taskFiles.set(taskId, { error: errorText(error) });
  }
  if (mounted) renderKinds();
}

function render(): void {
  if (!mounted || !rootEl) return;
  setTopbar({ title: t("反馈"), subtitle: t("报告问题或提出建议") });
  rootEl.className = "content feedback-content";
  rootEl.replaceChildren();
  const column = document.createElement("div");
  column.className = "feedback-column";
  if (sentId) {
    column.append(buildSentCard());
  } else {
    // 宽窗口左右两栏：左边写内容，右边挑附件；窄窗口退回上下排列。
    const grid = document.createElement("div");
    grid.className = "feedback-grid";
    grid.append(buildMessageCard(), buildAttachmentCard());
    column.append(grid, buildActions());
  }
  rootEl.append(column);
}

function sectionTitle(text: string, hint?: string): HTMLElement {
  const title = document.createElement("h3");
  title.className = "feedback-section-title";
  title.textContent = text;
  if (hint) title.append(createHintBadge(hint));
  return title;
}

function buildMessageCard(): HTMLElement {
  const card = createCard([], "feedback-card");

  card.append(sectionTitle(t("类型")));
  const seg = document.createElement("div");
  seg.className = "seg feedback-seg";
  for (const option of CATEGORIES) {
    const item = document.createElement("div");
    item.className = option.value === draft.category ? "segc on" : "segc";
    item.tabIndex = 0;
    item.setAttribute("role", "radio");
    item.setAttribute("aria-checked", String(option.value === draft.category));
    const b = document.createElement("b");
    b.textContent = option.label;
    const span = document.createElement("span");
    span.textContent = option.hint;
    item.append(b, span);
    const choose = (): void => {
      draft.category = option.value;
      render();
    };
    item.addEventListener("click", choose);
    item.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        choose();
      }
    });
    seg.append(item);
  }
  card.append(seg);

  card.append(sectionTitle(t("描述")));
  // 跟 Claude 的输入框一样：截图以缩略图卡片排在框内文字上方，框和图是一个整体。
  // 截图不限张数，只排一行、横向滚动，粘得再多也不会挤掉写字的地方。
  const composer = document.createElement("div");
  composer.className = "feedback-composer";
  imagesEl = document.createElement("div");
  imagesEl.className = "feedback-images";
  // 普通鼠标只有竖向滚轮：指针在截图行上时，把竖向滚动换成横向。
  imagesEl.addEventListener("wheel", (event) => {
    const strip = event.currentTarget as HTMLElement;
    if (Math.abs(event.deltaY) <= Math.abs(event.deltaX) || strip.scrollWidth <= strip.clientWidth) return;
    event.preventDefault();
    strip.scrollLeft += event.deltaY;
  }, { passive: false });
  const area = document.createElement("textarea");
  area.className = "feedback-text";
  area.rows = 12;
  area.maxLength = 5000;
  area.placeholder = t("请描述遇到的问题或希望改进的地方。如是问题，请说明操作步骤和看到的提示。可直接粘贴截图。");
  area.value = draft.description;
  area.addEventListener("input", () => {
    draft.description = area.value;
    sendFailure = "";
    syncActionState();
  });
  area.addEventListener("paste", (event) => {
    const images = imageFiles(event.clipboardData);
    if (!images.length) return;
    // 只有图片时不让浏览器再插一段文件名进去；图文混贴（如从 Word 复制）时文字照常粘贴。
    if (!event.clipboardData?.getData("text/plain")) event.preventDefault();
    void addImages(images);
  });
  composer.addEventListener("dragover", (event) => {
    if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
  });
  composer.addEventListener("drop", (event) => {
    const images = imageFiles(event.dataTransfer);
    if (!images.length) return;
    event.preventDefault();
    void addImages(images);
  });
  composer.append(imagesEl, area);
  composer.addEventListener("click", (event) => {
    if (event.target === composer) area.focus();
  });
  card.append(composer);
  renderImages();

  card.append(sectionTitle(t("邮箱（建议填写）")));
  const contact = document.createElement("input");
  contact.type = "email";
  contact.className = "feedback-input";
  contact.maxLength = 200;
  contact.autocomplete = "email";
  contact.spellcheck = false;
  contact.placeholder = "name@example.com";
  contact.value = draft.contact;
  const contactNote = document.createElement("p");
  contactNote.className = "feedback-note";
  const syncContactNote = (): void => {
    const invalid = !emailLooksValid(draft.contact);
    contact.classList.toggle("invalid", invalid);
    contactNote.classList.toggle("feedback-error", invalid);
    contactNote.textContent = invalid
      ? t("邮箱格式不正确，请检查后再发送。")
      : t("留下邮箱后，问题解决或功能上线时，我们会发邮件通知您。");
  };
  contact.addEventListener("input", () => {
    draft.contact = contact.value;
    draft.contactEdited = true;
    syncContactNote();
    syncActionState();
  });
  syncContactNote();
  card.append(contact, contactNote);
  return card;
}

function checkRow(options: { checked: boolean; disabled?: boolean; title: string; detail?: string; onChange: (checked: boolean) => void }): HTMLLabelElement {
  const row = document.createElement("label");
  row.className = options.disabled ? "feedback-check disabled" : "feedback-check";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.className = "ck";
  box.checked = options.checked;
  box.disabled = Boolean(options.disabled);
  box.addEventListener("change", () => options.onChange(box.checked));
  const copy = document.createElement("span");
  copy.className = "feedback-check-copy";
  const title = document.createElement("b");
  title.textContent = options.title;
  copy.append(title);
  if (options.detail) {
    const detail = document.createElement("span");
    detail.textContent = options.detail;
    copy.append(detail);
  }
  row.append(box, copy);
  return row;
}

// 描述框粘贴截图 ----------------------------------------------------------
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
let imagesEl: HTMLElement | null = null;

function imageFiles(data: DataTransfer | null): File[] {
  if (!data) return [];
  return Array.from(data.files).filter((file) => IMAGE_TYPES.includes(file.type));
}

function readBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

async function addImages(files: File[]): Promise<void> {
  for (const file of files) {
    if (file.size > MAX_IMAGE_BYTES) {
      showToast({ message: t("单张图片不能超过 10 MB。"), error: true });
      continue;
    }
    const used = draft.images.reduce((sum, image) => sum + image.bytes, 0);
    if (used + file.size > (context?.max_images_bytes ?? MAX_IMAGE_BYTES)) {
      showToast({ message: t("截图合计不能超过 10 MB，请删掉几张再粘贴。"), error: true });
      break;
    }
    try {
      draft.images.push({ data: await readBase64(file), bytes: file.size, url: URL.createObjectURL(file) });
    } catch (error) {
      showToast({ message: t("无法读取图片：{0}", errorText(error)), error: true });
    }
  }
  const added = draft.images.length;
  renderImages();
  renderFileSummary();
  // 新粘的截图排在最后，滚到能看见它。
  if (added && imagesEl) imagesEl.scrollLeft = imagesEl.scrollWidth;
}

function renderImages(): void {
  const el = imagesEl;
  if (!el) return;
  el.replaceChildren();
  el.hidden = !draft.images.length;
  draft.images.forEach((image, index) => {
    const tile = document.createElement("div");
    tile.className = "feedback-image";
    const view = document.createElement("button");
    view.type = "button";
    view.className = "feedback-image-view";
    view.title = t("查看大图");
    const img = document.createElement("img");
    img.src = image.url;
    img.alt = t("截图 {0}", index + 1);
    view.append(img);
    view.addEventListener("click", () => openImage(image));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "feedback-image-remove";
    remove.title = t("移除");
    remove.setAttribute("aria-label", t("移除"));
    remove.append(icon("close", { size: "sm" }));
    remove.addEventListener("click", () => {
      URL.revokeObjectURL(image.url);
      draft.images.splice(index, 1);
      renderImages();
      renderFileSummary();
    });
    tile.append(view, remove);
    el.append(tile);
  });
}

function openImage(image: PastedImage): void {
  const img = document.createElement("img");
  img.className = "feedback-image-full";
  img.src = image.url;
  openModal({ tone: "tint", icon: "search", title: t("截图"), body: [img], actions: [{ label: t("关闭"), variant: "primary" }], wide: true });
}

function buildAttachmentCard(): HTMLElement {
  const card = createCard([], "feedback-card");

  card.append(sectionTitle(t("附带内容")));
  kindsEl = document.createElement("div");
  kindsEl.className = "feedback-kinds";
  card.append(kindsEl);
  renderKinds();

  card.append(sectionTitle(
    t("选择任务"),
    t("上面勾选的任务日志、原文件和译文取自这里选中的任务。日志已去除 API 密钥和文件路径，文件名替换为「文件1」「文件2」等；为便于定位问题，可能保留少量原文片段。发送前可点击「查看将发送的内容」逐条核对。"),
  ));
  if (context?.tasks.length) card.append(buildTaskToolbar());
  taskListEl = document.createElement("div");
  taskListEl.className = "feedback-list feedback-task-list";
  renderTaskList();
  card.append(taskListEl);

  card.append(sectionTitle(t("其他文件"), t("任务以外的文件，例如别处的文档。添加了就会上传，移除即不上传。文件可能包含客户资料，仅在需要我们查看时添加。")));
  otherFilesEl = document.createElement("div");
  otherFilesEl.className = "feedback-other-files";
  card.append(otherFilesEl);
  renderOtherFiles();

  fileSummaryEl = document.createElement("p");
  card.append(fileSummaryEl);
  renderFileSummary();
  return card;
}

let kindsEl: HTMLElement | null = null;
let otherFilesEl: HTMLElement | null = null;

function sizeOf(files: PickedFile[]): number {
  return files.reduce((sum, file) => sum + file.bytes, 0);
}

// 原文件、译文后面跟一个小计，免得勾了才发现所选任务里没有。
function taskFilesCount(kinds: FileKind[]): string {
  if (!draft.taskIds.size) return "";
  if (tasksLoading()) return t("统计中…");
  const files = selectedTaskFiles(kinds);
  return files.length ? t("{0} 个，{1}", files.length, formatBytes(sizeOf(files))) : t("无");
}

function kindOption(options: { checked: boolean; title: string; hint: string; count?: string; onChange: (value: boolean) => void }): HTMLLabelElement {
  const row = document.createElement("label");
  row.className = options.checked ? "feedback-kind on" : "feedback-kind";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.className = "ck";
  box.checked = options.checked;
  box.addEventListener("change", () => {
    options.onChange(box.checked);
    renderKinds();
  });
  const title = document.createElement("span");
  title.className = "feedback-kind-title";
  title.textContent = options.title;
  const hint = createHintBadge(options.hint);
  // 问号在 label 里，点它只看说明，不要顺带切换勾选。
  hint.addEventListener("click", (event) => event.preventDefault());
  row.append(box, title, hint);
  if (options.count) {
    const count = document.createElement("span");
    count.className = "feedback-kind-count";
    count.textContent = options.count;
    count.title = options.count;
    row.append(count);
  }
  return row;
}

function renderKinds(): void {
  const el = kindsEl;
  if (el) {
    el.replaceChildren(
      kindOption({
        checked: draft.includeSystem,
        title: t("系统与版本信息"),
        hint: t("应用版本、操作系统、处理器和内存、界面语言、模型连接方式，不含 API 密钥。"),
        onChange: (value) => { draft.includeSystem = value; },
      }),
      kindOption({
        checked: draft.includeLogs,
        title: t("任务日志"),
        hint: t("所选任务的运行记录。日志已去除 API 密钥和文件路径，文件名替换为「文件1」「文件2」等；为便于定位问题，可能保留少量原文片段。"),
        onChange: (value) => { draft.includeLogs = value; },
      }),
      kindOption({
        checked: draft.includeSources,
        title: t("原文件"),
        hint: t("所选任务翻译的原始文件。文件可能包含客户资料，仅在需要我们查看时勾选。"),
        count: taskFilesCount(["source"]),
        onChange: (value) => { draft.includeSources = value; },
      }),
      kindOption({
        checked: draft.includeOutputs,
        title: t("译文"),
        hint: t("所选任务生成的译文文件，含压缩版 PDF。文件可能包含客户资料，仅在需要我们查看时勾选。"),
        count: taskFilesCount(["output", "compressed"]),
        onChange: (value) => { draft.includeOutputs = value; },
      }),
    );
    if (draft.includeSources || draft.includeOutputs) el.append(createPrivacyNote());
  }
  renderFileSummary();
}

function renderOtherFiles(): void {
  const list = otherFilesEl;
  if (!list) return;
  list.replaceChildren();
  for (const file of draft.files) {
    const row = document.createElement("div");
    row.className = "feedback-file";
    row.append(icon("folder", { size: "sm" }));
    const name = document.createElement("span");
    name.className = "feedback-file-name";
    name.textContent = file.name;
    name.title = file.path;
    const size = document.createElement("span");
    size.className = "feedback-file-size";
    size.textContent = file.exists ? formatBytes(file.bytes) : t("文件不存在");
    row.append(name, size, createButton({
      label: t("移除"),
      size: "mini",
      onClick: () => {
        draft.files = draft.files.filter((item) => item.path !== file.path);
        renderOtherFiles();
        renderFileSummary();
      },
    }));
    list.append(row);
  }
  const maxFiles = context?.max_files ?? 10;
  list.append(createButton({
    label: t("添加文件…"),
    icon: "plus",
    size: "mini",
    disabled: draft.files.length >= maxFiles,
    onClick: () => void pickFiles(),
  }));
}

let fileSummaryEl: HTMLElement | null = null;

// 卡片底部一行：一共会上传几个文件、多大。超过上限时变红。截图另有额度，不算在这里。
function renderFileSummary(): void {
  const el = fileSummaryEl;
  if (!el) return;
  const files = checkedFiles();
  const total = sizeOf(files);
  const limit = context?.max_attachment_bytes ?? 40 * 1024 * 1024;
  el.className = total > limit ? "feedback-total over" : "feedback-total";
  const needsTask = (draft.includeLogs || draft.includeSources || draft.includeOutputs) && !draft.taskIds.size;
  if (needsTask) el.textContent = t("尚未选择任务，任务日志、原文件和译文不会附上。");
  else if (total) el.textContent = t("文件合计 {0} / {1}", formatBytes(total), formatBytes(limit));
  else el.textContent = t("不附带文件。");
}

// 任务多的时候按关键词筛、按条数截断。已勾选的任务始终排在最前面，不受筛选和条数影响，
// 免得用户搜别的东西时以为之前勾的那一条被取消了。
const TASK_LIMITS = [10, 20, 50, 0];
let taskQuery = "";
let taskLimit = 10;
let taskListEl: HTMLElement | null = null;
let taskCountEl: HTMLElement | null = null;

function taskLabel(task: FeedbackTask): string {
  return [SURFACE_LABEL[task.surface] ?? task.surface, tx(task.source_label)].filter(Boolean).join(" · ");
}

function taskDetail(task: FeedbackTask): string {
  return `${taskStateWord(task.state)} · ${formatWhen(task.updated_at)}`;
}

function buildTaskToolbar(): HTMLElement {
  const bar = document.createElement("div");
  bar.className = "feedback-task-tools";
  const searchWrap = document.createElement("div");
  searchWrap.className = "feedback-search";
  const search = document.createElement("input");
  search.type = "text";
  search.placeholder = t("搜索文件名、类型、状态或日期…");
  search.value = taskQuery;
  search.addEventListener("input", () => {
    taskQuery = search.value;
    renderTaskList();
  });
  searchWrap.append(icon("search", { size: "sm" }), search);
  const limit = createDropdown({
    className: "feedback-limit",
    options: TASK_LIMITS.map((value) => ({ value: String(value), label: value ? t("显示 {0} 条", value) : t("显示全部") })),
    value: String(taskLimit),
    onChange: (value) => {
      taskLimit = Number(value) || 0;
      renderTaskList();
    },
  });
  taskCountEl = document.createElement("span");
  taskCountEl.className = "feedback-task-count";
  bar.append(searchWrap, limit.root, taskCountEl);
  return bar;
}

function renderTaskList(): void {
  const list = taskListEl;
  if (!list) return;
  list.replaceChildren();
  if (!context && !contextError) {
    list.append(note(t("正在读取任务记录…")));
    return;
  }
  if (contextError) {
    list.append(note(t("无法读取任务记录：{0}", tx(contextError))));
    return;
  }
  const tasks = context?.tasks ?? [];
  if (!tasks.length) {
    list.append(note(t("暂无已结束的任务。")));
    return;
  }
  const query = taskQuery.trim().toLowerCase();
  const matches = (task: FeedbackTask): boolean =>
    !query || `${taskLabel(task)} ${taskDetail(task)}`.toLowerCase().includes(query);
  const selected = tasks.filter((task) => draft.taskIds.has(task.task_id));
  const others = tasks.filter((task) => !draft.taskIds.has(task.task_id) && matches(task));
  const matchedCount = selected.filter(matches).length + others.length;
  const room = taskLimit ? Math.max(0, taskLimit - selected.length) : others.length;
  const shown = [...selected, ...others.slice(0, room)];
  if (taskCountEl) {
    taskCountEl.textContent = query
      ? t("找到 {0} 条，共 {1} 条", matchedCount, tasks.length)
      : t("共 {0} 条", tasks.length);
  }
  const maxTasks = context?.max_tasks ?? 5;
  for (const task of shown) {
    const checked = draft.taskIds.has(task.task_id);
    list.append(checkRow({
      checked,
      disabled: !checked && draft.taskIds.size >= maxTasks,
      title: taskLabel(task),
      detail: taskDetail(task),
      onChange: (value) => {
        setTaskChecked(task.task_id, value);
        renderTaskList();
        renderKinds();
      },
    }));
  }
  if (!matchedCount && query) list.append(note(t("没有匹配的任务。")));
  else if (others.length > room) list.append(note(t("还有 {0} 条未显示，可搜索或调整显示条数。", others.length - room)));
}

function note(text: string): HTMLElement {
  const p = document.createElement("p");
  p.className = "feedback-note";
  p.textContent = text;
  return p;
}

async function pickFiles(): Promise<void> {
  try {
    const picked = await open({ title: t("选择要附上的文件"), multiple: true, directory: false });
    const paths = (Array.isArray(picked) ? picked : picked ? [picked] : []).filter((path): path is string => typeof path === "string");
    const fresh = paths.filter((path) => !draft.files.some((file) => file.path === path));
    if (!fresh.length) return;
    const client = await getClient();
    const result = await client.request<{ files: PickedFile[] }>("/api/feedback/files", {
      method: "POST",
      body: JSON.stringify({ paths: fresh }),
    });
    const maxFiles = context?.max_files ?? 10;
    const combined = [...draft.files, ...result.files];
    if (combined.length > maxFiles) showToast({ message: t("一次最多附上 10 个文件。"), error: true });
    draft.files = combined.slice(0, maxFiles);
    render();
  } catch (error) {
    showToast({ message: t("无法添加文件：{0}", tx(errorText(error))), error: true });
  }
}

let previewButton: HTMLButtonElement | null = null;
let saveButton: HTMLButtonElement | null = null;
let sendButton: HTMLButtonElement | null = null;

function syncActionState(): void {
  const empty = !draft.description.trim();
  const configured = Boolean(context?.endpoint_configured);
  const emailOk = emailLooksValid(draft.contact);
  if (previewButton) previewButton.disabled = empty || busy !== "";
  if (saveButton) saveButton.disabled = empty || busy !== "" || !emailOk;
  if (sendButton) sendButton.disabled = empty || busy !== "" || !configured || !emailOk;
}

function buildActions(): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "feedback-actions-wrap";

  if (sendFailure) {
    const banner = document.createElement("div");
    banner.className = "banner res-err feedback-banner";
    const copy = document.createElement("div");
    copy.className = "copy";
    const head = document.createElement("b");
    head.textContent = t("反馈未发出");
    const sub = document.createElement("div");
    sub.className = "sub";
    sub.textContent = tx(sendFailure);
    copy.append(head, sub);
    banner.append(copy);
    wrap.append(banner);
  }

  const actions = document.createElement("div");
  actions.className = "feedback-actions";
  previewButton = createButton({ label: busy === "preview" ? t("正在生成…") : t("查看将发送的内容"), onClick: () => void showPreview() });
  saveButton = createButton({ label: busy === "save" ? t("正在保存…") : t("保存为 ZIP"), onClick: () => void saveZip() });
  sendButton = createButton({ label: busy === "send" ? t("正在发送…") : t("发送"), variant: "primary", onClick: () => void send() });
  actions.append(previewButton, saveButton, sendButton);

  // 底栏一行：左边说明（发送时换成上传进度），右边按钮。
  const bar = document.createElement("div");
  bar.className = "feedback-bar";
  const configured = context ? context.endpoint_configured : true;
  bar.append(busy === "send" ? buildProgress() : note(configured
    ? t("仅在点击「发送」后上传。附带的日志和文件仅供排查问题，问题处理完毕后即删除，最长保存 90 天。")
    : t("此版本尚未开通在线发送，可先保存为 ZIP。")), actions);
  wrap.append(bar);
  syncActionState();
  return wrap;
}

function buildProgress(): HTMLElement {
  progressView = createSendProgress();
  progressView.update(sendProgress);
  return progressView.root;
}

async function showPreview(): Promise<void> {
  busy = "preview";
  render();
  try {
    const client = await getClient();
    const preview = await client.request<PreviewPayload>("/api/feedback/preview", {
      method: "POST",
      body: JSON.stringify(requestBody()),
    });
    openPreviewModal(preview);
  } catch (error) {
    showToast({ message: tx(errorText(error)), error: true });
  } finally {
    busy = "";
    render();
  }
}

function openPreviewModal(preview: PreviewPayload): void {
  const body: HTMLElement[] = [];
  const list = document.createElement("ul");
  list.className = "feedback-preview-list";
  const item = (text: string): void => {
    const li = document.createElement("li");
    li.textContent = text;
    list.append(li);
  };
  item(draft.contact.trim() ? t("反馈内容与邮箱") : t("反馈内容"));
  if (preview.system) item(t("系统与版本信息"));
  for (const task of preview.tasks) item(t("任务日志：{0} · {1}", SURFACE_LABEL[task.surface] ?? task.surface, taskStateWord(task.state)));
  for (const image of preview.images ?? []) item(t("截图：{0}（{1}）", image.name, formatBytes(image.bytes)));
  for (const file of preview.files) item(t("文件：{0}（{1}）", file.name, formatBytes(file.bytes)));
  body.push(list);

  if (preview.system) {
    body.push(previewBlock(t("系统与版本信息"), JSON.stringify(preview.system, null, 2)));
  }
  for (const task of preview.tasks) {
    body.push(previewBlock(
      t("任务日志：{0} · {1}", SURFACE_LABEL[task.surface] ?? task.surface, taskStateWord(task.state)),
      task.log || t("（此任务没有日志）"),
    ));
  }
  const size = document.createElement("p");
  size.className = "feedback-note";
  size.textContent = t("打包后约 {0}。", formatBytes(preview.package_bytes));
  body.push(size);

  openModal({
    tone: "tint",
    icon: "message",
    title: t("将发送的内容"),
    body,
    wide: true,
    actions: [{ label: t("关闭"), variant: "primary" }],
  });
}

function previewBlock(title: string, text: string): HTMLElement {
  const details = document.createElement("details");
  details.className = "feedback-preview-block";
  const summary = document.createElement("summary");
  summary.textContent = title;
  const pre = document.createElement("pre");
  pre.textContent = text;
  details.append(summary, pre);
  return details;
}

async function saveZip(): Promise<void> {
  busy = "save";
  render();
  try {
    const client = await getClient();
    const saved = await client.saveBinaryDownload("/api/feedback/package", "translator-feedback.zip", {
      method: "POST",
      body: JSON.stringify(requestBody()),
    });
    if (saved) showToast({ message: t("反馈包已保存到：{0}", saved) });
  } catch (error) {
    showToast({ message: tx(errorText(error)), error: true });
  } finally {
    busy = "";
    render();
  }
}

async function send(): Promise<void> {
  // 没留邮箱时问一次（只到用户第一次不留邮箱发出为止）。
  if (!draft.contact.trim() && context?.ask_email) {
    if ((await confirmSendWithoutEmail()) === "fill") {
      rootEl?.querySelector<HTMLInputElement>("input.feedback-input[type=email]")?.focus();
      return;
    }
  }
  busy = "send";
  sendFailure = "";
  sendProgress = null;
  render();
  try {
    const client = await getClient();
    const status = await uploadFeedback(client, requestBody(), (progress) => {
      sendProgress = progress;
      progressView?.update(progress);
    });
    if (status.state === "error") {
      if (SEND_FAILURES.includes(status.reason)) sendFailure = status.message;
      else showToast({ message: tx(status.message), error: true });
    } else {
      sentId = status.id;
      sentEmail = draft.contact.trim();
      // 后端发送成功后已把这个邮箱记在本机（留空就忘掉，也不再问），下一条直接带上。
      rememberedEmail = sentEmail;
      if (context) context.ask_email = false;
      draft = emptyDraft();
    }
  } catch (error) {
    const reason = errorReason(error);
    // 输入问题（内容为空、附件过大、任务记录已删）只提示，不出「未发出」横幅。
    if (SEND_FAILURES.includes(reason) || !reason) {
      sendFailure = error instanceof ApiError ? error.rawMessage : errorText(error);
    } else {
      showToast({ message: tx(errorText(error)), error: true });
    }
  } finally {
    busy = "";
    sendProgress = null;
    progressView = null;
    render();
  }
}

function buildSentCard(): HTMLElement {
  const card = createCard([], "feedback-card feedback-sent");
  const iconBox = document.createElement("div");
  iconBox.className = "feedback-sent-icon";
  iconBox.append(icon("check", { size: "lg" }));
  const title = document.createElement("h2");
  title.textContent = t("反馈已发送");
  const ref = document.createElement("p");
  ref.textContent = t("反馈编号：{0}", sentId);
  const thanks = document.createElement("p");
  thanks.textContent = sentEmail
    ? t("感谢您的反馈。问题解决或功能上线时，我们会发邮件到 {0}。", sentEmail)
    : t("感谢您的反馈。");
  const actions = document.createElement("div");
  actions.className = "feedback-actions";
  actions.append(
    createButton({ label: t("再写一条"), onClick: () => { sentId = ""; render(); } }),
    createButton({ label: t("返回任务中心"), onClick: () => navigate("tasks") }),
  );
  card.append(iconBox, title, ref, thanks, actions);
  return card;
}
