// 「翻译」入口页（界面焕新阶段 2）：把文件交给 Translator，按类型自动进入对应工作区。
// 上面一块投放区（拖入或选择文件 / 文件夹，类型芯片直达各页），下面两栏：最近 4 条任务、
// 已就绪（模型、记忆库、补充要求、图纸转换工具）。下面两栏的数据都取现有接口，取不到的写「—」，
// 不挡页面先画出来。拖入的分配在 drop-dispatch.ts，投放区的光在 drop-glow.ts；
// 拖入混合文件夹后最上面多一张「本批文件」卡片（home-batch.ts）。

import { open } from "@tauri-apps/plugin-dialog";
import type { ViewParams } from "../router";
import { navigate } from "../router";
import { setTopbar } from "../shell";
import { createButton, createChip, showToast, type ChipTone } from "../components";
import { icon } from "../icons";
import { t, tx } from "../i18n";
import { ApiClient } from "../api-client";
import { dispatchPaths } from "../drop-dispatch";
import { installDropGlow } from "../drop-glow";
import { applyModelPillFromRoles, modelRoleSummary } from "../model-pill";
import { loadSurfaceDescriptors, visibleTypeKeys } from "../surface-descriptors";
import { onWorkspaceChange, stateOf, surfaceFileExtensions, surfaceIcon, surfaceLabel, type Surface } from "./workspace";
import { redactedText } from "./workspace-util";
import { taskRowSummary } from "./tasks";
import { createBatchCard } from "./home-batch";
import "./home.css";
import { FIT_CLIP } from "../text-fit";

const BUILTIN_TYPES: Surface[] = ["excel", "word", "pdf", "cad"];
let mountGeneration = 0;
let client: ApiClient | null = null;
let disposeListeners: (() => void) | null = null;

async function getClient(): Promise<ApiClient> {
  if (!client) {
    const instance = new ApiClient();
    await instance.connect();
    client = instance;
  }
  return client;
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** 芯片上的扩展名：PDF 页认的图片格式多，只列前几个，jpeg / tiff 与 jpg / tif 重复不列。 */
function extensionHint(surface: Surface): string {
  const list = surfaceFileExtensions(surface).filter((ext) => ext !== "jpeg" && ext !== "tiff");
  const shown = list.slice(0, 5).map((ext) => `.${ext}`).join(" ");
  return list.length > 5 ? `${shown} …` : shown;
}

function allExtensions(types: Surface[]): string[] {
  return [...new Set(types.flatMap((surface) => surfaceFileExtensions(surface)))];
}

async function pick(directory: boolean, types: Surface[]): Promise<void> {
  let picked: unknown;
  try {
    picked = directory
      ? await open({ title: t("选择来源文件夹"), directory: true, multiple: false })
      : await open({
        title: t("选择文件"),
        directory: false,
        multiple: true,
        filters: [{ name: t("可翻译的文件"), extensions: allExtensions(types) }],
      });
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, t("选择来源失败。")), error: true });
    return;
  }
  const paths = (Array.isArray(picked) ? picked : [picked]).filter((item): item is string => typeof item === "string" && item.trim().length > 0);
  if (paths.length) await dispatchPaths(paths);
}

function buildHero(types: Surface[]): HTMLElement {
  const hero = el("div", "hero");
  const big = el("div", "big");
  big.append(icon("upload", { size: "lg" }));
  hero.append(big, el("h2", undefined, t("拖入文件或文件夹")));
  hero.append(el("p", undefined, t("Excel、Word、PPT、PDF 与图片、CAD 图纸都可以放在这里，Translator 会按类型分到对应的工作区并读取内容。")));
  const chips = el("div", "types");
  for (const surface of types) {
    const chip = el("button", "type-chip");
    chip.type = "button";
    chip.append(icon(surfaceIcon(surface), { size: "sm" }), el("span", "nm", surfaceLabel(surface)), el("span", "ext", extensionHint(surface)));
    chip.addEventListener("click", () => navigate(surface));
    chips.append(chip);
  }
  hero.append(chips);
  const acts = el("div", "acts");
  acts.append(
    createButton({ label: t("选择文件"), icon: "doc-file", variant: "primary", onClick: () => void pick(false, types) }),
    createButton({ label: t("选择文件夹"), icon: "folder", onClick: () => void pick(true, types) }),
  );
  hero.append(acts);
  return hero;
}

