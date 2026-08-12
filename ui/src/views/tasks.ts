// 任务中心视图 —— 列表 + 任务详情，唯一全量任务视图（对应样张屏⑥）。
// 从 main.ts 移植：isTaskActive / taskStateMeta / taskSnapshotRows / taskResultReferences /
// upsertTask / watchTask / handleTaskEvent 等任务生命周期语义（按原样语义移植，非逐行照抄）。
//
// 架构要点：本模块在 app.ts 启动时被 import 一次（顶层代码立即执行），因此下面的
// “后台巡检”（ensureBackgroundLoop）在应用启动的第一时间就开始运行，与是否正挂载在
// 屏幕上无关——这样活动任务徽标（shell.setTaskBadge/setTaskPill）才能在用户停留在
// 其它视图时依然保持准确，满足“任务中心数据是徽标权威来源”的要求。凡是会触碰当前
// 视图 DOM 的渲染函数都额外用 `mounted` 门控，避免后台事件在别的视图挂载期间乱写 DOM。

import type { ViewParams } from "../router";
import { navigate } from "../router";
import { setTopbar, setTaskBadge, setTaskPill } from "../shell";
import {
  createChip,
  createButton,
  createEmptyState,
  openModal,
  showToast,
  type ChipTone,
} from "../components";
import { icon, type IconName } from "../icons";
import { ApiClient, type SseEvent, type TaskStatus } from "../api-client";
import { invoke } from "@tauri-apps/api/core";

// ---------------------------------------------------------------------------
// 基础类型 / 工具函数（与 main.ts 同名函数语义一致，独立实现以保持视图自包含）
// ---------------------------------------------------------------------------

type JsonObject = Record<string, unknown>;
type TaskSurface = TaskStatus["surface"];
type StreamState = "idle" | "connected" | "reconnecting" | "interrupted";

const TERMINAL_STATES = ["done", "completed_with_issues", "error", "stopped", "interrupted"];

function record(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" ? value : fallback;
}

function firstText(payload: JsonObject, keys: string[]): string {
  for (const key of keys) {
    const value = text(payload[key]);
    if (value) return value;
  }
  return "";
}

function firstNumber(payload: JsonObject, keys: string[]): number | null {
  for (const key of keys) {
    if (typeof payload[key] === "number") return payload[key] as number;
  }
  return null;
}

function resultEntries(result: JsonObject, keys: string[]): JsonObject[] {
  for (const key of keys) {
    const value = result[key];
    if (Array.isArray(value) && value.length) return value.map(record);
  }
  return [];
}

/** 脱敏：授权头 / sk-/rk-/pk-/api- 前缀密钥 / Bearer token 一律替换。 */
function redactedText(value: unknown, fallback = ""): string {
  const raw = text(value, fallback);
  if (!raw) return raw;
  return raw
    .replace(/(authorization\s*[:=]\s*)([^\s,;]+)/gi, "$1[redacted]")
    .replace(/\b(sk|rk|pk|api)[-_][a-z0-9_-]{8,}\b/gi, "[redacted]")
    .replace(/\bBearer\s+[^\s,;]+/gi, "Bearer [redacted]");
}

/** 内部位置串 → 用户照着能找到的位置。后端大多已经另给了 location_label，这里兜住两种
 *  情况：9.2.5 之前存下的任务记录里只有 location（body.paragraph[4]，用户没法数出是第几段），
 *  以及将来新增的位置形状。认不出来就原样返回——猜出来的位置比原值更害人。 */
function humanizeLocation(value: string): string {
  const raw = value.trim();
  if (!raw) return "";
  const paragraph = /^body\.paragraph\[(\d+)\]$/.exec(raw);
  if (paragraph) return `正文段落 ${Number(paragraph[1]) + 1}`;
  const cell = /^table\[(\d+)\]\.cell\[(\d+)\]$/.exec(raw);
  if (cell) return `表格 ${Number(cell[1]) + 1} / 单元格 ${Number(cell[2]) + 1}`;
  if (raw === "output.coverage") return "输出文档整体";
  if (raw === "document") return "整篇文档";
  return raw;
}

/** 上游故障原文 → 一句中文。源头已经在 core/user_facing_errors.py 收口了，但任务中心要显示
 *  历史记录：9.2.5 之前跑的任务里，问题列和错误列还躺着整段 503 英文原文加 MDN 链接。
 *  判定条件故意收得很紧（必须出现 URL、JSON 信封或 Server error 字样才动手），
 *  免得把后端本来就写好的中文句子换成更含糊的说法。 */
const HTTP_FAILURE_SENTENCES: Array<[RegExp, string]> = [
  [/\b(503|502|504)\b|service unavailable|bad gateway|gateway ?time-?out/i, "接口所在的服务暂时不可用，请稍后重试，或在设置里换一条连接。"],
  [/\b429\b|rate ?limit|too many requests/i, "接口这一刻拒绝了新请求（限流），自动重试后仍未成功。"],
  [/\b40[13]\b|unauthorized|invalid api key|permission denied/i, "接口拒绝了这条连接的密钥，请在设置里检查密钥与权限。"],
  [/timed? ?out|timeout/i, "接口超时没有返回结果。"],
];

