// 「扩展」页：侧栏「翻译」组里有哪些翻译类型、按什么顺序。
//
// 内置四种和运行时注册的类型（格式适配器，键为 `ext:<id>`）在同一张表里，都能隐藏、都能调序，
// 存在 settings.translation_types（v40）。隐藏只是不放进侧栏：页面照常注册，任务中心「打开
// 工作区」、翻译参数里它的页签都照旧。至少要留一种显示在侧栏，后端也拦。
// 「插件」页（运行组件）是另一页，与这里无关。

import { ApiClient } from "../api-client";
import { clearElement, createButton, createCard, createHintBadge, createSwitch, showToast } from "../components";
import { icon, type IconName } from "../icons";
import { t } from "../i18n";
import type { ViewId, ViewParams } from "../router";
import { builtinTranslateSideItem, setTopbar } from "../shell";
import {
  extSurfaceIcon,
  extSurfaceLabel,
  isExtSurface,
  loadSurfaceDescriptors,
  orderedTypeKeys,
  refreshTranslateSideItems,
  surfaceDescriptor,
  translationTypesLayout,
  visibleTypeKeys,
  type TranslationTypesLayout,
} from "../surface-descriptors";
import "./extensions-page.css";

let mounted = false;
let rootEl: HTMLElement | null = null;
let settings: Record<string, unknown> = {};
let busy = false;

export function mount(container: HTMLElement, _params: ViewParams): void {
  mounted = true;
  rootEl = container;
  render();
  void refresh();
}

export function unmount(): void {
  mounted = false;
  rootEl = null;
}

async function refresh(): Promise<void> {
  try {
    const client = new ApiClient();
    await client.connect();
    const [, saved] = await Promise.all([
      loadSurfaceDescriptors().catch(() => []),
      client.request<Record<string, unknown>>("/api/settings"),
    ]);
    settings = saved;
  } catch {
    // 拿不到设置时按默认顺序画内置四种；侧栏照旧，不打扰用户。
  }
  if (mounted) render();
}

/** 写 settings.translation_types；成功后按新设置重排侧栏。 */
async function saveLayout(layout: TranslationTypesLayout): Promise<void> {
  busy = true;
  render();
  try {
    const client = new ApiClient();
    await client.connect();
    settings = await client.request<Record<string, unknown>>("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ translation_types: layout }),
    });
    refreshTranslateSideItems(settings);
  } catch (error) {
    showToast({ message: error instanceof Error ? t("保存失败：{0}", error.message) : t("保存失败。"), error: true });
  } finally {
    busy = false;
    if (mounted) render();
  }
}

function setVisible(key: string, visible: boolean): void {
  if (busy) return;
  const layout = translationTypesLayout(settings);
  if (!visible && visibleTypeKeys(settings).every((item) => item === key)) {
    showToast({ message: t("至少保留一种翻译类型显示在侧栏。"), error: true });
    render();
    return;
  }
  const hidden = layout.hidden.filter((item) => item !== key);
  if (!visible) hidden.push(key);
  void saveLayout({ order: layout.order, hidden });
}

/** 上移 / 下移一位：整张表按新顺序写回；表里没有的旧键（已卸载的类型）留在末尾。 */
function move(key: string, step: -1 | 1): void {
  if (busy) return;
  const keys = orderedTypeKeys(settings);
  const from = keys.indexOf(key);
  const to = from + step;
  if (from < 0 || to < 0 || to >= keys.length) return;
  [keys[from], keys[to]] = [keys[to], keys[from]];
  const layout = translationTypesLayout(settings);
  void saveLayout({ order: [...keys, ...layout.order.filter((item) => !keys.includes(item))], hidden: layout.hidden });
}

function typeName(key: string): { label: string; icon: IconName } {
  if (isExtSurface(key)) return { label: extSurfaceLabel(key), icon: extSurfaceIcon(key) };
  const item = builtinTranslateSideItem(key as ViewId);
  return { label: item ? item.label() : key, icon: item?.icon ?? "doc-file" };
}

function typeSource(key: string): string {
  const adapter = surfaceDescriptor(key)?.adapter;
  if (!isExtSurface(key) || !adapter) return t("内置");
  return t("随应用提供") + (adapter.version ? ` · ${adapter.version}` : "");
}

function typeRow(key: string, index: number, count: number, visibleCount: number): HTMLDivElement {
  const { label, icon: iconName } = typeName(key);
  const row = document.createElement("div");
  row.className = "types-row";
  row.dataset.surface = key;

  const iconBox = document.createElement("div");
  iconBox.className = "types-icon";
  iconBox.append(icon(iconName));

  const name = document.createElement("div");
  name.className = "types-name";
  const title = document.createElement("b");
  title.textContent = label;
  name.append(title);
  const extensions = (surfaceDescriptor(key)?.extensions ?? []).map((ext) => `.${ext}`).join(" ");
  if (extensions) {
    const exts = document.createElement("small");
    exts.textContent = extensions;
    name.append(exts);
  }

  const source = document.createElement("div");
  source.className = "types-source";
  source.textContent = typeSource(key);

  const hidden = translationTypesLayout(settings).hidden.includes(key);
  const sidebar = createSwitch({
    checked: !hidden,
    // 最后一个显示着的不能关：开关照常可点，点了提示原因（比灰掉更说得清）。
    disabled: busy,
    onChange: (checked) => setVisible(key, checked),
  });
  sidebar.setAttribute("aria-label", t("显示在侧栏"));
  if (!hidden && visibleCount === 1) sidebar.title = t("至少保留一种翻译类型显示在侧栏。");

  const order = document.createElement("div");
  order.className = "types-order";
  const up = createButton({ icon: "chev", size: "mini", title: t("上移"), disabled: busy || index === 0, onClick: () => move(key, -1) });
  up.classList.add("up");
  up.setAttribute("aria-label", t("上移"));
  const down = createButton({ icon: "chev", size: "mini", title: t("下移"), disabled: busy || index === count - 1, onClick: () => move(key, 1) });
  down.setAttribute("aria-label", t("下移"));
  order.append(up, down);

  row.append(iconBox, name, source, sidebar, order);
  return row;
}

function render(): void {
  if (!mounted || !rootEl) return;
  setTopbar({ title: t("扩展"), subtitle: t("设置侧栏里显示哪些翻译类型，以及它们的顺序") });
  rootEl.className = "content extensions-content";
  clearElement(rootEl);

  const column = document.createElement("div");
  column.className = "extensions-column";
  const card = createCard([]);
  const table = document.createElement("div");
  table.className = "types-table";
  const header = document.createElement("div");
  header.className = "types-row head";
  for (const text of ["", t("翻译类型"), t("来源"), t("显示在侧栏"), t("顺序")]) {
    const cell = document.createElement("div");
    cell.textContent = text;
    header.append(cell);
  }
  header.children[3]?.append(createHintBadge(t("关闭后不在侧栏显示，已有任务仍可在任务中心打开，翻译参数照常保留。")));
  table.append(header);

  const keys = orderedTypeKeys(settings);
  const visibleCount = visibleTypeKeys(settings).length;
  keys.forEach((key, index) => table.append(typeRow(key, index, keys.length, visibleCount)));
  card.append(table);
  column.append(card);
  rootEl.append(column);
}
