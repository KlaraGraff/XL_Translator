// 顶栏右侧「当前模型」药丸的数据来源。
//
// setModelPill() 以前没有任何调用方，药丸从启动到关闭一直停在「未连接模型」——
// 连接测通了也不会变。药丸读的是翻译角色的生效配置：它就是文档翻译真正会拨的
// 那一路，和设置页详情卡看到的是同一份 /api/models/roles 数据。

import { ApiClient } from "./api-client";
import { setModelPill } from "./shell";
import { currentView, onNavigate, type ViewId } from "./router";

type RolePayload = {
  model?: unknown;
  mode?: unknown;
  availability_status?: unknown;
};

let client: ApiClient | null = null;
let connecting: Promise<ApiClient> | null = null;

// 药丸该显示哪个角色，看用户正站在哪个视图：PDF 与图片页翻译走的是 image 角色
// （后端 /api/models/roles 里就叫 "image"，实际拨的是 gpt-image-2 那一路，不是
// "pdf_translation"——那个名字只在模型配置导入/导出的档案 key 里用），Excel /
// Word 走的是 translation 角色。其余视图（设置、任务中心、记忆库、帮助）没有
// 自己的模型概念，沿用 translation，跟改之前的行为一样。
function roleKeyForView(id: ViewId | undefined): string {
  return id === "pdf" ? "image" : "translation";
}

// 角色表只在拿到时存一份，不是每次切视图都问后端要——applyModelPillFromRoles
// 本来就是「用已有数据刷新」这条路，缓存才对得上这个设计，也不会因为频繁切页面
// 打一堆没必要的请求。
let lastRoles: Record<string, unknown> | null | undefined = null;

async function getClient(): Promise<ApiClient> {
  if (client) return client;
  if (!connecting) {
    const instance = new ApiClient();
    connecting = instance
      .connect()
      .then(() => {
        client = instance;
        return instance;
      })
      .catch((error) => {
        connecting = null;
        throw error;
      });
  }
  return connecting;
}

/** 用缓存的角色表 + 当前视图重算一次药丸，不发请求。*/
function renderModelPill(): void {
  const roleKey = roleKeyForView(currentView()?.id);
  const role = (lastRoles?.[roleKey] ?? null) as RolePayload | null;
  const model = String(role?.model ?? "").trim();
  if (!model) {
    // 这个视图对应的角色没配模型就老实说「未连接模型」，不能回退去显示别的角色——
    // 那正是徽章跟当前页面对不上的 bug 本身。
    setModelPill({ label: "未连接模型", tone: "idle" });
    return;
  }
  const status = String(role?.availability_status ?? "").trim();
  const local = String(role?.mode ?? "") === "local";
  // 没测过就是没测过：绿点只给测通的那一刻，不能因为填了型号就假装连上了。
  const tone = status === "available" ? "ok" : status === "unavailable" ? "warn" : "idle";
  setModelPill({ label: local ? `本地 · ${model}` : model, tone });
}

// 切视图时用缓存重算，不重新发请求。模块只会被 import 一次（ESM 单例），
// 这里订阅一次就够，不需要额外的「订过没」标记。
onNavigate(() => renderModelPill());

/** 用已经拿到的角色表刷新药丸，不再多发一次请求。设置页每次保存/测试后都会走这里。 */
export function applyModelPillFromRoles(roles: Record<string, unknown> | null | undefined): void {
  lastRoles = roles;
  renderModelPill();
}

/** 主动拉一次角色表刷新药丸。启动时调用；拿不到就保持「未连接模型」，不弹错。 */
export async function refreshModelPill(): Promise<void> {
  try {
    const api = await getClient();
    const payload = await api.request<{ roles: Record<string, unknown> }>("/api/models/roles");
    applyModelPillFromRoles(payload.roles);
  } catch {
    setModelPill({ label: "未连接模型", tone: "idle" });
  }
}