function sectionHead(title: string, link: string, onClick: () => void): HTMLElement {
  const head = el("div", "h3", title);
  const more = el("button", "linklike more", link);
  more.type = "button";
  more.addEventListener("click", onClick);
  head.append(more);
  return head;
}

function readyRow(name: string): { row: HTMLElement; val: HTMLElement } {
  const row = el("div", "row");
  const val = el("span", "val", "—");
  row.append(el("span", "nm", name), val);
  return { row, val };
}

function setVal(val: HTMLElement, label: string, tone?: ChipTone | "idle"): void {
  val.textContent = "";
  if (!tone) {
    val.textContent = label;
    return;
  }
  const chip = createChip({ label, tone: tone === "idle" ? "mute" : tone });
  chip.prepend(el("span", "led"));
  val.append(chip);
}

async function fillRecent(rows: HTMLElement, generation: number): Promise<void> {
  try {
    const list = await (await getClient()).listTasks();
    if (generation !== mountGeneration) return;
    const tasks = [...list.active, ...list.recent]
      .filter((task) => !["cleaner", "tm_clean"].includes(task.surface))
      .slice(0, 4);
    rows.textContent = "";
    // 只有进页后的第一次填充浮现；本批文件开始、结束时的重取不再整列重浮。
    rows.classList.toggle("stag", !rows.dataset.filled);
    rows.dataset.filled = "1";
    if (!tasks.length) {
      rows.append(el("div", "empty", t("还没有翻译任务。拖入文件开始第一次翻译。")));
      return;
    }
    tasks.forEach((task, index) => {
      const info = taskRowSummary(task);
      const row = el("button", "row");
      row.type = "button";
      row.style.setProperty("--i", String(index));
      row.append(icon(info.icon, { size: "sm" }), el("span", `nm ${FIT_CLIP}`, info.title), el("span", "meta", info.meta), createChip(info.chip));
      row.title = tx(task.source_label ?? info.title);
      row.addEventListener("click", () => navigate("tasks", { taskId: task.task_id }));
      rows.append(row);
    });
  } catch {
    if (generation !== mountGeneration) return;
    rows.textContent = "";
    rows.append(el("div", "empty", "—"));
  }
}

async function fillReady(slots: Record<"text" | "image" | "tm" | "domain" | "oda", HTMLElement>, generation: number): Promise<void> {
  const c = await getClient().catch(() => null);
  if (!c || generation !== mountGeneration) return;
  const live = () => generation === mountGeneration;
  void c.request<{ roles: Record<string, unknown> }>("/api/models/roles").then((payload) => {
    applyModelPillFromRoles(payload.roles);
    if (!live()) return;
    const text = modelRoleSummary("translation");
    const image = modelRoleSummary("image");
    setVal(slots.text, text.label, text.tone);
    setVal(slots.image, image.label, image.tone);
  }).catch(() => undefined);
  void (async () => {
    const pairs = await c.request<{ source_options: Array<{ code: string; display_name?: string }>; target_options: Array<{ code: string; display_name?: string }>; selected?: { source_lang?: string; target_lang?: string } }>("/api/tm/language-pairs");
    const source = pairs.selected?.source_lang || "zh";
    const target = pairs.selected?.target_lang || "en";
    const entries = await c.request<{ total?: number; stats?: { total?: number } }>(
      `/api/tm/entries?lang_pair=${encodeURIComponent(`${source}-${target}`)}&keyword=&page=1&page_size=1&entry_filter=all`,
    );
    if (!live()) return;
    const name = (options: Array<{ code: string; display_name?: string }>, code: string) => tx(options.find((o) => o.code === code)?.display_name ?? code);
    const total = Number(entries.stats?.total ?? entries.total ?? 0);
    setVal(slots.tm, t("{0} 条 · {1} → {2}", total, name(pairs.source_options, source), name(pairs.target_options, target)));
  })().catch(() => undefined);
  void c.request<Record<string, unknown>>("/api/settings").then((settings) => {
    if (!live()) return;
    // 各类型各有一份补充要求；都一样时写一次，不一样时列出用到的几个，全没选时写「无」。
    const names = [...new Set(["excel", "word", "cad"].map((key) => String(settings[`${key}_domain_preset`] ?? "").trim()).filter((v) => v && v !== "无"))]; // i18n-ignore 后端存的哨兵值
    setVal(slots.domain, names.length ? names.join(" · ") : t("无"));
  }).catch(() => undefined);
  void c.getCadCapability().then((cap) => {
    if (!live()) return;
    const oda = String(cap.oda ?? "");
    if (oda === "connected") setVal(slots.oda, t("已连接"), "ok");
    else setVal(slots.oda, t("未安装"), "idle");
  }).catch(() => undefined);
}

