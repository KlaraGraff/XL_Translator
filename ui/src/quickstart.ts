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
import "./quickstart.css";

type JsonObject = Record<string, unknown>;
type QuickStartDestination = "config" | "excel" | "word" | "pdf" | "cad";

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

async function updatePreferences(patch: JsonObject): Promise<JsonObject> {
  await ensureConnected();
  return client.request<JsonObject>("/api/updates/preferences", {
    method: "PUT",
    body: JSON.stringify(patch),
  });
}

let activeHandle: ModalHandle | null = null;

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
  if (destination === "config") {
    navigate("settings", { page: "models" });
  } else if (destination === "excel" || destination === "word" || destination === "pdf" || destination === "cad") {
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
      "1. 配置翻译模型",
      "填好服务商、模型和 API 密钥，再点「测试连接」确认能用。",
      [createButton({ label: "打开配置", size: "mini", onClick: () => finish("config") })],
    ),
    buildStep(
      "2. 选择文件类型与语言",
      "进入 Excel、Word、PDF / 图片或 CAD 图纸页面。源语言默认自动识别，PDF 和图片只需选目标语言。",
      [
        createButton({ label: "Excel", size: "mini", onClick: () => finish("excel") }),
        createButton({ label: "Word", size: "mini", onClick: () => finish("word") }),
        createButton({ label: "PDF / 图片", size: "mini", onClick: () => finish("pdf") }),
        createButton({ label: "CAD 图纸", size: "mini", onClick: () => finish("cad") }),
      ],
    ),
    buildStep(
      "3. 选择文件并开始",
      "用「浏览」选好文件或文件夹，勾选要翻译的文件，确认右侧选项后点「开始翻译」。开始后再改设置，只影响下一次任务。",
    ),
  );

  activeHandle = openModal({
    tone: "warn",
    icon: "gear",
    sourceLabel: "欢迎使用 Translator",
    title: "快速开始",
    body: [
      "照下面三步就能开始第一次翻译。这个引导不会替你测试连接，也不会发出任何翻译请求。",
      steps,
    ],
    actions: [
      { label: "跳过", onClick: () => finish() },
      { label: "完成", variant: "primary", onClick: () => finish() },
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
  await ensureConnected();
  const state = await client.request<JsonObject>("/api/updates/state");
  const preferences = state.preferences && typeof state.preferences === "object"
    ? (state.preferences as JsonObject)
    : {};
  if (!preferences.quick_start_completed) {
    openQuickStartModal();
  }
}
