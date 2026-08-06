// 快速开始向导 —— 端口自 main.ts 的 quick-start 模态（main.ts:1934，触发逻辑见
// main.ts:3422-3440、3989-3993）。文案逐字沿用旧版，不做改写。
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
type QuickStartDestination = "config" | "excel" | "word" | "pdf";

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
  } else if (destination === "excel" || destination === "word" || destination === "pdf") {
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
      "保存服务商、Base URL、模型和 Key；连接测试仅在你主动点击时执行。",
      [createButton({ label: "打开配置", size: "mini", onClick: () => finish("config") })],
    ),
    buildStep(
      "2. 选择工作流与语言",
      "进入 Excel、Word 或 PDF / 图片页面。Excel 和 Word 默认使用自动识别，PDF 只选择目标语言。",
      [
        createButton({ label: "Excel", size: "mini", onClick: () => finish("excel") }),
        createButton({ label: "Word", size: "mini", onClick: () => finish("word") }),
        createButton({ label: "PDF / 图片", size: "mini", onClick: () => finish("pdf") }),
      ],
    ),
    buildStep(
      "3. 扫描文件并开始",
      "选择文件或文件夹，检查输出选项后启动。任务会冻结本次语言、模型、Key 和吞吐快照。",
    ),
  );

  activeHandle = openModal({
    tone: "warn",
    icon: "gear",
    sourceLabel: "欢迎使用 Translator",
    title: "快速开始",
    body: [
      "这是全新的应用数据基线。不会读取、导入、迁移或删除任何旧版本数据；本引导也不会自动测试 API Key 或发送服务请求。",
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
