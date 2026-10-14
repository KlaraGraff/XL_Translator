// 记忆库的三个开关：「翻译参数 · 记忆库」卡片和记忆库页右上角「设置」共用这一份
// 定义，两处改的都是 settings.tm 里的同一项。单独成文件，记忆库页不必引入设置页。

import { t } from "../i18n";

export interface TmSwitchDef {
  key: string;
  label: string;
  hint: string;
  fallback: boolean;
}

export function tmSwitchDefs(): TmSwitchDef[] {
  return [
    {
      key: "apply_in_sentences",
      label: t("在句中采用设定译法"),
      hint: t("开启后，采用方式为「始终采用」的词条在句中出现时同样采用其译法；关闭时仅在整段文字完全相同时采用。默认开启。"),
      fallback: true,
    },
    {
      key: "auto_save_short_scene",
      label: t("自动保存场景译法"),
      hint: t("开启后，「甲」这类短词出现在新场景中时，以及文中反复出现、已统一译法的词，将自动保存为新词条，并附上标明适用场景的备注；关闭时不自动保存。默认开启。"),
      fallback: true,
    },
    {
      key: "local_scene_candidates",
      label: t("本地模型也按场景选用"),
      hint: t("开启后，本地模型同样会收到「按场景选用」的候选译法；关闭时仅发送「始终采用」的词条。本地模型较易受候选译法干扰，默认关闭。"),
      fallback: false,
    },
  ];
}
