# 界面英法译名对照（Glossary）

界面英文、法文文案的统一叫法。新增词条时先查这里，同一个中文说法在各处只用一个译法。
中文以 PR #10 统一后的用词为准。

## 写法约定

- 英文：按钮、标签用句首大写（Sentence case），如 `Test connection`，不要每个词大写。
- 法文：`: ; ? !` 前用不换行空格（U+00A0），如 `Langue cible :`；引号用 `« … »`（内侧也是不换行空格）。
- 中文的「」在英文里换成 “ ”，法文换成 « »；中文书名号《》同理。
- 数量：译文里用 `{0|单数|复数}` 选单复数，例如 `{0} {0|file|files}`、`{0} {0|fichier|fichiers}`。
- 尽量不比中文长太多：按钮、表头、分段控件里的文字优先用短说法；长说明可以完整。
- 不出现程序内部术语（TM、prompt 模板、sidecar、SSE、429……），和中文保持一样的「人话」。

## 改写后端文案时保留旧说法

任务记录里存的是当时的中文原句，切换语言时靠目录整句匹配来翻。改写后端的报错、日志或报告文字时，把旧句子连同新译文放进 `catalog/backend-legacy.json`，旧任务在英文、法文界面下才能继续正常显示。界面自己的文字（`t()`）每次现画，不需要保留旧说法。

## 语气

中文用正式、简洁的书面说法：不用「跑」「格子」「接着」「没能」「记得」这类口语，称呼用户时用「您」或省略主语。英文不用缩写（cannot、do not），法文用 vous、避免口语。

## 模块与页面

| 中文 | English | Français |
|---|---|---|
| Excel 表格 | Excel files | Fichiers Excel |
| Word 文档 | Word documents | Documents Word |
| PDF 与图片 | PDF & images | PDF et images |
| CAD 图纸 | CAD drawings | Plans CAO |
| 任务中心 | Task center | Centre des tâches |
| 记忆库 | Translation memory | Mémoire de traduction |
| 插件 | Plugins | Plugins |
| 设置 | Settings | Paramètres |
| 帮助 | Help | Aide |
| 反馈 | Feedback | Retours |
| 快速开始 | Quick start | Démarrage rapide |
| 外观与语言 | Appearance & language | Apparence et langue |
| 数据与维护 | Data & maintenance | Données et maintenance |
| 模型配置 | Model settings | Configuration des modèles |
| 任务 | Tasks | Tâches |
| 扩展 | Extensions | Extensions |
| 组件 | Components | Composants |
| 智能体接入 | Agent access | Accès des agents |

任务中心、插件是 V10.2 及以前的页面名，只在旧句子里保留。

## 常用词

| 中文 | English | Français |
|---|---|---|
| 翻译（动作 / 阶段名） | Translate / Translating | Traduire / Traduction |
| 读取文件（阶段） | Reading files | Lecture des fichiers |
| 调整行高（阶段） | Adjusting row heights | Ajustement des hauteurs de ligne |
| 生成文件（阶段） | Generating files | Génération des fichiers |
| 开始翻译 | Start translation | Lancer la traduction |
| 暂停 / 继续 / 停止 | Pause / Resume / Stop | Pause / Reprendre / Arrêter |
| 已暂停 / 已停止 / 已完成 | Paused / Stopped / Completed | En pause / Arrêtée / Terminée |
| 任务失败 | Task failed | Échec de la tâche |
| 完成但有问题 | Completed with issues | Terminée avec des problèmes |
| 需复核 / 复核 | Needs review / Review | À vérifier / Vérification |
| 源语言 / 目标语言 | Source language / Target language | Langue source / Langue cible |
| 自动识别 | Auto-detect | Détection automatique |
| 专业领域 | Domain | Domaine |
| 提示词 | Prompt | Prompt |
| 模型 / 服务商 | Model / Provider | Modèle / Fournisseur |
| API 密钥 | API key | Clé API |
| 测试连接 | Test connection | Tester la connexion |
| 连接池 / 连接 | Connections | Connexions |
| 并发 | Concurrency / Parallel requests | Requêtes simultanées |
| 输出目录 | Output folder | Dossier de sortie |
| 打开输出目录 | Open output folder | Ouvrir le dossier de sortie |
| 运行日志 | Activity log | Journal d'activité |
| 记忆库词条 | Entry | Entrée |
| 固定词条 | Locked entry | Entrée verrouillée |
| 手动维护 / 自动保存（词条来源） | Manual / Auto-saved | Manuelles / Automatiques |
| 备注（词条适用场景） | Note | Note |
| 设定译法 | Set translation | Traduction définie |
| 采用方式 | How to apply | Mode d'application |
| 始终采用 / 按场景选用 / 仅整段复用 | Always apply / By context / Whole text only | Toujours appliquer / Selon le contexte / Texte entier uniquement |
| 生效范围 | Applies to | Portée |
| 未采用设定译法 | Set translation not used | Traduction définie non utilisée |
| 高级模式 | Advanced mode | Mode avancé |
| 清洗记忆库 | Clean up memory | Nettoyer la mémoire |
| 补译 | Fill in missing translations | Compléter les traductions |
| 双语 | Bilingual | Bilingue |
| 原文 / 译文 | Source text / Translation | Texte source / Traduction |
| 文本单元格 | Text cells | Cellules de texte |
| 工作表 | Worksheet | Feuille |
| 段落 | Paragraphs | Paragraphes |
| 页眉页脚 | Headers & footers | En-têtes et pieds de page |
| 批注 | Comments | Commentaires |
| 文本框 | Text boxes | Zones de texte |
| 图纸 | Drawing | Plan |
| 浏览 | Browse | Parcourir |
| 保存 / 取消 / 确定 / 关闭 | Save / Cancel / OK / Close | Enregistrer / Annuler / OK / Fermer |
| 浅色 / 深色 / 跟随系统 | Light / Dark / System | Clair / Sombre / Système |
| 界面语言 | Interface language | Langue de l'interface |
| 检查更新 | Check for updates | Rechercher des mises à jour |

## 语言名（后端送来的显示名，用 tx() 翻）

| 中文 | English | Français |
|---|---|---|
| 中文 / 简体中文 | Chinese / Simplified Chinese | Chinois / Chinois simplifié |
| 英语 | English | Anglais |
| 法语 | French | Français |
| 自动识别 | Auto-detect | Détection automatique |
