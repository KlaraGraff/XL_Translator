// 任务中心 · CAD「查看与修改译文」：译文对照表弹窗。
//
// 一张图纸一张表，同一段原文在图上出现多次时只列一行（写回时也是按原文整体替换）。
// 改好以后「按修改重新写回」：后端拿对照表代替模型重新生成译文图纸，不调用模型、不花钱；
// 改过的译文同时存进记忆库，下次翻到同样的文字直接用。也可以导出 Excel 交给别人改，
// 改好再导回来。数据和接口见 api/cad_review_service.py、core/cad_review.py。

import {
  ApiError,
  type ApiClient,
  type CadReviewFile,
  type CadReviewRow,
  type CadReviewSnapshot,
} from "../api-client";
import { createButton, createChip, createDropdown, openModal, showToast } from "../components";
import { t, tx } from "../i18n";
import { icon } from "../icons";
import { el } from "./workspace-util";
import "./cad-review.css";

type Filter = "all" | "unresolved" | "review" | "edited" | "crowded";

/** 一次最多画这么多行，其余点「显示更多」：上千段文字的总图一次铺开会卡。 */
const PAGE_ROWS = 200;
const POLL_MS = 1000;

function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError || error instanceof Error) return tx(error.message) || fallback;
  return fallback;
}

function readBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

/** WKWebView（macOS）还不认 CSS field-sizing，译文框高度按内容手动撑开。 */
function autosize(input: HTMLTextAreaElement): void {
  input.style.height = "auto";
  input.style.height = `${Math.max(32, input.scrollHeight + 2)}px`;
}

function pickWorkbook(onFile: (file: File) => void): void {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
  input.onchange = () => {
    const file = input.files?.[0];
    if (file) onFile(file);
  };
  input.click();
}

