// 应用内更新的能力层 —— 把 Tauri updater / process 插件包成一组窄接口，
// 让设置页只关心「能不能自己更新 / 下到多少了 / 装完没有」。
//
// 分工（重要）：
//   * 「有没有新版」由后端 /api/updates/check 判定。它认识忽略版本、暂停提醒、
//     发布包未就绪这些产品规则，而 latest.json 只知道最新版本号。两边各查一次
//     再拼答案，只会给出互相矛盾的结论。
//   * 「怎么装」才交给 updater 插件：真正下载、验签、替换 app 的是它。
//     所以 `resolveUpdate()` 只在用户按下「下载并安装」时才调用插件的 check()，
//     用来换取那个能执行安装的句柄。

export interface UpdaterEnvironment {
  canSelfUpdate: boolean;
  /** 机器可读的原因码，见 src-tauri/src/main.rs 的 update_environment。 */
  reason: string;
  /**
   * in_place（macOS）：装完就地替换 .app，当前进程不受影响，由用户决定何时重启。
   * installer_restart（Windows）：交给 NSIS 安装程序并结束当前进程，安装完自动重开。
   * 后者没有「装好了等重启」这个中间态，而且会直接打断正在跑的任务，必须先确认。
   */
  installBehavior: "in_place" | "installer_restart";
}

export interface UpdateHandle {
  version: string;
  /** 下载安装包；onProgress 收到的是 0-100 的百分比，总长度未知时为 null。 */
  download(onProgress: (percent: number | null, received: number, total: number | null) => void): Promise<void>;
  /** 落盘替换。macOS 上会解包 .app.tar.gz 覆盖当前 bundle，Windows 上跑 NSIS。 */
  install(): Promise<void>;
  close(): Promise<void>;
}

export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/** 这台机器 / 这份安装能不能原地自更新。拿不到答案时按「不能」处理。 */
export async function updaterEnvironment(): Promise<UpdaterEnvironment> {
  if (!isTauriRuntime()) {
    return { canSelfUpdate: false, reason: "browser_preview", installBehavior: "in_place" };
  }
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    return await invoke<UpdaterEnvironment>("update_environment");
  } catch {
    return { canSelfUpdate: false, reason: "environment_probe_failed", installBehavior: "in_place" };
  }
}

/**
 * 向 latest.json 要一个可安装的更新句柄。
 * 返回 null 表示 latest.json 认为当前已是最新（或这个平台没有更新产物）。
 */
export async function resolveUpdate(): Promise<UpdateHandle | null> {
  const { check } = await import("@tauri-apps/plugin-updater");
  const update = await check();
  if (!update) return null;

  return {
    version: update.version,
    async download(onProgress) {
      let total: number | null = null;
      let received = 0;
      await update.download((event) => {
        if (event.event === "Started") {
          total = event.data.contentLength ?? null;
          onProgress(total ? 0 : null, 0, total);
        } else if (event.event === "Progress") {
          received += event.data.chunkLength;
          onProgress(total ? Math.min(100, (received / total) * 100) : null, received, total);
        } else {
          onProgress(100, total ?? received, total);
        }
      });
    },
    install: () => update.install(),
    close: () => update.close(),
  };
}

/** 重启应用让更新生效。调用方负责先确认没有任务被打断。 */
export async function restartApp(): Promise<void> {
  const { relaunch } = await import("@tauri-apps/plugin-process");
  await relaunch();
}