function plainFailureText(value: string): string {
  const raw = value.replace(/\s+/g, " ").trim();
  if (!raw) return "";
  if (!/https?:\/\//i.test(raw) && !/\{\s*"/.test(raw) && !/server error/i.test(raw)) return raw;
  const matched = HTTP_FAILURE_SENTENCES.find(([pattern]) => pattern.test(raw));
  const sentence = matched ? matched[1] : "接口调用失败，完整原文写在运行日志与诊断包里。";
  // 后端很多句子是「我们自己的中文前缀 + 原始英文尾巴」，前缀里有用户真正需要的信息
  // （哪一步失败、保留了什么），不能连它一起丢：只换掉从第一段英文/JSON 开始的部分。
  const head = raw
    .split(/(?=server error|https?:\/\/|\{\s*")/i)[0]
    .trim()
    .replace(/[：:，,;；、]$/, "");
  return head && /[一-龥]/.test(head) ? `${head}：${sentence}` : sentence;
}

/** 「[path]」是任务中心记录的脱敏占位符（core/task_logger.py 的 redact_absolute_paths），
 *  不是路径。原样渲染出去，用户看到的就是一个假路径。 */
function realPath(value: string): string {
  const raw = value.trim();
  return !raw || raw === "[path]" ? "" : raw;
}

function fileNameOf(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

function isTaskActive(task: TaskStatus): boolean {
  return !task.terminal && !TERMINAL_STATES.includes(task.state);
}

/**
 * 逐文件产出统计。任务级状态说的是「流程走完了」，不是「拿到了文件」：上游全程 503 时
 * PDF 任务同样收在 completed_with_issues，一个文件都没生成。列表徽章、副标题、详情徽章
 * 都必须按这个数说话，否则界面会把「什么都没拿到」显示成「需复核」。
 */
function producedCounts(task: TaskStatus): { produced: number; failed: number; total: number } {
  const entries = resultEntries(record(task.result), ["files", "file_results", "file_records"]);
  let produced = 0;
  let failed = 0;
  for (const item of entries) {
    const status = firstText(item, ["status", "state", "terminal_state"]);
    const ok = status
      ? status === "succeeded" || status === "needs_review" || status === "completed"
      : item.success === true || Boolean(realPath(firstText(item, ["output_path", "output", "result_path"])));
    if (ok) produced += 1;
    else failed += 1;
  }
  return { produced, failed, total: entries.length };
}

function surfaceIcon(surface: TaskSurface): IconName {
  if (surface === "excel") return "excel";
  if (surface === "word") return "word";
  if (surface === "pdf") return "pdf";
  return "book"; // cleaner / tm_clean：图标集里没有专属图标，用记忆库图标近似。
}

function taskSurfaceLabel(surface: TaskSurface): string {
  if (surface === "excel") return "Excel";
  if (surface === "word") return "Word";
  if (surface === "pdf") return "PDF";
  return "TM 清洗";
}

const STATE_LABELS: Record<string, string> = {
  preflight: "等待确认",
  running: "执行中",
  pausing: "暂停提交中",
  paused: "已暂停提交",
  stopping: "安全停止中",
  finalizing: "正在收尾",
  done: "已完成",
  completed_with_issues: "完成但有问题",
  error: "发生错误",
  stopped: "已中止",
  interrupted: "应用中断",
};

const TONE_MAP: Record<string, ChipTone> = {
  preflight: "tint",
  running: "tint",
  pausing: "tint",
  paused: "warn",
  stopping: "tint",
  finalizing: "tint",
  done: "ok",
  completed_with_issues: "warn",
  error: "dgr",
  stopped: "mute",
  interrupted: "dgr",
};

function taskStateMeta(task: TaskStatus, streamState?: StreamState): { label: string; tone: ChipTone } {
  if (streamState === "reconnecting" && isTaskActive(task)) {
    return { label: "正在补拉事件", tone: "tint" };
  }
  if (streamState === "interrupted") {
    return { label: "应用中断", tone: "dgr" };
  }
  return { label: STATE_LABELS[task.state] ?? task.state, tone: TONE_MAP[task.state] ?? "mute" };
}

function formatClock(epoch?: number): string {
  if (!epoch) return "--:--";
  return new Date(epoch * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
}

function formatDay(epoch?: number): string {
  if (!epoch) return "";
  const date = new Date(epoch * 1000);
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return formatClock(epoch);
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return `昨天 ${formatClock(epoch)}`;
  return `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${formatClock(epoch)}`;
}

// ---------------------------------------------------------------------------
// 任务快照 / KPI / 结果定位清单 / 产物引用 —— 移植自 main.ts 的同名函数
// ---------------------------------------------------------------------------

function taskResultReferences(task: TaskStatus): Array<{ label: string; path: string; reveal: boolean }> {
  const result = record(task.result);
  const summary = record(result.summary);
  const source = { ...summary, ...result };
  const refs: Array<{ label: string; path: string; reveal: boolean }> = [];
  const actionLabels: Record<string, string> = {
    open_output: "打开输出目录",
    reveal_output: "在 Finder 中显示主输出",
    open_report: "打开报告",
    open_manifest: "打开清单",
    copy_output_path: "复制输出路径",
  };
  for (const operation of resultEntries(result, ["local_operations"])) {
    const action = text(operation.action);
    const path = firstText(operation, ["path"]);
    if (path && path !== "[path]" && !refs.some((entry) => entry.path === path)) {
      refs.push({ label: actionLabels[action] ?? "打开本地结果", path, reveal: action === "reveal_output" });
    }
  }
  const add = (label: string, keys: string[], reveal = false) => {
    const path = firstText(source, keys);
    if (path && !refs.some((entry) => entry.path === path)) refs.push({ label, path, reveal });
  };
  add("打开输出目录", ["output_dir", "output_directory"]);
  add("在 Finder 中显示主输出", ["output_path", "result_path", "output"], true);
  add("打开报告", ["report_path", "word_translation_report_path", "pdf_translation_report_path"]);
  add("打开清单", ["manifest_path", "pdf_translation_manifest_path"]);
  return refs;
}

function taskSnapshotRows(task: TaskStatus): Array<[string, string]> {
  const snapshot = record(task.task_snapshot);
  const modelSnapshot = record(task.model_snapshot);
  const language = record(snapshot.language);
  const output = record(snapshot.output);
  const resources = [
    ...(Array.isArray(task.resource_groups) ? task.resource_groups : []),
    ...(Array.isArray(snapshot.connections) ? snapshot.connections : []),
  ];
  // 角色名用后端已经给好的中文 label（model_snapshot[role].label，例如「PDF 翻译模型」）。
  // 之前直接印 role 键，用户读到的是 image / pdf_review / translation 这些内部名字。
  const models = Object.entries(modelSnapshot)
    .map(([role, value]) => {
      const model = record(value);
      const name = redactedText(model.label, ROLE_LABELS[role] ?? role);
      return `${name} ${redactedText(model.model, "已冻结")}`;
    })
    .join(" · ");
  const sourceLang = firstText({ ...snapshot, ...language }, ["source_lang", "source_selection"]);
  const targetLang = firstText({ ...snapshot, ...language }, ["target_lang"]);
  const domain = firstText(snapshot, ["domain_preset", "domain"]);
  const promptVersion = firstText(snapshot, ["prompt_version", "domain_prompt_version"]);
  const configuredOutput = record(snapshot.excel_output || snapshot.word_output || snapshot.pdf_output);
  const outputPath =
    firstText({ ...snapshot, ...output, ...configuredOutput }, ["output_dir", "output_directory", "custom_output_dir"]) ||
    (configuredOutput.use_custom_output_dir === false ? "与源文件相邻的唯一输出目录" : "任务唯一输出目录");
  const throughput = record(snapshot.throughput);
  // 一份任务里几个模型角色常常走同一条连接（同一个 provider、同一个 base_url），
  // 而 resource_groups 与 task_snapshot.connections 又是同一批连接的两次成像。
  // 逐条印出来就是「custom_openai @ https://… ；custom_openai @ https://…」，
  // 同一个地址在同一行里出现两遍，用户会以为配了两条连接。这里按地址去重，
  // 并且只留主机名——完整 base_url 对用户没有增量信息，还会把这一行挤爆。
  const connectionNames: string[] = [];
  let pooledTotal = 0;
  for (const group of resources) {
    const value = record(group);
    const summary = record(value.summary);
    const label = redactedText(
      value.pool_connection_label ||
        value.label ||
        value.connection_summary ||
        summary.base_url ||
        value.base_url ||
        summary.provider ||
        value.provider,
    );
    const host = hostOf(label);
    if (host && !connectionNames.includes(host)) connectionNames.push(host);
    pooledTotal = Math.max(pooledTotal, num(value.pool_connection_count));
  }
  const connectionSummary = connectionNames.length
    ? connectionNames.join("、") + (pooledTotal > 1 ? `（连接池共 ${pooledTotal} 个地址，会自动轮换）` : "")
    : "";
  const rows: Array<[string, string]> = [
    ["语言", [sourceLang, targetLang].filter(Boolean).join(" → ")],
    ["模型", models],
    ["连接", connectionSummary],
    ["运行中换过连接", connectionSwitchText(task)],
    ["领域 / Prompt", [domain, promptVersion].filter(Boolean).join(" · ")],
    ["输出位置", outputPath],
    [
      "吞吐",
      Object.keys(throughput).length
        ? throughputPhrase("", throughput)
        : Object.entries(modelSnapshot)
            .map(([role, value]) => {
              const model = record(value);
              return throughputPhrase(redactedText(model.label, ROLE_LABELS[role] ?? role), record(model.throughput));
            })
            .filter(Boolean)
            .join(" · "),
    ],
  ];
  return rows.filter(([, value]) => Boolean(value));
}

/** 上一条连接顶不住时任务会自动换到备用连接，之后的译文出自另一家服务商。
 *  这件事以前只在运行日志里闪一行 WARN，日志一滚就没了，记录里查不到——用户拿两次任务
 *  比质量，看到的「连接」都是开跑时冻结的那一条。没换过连接就整行不出现。 */
function connectionSwitchText(task: TaskStatus): string {
  const connections = record(record(task.result).connections);
  const switches = Array.isArray(connections.switches) ? connections.switches.map(record) : [];
  if (!switches.length) return "";
  const steps = switches.map((item) => {
    const from = firstText(item, ["from_label"]);
    const to = firstText(item, ["to_label"]);
    const reason = firstText(item, ["reason"]);
    const at = firstText(item, ["at"]);
    return `${at ? `${at} ` : ""}${from} → ${to}${reason ? `（${reason}）` : ""}`;
  });
  const finalLabel = firstText(connections, ["final_label"]);
  return [`换过 ${switches.length} 次`, steps.join("；"), finalLabel ? `此后由「${finalLabel}」完成` : ""]
    .filter(Boolean)
    .join(" · ");
}

/** model_snapshot 里没给 label 的角色（老记录）才用得到的兜底名字。 */
const ROLE_LABELS: Record<string, string> = {
  translation: "翻译模型",
  image: "图像翻译模型",
  pdf_review: "图像审核模型",
  review: "审核模型",
  cleaner: "记忆库清洗模型",
};

function hostOf(value: string): string {
  const raw = value.trim();
  if (!raw) return "";
  const match = /^[a-z][a-z0-9+.-]*:\/\/([^/?#]+)/i.exec(raw);
  return match ? match[1] : raw;
}

/** 吞吐一行只说两件用户能对上设置项的事：同时几路、每批多少段。
 *  profile_key 这类内部标识不进界面。 */
function throughputPhrase(name: string, throughput: JsonObject): string {
  const parts: string[] = [];
  const concurrency = firstNumber(throughput, ["concurrency"]);
  const batch = firstNumber(throughput, ["batch_size", "max_paragraphs_per_batch"]);
  if (concurrency !== null) parts.push(`同时 ${concurrency} 路`);
  if (batch !== null) parts.push(`每批 ${batch} 段`);
  if (!parts.length) return "";
  return [name, parts.join(" / ")].filter(Boolean).join(" ");
}

function pickKpis(pairs: Array<[string, string[]]>, source: JsonObject): Array<[string, number]> {
  const out: Array<[string, number]> = [];
  for (const [label, keys] of pairs) {
    const value = firstNumber(source, keys);
    if (value !== null) out.push([label, value]);
  }
  return out;
}

/** 富 KPI 集合：按 surface 采用与 main.ts renderXxxResultDetails 相同的字段回退表。
 *
 *  文件级那两格叫「已生成 / 未生成」，不叫「成功 / 失败」：CONTEXT.md 的词表里
 *  「成功」是任务级标签，只有内容全部通过时才能说；一份写出来了但有需复核内容的文件，
 *  格子写「成功 1」而徽章同屏写「需复核 5」，等于同一件事同时说成两种结论。 */
function richKpiRows(task: TaskStatus): Array<[string, number]> {
  // 任务收尾之后「未开始 0」是一句废话，还会让人以为有东西卡住没跑；只有真的剩了文件
  // 没开始（中止、出错）才值得占一格。
  return surfaceKpiRows(task).filter(
    ([label, value]) => !(label === "未开始" && task.terminal && value === 0),
  );
}

function surfaceKpiRows(task: TaskStatus): Array<[string, number]> {
  const result = record(task.result);
  const summary = record(result.summary);
  const kpi = record(result.kpi);
  const source = { ...result, ...summary, ...kpi };
  if (task.surface === "excel") {
    // 和 Word 同一套口径：「需复核」这一格必须等于同屏结果定位清单里待办的行数，
    // 也等于列表卡片上的徽章。9.2.6 的 Excel 详情干脆没有这一格——同屏清单列着 1 行
    // 需复核，KPI 里一个字都没提。
    const pending = reviewCount(task);
    const reviewSource = pending === null ? source : { ...source, review_count: pending };
    return pickKpis(
      [
        ["已选", ["selected_count", "selected_files", "selected_file_count", "total_files"]],
        ["已生成", ["success_count", "completed_count", "successful_files", "succeeded_file_count"]],
        ["未生成", ["failed_count", "error_count", "failed_files", "failed_file_count"]],
        ["未开始", ["unstarted_count", "not_started_count", "unstarted_file_count"]],
        ["需复核", ["review_count", "review_items_count", "review_total", "review_text_count"]],
        ["TM 命中", ["tm_hit_count", "tm_hits"]],
        ["送模型文本", ["model_translation_text_count", "model_text_count", "translated_text_count"]],
      ],
      reviewSource,
    );
  }
  if (task.surface === "word") {
    const recovery = record(result.recovery);
    const withRecovery = { ...source, ...recovery };
    // 「需复核」这一格和列表徽章必须是同一个数。后端的 review_text_count 数的是全部
    // review_items，里面混着已经自动恢复好、用户不用管的条目（severity=resolved）——
    // 照抄就会在这一格报出比实际待办多得多的数字。reviewCount() 只数 needs_review 桶。
    const pending = reviewCount(task);
    const reviewSource = pending === null ? source : { ...source, review_count: pending };
    return pickKpis(
      [
        ["已选", ["selected_count", "selected_files", "selected_file_count", "total_files"]],
        ["已生成", ["success_count", "completed_count", "successful_files", "succeeded_file_count"]],
        ["未生成", ["failed_count", "error_count", "failed_files", "failed_file_count"]],
        ["需复核", ["review_count", "review_items_count", "review_total", "review_text_count"]],
        ["TM 命中", ["tm_hit_count", "tm_hits"]],
      ],
      reviewSource,
    ).concat(
      pickKpis(
        [
          ["严格恢复", ["retry_recovered_count", "recovered_count"]],
          ["仲裁接受", ["semantic_accepted_count"]],
        ],
        withRecovery,
      ),
    );
  }
  if (task.surface === "pdf") {
    return pickKpis(
      [
        ["文件", ["file_count", "selected_file_count"]],
        ["页 / 图片", ["total_page_count", "page_count"]],
        // 只有中止/失败的任务才带这两个键（见后端 _done_kpi）：跑完的任务里「已生成页」
        // 就是总页数，没必要再占一格；中止的任务没有它就只剩一排零。
        ["已生成页", ["generated_page_count"]],
        ["未开始页", ["unstarted_page_count"]],
        ["高清 PDF", ["generated_pdf_count"]],
        ["译图", ["generated_image_count"]],
        ["失败占位", ["placeholder_page_count"]],
        // 跟「失败占位」并列的必须是与它互斥的那个数。「审核未通过」里有一半是直接退回
        // 失败占位页的同一批页，两格并列等于把一页坏页数成两页（后端 _done_kpi 同注）。
        ["有疑点仍采用", ["suspect_adopted_page_count"]],
        ["跳过 A3+", ["skipped_oversize_page_count"]],
      ],
      source,
    );
  }
  return pickKpis(
    [
      ["已选", ["selected_count", "selected_file_count", "file_count", "total_files"]],
      ["已生成", ["success_count", "successful_files", "succeeded_file_count", "completed_count"]],
      ["未生成", ["failed_count", "failed_file_count", "error_count"]],
      ["未开始", ["unstarted_count", "unstarted_file_count", "not_started_count"]],
    ],
    source,
  );
}

const WARN_KPI_LABELS = new Set(["需复核", "未生成", "失败占位", "有疑点仍采用"]);

interface ReviewRow {
  file: string;
  location: string;
  excerpt: string;
  issue: string;
  /** 给用户看的整句处理说明（Excel 已换成中文，Word 就是后端原句）。 */
  action: string;
  /** Excel 复核项的英文枚举原值；Word 没有这个概念，留空。 */
  actionCode: string;
  /** 后端每条复核项自带的严重度：resolved / needs_review。空字符串表示这一路没给。 */
  severity: string;
  needsReview: boolean;
}

/** Excel 复核项的 category（core/config.py 的 REVIEW_MARK_*）。后端存的是英文枚举，
 *  直接摆到界面上等于让用户去猜 unresolved 和 foreign_noise 差在哪；这里只做展示层
 *  翻译，不动后端契约。取不到映射就原样显示，将来后端加了新枚举也不会显示成空白。 */
const EXCEL_REVIEW_CATEGORY_LABELS: Record<string, string> = {
  unresolved: "混合语言未能确认",
  semantic: "经语义校验后接受",
  foreign_noise: "原文疑似夹杂错误外文",
};

/** Excel 复核项的 action（core/xlsx_patcher.py `_record_review_position`）。
 *  这三个值说的是「复核标记有没有真的写进 xlsx」，全都不是「这条问题已经解决」——
 *  尤其 preserved_existing_fill 是「单元格本来就有底色，标记没写进去」，
 *  之前一律显示灰色「已处理」，等于告诉用户文件里能找到标记，实际上找不到。 */
const EXCEL_REVIEW_ACTIONS: Record<string, { label: string; note: string; tone: ChipTone }> = {
  marked_fill: { label: "已标底色", note: "已在该单元格填入复核底色，请在文件中按底色复核。", tone: "warn" },
  marked_red_font: { label: "已标红字", note: "单元格原有底色已保留，改用红色字体标出，请按红字复核。", tone: "warn" },
  preserved_existing_fill: {
    label: "未写入标记",
    note: "单元格已有底色，按现有底色保留策略跳过了标记；文件里看不到这处复核提示，只能按本行定位。",
    tone: "dgr",
  },
};

/** 结果定位清单：合并 Excel（工作表/单元格）与 Word（章节/位置/摘录）两种复核行形状。
 *  main.ts 里 Excel 复核行本没有摘录列，这里仍尝试常见摘录键，取不到就留空——
 *  这是为了让同一张表能承接两种 surface 的数据，而不改变各自后端已产出的字段。 */
function reviewRows(task: TaskStatus): ReviewRow[] {
  const result = record(task.result);
  const reviewPayload = record(result.review);
  // 同一批复核项在结果里躺着两份：顶层 `issues`（报告和诊断包用的原始形状，位置是
  // body.paragraph[4]、处理说明在 `status` 键上）和 `review.items`（给界面用的中文形状）。
  // 两份都读、又不去重，一次任务的 5 处问题就在表里排成 10 行，其中 5 行位置读不懂、
  // 「处理」列还全是「—」。所以两份都收，但按「文件 + 位置 + 摘录 + 严重度」合并。
  const raw = resultEntries(result, ["review_items", "review_locations", "review_details", "issues"]).concat(
    resultEntries(reviewPayload, ["items", "locations", "details", "issues"]),
  );
  const merged = new Map<string, ReviewRow>();
  for (const entry of raw) {
    const row = toReviewRow(entry);
    // 位置相同、判定不同的两行（「重试后仍未获得有效译文」+「输出文档仍存在未译源文」
    // 说的是同一段的同一件事）也在这里并成一行：按判定条数报，界面会把 3 处问题说成
    // 5 处，用户拿着 5 去文档里找第 4、第 5 处，永远找不到。CONTEXT.md 的「位置计数」
    // 要求按文档位置计数——但每一句判定和处理说明都保留，一句都不丢。
    // 分隔符写成转义，不要在源码里放一个真的空字节：那会让 grep 之类的工具把整个文件
    // 判成二进制，从此在这个文件里搜任何东西都搜不到（9.2.6 就是这样）。
    const key = [row.file, row.location, row.excerpt, row.severity, row.actionCode].join("\u0000");
    const existing = merged.get(key);
    if (!existing) {
      merged.set(key, row);
      continue;
    }
    existing.issue = mergeSentences(existing.issue, row.issue);
    existing.action = mergeSentences(existing.action, row.action);
    existing.needsReview = existing.needsReview || row.needsReview;
  }
  return [...merged.values()].sort((a, b) => Number(b.needsReview) - Number(a.needsReview));
}

/** 合并同一位置的两句判定；重复、空值和占位「—」都不参与。 */
function mergeSentences(current: string, addition: string): string {
  const next = addition.trim();
  if (!next || next === "—") return current;
  if (!current || current === "—") return next;
  if (current.includes(next)) return current;
  return `${current}；${next}`;
}

function toReviewRow(entry: JsonObject): ReviewRow {
  const file = firstText(entry, ["file", "source_relative_path", "relative_path", "path"]);
  // location_label 是后端专门为界面准备的中文位置（「正文段落 5」「第 1 页」），必须排在
  // 内部 location 之前——PDF 那一路只给 location_label，之前整列显示「—」。
  const location = [
    firstText(entry, ["worksheet", "sheet", "sheet_name", "section", "chapter", "section_path", "heading"]),
    humanizeLocation(
      firstText(entry, ["cell", "cell_reference", "location_label", "location", "paragraph", "paragraph_index", "table_cell"]),
    ),
  ]
    .filter(Boolean)
    .join(" · ");
  const excerptRaw = firstText(entry, ["excerpt", "snippet", "source_excerpt", "text", "source_text"]);
  const excerpt = excerptRaw.length > 80 ? `${excerptRaw.slice(0, 77)}…` : excerptRaw;
  const issueRaw = firstText(entry, ["category", "mark", "type", "issue", "problem"]);
  // 这张表定位的是内容问题，但 PDF 那一路会把整页失败的原因塞进 problem，历史记录里
  // 就是整段 503 英文原文加 MDN 链接。换成一句中文，原文留在运行日志和诊断包里。
  const issue = EXCEL_REVIEW_CATEGORY_LABELS[issueRaw] ?? plainFailureText(issueRaw);
  // `status` 是后端原始形状里的处理说明键。不认它，「处理」列就整列是「—」，
  // 而日志里明明写了处置结果（保留原文 / 已恢复译文）。
  const actionRaw = firstText(entry, ["action", "applied_action", "status", "review_status", "message"]);
  const excelAction = EXCEL_REVIEW_ACTIONS[actionRaw];
  const actionCode = excelAction ? actionRaw : "";
  const action = excelAction ? excelAction.note : plainFailureText(actionRaw);
  const severity = firstText(entry, ["severity"]);
  // 是不是「还没解决」优先信后端：Word 每条复核项都带 severity，正则读整句中文读不出
  // 否定（「未参与翻译」里没有任何一个词能被 /复核|未通过|待/ 命中）。
  // Excel 那一路没有 severity，但它写进 review_positions 的每一格本来就是复核标记，
  // 一律算需复核。两者都没有时才退回正则，只用来兜住将来新增的第三种数据形状。
  const needsReview = severity
    ? severity !== "resolved"
    : actionCode
      ? true
      : /复核|未通过|待/.test(action) || /复核|未通过|待/.test(issue);
  return { file, location, excerpt, issue: issue || "—", action: action || "—", actionCode, severity, needsReview };
}

/** 「处理」列的短结论标签 + 配色。
 *  配色只认后端的 severity，绝不让正则决定颜色：action 是一整句中文，正则不认否定，
 *  「…被未接受的修订包裹，未参与翻译」会同时命中「接受」和「参与」，之前就被染成绿色
 *  「已接受」——一条明确要人去看的问题，界面上写着「没事了」。正则现在只用来在同一个
 *  颜色档里挑一句更短的措辞，挑不出就退回中性说法。
 *  action 为空或「—」时返回 null，只显示一个「—」。 */
function reviewActionMeta(row: ReviewRow): { label: string; tone: ChipTone } | null {
  // Excel 那一路的 action 是固定枚举，标签和配色都已经在映射表里定死，不用猜。
  const excelAction = EXCEL_REVIEW_ACTIONS[row.actionCode];
  if (excelAction) return { label: excelAction.label, tone: excelAction.tone };

  const value = row.action.trim();
  if (!value || value === "—") return null;
  if (row.severity === "needs_review") {
    if (/保留原文|保持原文|保留原内容/.test(value)) return { label: "保留原文，待复核", tone: "warn" };
    return { label: "需复核", tone: "warn" };
  }
  if (row.severity === "resolved") {
    if (/保留原内容|保留原文/.test(value)) return { label: "已保留原文", tone: "ok" };
    if (/恢复/.test(value)) return { label: "已恢复译文", tone: "ok" };
    if (/接受|通过/.test(value)) return { label: "已接受", tone: "ok" };
    if (/写入|输出/.test(value)) return { label: "已写入译文", tone: "ok" };
    return { label: "已自动处理", tone: "ok" };
  }
  // severity 缺失：这条数据的来路不明，只报事实（失败是 action 里唯一能确定的词面），
  // 其余一律中性——宁可少说一句，也不能替后端下「已处理」的结论。
  if (/失败|拒绝/.test(value)) return { label: "未处理", tone: "dgr" };
  return { label: "已记录", tone: "mute" };
}

interface FileRow {
  name: string;
  status: string;
  tone: ChipTone;
  output: string;
  outputPath: string;
  /** 压缩版 PDF（只有 PDF 那一路会有），空串表示这次没生成。 */
  compressedOutput: string;
  compressedOutputPath: string;
  error: string;
}

/** 后端逐文件状态枚举 → 用户读得懂的中文。文件级只说「已生成 / 未生成」（CONTEXT.md
 *  词表：文件级不许说「成功」「失败」）。之前这一列直接印 succeeded / failed，
 *  用户在界面上读到的是后端的内部枚举名。 */
const FILE_STATUS_LABELS: Record<string, { label: string; tone: ChipTone }> = {
  succeeded: { label: "已生成", tone: "ok" },
  completed: { label: "已生成", tone: "ok" },
  needs_review: { label: "已生成 · 需复核", tone: "warn" },
  failed: { label: "未生成", tone: "dgr" },
  error: { label: "未生成", tone: "dgr" },
  unstarted: { label: "未开始", tone: "mute" },
  skipped: { label: "已跳过", tone: "mute" },
  stopped: { label: "已中止", tone: "mute" },
};

function fileRows(task: TaskStatus): FileRow[] {
  const result = record(task.result);
  const files = resultEntries(result, ["files", "file_results", "file_records"]);
  return files.map((entry) => {
    const statusCode = firstText(entry, ["status", "state", "terminal_state"]);
    const known = FILE_STATUS_LABELS[statusCode];
    const fallback: { label: string; tone: ChipTone } =
      entry.success === true
        ? { label: "已生成", tone: "ok" }
        : entry.success === false
          ? { label: "未生成", tone: "dgr" }
          : { label: statusCode || "结果未知", tone: "mute" };
    const meta = known ?? fallback;
    const outputPath = realPath(firstText(entry, ["output_path", "result_path", "output", "translated_image_path"]));
    const compressedOutputPath = realPath(
      firstText(entry, ["compressed_output", "compressed_output_path", "compressed_pdf_path"]),
    );
    return {
      name: firstText(entry, ["source_relative_path", "relative_path", "name"]),
      status: meta.label,
      tone: meta.tone,
      // 窄表里铺一条绝对路径会把其余三列挤没；文件名足够对上输出目录里的东西，
      // 完整路径挂在 title 上，悬停可见。
      output: outputPath ? fileNameOf(outputPath) : "",
      outputPath,
      compressedOutput: compressedOutputPath ? fileNameOf(compressedOutputPath) : "",
      compressedOutputPath,
      error: plainFailureText(firstText(entry, ["error", "error_message", "message", "detail"])),
    };
  });
}

// ---------------------------------------------------------------------------
// 模块状态（顶层单例——整个应用生命周期只有一份，视图挂载/卸载不清空）
// ---------------------------------------------------------------------------

interface TaskEntry {
  task: TaskStatus;
  phaseName: string;
  stepDone: number;
  stepTotal: number;
  lastEventId: number;
  streamState: StreamState;
  watcherActive: boolean;
}

const tasks = new Map<string, TaskEntry>();
let order: string[] = [];
let filter: "all" | "active" | "terminal" = "all";
let selectedId: string | null = null;

let mounted = false;
let listRootEl: HTMLDivElement | null = null;
let filtersEl: HTMLDivElement | null = null;
let detailRootEl: HTMLDivElement | null = null;
let fastPollTimer: number | null = null;

let connectPromise: Promise<ApiClient> | null = null;

async function getClient(): Promise<ApiClient> {
  if (!connectPromise) {
    const instance = new ApiClient();
    connectPromise = instance
      .connect()
      .then(() => instance)
      .catch((error) => {
        connectPromise = null;
        throw error;
      });
  }
  return connectPromise;
}

/**
 * 已删除任务的墓碑集合。DELETE 往返期间，4s 前台轮询和 12s 后台巡检可能已经发出
 * listTasks()，快照里还带着这条记录；等它回来时 upsert 又把记录塞回列表最上方，
 * 而 refreshRegistry 只做加法，从此这条记录再也不会消失。
 *
 * 另一条路——在 refreshRegistry 里以服务端快照为准、把不在快照里的 terminal 记录裁掉
 * ——不能用：GET /api/tasks 只回 active + recent 的有限窗口，滚出窗口的历史任务本来就
 * 不在快照里，按快照裁剪会把用户正在看的旧记录一起抹掉，比原来的问题更糟。
 *
 * 墓碑只存 id（字符串），进程内有界增长：一次会话里手动删的记录不会有几百条，
 * 不做淘汰反而更安全——淘汰早了迟到的快照又能把记录塞回来。
 */
const deletedTaskIds = new Set<string>();

function upsert(task: TaskStatus): TaskEntry | null {
  if (deletedTaskIds.has(task.task_id)) return null;
  const previous = tasks.get(task.task_id);
  const entry: TaskEntry = {
    task: { ...previous?.task, ...task },
    phaseName: previous?.phaseName ?? "正在准备任务",
    stepDone: previous?.stepDone ?? 0,
    stepTotal: previous?.stepTotal ?? 0,
    lastEventId: previous?.lastEventId ?? 0,
    streamState: previous?.streamState ?? "idle",
    watcherActive: previous?.watcherActive ?? false,
  };
  tasks.set(task.task_id, entry);
  order = [task.task_id, ...order.filter((id) => id !== task.task_id)];
  return entry;
}

function activeCount(): number {
  let count = 0;
  for (const id of order) {
    const entry = tasks.get(id);
    if (entry && isTaskActive(entry.task)) count += 1;
  }
  return count;
}

/** 徽标是全局状态（侧栏 + 顶栏右侧药丸），无论任务中心是否正挂载都要保持准确。 */
function updateBadge(): void {
  const count = activeCount();
  setTaskBadge(count);
  setTaskPill({ count });
}

function refreshTopbarStatus(): void {
  if (!mounted) return;
  const count = activeCount();
  setTopbar({
    title: "任务中心",
    status: count > 0 ? { label: `${count} 个活动任务`, tone: "run" } : { label: "空闲", tone: "idle" },
    subtitle: "运行中的任务与最近结果都在这里",
  });
}

function touch(taskId?: string): void {
  updateBadge();
  refreshTopbarStatus();
  if (!mounted) return;
  renderList();
  if (!taskId || selectedId === taskId) renderDetail();
}

// ---------------------------------------------------------------------------
// 事件流 / 轮询
// ---------------------------------------------------------------------------

async function watchTask(taskId: string): Promise<void> {
  const entry = tasks.get(taskId);
  if (!entry || entry.watcherActive || entry.task.terminal) return;
  entry.watcherActive = true;
  try {
    const client = await getClient();
    entry.lastEventId = await client.streamTask(taskId, (event) => handleEvent(taskId, event), {
      lastEventId: entry.lastEventId,
      onConnectionState: (state) => {
        const live = tasks.get(taskId);
        if (!live) return;
        live.streamState = state;
        touch(taskId);
      },
    });
    const latest = tasks.get(taskId);
    if (latest && !latest.task.terminal) {
      const refreshed = await client.getTask(taskId);
      const revived = upsert(refreshed);
      if (revived) revived.streamState = "connected";
    }
  } catch (error) {
    const latest = tasks.get(taskId);
    if (!latest) return;
    try {
      const client = await getClient();
      const refreshed = await client.getTask(taskId);
      const revived = upsert(refreshed);
      if (revived) revived.streamState = "connected";
      if (revived && !refreshed.terminal) {
        window.setTimeout(() => void watchTask(taskId), 0);
        return;
      }
    } catch {
      latest.task = { ...latest.task, state: "interrupted", terminal: true };
      latest.streamState = "interrupted";
      latest.phaseName = "sidecar 已重启或应用异常退出；本任务不能继续，请依据已生成产物或清单新建任务。";
      if (mounted) showToast({ message: "任务监控无法恢复，已标记为应用中断。", error: true });
    }
  } finally {
    const latest = tasks.get(taskId);
    if (latest) latest.watcherActive = false;
    touch(taskId);
  }
}

function handleEvent(taskId: string, event: SseEvent): void {
  const entry = tasks.get(taskId);
  if (!entry) return;
  entry.lastEventId = Math.max(entry.lastEventId, event.id);
  const data = event.data;
  if (event.type === "progress") {
    entry.phaseName = text(data.phase_name, "正在处理");
    entry.stepDone = num(data.step_done);
    entry.stepTotal = num(data.step_total);
  }
  if (event.type === "status") {
    entry.phaseName = text(data.phase_desc, entry.phaseName);
  }
  if (event.type === "stopping") entry.task = { ...entry.task, state: "stopping" };
  if (event.type === "paused") {
    entry.task = { ...entry.task, state: "paused" };
    entry.phaseName = "已暂停提交新页面";
  }
  if (event.type === "resumed") {
    entry.task = { ...entry.task, state: "running" };
    entry.phaseName = "正在继续提交页面";
  }
  if (TERMINAL_STATES.includes(event.type)) {
    entry.task = { ...entry.task, state: event.type as TaskStatus["state"], terminal: true, result: data };
    if (mounted) {
      const label = taskSurfaceLabel(entry.task.surface);
      showToast({
        message: event.type === "done" || event.type === "completed_with_issues" ? `${label} 任务已完成。` : text(data.message, "任务未完成。"),
        error: event.type === "error" || event.type === "stopped" || event.type === "interrupted",
      });
    }
  }
  touch(taskId);
}

async function refreshRegistry(): Promise<void> {
  try {
    const client = await getClient();
    const payload = await client.listTasks();
    const combined = [...(Array.isArray(payload.active) ? payload.active : []), ...(Array.isArray(payload.recent) ? payload.recent : [])];
    for (const task of combined) {
      // upsert 返回 null 表示这条记录已经被用户删掉（墓碑命中），这次快照只是在途的旧成像，
      // 不能让它复活，更不能给它挂事件流监听。
      if (!upsert(task)) continue;
      if (isTaskActive(task)) void watchTask(task.task_id);
    }
    // 只能选当前筛选下看得见的任务。用 order[0]（全量顺序）的话，「最近结果」筛选下删掉
    // 最后一条记录、列表进入空态之后，12 秒后的这次巡检会把选中项挪到一个运行中的任务上：
    // 左边列表空空如也，右边详情却显示着另一条任务的内容。
    if (!selectedId) selectedId = visibleOrder()[0] ?? null;
    touch();
  } catch {
    // sidecar 尚未就绪或临时不可达；下一次巡检自然会重试，这里不打扰用户。
  }
}

/** 工作台刚 POST 出一个任务时调用。
 *  在这之前，新任务要等下一次巡检才进得了登记册：前台轮询 4 秒、只在任务中心挂载时跑，
 *  后台巡检 12 秒。用户在工作台点了「开始」立刻切到任务中心，看到的是一份还没有这条任务
 *  的列表，侧栏徽标也还是旧数字——像是没提交成功。这里直接把返回的任务塞进登记册并挂上
 *  事件流，徽标和列表同一帧就对。 */
export function noteTaskStarted(task: TaskStatus): void {
  if (!upsert(task)) return;
  if (isTaskActive(task)) void watchTask(task.task_id);
  touch(task.task_id);
}

let backgroundStarted = false;

/** 后台巡检：应用启动时立即开始（模块被 app.ts 顶层 import 时触发），
 *  与任务中心视图是否挂载无关——这样徽标在其它视图上也能保持准确。 */
function ensureBackgroundLoop(): void {
  if (backgroundStarted) return;
  backgroundStarted = true;
  void refreshRegistry();
  window.setInterval(() => void refreshRegistry(), 12_000);
}
ensureBackgroundLoop();

// ---------------------------------------------------------------------------
// 任务操作（停止 / PDF 暂停恢复 / 打开本地文件 / 复制路径）
// ---------------------------------------------------------------------------

async function stopTask(taskId: string): Promise<void> {
  const entry = tasks.get(taskId);
  if (!entry) return;
  const client = await getClient();
  entry.task = await client.request<TaskStatus>(`/api/tasks/${taskId}/stop`, { method: "POST" });
  touch(taskId);
}

async function pausePdfTask(taskId: string): Promise<void> {
  const entry = tasks.get(taskId);
  if (!entry || entry.task.surface !== "pdf") return;
  const client = await getClient();
  entry.task = await client.request<TaskStatus>(`/api/tasks/${taskId}/pause`, { method: "POST" });
  touch(taskId);
}

async function resumePdfTask(taskId: string): Promise<void> {
  const entry = tasks.get(taskId);
  if (!entry || entry.task.surface !== "pdf") return;
  const client = await getClient();
  entry.task = await client.request<TaskStatus>(`/api/tasks/${taskId}/resume`, { method: "POST" });
  touch(taskId);
}

async function endPausedPdfTask(taskId: string): Promise<void> {
  const entry = tasks.get(taskId);
  if (!entry || entry.task.surface !== "pdf") return;
  if (!window.confirm("结束暂停任务将不再提交未处理页面，但会写入并保留已完成页面、素材、清单和报告。是否结束？")) return;
  const client = await getClient();
  entry.task = await client.request<TaskStatus>(`/api/tasks/${taskId}/end-paused`, { method: "POST" });
  touch(taskId);
}

async function openTaskLocalFile(path: string, reveal: boolean): Promise<void> {
  if (!path.trim()) return;
  try {
    await invoke("open_local_path", { path, reveal });
  } catch (error) {
    showToast({ message: `无法打开：${error instanceof Error ? error.message : String(error)}`, error: true });
  }
}

async function exportTaskDiagnostic(taskId: string): Promise<void> {
  try {
    const client = await getClient();
    const saved = await client.saveBinaryDownload(
      `/api/diagnostics/task/${encodeURIComponent(taskId)}.zip`,
      `translator-diagnostic-${taskId.slice(0, 8)}.zip`,
    );
    if (!saved) return; // 用户在保存框里取消：静默返回，不弹任何提示
    showToast({ message: `诊断包已保存到：${saved}` });
  } catch (error) {
    showToast({ message: `导出诊断失败：${error instanceof Error ? error.message : String(error)}`, error: true });
  }
}

async function copyTaskPath(path: string): Promise<void> {
  if (!path.trim() || !navigator.clipboard?.writeText) return;
  await navigator.clipboard.writeText(path);
  showToast({ message: "路径已复制。" });
}

function confirmStopTask(taskId: string): void {
  openModal({
    tone: "warn",
    icon: "warn",
    title: "安全停止当前任务？",
    body: ["已生成的文件会保留在输出目录；Excel、Word 会结束为终态。PDF / 图片应先“暂停提交”，再选择继续或结束暂停。"],
    actions: [
      { label: "继续执行", variant: "default" },
      { label: "安全停止", variant: "danger-solid", onClick: () => stopTask(taskId) },
    ],
  });
}

function confirmDeleteTask(taskId: string): void {
  openModal({
    tone: "warn",
    icon: "trash",
    title: "删除这条任务记录？",
    body: ["只从任务中心移除这条记录。已经生成的译文、报告和诊断包都留在原处，不会被删除。"],
    actions: [
      { label: "取消", variant: "default" },
      { label: "删除记录", variant: "danger-solid", onClick: () => void deleteTaskRecord(taskId) },
    ],
  });
}

async function deleteTaskRecord(taskId: string): Promise<void> {
  try {
    const client = await getClient();
    await client.request(`/api/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
  } catch (error) {
    showToast({ message: `删除失败：${error instanceof Error ? error.message : String(error)}`, error: true });
    return;
  }
  // 只有终态任务能删（后端 409 挡住运行中的），所以这里没有事件流要收——
  // watchTask() 对 terminal 任务直接返回，不会有 watcher 把记录 upsert 回来。
  // 但轮询快照可能是 DELETE 之前成的像，所以还要立个墓碑（见 deletedTaskIds）。
  deletedTaskIds.add(taskId);
  tasks.delete(taskId);
  order = order.filter((id) => id !== taskId);
  // 选中项要落在「当前筛选下看得见」的任务上：order 是全量顺序，删完直接取 order[0]
  // 会在「活动」筛选下选中一条已完成的任务——左边列表没有高亮项，右边详情却换了内容。
  if (selectedId === taskId) selectedId = visibleOrder()[0] ?? null;
  renderList();
  renderDetail();
  showToast({ message: "记录已删除；输出文件没有被动过。" });
}

// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------

const FILTERS: Array<["all" | "active" | "terminal", string]> = [
  ["all", "全部"],
  ["active", "活动"],
  ["terminal", "最近结果"],
];

function visibleOrder(): string[] {
  return order.filter((id) => {
    const entry = tasks.get(id);
    if (!entry) return false;
    if (filter === "all") return true;
    return filter === "active" ? isTaskActive(entry.task) : !isTaskActive(entry.task);
  });
}

/**
 * completed_with_issues 这一个状态的统一措辞：一律「需复核」，数得出条数就带上条数。
 * 之前列表卡片有条目时说「需复核 11」、没条目时说「完成但有问题」，详情页又说
 * 「完成但有问题 · 需复核 11」——同一个状态在三处三种说法，用户没法判断是不是三件事。
 *
 * 数字只认一个来源：能列出定位清单时就数清单里的位置（reviewRows 已按位置合并，
 * 同一段的两条判定算一处），列不出来才退回后端的 `review` 段。徽章上的数字必须能和
 * 下面那张表一行一行对上——数字 5、表里 3 行，用户会以为界面漏了内容。
 *
 * 退回后端时也不能直接用 `review.total_count`：Word 的 review_items 里混着
 * severity=resolved 的条目（首次没出译文、严格重试已经把译文补回来了，用户不用管），
 * 把它们算进「需复核 N」，徽章说 8 处待办、点进去 8 行全是绿色「已自动处理」。
 * 所以 Word 只数 needs_review 桶。Excel 的 counts 按 category 分桶、没有 severity 概念，
 * 它写进 review_positions 的每一格本来就是复核标记，仍取总数。
 */
function reviewCount(task: TaskStatus): number | null {
  const rows = reviewRows(task);
  if (rows.length) return rows.filter((row) => row.needsReview).length;
  const result = record(task.result);
  const review = record(result.review);
  const counts = record(review.counts);
  // severity 分桶（Word）认这两个键；只要出现过其中之一，总数就不是「待办数」。
  if ("needs_review" in counts || "resolved" in counts) {
    return firstNumber(counts, ["needs_review"]) ?? 0;
  }
  const merged = { ...result, ...record(result.summary), ...record(result.kpi) };
  return (
    firstNumber(review, ["total_count"]) ??
    firstNumber(merged, ["review_count", "review_items_count", "review_total", "review_text_count"])
  );
}

/**
 * 终态徽章。「没生成任何文件」永远排在「需复核」前面：上游全程 503 的那次 PDF 任务
 * 收在 completed_with_issues，列表里三张卡全写着「需复核」，而输出目录里一个译文 PDF
 * 都没有——用户以为有东西可看，点进去只有报告。有文件没生成时也先说这件事，
 * 它决定用户下一步要不要重跑。
 */
function reviewChip(task: TaskStatus): { label: string; tone: ChipTone } | null {
  // 中止 / 出错 / 应用中断这三种终态，状态词本身就是用户最需要的一句话（「已中止」
  // 比「没有生成文件」更能解释为什么没有东西），交回 taskStateMeta。
  if (task.state !== "done" && task.state !== "completed_with_issues") return null;
  const { produced, failed, total } = producedCounts(task);
  if (total > 0 && produced === 0) return { label: "没有生成文件", tone: "dgr" };
  if (failed > 0) return { label: `${failed} 个文件未生成`, tone: "dgr" };
  if (task.state !== "completed_with_issues") return null;
  const count = reviewCount(task);
  return { label: count !== null && count > 0 ? `需复核 ${count}` : "需复核", tone: "warn" };
}

function cardChip(entry: TaskEntry): { label: string; tone: ChipTone } {
  const meta = taskStateMeta(entry.task, entry.streamState);
  if (isTaskActive(entry.task)) {
    const percent = entry.stepTotal > 0 ? Math.round((entry.stepDone / entry.stepTotal) * 100) : null;
    return { label: percent !== null ? `${percent}%` : meta.label, tone: "tint" };
  }
  return reviewChip(entry.task) ?? meta;
}

function cardSubtitle(entry: TaskEntry): string {
  const meta = taskStateMeta(entry.task, entry.streamState);
  if (isTaskActive(entry.task)) {
    return `${meta.label} · ${entry.phaseName || "正在准备任务"} · ${formatClock(entry.task.created_at)} 开始`;
  }
  // 逐文件结果优先于 KPI 计数：KPI 那几个键各 surface 名字不同，取不到就会退回状态词，
  // 而 file_results 三个 surface 都有。
  const { produced, total } = producedCounts(entry.task);
  const successCount =
    total > 0
      ? produced
      : firstNumber(record({ ...record(entry.task.result), ...record(record(entry.task.result).summary) }), [
          "success_count",
          "completed_count",
          "successful_files",
        ]);
  const detail = successCount === null ? meta.label : successCount > 0 ? `已生成 ${successCount} 个文件` : "没有生成文件";
  return `${detail} · ${formatDay(entry.task.updated_at ?? entry.task.created_at)}`;
}

function renderCard(id: string): HTMLDivElement {
  const entry = tasks.get(id)!;
  const card = document.createElement("div");
  card.className = id === selectedId ? "card tkcard sel" : "card tkcard";
  card.addEventListener("click", () => {
    selectedId = id;
    renderList();
    renderDetail();
  });

  const r1 = document.createElement("div");
  r1.className = "r1";
  const ic = icon(surfaceIcon(entry.task.surface), { size: "sm" });
  r1.append(ic);
  // 标题挂在独立的 .ttl 上（不是裸文本节点）才能省略号截断：source_label 现在是文件名，
  // 下划线连写的长英文名是不可断 token，直接铺出去会把状态徽章顶出 340px 的列表宽度，
  // 用户看不见这条任务到底成没成。完整名字放 title，悬停可看全。
  const titleText = `${taskSurfaceLabel(entry.task.surface)} · ${text(entry.task.source_label, `任务 ${entry.task.task_id.slice(0, 8)}`)}`;
  const titleEl = document.createElement("span");
  titleEl.className = "ttl";
  titleEl.textContent = titleText;
  titleEl.title = titleText;
  r1.append(titleEl);
  const chipInfo = cardChip(entry);
  const chip = createChip({ label: chipInfo.label, tone: chipInfo.tone });
  chip.style.marginLeft = "auto";
  r1.append(chip);
  card.append(r1);

  const r2 = document.createElement("div");
  r2.className = "r2";
  r2.textContent = cardSubtitle(entry);
  card.append(r2);

  if (isTaskActive(entry.task) && entry.stepTotal > 0) {
    const bar = document.createElement("div");
    bar.className = "bar";
    bar.style.marginTop = "9px";
    const fill = document.createElement("i");
    fill.style.width = `${Math.round((entry.stepDone / entry.stepTotal) * 100)}%`;
    bar.append(fill);
    card.append(bar);
  }
  return card;
}

function renderList(): void {
  if (!listRootEl) return;
  while (listRootEl.children.length > 1) listRootEl.removeChild(listRootEl.lastChild!);
  const ids = visibleOrder();
  if (!ids.length) {
    const empty = createEmptyState({
      title: "当前没有可显示的任务",
      description: "启动 Excel、Word、PDF / 图片翻译或记忆库深度清洗后，状态会保留在这里。",
      icon: "tasks",
    });
    empty.style.flex = "1";
    listRootEl.append(empty);
    return;
  }
  for (const id of ids) {
    listRootEl.append(renderCard(id));
  }
}

function buildActionButton(reference: { label: string; path: string; reveal: boolean }): HTMLButtonElement {
  return createButton({
    label: reference.label,
    icon: "folder",
    size: "mini",
    onClick: () => void openTaskLocalFile(reference.path, reference.reveal),
  });
}

function renderDetail(): void {
  if (!detailRootEl) return;
  detailRootEl.innerHTML = "";
  const entry = selectedId ? tasks.get(selectedId) : null;
  if (!entry) {
    const empty = createEmptyState({
      title: "选择左侧任务查看详情",
      description: "结果快照、KPI、结果定位清单与产物入口都在这里。",
      icon: "tasks",
    });
    empty.style.flex = "1";
    detailRootEl.append(empty);
    return;
  }
  const { task } = entry;

  const header = document.createElement("div");
  header.style.display = "flex";
  header.style.alignItems = "center";
  header.style.gap = "10px";
  header.style.flexWrap = "wrap";
  const title = document.createElement("h2");
  title.style.fontSize = "15px";
  title.style.fontWeight = "650";
  // 同卡片标题：长文件名不截断会把右侧「删除记录」「导出诊断」推到可视区外，
  // 详情页横向不滚动，那几个按钮就彻底点不到了。
  title.style.flex = "1 1 200px";
  title.style.minWidth = "0";
  title.style.overflow = "hidden";
  title.style.textOverflow = "ellipsis";
  title.style.whiteSpace = "nowrap";
  const titleText = `${taskSurfaceLabel(task.surface)} · ${text(task.source_label, `任务 ${task.task_id.slice(0, 8)}`)}`;
  title.textContent = titleText;
  title.title = titleText;
  header.append(title);

  const meta = taskStateMeta(task, entry.streamState);
  const reviews = reviewRows(task);
  header.append(createChip(reviewChip(task) ?? meta));

  const actions = document.createElement("div");
  actions.style.marginLeft = "auto";
  actions.style.display = "flex";
  actions.style.gap = "8px";
  actions.style.flexWrap = "wrap";
  const references = taskResultReferences(task);
  for (const reference of references) {
    actions.append(buildActionButton(reference));
  }
  if (task.terminal) {
    actions.append(
      createButton({
        label: "导出诊断",
        size: "mini",
        // 详情页上的这个按钮说的是「这一次任务」，就该直接把这一次的诊断包存下来。
        // 之前它把人扔到设置页去自己找——找的还是全部任务的合集。
        onClick: () => void exportTaskDiagnostic(task.task_id),
      }),
    );
  }
  if (task.terminal && (task.surface === "tm_clean" || task.surface === "cleaner")) {
    actions.append(
      createButton({
        label: "查看清洗建议",
        icon: "book",
        size: "mini",
        onClick: () => navigate("library", { reviewCleanTaskId: task.task_id }),
      }),
    );
  }
  const canOpenWorkspace = task.surface === "excel" || task.surface === "word" || task.surface === "pdf";
  if (canOpenWorkspace) {
    actions.append(
      createButton({
        label: "打开工作区",
        icon: "play",
        size: "mini",
        onClick: () => navigate(task.surface as "excel" | "word" | "pdf"),
      }),
    );
  }
  if (isTaskActive(task)) {
    if (task.surface === "pdf" && task.state === "paused") {
      actions.append(createButton({ label: "继续翻译", variant: "primary", size: "mini", onClick: () => void resumePdfTask(task.task_id) }));
      actions.append(createButton({ label: "结束暂停", variant: "danger", size: "mini", onClick: () => void endPausedPdfTask(task.task_id) }));
    } else if (task.surface === "pdf") {
      actions.append(createButton({ label: "暂停提交", size: "mini", onClick: () => void pausePdfTask(task.task_id) }));
    } else {
      actions.append(createButton({ label: "安全停止", variant: "danger", size: "mini", onClick: () => confirmStopTask(task.task_id) }));
    }
  }
  if (task.terminal) {
    // 只删任务中心的这条记录；译文、报告、诊断包都在磁盘上原样留着。
    actions.append(createButton({ label: "删除记录", icon: "trash", variant: "danger", size: "mini", onClick: () => confirmDeleteTask(task.task_id) }));
  }
  header.append(actions);
  detailRootEl.append(header);

  const snapshots = taskSnapshotRows(task);
  if (snapshots.length) {
    const kv = document.createElement("dl");
    kv.className = "kv";
    for (const [label, value] of snapshots) {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      dd.title = value;
      if (label === "输出位置") {
        const copyLink = document.createElement("span");
        copyLink.className = "linklike";
        copyLink.textContent = " 复制路径";
        copyLink.addEventListener("click", (event) => {
          event.stopPropagation();
          void copyTaskPath(value);
        });
        dd.append(copyLink);
      }
      kv.append(dt, dd);
    }
    detailRootEl.append(kv);
  }

  const kpis = richKpiRows(task);
  if (kpis.length) {
    const grid = document.createElement("div");
    grid.className = "kpis";
    for (const [label, value] of kpis) {
      const tile = document.createElement("div");
      tile.className = WARN_KPI_LABELS.has(label) && value > 0 ? "kpi warn" : "kpi";
      const span = document.createElement("span");
      span.textContent = label;
      const b = document.createElement("b");
      b.textContent = String(value);
      tile.append(span, b);
      grid.append(tile);
    }
    detailRootEl.append(grid);
  }

  if (reviews.length) {
    const sectionTitle = document.createElement("div");
    sectionTitle.style.fontSize = "13px";
    sectionTitle.style.fontWeight = "650";
    sectionTitle.style.margin = "6px 0 8px";
    sectionTitle.textContent = "结果定位清单 ";
    const hint = document.createElement("span");
    hint.style.fontWeight = "400";
    hint.style.color = "var(--ink-3)";
    hint.style.fontSize = "12px";
    hint.textContent = "需复核在前 · 一行一个位置，同一位置的多条判定已并成一行";
    sectionTitle.append(hint);
    detailRootEl.append(sectionTitle);

    const table = document.createElement("table");
    table.className = "tbl review-tbl";
    // Excel 那一路的复核项不带原文摘录（定位靠工作表 + 单元格，文件里还有底色标记），
    // 整列「—」只是占掉了窄面板里四分之一的宽度，还让人以为摘录没取到。一行都没有摘录
    // 时干脆不要这一列，宽度让给「问题」和「处理」。
    const showExcerpt = reviews.some((row) => row.excerpt.trim());
    // 固定列宽：任务详情是窄面板，交给浏览器按最小内容宽度分配会把文件名压成竖排单字。
    const cols = document.createElement("colgroup");
    for (const width of showExcerpt ? ["20%", "14%", "26%", "12%", "28%"] : ["22%", "18%", "22%", "38%"]) {
      const col = document.createElement("col");
      col.style.width = width;
      cols.append(col);
    }
    table.append(cols);
    const headRow = document.createElement("tr");
    for (const label of showExcerpt ? ["文件", "位置", "原文摘录", "问题", "处理"] : ["文件", "位置", "问题", "处理"]) {
      const th = document.createElement("th");
      th.textContent = label;
      headRow.append(th);
    }
    table.append(headRow);
    for (const row of reviews.slice(0, 50)) {
      const tr = document.createElement("tr");
      // 文件名单行显示、超出省略；完整名字放 title，悬停可看全。
      const fileTd = document.createElement("td");
      fileTd.className = "fname";
      fileTd.textContent = row.file || "—";
      if (row.file) fileTd.title = row.file;
      tr.append(fileTd);
      const middle = showExcerpt
        ? [row.location || "—", row.excerpt || "—", row.issue]
        : [row.location || "—", row.issue];
      for (const value of middle) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.append(td);
      }
      const actionTd = document.createElement("td");
      const actionMeta = reviewActionMeta(row);
      if (!actionMeta) {
        actionTd.textContent = "—";
      } else {
        // 徽章按两三个字设计，整句说明塞进去会直接溢出：
        // 第一行只放短结论，完整说明降级成第二行的灰色小字。
        const stack = document.createElement("div");
        stack.className = "review-act";
        stack.append(createChip({ label: actionMeta.label, tone: actionMeta.tone }));
        if (row.action.trim() !== actionMeta.label) {
          const note = document.createElement("div");
          note.className = "note";
          note.textContent = row.action;
          stack.append(note);
        }
        actionTd.append(stack);
      }
      tr.append(actionTd);
      table.append(tr);
    }
    detailRootEl.append(table);
    if (reviews.length > 50) {
      const note = document.createElement("p");
      note.style.fontSize = "12px";
      note.style.color = "var(--ink-3)";
      note.style.margin = "6px 0";
      note.textContent = `仅显示前 50 处，共 ${reviews.length} 处。完整清单在运行日志与诊断包里。`;
      detailRootEl.append(note);
    }
  } else if ((reviewCount(task) ?? 0) > 0) {
    // 关掉「标记需复核内容」时后端仍然数得出条数，但没有一条能逐格定位（Excel 那一路会
    // 用 review_marks 回填计数，见 core/task_runner.py）。什么都不显示的话，徽章上的
    // 「需复核 37」在详情页里无处可查，用户只能以为界面漏了内容。
    const note = document.createElement("p");
    note.style.fontSize = "12px";
    note.style.color = "var(--ink-3)";
    note.style.margin = "6px 0 0";
    note.textContent = "本次没有把复核标记写进输出文件，因此无法逐格定位。想要逐格定位，请在开始翻译前打开「标记需复核内容」。";
    detailRootEl.append(note);
  }

  const files = fileRows(task);
  if (files.length) {
    const sectionTitle = document.createElement("div");
    sectionTitle.style.fontSize = "13px";
    sectionTitle.style.fontWeight = "650";
    sectionTitle.style.margin = "16px 0 8px";
    sectionTitle.textContent = "产物文件";
    detailRootEl.append(sectionTitle);

    const table = document.createElement("table");
    table.className = "tbl";
    const headRow = document.createElement("tr");
    for (const label of ["文件", "状态", "输出", "错误"]) {
      const th = document.createElement("th");
      th.textContent = label;
      headRow.append(th);
    }
    table.append(headRow);
    for (const file of files.slice(0, 30)) {
      const tr = document.createElement("tr");
      // 文件名同样单行省略 + 悬停看全名，和上面那张表保持一致。
      const nameTd = document.createElement("td");
      nameTd.className = "fname";
      nameTd.textContent = file.name || "—";
      if (file.name) nameTd.title = file.name;
      tr.append(nameTd);
      // 状态是枚举，用徽章上色；纯文字的「未生成」和「已生成」在窄表里几乎分不出轻重。
      const statusTd = document.createElement("td");
      if (file.status) statusTd.append(createChip({ label: file.status, tone: file.tone }));
      else statusTd.textContent = "—";
      tr.append(statusTd);
      // 「输出」只显示文件名；完整路径进 title，脱敏后的占位值在 fileRows 里已经被丢掉了。
      const outputTd = document.createElement("td");
      outputTd.className = "fname";
      outputTd.textContent = file.output || "—";
      if (file.outputPath) outputTd.title = file.outputPath;
      // 压缩版 PDF 也是一份真产物：运行日志里写了「已生成压缩版」，这张表以前只列高清版，
      // 用户在输出目录里看到两个 PDF，却只有一个能在界面上对上。
      if (file.compressedOutput) {
        const extra = document.createElement("div");
        extra.style.fontSize = "11.5px";
        extra.style.color = "var(--ink-3)";
        extra.style.marginTop = "2px";
        extra.textContent = `压缩版 ${file.compressedOutput}`;
        if (file.compressedOutputPath) extra.title = file.compressedOutputPath;
        outputTd.append(extra);
      }
      tr.append(outputTd);
      const errorTd = document.createElement("td");
      errorTd.textContent = file.error || "—";
      tr.append(errorTd);
      table.append(tr);
    }
    detailRootEl.append(table);
    if (files.length > 30) {
      const note = document.createElement("p");
      note.style.fontSize = "12px";
      note.style.color = "var(--ink-3)";
      note.style.margin = "6px 0";
      note.textContent = `仅显示前 30 个文件，共 ${files.length} 个文件。完整清单在运行日志与诊断包里。`;
      detailRootEl.append(note);
    }
  }

  if (!task.terminal) {
    const logNote = document.createElement("p");
    logNote.style.fontSize = "12px";
    logNote.style.color = "var(--ink-3)";
    logNote.style.marginTop = "10px";
    logNote.textContent =
      entry.streamState === "reconnecting"
        ? `事件流暂时断开，正在从事件 ${entry.lastEventId} 补拉，不会重复处理已有进度。`
        : "任务运行中，快照与产物会在收尾后补全。";
    detailRootEl.append(logNote);
  }
}

// ---------------------------------------------------------------------------
// View 生命周期
// ---------------------------------------------------------------------------

export function mount(container: HTMLElement, params: ViewParams): void {
  mounted = true;
  if (typeof params.taskId === "string") selectedId = params.taskId;
  else if (!selectedId && order.length) selectedId = order[0];

  refreshTopbarStatus();

  listRootEl = document.createElement("div");
  listRootEl.className = "tk-list";
  filtersEl = document.createElement("div");
  filtersEl.className = "tk-filters";
  for (const [key, label] of FILTERS) {
    const f = document.createElement("span");
    f.className = key === filter ? "f on" : "f";
    f.textContent = label;
    f.addEventListener("click", () => {
      filter = key;
      renderFilters();
      renderList();
    });
    filtersEl.append(f);
  }
  listRootEl.append(filtersEl);

  detailRootEl = document.createElement("div");
  detailRootEl.className = "card tk-detail";

  container.append(listRootEl, detailRootEl);

  renderList();
  renderDetail();

  // 进入任务中心先立刻拉一次：否则第一份画面用的是上一次巡检的成像，最长能差 12 秒。
  void refreshRegistry();
  // 前台快速轮询：任务中心可见时更快发现新任务/刷新列表；后台巡检（12s）在任何视图下都跑。
  fastPollTimer = window.setInterval(() => void refreshRegistry(), 4000);
}

function renderFilters(): void {
  if (!filtersEl) return;
  const children = Array.from(filtersEl.children) as HTMLSpanElement[];
  FILTERS.forEach(([key], index) => {
    children[index]?.classList.toggle("on", key === filter);
  });
}

export function unmount(): void {
  mounted = false;
  if (fastPollTimer !== null) {
    window.clearInterval(fastPollTimer);
    fastPollTimer = null;
  }
  listRootEl = null;
  filtersEl = null;
  detailRootEl = null;
}
