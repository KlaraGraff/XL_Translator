/**
 * 工作台：「输出位置」区（输出到哪里、文件名怎么起）和它的设置保存。
 */
import { createButton, createField, createHintBadge, createSegmented, createTextField, openModal, showToast } from "../components";
import { t } from "../i18n";
import { showPathWithMiddleEllipsis } from "../path-input";
import { isExtSurface } from "../surface-descriptors";
import { fitPlaceholder } from "../text-fit";
import { el, nestedPatch, redactedText, text, type JsonObject } from "./workspace-util";
import { open } from "@tauri-apps/plugin-dialog";
import { type Surface, type SurfaceState, extSettingsPatch, outputRecord, outputSettingPathPrefix, persistSettingsOrRevert, rerender } from "./workspace";

/** 写输出位置的一项（use_custom_output_dir / custom_output_dir）的补丁。 */
function outputSettingPatch(surface: Surface, name: string, value: unknown): JsonObject {
  return isExtSurface(surface) ? extSettingsPatch(surface, name, value) : nestedPatch(`${outputSettingPathPrefix(surface)}.${name}`, value);
}

/** 输出位置：先选译文放在单独任务文件夹还是源文件夹，再选单独任务文件夹的首选根目录。
 * 首选位置失效时，备用根目录和桌面由后端自动接管；备用位置对两种译文存放方式都生效。 */
