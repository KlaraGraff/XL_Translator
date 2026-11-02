/**
 * 工作台：Word 文件里有未接受的修订时，开始前问怎么处理的弹窗。
 */
import { createButton, openModal, showToast } from "../components";
import { fmtNum, t } from "../i18n";
import { icon } from "../icons";
import { el, num, redactedText } from "./workspace-util";
import { invoke } from "@tauri-apps/api/core";
import { type FileItem, type RevisionMode, type SurfaceState, fileLabel, submitAfterResumeCheck } from "./workspace";

/** 文件名列表接成一句话：「合同.docx」12 处，「补充协议.docx」3 处。 */
function revisionCountPhrase(entries: Array<{ file: FileItem; count: number }>): string {
  return entries
    .map((entry) => t("「{0}」{1} 处", fileLabel(entry.file), fmtNum(entry.count)))
    .join("，");
}

export function showRevisionModal(st: SurfaceState, entries: Array<{ file: FileItem; count: number }>): void {
  const single = entries.length === 1;
  const title = single
    ? t("「{0}」里有 {1} 处未接受的修订", fileLabel(entries[0].file), fmtNum(entries[0].count))
    : t("本批有 {0} 个文件含未接受的修订", entries.length);
  const otherSelected = st.files.filter(
    (file) => st.selected.has(file.path) && num(file.tracked_change_count) === 0,
  ).length;
  const intro = single
    ? t("修订就是在 Word 里开着「修订」功能时做的改动（新增、删除的文字）。这些文字要怎么处理？")
    : otherSelected > 0
      ? t("{0}。其余 {1} 个文件没有修订，会正常翻译。", revisionCountPhrase(entries), otherSelected)
      : revisionCountPhrase(entries);

  const options: Array<{ value: RevisionMode | "resolve"; label: string; hint: string }> = [
    {
      value: "resolve",
      label: t("先处理修订，再翻译"),
      hint: single
        ? t("这次先不翻译。请在 Word 里接受或拒绝修订、保存后，回来重新扫描。最干净。")
        : t("这次全部先不开始。处理完修订后重新扫描。"),
    },
    {
      value: "translate",
      label: t("修订一起翻译"),
      hint: single
        ? t("新增和删除的文字都翻译，并保留修订标记，对方打开译文仍能看到改了哪些地方。")
        : t("新增和删除的文字都翻译，保留修订标记。"),
    },
    {
      value: "skip",
      label: t("跳过修订，只翻译其余部分"),
      hint: single
        ? t("修订里的文字保持原文不动，其余部分正常翻译。（默认）")
        : t("修订里的文字保持原文不动。（默认）"),
    },
  ];

  let chosen: RevisionMode | "resolve" = "skip";
  const overlay = el("div", "overlay");
  const modal = el("div", "modal word-revision");
  const mark = el("div", "mi warn");
  mark.append(icon("warn"));
  const source = el("span", "mlabel");
  source.textContent = t("Word 文档 · 开始前核对");
  const heading = el("h3");
  heading.textContent = title;
  const body = el("div", "mbody");
  const introLine = el("p");
  introLine.textContent = intro;
  const list = el("div", "rev-options");
  const rows: HTMLButtonElement[] = [];
  for (const option of options) {
    const row = el("button", "rev-option") as HTMLButtonElement;
    row.type = "button";
    row.setAttribute("role", "radio");
    row.setAttribute("aria-checked", String(option.value === "skip"));
    const dot = el("span", "rev-dot");
    const copy = el("div", "rev-copy");
    const name = el("b");
    name.textContent = option.label;
    const hint = el("small");
    hint.textContent = option.hint;
    copy.append(name, hint);
    row.append(dot, copy);
    if (option.value === "skip") row.classList.add("sel");
    row.addEventListener("click", () => {
      chosen = option.value;
      for (const item of rows) {
        item.classList.toggle("sel", item === row);
        item.setAttribute("aria-checked", String(item === row));
      }
      primary.textContent = chosen === "resolve" ? t("知道了") : t("开始翻译");
    });
    rows.push(row);
    list.append(row);
  }

  body.append(introLine, list);

  const acts = el("div", "macts");
  const close = () => overlay.remove();
  const cancel = createButton({ label: t("取消"), onClick: close });
  const primary = createButton({
    label: t("开始翻译"),
    variant: "primary",
    onClick: () => {
      if (chosen === "resolve") {
        close();
        showRevisionResolveModal(entries);
        return;
      }
      st.revisionMode = chosen;
      close();
      void submitAfterResumeCheck("word", st);
    },
  });
  acts.append(cancel, primary);
  modal.append(mark, source, heading, body, acts);
  overlay.append(modal);
  document.body.append(overlay);
}

/** 样张③：选了「先处理修订」之后那一步。这里已经不再启动翻译了。 */
function showRevisionResolveModal(entries: Array<{ file: FileItem; count: number }>): void {
  const single = entries.length === 1;
  const name = single ? fileLabel(entries[0].file) : t("这批文件");
  openModal({
    tone: "tint",
    icon: "folder",
    title: t("好的，先处理修订"),
    body: [
      single
        ? t("请用 Word 打开「{0}」，在「审阅」里接受或拒绝全部修订并保存。处理完回到这里点「重新扫描」，就可以开始翻译了。", name)
        : t("请用 Word 打开这批文件，在「审阅」里接受或拒绝全部修订并保存。处理完回到这里点「重新扫描」，就可以开始翻译了。"),
    ],
    actions: [
      {
        label: t("在访达中显示"),
        onClick: () => revealSourceInFinder(entries),
      },
      // 「知道了」= 主按钮；点完什么也不启动，用户自己回 Word 处理。
      { label: t("知道了"), variant: "primary" },
    ],
  });
}

/** 「在访达中显示」：macOS 是访达，其他平台由壳自己换成文件管理器。
 *  多个文件时按所在文件夹归拢：同一个文件夹里只有一个要处理的文件就选中它，有好几个
 *  就打开那个文件夹——不能只显示第一个，其余文件让用户自己找。 */
function revealSourceInFinder(entries: Array<{ file: FileItem; count: number }>): void {
  const byFolder = new Map<string, string[]>();
  for (const entry of entries) {
    const path = entry.file.path;
    const cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
    const folder = cut > 0 ? path.slice(0, cut) : path;
    byFolder.set(folder, [...(byFolder.get(folder) ?? []), path]);
  }
  if (!byFolder.size) return;
  void (async () => {
    try {
      for (const [folder, paths] of byFolder) {
        if (paths.length === 1) await invoke("open_local_path", { path: paths[0], reveal: true });
        else await invoke("open_local_path", { path: folder, reveal: false });
      }
    } catch (error) {
      showToast({ message: redactedText((error as Error)?.message, t("打开路径失败。")), error: true });
    }
  })();
}
