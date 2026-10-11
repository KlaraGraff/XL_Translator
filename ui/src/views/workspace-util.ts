// 翻译工作区（workspace.ts 及其拆出去的子模块）共用的小工具：建 DOM 节点、从后端
// 的松散 JSON 里安全取值、脱敏、格式化。全是无状态的纯函数，任何子模块都可以放心引用。

export type JsonObject = Record<string, unknown>;


export function el<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  return node;
}

export function text(value: unknown, fallback = ""): string {
  return typeof value === "string" && value ? value : fallback;
}

export function record(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}

export function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function hasOwn(obj: JsonObject, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

/** 点路径写入：nestedPatch("a.b.c", 1) => {a:{b:{c:1}}}。与 main.ts 的 nestedPatch verbatim 同构。 */
export function nestedPatch(path: string, value: unknown): JsonObject {
  const parts = path.split(".");
  const last = parts.pop() as string;
  let root: JsonObject = {};
  let cursor = root;
  for (const part of parts) {
    const next: JsonObject = {};
    cursor[part] = next;
    cursor = next;
  }
  cursor[last] = value;
  return root;
}

/** 后端自由文本可能带凭证碎片，展示前一律脱敏。三处（main.ts / tasks.ts / 工作区）各自独立实现，故意不共享，见 workspace.ts 文件尾说明。 */
export function redactedText(value: unknown, fallback = ""): string {
  const raw = text(value, fallback);
  if (!raw) return raw;
  return raw
    .replace(/(authorization\s*[:=]\s*)([^\s,;]+)/gi, "$1[redacted]")
    .replace(/\b(sk|rk|pk|api)[-_][a-z0-9_-]{8,}\b/gi, "[redacted]")
    .replace(/\bBearer\s+[^\s,;]+/gi, "Bearer [redacted]");
}

export function formatTime(ts?: number): string {
  const date = ts ? new Date(ts * (ts < 2e10 ? 1000 : 1)) : new Date();
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

export function formatSizeKb(kb: unknown): string {
  const value = num(kb, 0);
  if (value >= 1024) return `${(value / 1024).toFixed(1)} MB`;
  return `${Math.round(value)} KB`;
}
