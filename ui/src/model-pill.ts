// 顶栏右侧「当前模型」药丸的数据来源。
//
// setModelPill() 以前没有任何调用方，药丸从启动到关闭一直停在「未连接模型」——
// 连接测通了也不会变。药丸读的是翻译角色的生效配置：它就是文档翻译真正会拨的
// 那一路，和设置页详情卡看到的是同一份 /api/models/roles 数据。

import { ApiClient } from "./api-client";
import { setModelPill } from "./shell";

type RolePayload = {
  model?: unknown;
  mode?: unknown;
  availability_status?: unknown;
};

let client: ApiClient | null = null;
let connecting: Promise<ApiClient> | null = null;

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

/** 用已经拿到的角色表刷新药丸，不再多发一次请求。设置页每次保存/测试后都会走这里。 */
export function applyModelPillFromRoles(roles: Record<string, unknown> | null | undefined): void {
  const role = (roles?.translation ?? null) as RolePayload | null;
  const model = String(role?.model ?? "").trim();
  if (!model) {
    setModelPill({ label: "未连接模型", tone: "idle" });
    return;
  }
  const status = String(role?.availability_status ?? "").trim();
  const local = String(role?.mode ?? "") === "local";
  // 没测过就是没测过：绿点只给测通的那一刻，不能因为填了型号就假装连上了。
  const tone = status === "available" ? "ok" : status === "unavailable" ? "warn" : "idle";
  setModelPill({ label: local ? `本地 · ${model}` : model, tone });
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