export function mount(container: HTMLElement, _params: ViewParams): void {
  const generation = ++mountGeneration;
  installDropGlow();
  setTopbar({ title: t("翻译"), subtitle: t("把文件交给 Translator，按类型自动进入对应工作区") });
  const page = el("div", "home");
  container.append(page);
  disposeListeners?.();
  const batchCard = createBatchCard();
  disposeListeners = batchCard.dispose;
  page.append(batchCard.node);

  let hero = buildHero(BUILTIN_TYPES);
  page.append(hero);
  const grid = el("div", "hgrid");
  const recent = el("div", "hcol");
  // 「正在加载…」不浮现，等列表到了再浮一次：两样都浮就是进页连闪两下。
  const rows = el("div", "rows");
  rows.append(el("div", "empty", t("正在加载…")));
  recent.append(sectionHead(t("最近"), t("全部任务"), () => navigate("tasks")), rows);
  const ready = el("div", "hcol");
  const readyRows = el("div", "rows ready stag");
  const slots = {
    text: readyRow(t("文本翻译模型")),
    image: readyRow(t("图像翻译模型")),
    tm: readyRow(t("记忆库")),
    domain: readyRow(t("补充要求")),
    oda: readyRow(t("图纸转换工具")),
  };
  Object.values(slots).forEach(({ row }, index) => {
    row.style.setProperty("--i", String(index + 4));
    readyRows.append(row);
  });
  ready.append(sectionHead(t("已就绪"), t("设置"), () => navigate("settings")), readyRows);
  grid.append(recent, ready);
  page.append(grid);
  void fillRecent(rows, generation);
  // 本批文件在这一页上开始、结束时，「最近」跟着换；进度事件本身不重取。
  const seen = new Map<Surface, string>();
  const offRecent = onWorkspaceChange((surface) => {
    const task = stateOf(surface).task?.task;
    const mark = task ? `${task.task_id}:${task.terminal}` : "";
    if ((seen.get(surface) ?? "") === mark) return;
    seen.set(surface, mark);
    if (task) void fillRecent(rows, generation);
  });
  const disposeCard = disposeListeners;
  disposeListeners = () => {
    disposeCard?.();
    offRecent();
  };
  void fillReady({ text: slots.text.val, image: slots.image.val, tm: slots.tm.val, domain: slots.domain.val, oda: slots.oda.val }, generation);

  // 先按内置四种画出来，拿到设置后按「扩展」页的显示设置换成实际的类型芯片。
  // 启动时落在这一页，类型描述符可能还没拉到：也要等它，不然 PPT 这类随应用提供的类型会漏掉。
  void Promise.all([
    getClient().then((c) => c.request<Record<string, unknown>>("/api/settings")),
    loadSurfaceDescriptors().catch(() => []),
  ])
    .then(([settings]) => {
      if (generation !== mountGeneration) return;
      const keys = visibleTypeKeys(settings) as Surface[];
      if (!keys.length || keys.join() === BUILTIN_TYPES.join()) return;
      const next = buildHero(keys);
      hero.replaceWith(next);
      hero = next;
    })
    .catch(() => undefined);
}

export function unmount(): void {
  mountGeneration += 1;
  disposeListeners?.();
  disposeListeners = null;
}
