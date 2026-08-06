/**
 * 浏览器开发调试用的 Tauri IPC 垫片 —— 仅在 `npm run dev` 且页面不在 Tauri
 * WebView 里时生效（import.meta.env.DEV 在生产构建中被替换为 false 并整体
 * tree-shake 掉，与后端 TRANSLATOR_DEV_ORIGIN 的 dev-only 口径一致）。
 *
 * 用法：在 ui/.env.development.local 写入
 *   VITE_DEV_SIDECAR_PORT=<端口>
 *   VITE_DEV_SIDECAR_TOKEN=<令牌>
 * 再以相同端口/令牌启动 API（create_app(auth_token=...)，并设置
 * TRANSLATOR_DEV_ORIGIN=http://127.0.0.1:1420 放行 CORS）。
 *
 * 命令覆盖范围只到「视觉与流程走查」够用为止：
 *   - sidecar_info      → 返回上面两个环境变量
 *   - plugin:dialog|open → window.prompt 手输本机路径（取消返回 null）
 *   - open_external_url / open_local_path → window.open / console 提示
 * 其余命令一律 reject，让缺口显式暴露而不是静默吞掉。
 */

type InvokeArgs = Record<string, unknown> | undefined;

function installDevShim(): void {
  const port = Number(import.meta.env.VITE_DEV_SIDECAR_PORT);
  const token = String(import.meta.env.VITE_DEV_SIDECAR_TOKEN ?? "");
  if (!Number.isFinite(port) || port <= 0 || !token) {
    console.warn("[dev-shim] 缺少 VITE_DEV_SIDECAR_PORT / VITE_DEV_SIDECAR_TOKEN，未安装垫片。");
    return;
  }

  const invoke = async (cmd: string, args?: InvokeArgs): Promise<unknown> => {
    switch (cmd) {
      case "sidecar_info":
        return { port, token };
      case "plugin:dialog|open": {
        const options = (args?.options ?? {}) as { directory?: boolean; multiple?: boolean };
        const input = window.prompt(
          `[dev-shim] 输入本机${options.directory ? "目录" : "文件"}路径（可用逗号分隔多个，取消=不选）`,
        );
        if (input === null || !input.trim()) return null;
        const paths = input.split(",").map((item) => item.trim()).filter(Boolean);
        return options.multiple ? paths : paths[0];
      }
      case "open_external_url": {
        const url = String((args as { url?: unknown } | undefined)?.url ?? "");
        if (url) window.open(url, "_blank", "noopener");
        return null;
      }
      case "open_local_path":
        console.info("[dev-shim] open_local_path:", args);
        return null;
      default:
        throw new Error(`[dev-shim] 未实现的 Tauri 命令：${cmd}`);
    }
  };

  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {
    invoke,
    // @tauri-apps/api 的部分模块（Channel 等）会取用 transformCallback；
    // 走查场景用不到真正的回调注册，返回 0 让调用不抛错即可。
    transformCallback: () => 0,
  };
  console.info(`[dev-shim] 已安装：sidecar http://127.0.0.1:${port}`);
}

if (import.meta.env.DEV && !("__TAURI_INTERNALS__" in window)) {
  installDevShim();
}

export {};