export function buildOutputLocation(surface: Surface, st: SurfaceState, active: boolean): HTMLElement {
  const container = el("div");
  const outputMode = createSegmented({
    options: [
      { value: "task_folder", label: t("单独的翻译文件夹") },
      { value: "source_folder", label: t("直接放回源文件夹") },
    ],
    value: st.outputMode,
    disabled: active,
    ariaLabel: t("译文存放"),
    onChange: (choice) => {
      const value = choice as SurfaceState["outputMode"];
      const apply = (): void => {
        const prev = st.outputMode;
        st.outputMode = value;
        rerender(surface);
        void persistSettingsOrRevert(
          surface,
          outputSettingPatch(surface, "output_mode", value),
          () => { st.outputMode = prev; },
          t("译文存放"),
        );
      };
      if (value === "source_folder" && st.outputMode !== value) {
        openModal({
          tone: "warn",
          icon: "warn",
          title: t("确认直接放回源文件夹？"),
          body: [
            t("译文会和源文件放在一起，源文件不会被覆盖。"),
            t("源文件夹无法写入时，仍会使用备用输出文件夹，最后退回桌面。"),
          ],
          actions: [
            { label: t("取消") },
            { label: t("继续使用"), variant: "primary", onClick: apply },
          ],
        });
      } else {
        apply();
      }
    },
  });
  const outputField = createField(t("译文存放"), outputMode);
  outputField.querySelector("label")?.append(createHintBadge(t("选择单独的翻译文件夹，或把译文放回源文件夹；源文件始终不会被覆盖。")));
  container.append(outputField);

  const preferred = createSegmented({
    options: [
      { value: "source", label: t("源文件旁") },
      { value: "custom", label: t("指定位置") },
    ],
    value: st.useCustomOutputDir ? "custom" : "source",
    disabled: active || st.outputMode === "source_folder",
    ariaLabel: t("首选位置"),
    onChange: (choice) => {
      const value = choice === "custom";
      const prev = st.useCustomOutputDir;
      st.useCustomOutputDir = value;
      rerender(surface);
      void persistSettingsOrRevert(
        surface,
        outputSettingPatch(surface, "use_custom_output_dir", value),
        () => { st.useCustomOutputDir = prev; },
        t("首选位置"),
      );
    },
  });
  const preferredField = createField(t("首选位置"), preferred);
  preferredField.querySelector("label")?.append(createHintBadge(t("单独的翻译文件夹会建在源文件旁，或建在你指定的位置。直接放回源文件夹时，这项不参与选择。")));
  container.append(preferredField);

  if (st.outputMode === "source_folder") {
    const note = el("p", "rp-note");
    note.textContent = t("当前会直接放回源文件夹；写入失败时使用下面的备用输出文件夹，最后退回桌面。");
    container.append(note);
  } else if (st.useCustomOutputDir) {
    const { root, input } = createTextField({
      label: "",
      value: st.customOutputDir,
      placeholder: t("自定义输出目录路径…"),
      disabled: active,
      onInput: (value) => {
        st.customOutputDir = value;
      },
    });
    const saveCustomDir = (): void => {
      // 落盘前的旧值要从「上一次成功落盘的值」取，不能取 st.customOutputDir 自己——
      // 打字过程中每敲一下 onInput 都已经把它改成了正在输入的新值，到这里已经晚了。
      // outputRecord() 读的是 settings 模块变量，只在 persistSettings 成功时才会更新，
      // 是这一刻唯一还留着「磁盘上真实值」的地方。
      const prevDir = text(outputRecord(surface).custom_output_dir);
      void persistSettingsOrRevert(
        surface,
        outputSettingPatch(surface, "custom_output_dir", st.customOutputDir),
        () => { st.customOutputDir = prevDir; },
        t("自定义输出目录"),
      );
    };
    input.addEventListener("change", saveCustomDir);
    const relayoutDir = showPathWithMiddleEllipsis(input, () => st.customOutputDir);
    // 输入框后面跟一个「浏览」，在系统选择框里挑文件夹，不必手动粘贴路径。
    const browseBtn = createButton({
      label: t("浏览"),
      icon: "folder",
      size: "mini",
      disabled: active,
      onClick: () => void (async () => {
        let picked: unknown;
        try {
          picked = await open({ title: t("选择输出文件夹"), directory: true, multiple: false, defaultPath: st.customOutputDir.trim() || undefined });
        } catch (error) {
          showToast({ message: redactedText((error as Error)?.message, t("选择输出文件夹失败。")), error: true });
          return;
        }
        if (typeof picked !== "string" || !picked.trim()) return;
        st.customOutputDir = picked;
        input.value = picked;
        relayoutDir();
        saveCustomDir();
      })(),
    });
    root.classList.add("out-dir-input");
    const line = el("div", "out-dir");
    line.append(root, browseBtn);
    container.append(line);
  }

  const fallbackInput = createTextField({
    label: "",
    value: st.fallbackOutputDir,
    placeholder: t("未设置，失败时使用桌面"),
    disabled: active,
    onInput: (value) => { st.fallbackOutputDir = value; },
  });
  fitPlaceholder(fallbackInput.input, t("未设置，失败时使用桌面"), t("未设置"));
  const saveFallbackDir = (): void => {
    const prevDir = text(outputRecord(surface).fallback_output_dir);
    void persistSettingsOrRevert(
      surface,
      outputSettingPatch(surface, "fallback_output_dir", st.fallbackOutputDir),
      () => { st.fallbackOutputDir = prevDir; },
      t("备用输出文件夹"),
    );
  };
  fallbackInput.input.addEventListener("change", saveFallbackDir);
  const relayoutFallback = showPathWithMiddleEllipsis(fallbackInput.input, () => st.fallbackOutputDir);
  const browseFallback = createButton({
    label: t("选择"),
    icon: "folder",
    size: "mini",
    disabled: active,
    onClick: () => void (async () => {
      let picked: unknown;
      try {
        picked = await open({ title: t("选择备用输出文件夹"), directory: true, multiple: false, defaultPath: st.fallbackOutputDir.trim() || undefined });
      } catch (error) {
        showToast({ message: redactedText((error as Error)?.message, t("选择备用输出文件夹失败。")), error: true });
        return;
      }
      if (typeof picked !== "string" || !picked.trim()) return;
      st.fallbackOutputDir = picked;
      fallbackInput.input.value = picked;
      relayoutFallback();
      saveFallbackDir();
    })(),
  });
  // 「.hint」是「?」角标的类名（15px 圆圈），不能拿来装整句文字，否则会被挤成一字一行。
  // 备用文件夹和上面两项一样走 .field（名称 + 角标），说明小字用右栏统一的 .rp-note。
  fallbackInput.root.classList.add("out-dir-input");
  fallbackInput.input.setAttribute("aria-label", t("备用输出文件夹"));
  const fallbackLine = el("div", "out-dir");
  fallbackLine.append(fallbackInput.root, browseFallback);
  const fallback = createField(t("备用输出文件夹"), fallbackLine);
  fallback.classList.add("out-dir-fallback");
  fallback.querySelector("label")?.append(createHintBadge(t("首选位置无法创建或写入时使用；如果这里也不可用，程序会自动退回桌面。")));
  container.append(fallback);

  const metadataHint = el("p", "rp-note");
  metadataHint.textContent = t("翻译参数会自动写入输出文件属性，方便以后直接识别和继续翻译。");
  container.append(metadataHint);
  return container;
}