/** 打开「查看与修改译文」。title 是任务名，显示在标题上方。 */
export function openCadReview(client: ApiClient, taskId: string, title: string): void {
  let snapshot: CadReviewSnapshot | null = null;
  let fileKey = "";
  let filter: Filter = "all";
  let keyword = "";
  let shown = PAGE_ROWS;
  let pollTimer: number | null = null;
  let closed = false;
  let busy = false;
  // 点了「按修改重新写回」、还没报告结果；写回很快时第一次读回就已经结束了。
  let awaitingRewrite = false;
  // 上一次写回的结果，留在底栏上（提示条几秒就消失，容易错过）。
  let lastRewrite: { text: string; error: boolean } | null = null;
  // 还没保存的修改：图纸 key → 原文 → 当前输入框里的译文。
  const dirty = new Map<string, Map<string, string>>();

  const overlay = el("div", "overlay");
  const modal = el("div", "modal cad-review");
  const label = el("span", "mlabel");
  label.textContent = t("任务中心 · {0}", title);
  const heading = el("h3");
  heading.textContent = t("查看与修改译文");
  const intro = el("p", "cr-intro");
  intro.textContent = t("改好后点「按修改重新写回」，只把译文重新写进图纸，不调用模型、不产生费用。改过的译文会存进记忆库，下次翻译直接使用。");
  const toolbar = el("div", "cr-toolbar");
  const notice = el("div", "cr-notice");
  const tableWrap = el("div", "cr-table");
  const footer = el("div", "cr-footer");
  modal.append(label, heading, intro, toolbar, notice, tableWrap, footer);
  overlay.append(modal);
  document.body.append(overlay);

  const currentFile = (): CadReviewFile | undefined => snapshot?.files.find((file) => file.key === fileKey);
  const dirtyCount = (): number => [...dirty.values()].reduce((sum, edits) => sum + edits.size, 0);
  const rewriting = (): boolean => Boolean(snapshot?.rewrite.active);
  const editable = (): boolean => !rewriting() && !busy;

  function close(): void {
    closed = true;
    if (pollTimer !== null) window.clearTimeout(pollTimer);
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  }

  function requestClose(): void {
    const pending = dirtyCount();
    if (!pending) {
      close();
      return;
    }
    openModal({
      tone: "warn",
      icon: "warn",
      title: t("有 {0} 处修改还没保存", pending),
      body: [t("关闭后这些修改会丢失。")],
      actions: [
        { label: t("不保存"), onClick: () => close() },
        {
          label: t("保存并关闭"),
          variant: "primary",
          onClick: async () => {
            if (await saveDirty()) close();
          },
        },
      ],
    });
  }

  function onKey(event: KeyboardEvent): void {
    // 其他弹窗（未保存确认）叠在上面时不抢它的 Esc。
    const overlays = document.querySelectorAll(".overlay");
    if (event.key !== "Escape" || overlays[overlays.length - 1] !== overlay) return;
    requestClose();
  }
  document.addEventListener("keydown", onKey);

  // ── 数据 ─────────────────────────────────────────────

  async function load(options: { quiet?: boolean } = {}): Promise<void> {
    try {
      snapshot = await client.getCadReview(taskId);
    } catch (error) {
      if (closed) return;
      if (!snapshot) {
        tableWrap.innerHTML = "";
        const failed = el("div", "cr-empty");
        failed.textContent = errorText(error, t("读取译文对照表失败。"));
        tableWrap.append(failed);
        renderFooter();
        return;
      }
      if (!options.quiet) showToast({ message: errorText(error, t("读取译文对照表失败。")), error: true });
      return;
    }
    if (closed) return;
    if (!snapshot.files.some((file) => file.key === fileKey)) {
      fileKey = (snapshot.files.find((file) => file.available) ?? snapshot.files[0])?.key ?? "";
    }
    render();
    if (rewriting()) {
      schedulePoll();
    } else if (awaitingRewrite) {
      awaitingRewrite = false;
      reportRewrite();
    }
  }

  function schedulePoll(): void {
    if (pollTimer !== null) window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(async () => {
      pollTimer = null;
      await load({ quiet: true });
    }, POLL_MS);
  }

  function reportRewrite(): void {
    const results = snapshot?.rewrite.results ?? [];
    const failed = results.filter((item) => !item.ok);
    const done = results.length - failed.length;
    const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (!failed.length) {
      const message = t("已按修改重新写回 {0} 张图纸。", done);
      lastRewrite = { text: t("{0} 已重新写回 {1} 张图纸，输出文件已更新。", time, done), error: false };
      renderFooter();
      showToast({ message });
      return;
    }
    const first = failed[0];
    const message = t("{0} 张图纸没有写回：{1}", failed.length, `${first.name ? `${first.name}：` : ""}${tx(first.message)}`);
    lastRewrite = { text: `${time} ${message}`, error: true };
    renderFooter();
    showToast({ message, error: true, duration: 8000 });
  }

  async function saveDirty(): Promise<boolean> {
    if (!dirtyCount()) return true;
    busy = true;
    renderFooter();
    let remembered = 0;
    try {
      for (const [key, edits] of [...dirty.entries()]) {
        if (!edits.size) continue;
        const result = await client.saveCadReviewEdits(taskId, key, Object.fromEntries(edits));
        remembered += result.remembered;
        dirty.delete(key);
      }
    } catch (error) {
      showToast({ message: errorText(error, t("保存失败。")), error: true });
      return false;
    } finally {
      busy = false;
    }
    showToast({
      message: remembered ? t("修改已保存，{0} 条已存进记忆库。", remembered) : t("修改已保存。"),
    });
    await load({ quiet: true });
    return true;
  }

  async function rewrite(): Promise<void> {
    if (!(await saveDirty())) return;
    const keys = (snapshot?.files ?? []).filter((file) => file.rewritable && (file.edited ?? 0) > 0).map((file) => file.key);
    if (!keys.length) {
      const reason = (snapshot?.files ?? []).find((file) => (file.edited ?? 0) > 0 && file.reason)?.reason;
      showToast({ message: reason ? tx(reason) : t("没有需要重新写回的修改。"), error: Boolean(reason) });
      return;
    }
    busy = true;
    renderFooter();
    try {
      await client.rewriteCadReview(taskId, keys);
      awaitingRewrite = true;
      lastRewrite = null;
    } catch (error) {
      showToast({ message: errorText(error, t("重新写回失败。")), error: true });
    } finally {
      busy = false;
    }
    await load({ quiet: true });
  }

  async function exportWorkbook(): Promise<void> {
    try {
      const saved = await client.saveBinaryDownload(
        `/api/tasks/${taskId}/cad-review/export`,
        `${t("译文对照表")}.xlsx`, // i18n-ignore
      );
      if (saved) showToast({ message: t("已导出到 {0}", saved) });
    } catch (error) {
      showToast({ message: errorText(error, t("导出失败。")), error: true });
    }
  }

  function importWorkbook(): void {
    pickWorkbook(async (file) => {
      if (dirtyCount() && !(await saveDirty())) return;
      busy = true;
      renderFooter();
      try {
        const result = await client.importCadReview(taskId, await readBase64(file));
        showToast({
          message: result.rows
            ? t("已导入 {0} 处修改。", result.rows)
            : t("表格里的译文和现在的一样，没有需要导入的修改。"),
        });
      } catch (error) {
        showToast({ message: errorText(error, t("导入失败。")), error: true });
      } finally {
        busy = false;
      }
      await load({ quiet: true });
    });
  }

  // ── 画面 ─────────────────────────────────────────────

  function render(): void {
    renderToolbar();
    renderNotice();
    renderTable();
    renderFooter();
  }

  function renderToolbar(): void {
    toolbar.innerHTML = "";
    const files = snapshot?.files ?? [];
    if (files.length > 1) {
      const picker = createDropdown({
        className: "cr-file",
        value: fileKey,
        options: files.map((file) => {
          const count = (file.edited ?? 0) + (dirty.get(file.key)?.size ?? 0);
          return { value: file.key, label: count ? t("{0}（已修改 {1}）", file.name, count) : file.name };
        }),
        onChange: (value) => {
          fileKey = value;
          shown = PAGE_ROWS;
          render();
        },
      });
      toolbar.append(picker.root);
    }
    const searchWrap = el("div", "cr-search");
    const search = el("input");
    search.type = "text";
    search.placeholder = t("搜索原文或译文…");
    search.value = keyword;
    search.addEventListener("input", () => {
      keyword = search.value;
      shown = PAGE_ROWS;
      renderTable();
    });
    searchWrap.append(icon("search", { size: "sm" }), search);
    toolbar.append(searchWrap);
    const filterPicker = createDropdown({
      className: "cr-filter",
      value: filter,
      options: [
        { value: "all", label: t("全部") },
        { value: "unresolved", label: t("未译出") },
        { value: "review", label: t("需复核") },
        { value: "edited", label: t("已修改") },
        { value: "crowded", label: t("排版拥挤") },
      ],
      onChange: (value) => {
        filter = value as Filter;
        shown = PAGE_ROWS;
        renderTable();
      },
    });
    toolbar.append(filterPicker.root);
    const spacer = el("div", "cr-spacer");
    toolbar.append(
      spacer,
      createButton({ label: t("导出 Excel"), icon: "down", size: "mini", disabled: !files.length, onClick: () => void exportWorkbook() }),
      createButton({ label: t("导入 Excel"), icon: "plus", size: "mini", disabled: !files.length || !editable(), onClick: () => importWorkbook() }),
    );
  }

  function renderNotice(): void {
    notice.innerHTML = "";
    const file = currentFile();
    const message = rewriting()
      ? t("正在重新写回{0}（{1}/{2}），完成前不能修改。", snapshot?.rewrite.current ? ` ${snapshot.rewrite.current}` : "", snapshot?.rewrite.done ?? 0, snapshot?.rewrite.total ?? 0)
      : file && !file.rewritable && file.reason
        ? tx(file.reason)
        : "";
    notice.hidden = !message;
    if (message) {
      notice.append(icon(rewriting() ? "restart" : "warn", { size: "sm" }));
      const copy = el("span");
      copy.textContent = message;
      notice.append(copy);
    }
  }

  function matches(row: CadReviewRow, pending: string | undefined): boolean {
    const translation = pending ?? row.translation;
    if (filter === "unresolved" && !(row.status === "unresolved" && !translation)) return false;
    if (filter === "review" && !(row.status === "review" && !row.edited && pending === undefined)) return false;
    if (filter === "edited" && !(row.edited || pending !== undefined)) return false;
    if (filter === "crowded" && !row.crowded) return false;
    if (!keyword.trim()) return true;
    const needle = keyword.trim().toLowerCase();
    return row.source.toLowerCase().includes(needle) || translation.toLowerCase().includes(needle);
  }

  function statusChip(row: CadReviewRow, pending: string | undefined): HTMLElement {
    if (pending !== undefined || row.edited) return createChip({ label: t("已修改"), tone: "tint" });
    if (row.status === "unresolved") return createChip({ label: t("未译出"), tone: "warn" });
    if (row.status === "kept") return createChip({ label: t("保留原文"), tone: "mute" });
    if (row.status === "review") {
      const chip = createChip({ label: t("需复核"), tone: "warn" });
      if (row.reason) chip.title = tx(row.reason);
      return chip;
    }
    return createChip({ label: t("已翻译"), tone: "ok" });
  }

  function renderTable(): void {
    tableWrap.innerHTML = "";
    const file = currentFile();
    if (!snapshot) {
      const loading = el("div", "cr-empty");
      loading.textContent = t("加载中…");
      tableWrap.append(loading);
      return;
    }
    if (!file || !file.available) {
      const empty = el("div", "cr-empty");
      empty.textContent = file ? tx(file.reason) : t("这次任务没有译好的图纸。");
      tableWrap.append(empty);
      return;
    }
    const edits = dirty.get(file.key);
    const rows = file.rows.filter((row) => matches(row, edits?.get(row.source)));
    if (!rows.length) {
      const empty = el("div", "cr-empty");
      empty.textContent = t("没有符合条件的文字。");
      tableWrap.append(empty);
      return;
    }
    const table = el("table", "tbl");
    const head = el("tr");
    for (const [text, className] of [
      [t("原文"), "cr-src"],
      [t("译文"), "cr-dst"],
      [t("状态"), "cr-st"],
      [t("次数"), "cr-n"],
    ]) {
      const th = el("th", className);
      th.textContent = text;
      head.append(th);
    }
    table.append(head);
    for (const row of rows.slice(0, shown)) table.append(renderRow(file, row));
    tableWrap.append(table);
    requestAnimationFrame(() => table.querySelectorAll("textarea").forEach(autosize));
    if (rows.length > shown) {
      const more = createButton({
        label: t("显示更多（还有 {0} 条）", rows.length - shown),
        size: "mini",
        onClick: () => {
          shown += PAGE_ROWS;
          renderTable();
        },
      });
      more.classList.add("cr-more");
      tableWrap.append(more);
    }
  }

  function renderRow(file: CadReviewFile, row: CadReviewRow): HTMLTableRowElement {
    const tr = el("tr");
    if (row.crowded) tr.classList.add("cr-crowded");
    const source = el("td", "cr-src");
    source.textContent = row.source;
    const target = el("td", "cr-dst");
    const input = el("textarea");
    input.rows = 1;
    input.value = dirty.get(file.key)?.get(row.source) ?? row.translation;
    input.placeholder = row.status === "kept" ? t("保留原文") : t("填写译文");
    input.disabled = !editable();
    const status = el("td", "cr-st");
    const crowdedHint = row.crowded ? t("这段译文缩到最小比例仍会压到旁边的文字或伸出图框，可以改得更短。") : "";
    const paintStatus = () => {
      status.innerHTML = "";
      status.append(statusChip(row, dirty.get(file.key)?.get(row.source)));
      if (row.crowded) {
        const crowded = createChip({ label: t("排版拥挤"), tone: "warn" });
        crowded.title = crowdedHint;
        status.append(crowded);
      }
    };
    input.addEventListener("input", () => {
      autosize(input);
      const value = input.value;
      let edits = dirty.get(file.key);
      if (value === row.translation) {
        edits?.delete(row.source);
      } else {
        if (!edits) dirty.set(file.key, (edits = new Map()));
        // 清空就是改回第一次的译文（后端按空值处理）。
        edits.set(row.source, value);
      }
      paintStatus();
      renderFooter();
    });
    paintStatus();
    target.append(input);
    const count = el("td", "cr-n num");
    count.textContent = String(row.count);
    tr.append(source, target, status, count);
    return tr;
  }

  function renderFooter(): void {
    footer.innerHTML = "";
    const pending = dirtyCount();
    const saved = (snapshot?.files ?? []).reduce((sum, file) => sum + (file.rewritable ? file.edited ?? 0 : 0), 0);
    const summary = el("span", "cr-sum");
    const result = !pending && !saved && !rewriting() ? lastRewrite : null;
    if (result) summary.classList.add(result.error ? "error" : "ok");
    summary.textContent = pending
      ? t("{0} 处修改还没保存", pending)
      : saved
        ? t("{0} 处修改待写回图纸", saved)
        : result
          ? result.text
          : t("改完译文后先保存，再重新写回图纸。");
    footer.append(summary);
    footer.append(
      createButton({ label: t("关闭"), onClick: () => requestClose() }),
      createButton({
        label: t("保存修改"),
        disabled: !pending || !editable(),
        onClick: () => void saveDirty(),
      }),
      createButton({
        label: t("按修改重新写回"),
        variant: "primary",
        icon: "restart",
        disabled: !editable() || !snapshot?.terminal || (!pending && !saved),
        title: snapshot && !snapshot.terminal ? t("任务结束后才能重新写回。") : undefined,
        onClick: () => void rewrite(),
      }),
    );
  }

  render();
  void load();
}
