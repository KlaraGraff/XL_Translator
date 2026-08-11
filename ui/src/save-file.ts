/**
 * 导出落盘：原生保存对话框 + Rust 侧写盘命令。所有「导出」按钮都必须走这里。
 *
 * ⚠️ 不要改回网页那套 `URL.createObjectURL()` + `anchor.download` + `anchor.click()`。
 * 那是一次真正的浏览器下载请求，而在 macOS 的 WKWebView 里下载要由宿主应用实现
 * 下载代理（WKDownloadDelegate）来回答「存到哪里」。这个应用从未注册过下载处理器
 * （Tauri 的 `on_download` 全项目零命中），WKWebView 于是把请求静默丢弃：不报错、
 * 不弹框、文件哪儿都不落地。9.2.x 之前每一个导出按钮点了没反应就是这么来的，而且
 * 当时成功 toast 还是无条件弹的，界面报告了一件根本没发生的事。
 *
 * 调用约定（改动这个模块或它的调用方时必须一起守住）：
 *   - 返回真正写入的路径 → 这时候才可以弹成功 toast；
 *   - 返回 null = 用户在保存框里按了取消 → 直接 return，不弹任何 toast；
 *   - 写盘失败会抛出（消息来自 Rust 侧，是真实的系统错误）→ 调用方弹红色 toast。
 */

import { invoke } from "@tauri-apps/api/core";
import { save } from "@tauri-apps/plugin-dialog";

// 保存框的扩展名过滤器按默认文件名的后缀选。列表之外的后缀不给过滤器，
// 保存框退化成「全部文件」，仍然可用。
const FILTERS: Record<string, { name: string; extensions: string[] }> = {
  json: { name: "JSON 文件", extensions: ["json"] },
  csv: { name: "CSV 文件", extensions: ["csv"] },
  zip: { name: "ZIP 压缩包", extensions: ["zip"] },
  xltcfg: { name: "XL Translator 配置文件", extensions: ["xltcfg"] },
};

function filtersFor(defaultFilename: string): Array<{ name: string; extensions: string[] }> {
  const dot = defaultFilename.lastIndexOf(".");
  const known = dot > 0 ? FILTERS[defaultFilename.slice(dot + 1).toLowerCase()] : undefined;
  return known ? [known] : [];
}

/** 弹原生保存框；用户取消返回 null。`dialog:allow-save` 权限已在 capabilities/default.json 里。 */
async function pickSavePath(defaultFilename: string): Promise<string | null> {
  const path = await save({ defaultPath: defaultFilename, filters: filtersFor(defaultFilename) });
  return path ?? null;
}

/** 文本导出（JSON / CSV）。返回写入的路径，null = 用户取消。 */
export async function saveTextFile(defaultFilename: string, contents: string): Promise<string | null> {
  const path = await pickSavePath(defaultFilename);
  if (!path) return null;
  await invoke("save_text_file", { path, contents });
  return path;
}

/** JSON 导出。缩进沿用原来的 2 空格，导出文件仍然是可读、可 diff 的。 */
export async function saveJsonFile(defaultFilename: string, payload: unknown): Promise<string | null> {
  return saveTextFile(defaultFilename, JSON.stringify(payload, null, 2));
}

/**
 * 把路径和内容打包成一段字节：`[u32 小端 路径字节数][UTF-8 路径][文件内容]`。
 *
 * 路径不走 invoke 的 header：header 值是字节串，`new Headers()` 遇到码位大于 255
 * 的字符会当场抛 TypeError，而保存路径带中文是常态（用户存到「桌面」「下载」这类
 * 中文目录，或者自己把文件名改成中文）。放进同一段字节里就没有编码问题。
 */
function frameSavePayload(path: string, contents: ArrayBuffer): ArrayBuffer {
  const pathBytes = new TextEncoder().encode(path);
  const frame = new Uint8Array(4 + pathBytes.length + contents.byteLength);
  new DataView(frame.buffer).setUint32(0, pathBytes.length, true);
  frame.set(pathBytes, 4);
  frame.set(new Uint8Array(contents), 4 + pathBytes.length);
  return frame.buffer;
}

/**
 * 二进制导出（诊断包、任务产物，可能几十 MB）。返回写入的路径，null = 用户取消。
 *
 * 字节直接以 ArrayBuffer 传给 invoke，Tauri 会作为原始请求体（InvokeBody::Raw）送到
 * Rust 侧。别改成把 `Array.from(bytes)` 塞进 JSON 参数——那条路每个字节都要膨胀成
 * 十进制数字加逗号，几十 MB 足以把界面卡死。
 */
export async function saveBinaryFile(defaultFilename: string, contents: ArrayBuffer): Promise<string | null> {
  const path = await pickSavePath(defaultFilename);
  if (!path) return null;
  await invoke("save_binary_file", frameSavePayload(path, contents));
  return path;
}
