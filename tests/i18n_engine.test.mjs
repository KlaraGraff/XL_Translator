// node tests/i18n_engine.test.mjs —— 词条匹配引擎（ui/src/i18n/engine.ts）的单测。
import assert from "node:assert/strict";
import { createTranslator } from "../ui/src/i18n/engine.ts";

const catalog = {
  "开始翻译": { en: "Start translation", fr: "Lancer la traduction" },
  "{0} 个任务运行中": { en: "{0} {0|task|tasks} running", fr: "{0} {0|tâche|tâches} en cours" },
  "扫描到 {0} 个文件": { en: "Found {0} {0|file|files}", fr: "{0} {0|fichier trouvé|fichiers trouvés}" },
  "Excel 没能启动：{0}": { en: "Excel could not start: {0}", fr: "Excel n'a pas pu démarrer : {0}" },
  "没有找到 Excel": { en: "Excel was not found", fr: "Excel est introuvable" },
  "第 {0} 页，共 {1} 页": { en: "Page {0} of {1}", fr: "Page {0} sur {1}" },
  "{1} 里有 {0} 处": { en: "{0} in {1}", fr: "{0} dans {1}" },
  "{0}：{1}": { en: "{0}: {1}", fr: "{0} : {1}" },
  "读取文件": { en: "Reading files", fr: "Lecture des fichiers" },
  "已停止。": { en: "Stopped.", fr: "Arrêtée." },
  "原件不会被改动": { en: "The original is left unchanged", fr: "L'original n'est pas modifié" },
  "{0}": { en: "nope {0}", fr: "nope {0}" },
  "这 {0} 条保留原文": { en: "{0|This item keeps|These # items keep} the source text", fr: "{0|Cet élément conserve|Ces # éléments conservent} le texte source" },
};

const zh = createTranslator("zh", [catalog]);
assert.equal(zh.t("开始翻译"), "开始翻译");
assert.equal(zh.t("{0} 个任务运行中", 3), "3 个任务运行中");
assert.equal(zh.tx("扫描到 3 个文件"), "扫描到 3 个文件");

const en = createTranslator("en", [catalog]);
assert.equal(en.t("开始翻译"), "Start translation");
assert.equal(en.t("{0} 个任务运行中", 1), "1 task running");
assert.equal(en.t("{0} 个任务运行中", 4), "4 tasks running");
assert.equal(en.t("没有词条的中文"), "没有词条的中文", "missing entries fall back to Chinese");
assert.equal(en.tx("扫描到 12 个文件"), "Found 12 files");
assert.equal(en.tx("Excel 没能启动：没有找到 Excel"), "Excel could not start: Excel was not found", "captures are translated recursively");
assert.equal(en.tx("Excel 没能启动：某个未知原因"), "Excel could not start: 某个未知原因");
assert.equal(en.tx("第 3 页，共 10 页"), "Page 3 of 10");
assert.equal(en.tx("{1} 里有 {0} 处".replace("{1}", "a.xlsx").replace("{0}", "5")), "5 in a.xlsx", "placeholders may be reordered");
assert.equal(en.tx("读取文件：没有找到 Excel"), "Reading files: Excel was not found", "generic template still works");
assert.equal(en.tx("  读取文件 "), "  Reading files ", "surrounding whitespace is kept");
assert.equal(en.tx("读取文件\n没有找到 Excel"), "Reading files\nExcel was not found", "multi-line messages are split");
assert.equal(en.tx("已停止。原件不会被改动。"), "Stopped. The original is left unchanged.", "sentences are split");
assert.equal(en.tx("Already English"), "Already English");
assert.equal(en.tx(null), "");
assert.equal(en.tx("完全不认识"), "完全不认识", "a bare {0} template never matches everything");

const fr = createTranslator("fr", [catalog]);
assert.equal(fr.t("{0} 个任务运行中", 0), "0 tâche en cours", "French treats 0 as singular");
assert.equal(fr.t("{0} 个任务运行中", 2), "2 tâches en cours");
assert.equal(fr.tx("扫描到 1 个文件"), "1 fichier trouvé");
assert.equal(fr.t("这 {0} 条保留原文", 3), "Ces 3 éléments conservent le texte source", "# in a plural branch is the number");
assert.equal(fr.tx("这 1 条保留原文"), "Cet élément conserve le texte source");
assert.equal(en.tx("这 12 条保留原文"), "These 12 items keep the source text");
assert.equal(en.isSingular(1), true);
assert.equal(en.isSingular(0), false);
assert.equal(fr.isSingular(0), true);
assert.equal(fr.isSingular(2), false);
assert.equal(fr.tx("Excel 没能启动：没有找到 Excel"), "Excel n'a pas pu démarrer : Excel est introuvable");

console.log("i18n engine tests passed");

const prefixed = createTranslator("en", [{
  "第 {0} 页 ": { en: "Page {0} — " },
  "{0}第 {1}/{2} 次失败": { en: "{0}attempt {1}/{2} failed" },
}]);
assert.equal(prefixed.tx("第 3 页 第 2/3 次失败"), "Page 3 — attempt 2/3 failed", "a leading placeholder takes the longest prefix, trailing-space keys still match");
console.log("i18n engine prefix tests passed");

const sentences = createTranslator("en", [{
  "图片翻译失败；已保留源图、页面素材和报告。": { en: "Image translation failed; the source image, page assets and report were kept." },
  "首个错误：{0}": { en: "First error: {0}" },
  "{0}、{1}": { en: "{0}, {1}" },
}]);
assert.equal(
  sentences.tx("图片翻译失败；已保留源图、页面素材和报告。首个错误：超时"),
  "Image translation failed; the source image, page assets and report were kept. First error: 超时",
  "whole sentences win over splitting at semicolons or list commas",
);
const joined = createTranslator("en", [{
  "有 {0} 条内容未能获得译文。请检查配置后重试。": { en: "{0} {0|item|items} got no translation. Check the settings and try again." },
  "有 {0} 条译文需复核。": { en: "{0} {0|translation needs|translations need} review." },
  "{0}、{1}": { en: "{0}, {1}" },
}]);
assert.equal(
  joined.tx("有 13 条内容未能获得译文。请检查配置后重试。；有 3 条译文需复核。"),
  "13 items got no translation. Check the settings and try again. 3 translations need review.",
  "messages joined with a semicolon are translated one by one",
);
console.log("i18n engine sentence tests passed");

const status = createTranslator("fr", [{
  "状态：[阶段 2/{0}] 正在补译遗留的原文（{1}/{2}）...": { fr: "État : [Étape 2/{0}] complétion du texte non traduit ({1}/{2})…" },
}]);
assert.equal(status.tx("[阶段 2/3] 正在补译遗留的原文（13/13）..."), "[Étape 2/3] complétion du texte non traduit (13/13)…", "status lines match without their prefix");
assert.equal(status.tx("正在补译遗留的原文（13/13）..."), "Complétion du texte non traduit (13/13)…", "status lines match without their stage mark");
console.log("i18n engine status tests passed");
