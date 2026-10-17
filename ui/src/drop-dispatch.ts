// 拖入即分配（界面焕新阶段 2）：拖进窗口、或在「翻译」入口页选中的文件和文件夹，按扩展名数一数，
// 进入个数最多的那个类型页，并只把那一类交给它读取；其他类型若有，提示一句去哪一页处理。
// 已经在某个类型页上、而拖入的东西里有这一页认的文件时，就交给当前页，等同于填进路径栏后回车。

import { currentView, navigate, type ViewId } from "./router";
import { showToast } from "./components";
import { currentLocale, t } from "./i18n";
import { isExtSurface, surfaceDescriptors } from "./surface-descriptors";
import {
  fileNounPhrase,
  getClient,
  openSourcePaths,
  surfaceFileExtensions,
  type Surface,
} from "./views/workspace";

const BUILTIN_ORDER: Surface[] = ["excel", "word", "pdf", "cad"];

interface ClassifiedPath {
  path: string;
  kind: "file" | "dir" | "missing";
  suffixes: Record<string, number>;
}

interface Bucket {
  count: number;
  paths: string[];
}

/** 内置四种在前、扩展类型按描述符顺序在后；个数相同时排在前面的胜出。 */
function candidateSurfaces(): Surface[] {
  const ext = surfaceDescriptors().filter((d) => !d.builtin && isExtSurface(d.key)).map((d) => d.key as Surface);
  return [...BUILTIN_ORDER, ...ext];
}

function suffixOf(path: string): string {
  const name = path.split(/[\\/]/).pop() ?? "";
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/** 后端数不到（浏览器开发态只有文件名、接口失败）时，按文件名自己的扩展名算。 */
function classifyLocally(paths: string[]): ClassifiedPath[] {
  return paths.map((path) => {
    const suffix = suffixOf(path);
    return { path, kind: suffix ? "file" : "missing", suffixes: suffix ? { [suffix]: 1 } : {} };
  });
}

async function classify(paths: string[]): Promise<ClassifiedPath[]> {
  try {
    const client = await getClient();
    const response = await client.request<{ items: ClassifiedPath[] }>("/api/sources/classify", {
      method: "POST",
      body: JSON.stringify({ paths }),
    });
    // 本机找不到的路径（开发态拖进来的只有文件名）退回按名字判断。
    return response.items.map((item) => (item.kind === "missing" ? classifyLocally([item.path])[0] : item));
  } catch {
    return classifyLocally(paths);
  }
}

function bucketize(items: ClassifiedPath[]): Map<Surface, Bucket> {
  const owners = new Map<string, Surface>();
  for (const surface of candidateSurfaces()) {
    for (const ext of surfaceFileExtensions(surface)) if (!owners.has(ext)) owners.set(ext, surface);
  }
  const buckets = new Map<Surface, Bucket>();
  for (const item of items) {
    const perSurface = new Map<Surface, number>();
    for (const [suffix, n] of Object.entries(item.suffixes)) {
      const owner = owners.get(suffix.toLowerCase());
      if (owner) perSurface.set(owner, (perSurface.get(owner) ?? 0) + n);
    }
    for (const [surface, n] of perSurface) {
      const bucket = buckets.get(surface) ?? { count: 0, paths: [] };
      bucket.count += n;
      // 文件夹整个交给每个相关的类型页：各页的扫描只会挑自己认的文件。
      bucket.paths.push(item.path);
      buckets.set(surface, bucket);
    }
  }
  return buckets;
}

function pickWinner(buckets: Map<Surface, Bucket>, prefer: Surface | null): Surface | null {
  if (prefer && buckets.has(prefer)) return prefer;
  let best: Surface | null = null;
  for (const surface of candidateSurfaces()) {
    const bucket = buckets.get(surface);
    if (bucket && (!best || bucket.count > buckets.get(best)!.count)) best = surface;
  }
  return best;
}

function isSurfaceView(id: ViewId | undefined): id is Surface {
  return id !== undefined && (BUILTIN_ORDER.includes(id as Surface) || isExtSurface(id));
}

/** 把一批路径分到合适的类型页。没有可翻译的文件时提示一句，留在当前页（或入口页）。 */
export async function dispatchPaths(paths: string[]): Promise<void> {
  const list = paths.map((path) => path.trim()).filter(Boolean);
  if (!list.length) return;
  // 在任务、设置这类页面上松手：先回到入口页，没有可翻译的文件时就停在那里。
  if (!isSurfaceView(currentView()?.id) && currentView()?.id !== "home") navigate("home");
  const buckets = bucketize(await classify(list));
  const here = currentView()?.id;
  const target = pickWinner(buckets, isSurfaceView(here) ? here : null);
  if (!target) {
    showToast({ message: t("没有可翻译的文件。"), error: true });
    return;
  }
  if (here !== target) navigate(target);
  const opened = await openSourcePaths(target, buckets.get(target)!.paths);
  if (!opened) return;
  const others = candidateSurfaces()
    .filter((surface) => surface !== target && buckets.has(surface))
    .map((surface) => fileNounPhrase(surface, buckets.get(surface)!.count));
  if (others.length) {
    showToast({
      message: t("另有 {0}，可到对应页面处理。", others.join(currentLocale() === "zh" ? "、" : ", ")),
      duration: 6000,
    });
  }
}
