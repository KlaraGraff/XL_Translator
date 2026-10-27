// 快速开始向导 —— 端口自 main.ts 的 quick-start 模态（main.ts:1934，触发逻辑见
// main.ts:3422-3440、3989-3993）。文案已按面向普通用户的口径改写过，并补上了 CAD 图纸。
//
// 「是否已看过」标志：后端设置字段 settings.onboarding.quick_start_completed，
// 通过 GET /api/updates/state 读取（payload.preferences.quick_start_completed）、
// PUT /api/updates/preferences 写入（body { quick_start_completed: boolean }）——
// 与「更新提醒是否暂停」共用同一个 preferences 端点，不是 localStorage。
//
// 三处触发点：
//   1. app.ts 启动时调用 checkFirstLaunch()：未完成则自动弹出。
//   2. 帮助页 hero「重新查看快速开始」按钮调用 showQuickStart()。
//   3. 设置页「数据与维护」子页「快速开始 · 重新显示」按钮调用 showQuickStart()。

import { navigate } from "./router";
import { openModal, createButton, type ModalHandle } from "./components";
import { ApiClient } from "./api-client";
import { t } from "./i18n";
import "./quickstart.css";

type JsonObject = Record<string, unknown>;
type QuickStartDestination = "config" | "excel" | "word" | "pdf" | "cad" | "feedback";

const client = new ApiClient();
let connectPromise: Promise<void> | null = null;
async function ensureConnected(): Promise<void> {
  if (!connectPromise) {
    const attempt = client.connect();
    // 失败不进缓存，理由同 views/settings.ts：这是首启向导，正好是后端最可能还没就绪的
    // 时刻，缓存住一次失败会让向导整个卡死。
    attempt.catch(() => {
      if (connectPromise === attempt) connectPromise = null;
    });
    connectPromise = attempt;
  }
  return connectPromise;
}

/** 已连上后台的同一个客户端（guide.ts 查组件状态也用它）。 */
export async function connectedClient(): Promise<ApiClient> {
  await ensureConnected();
  return client;
}

/** PDF / CAD 图文指引（guide.ts）的「看过」标志也走这个端点，和快速开始同一套。 */
export async function updatePreferences(patch: JsonObject): Promise<JsonObject> {
  await ensureConnected();
  return client.request<JsonObject>("/api/updates/preferences", {
    method: "PUT",
    body: JSON.stringify(patch),
  });
}

/** 后端设置里的偏好（quick_start_completed、pdf_guide_seen、cad_guide_seen 等）。 */
export async function readPreferences(): Promise<JsonObject> {
  await ensureConnected();
  const state = await client.request<JsonObject>("/api/updates/state");
  return state.preferences && typeof state.preferences === "object" ? (state.preferences as JsonObject) : {};
}

let activeHandle: ModalHandle | null = null;
const closeListeners = new Set<() => void>();

/** 快速开始还开着：PDF / CAD 指引等它关掉再弹，不叠两层弹窗。 */
export function isQuickStartOpen(): boolean {
  return activeHandle !== null;
}

/** 快速开始关掉后通知一次（指引据此补弹）。返回取消订阅函数。 */
export function onQuickStartClosed(listener: () => void): () => void {
  closeListeners.add(listener);
  return () => closeListeners.delete(listener);
}

function buildStep(title: string, body: string, actions?: HTMLElement[]): HTMLDivElement {
  const step = document.createElement("div");
  step.className = "qs-step";
  const b = document.createElement("b");
  b.textContent = title;
  const span = document.createElement("span");
  span.textContent = body;
  step.append(b, span);
  if (actions?.length) {
    const row = document.createElement("div");
    row.className = "qs-actions";
    row.append(...actions);
    step.append(row);
  }
  return step;
}

function finish(destination?: QuickStartDestination): void {
  activeHandle?.close();
  activeHandle = null;
  void updatePreferences({ quick_start_completed: true }).catch(() => undefined);
  for (const listener of [...closeListeners]) listener();
  if (destination === "config") {
    navigate("settings", { page: "models" });
  } else if (destination) {
    navigate(destination);
  }
}

/** 打开快速开始弹窗；不改动完成标志（由调用方决定是否先重置）。 */
function openQuickStartModal(): void {
  activeHandle?.close();

  const steps = document.createElement("div");
  steps.className = "qs-steps";
  steps.append(
    buildStep(
      t("1. 配置翻译模型"),
      t("填写服务商、模型和 API 密钥后，点击「测试连接」确认配置可用。"),
      [createButton({ label: t("打开配置"), size: "mini", onClick: () => finish("config") })],
    ),
    buildStep(
      t("2. 选择文件类型与语言"),
      t("进入 Excel、Word、PDF / 图片或 CAD 图纸页面。源语言默认自动识别，PDF 和图片只需选择目标语言。"),
      [
        createButton({ label: "Excel", size: "mini", onClick: () => finish("excel") }),
        createButton({ label: "Word", size: "mini", onClick: () => finish("word") }),
        createButton({ label: t("PDF / 图片"), size: "mini", onClick: () => finish("pdf") }),
        createButton({ label: t("CAD 图纸"), size: "mini", onClick: () => finish("cad") }),
      ],
    ),
    buildStep(
      t("3. 选择文件并开始"),
      t("点击路径栏右侧的「选择」选择文件或文件夹，勾选要翻译的文件，确认右侧选项后点击「开始翻译」。开始后修改的设置仅对下一次任务生效。"),
    ),
  );
  // 不算翻译步骤，单独放在三步之后：首次使用就知道有问题可以反馈、处理完会收到邮件。
  const feedback = buildStep(
    t("遇到问题或有建议"),
    t("可随时在侧栏「反馈」中告诉我们。问题修复或功能上线后，我们会发邮件通知您。"),
    [createButton({ label: t("打开反馈"), size: "mini", onClick: () => finish("feedback") })],
  );
  feedback.classList.add("qs-tip");
  steps.append(feedback);

  activeHandle = openModal({
    tone: "warn",
    icon: "gear",
    sourceLabel: t("欢迎使用 Translator"),
    title: t("快速开始"),
    body: [
      t("按以下三个步骤即可开始首次翻译。本引导不会测试连接，也不会发送任何翻译请求。"),
      steps,
    ],
    actions: [
      { label: t("跳过"), onClick: () => finish() },
      { label: t("完成"), variant: "primary", onClick: () => finish() },
    ],
  });
}

/** 帮助页 / 设置页的「重新显示」入口：先把完成标志重置为 false，再弹窗
 *（对照 main.ts:3436 showQuickStart，保持同样的语义：重新看一遍等价于「还没看过」）。 */
export async function showQuickStart(): Promise<void> {
  await updatePreferences({ quick_start_completed: false }).catch(() => undefined);
  openQuickStartModal();
}

/** 首次启动检查：app.ts 启动时调用一次；quick_start_completed 为 false 时自动弹出。 */
export async function checkFirstLaunch(): Promise<void> {
  const preferences = await readPreferences();
  if (!preferences.quick_start_completed) {
    openQuickStartModal();
  }
}
