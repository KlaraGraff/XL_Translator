# UI 全面重设计 —— 决策记录与实施进度

- **日期**：2026-07-16
- **状态**：设计已定稿；实施第 1 步（设计系统层）已完成并提交；**第 2 步起暂停**，等待整体重构的技术选型决策（见 `docs/refactor/2026-07-16_stack_refactor_assessment.md`）。
- **分支**：`redesign/design-system`（未推送）
- **设计唯一真相源**：`docs/mockups/2026-07-16_redesign_prototype.html`（可交互原型，浏览器直接打开；线上副本 https://claude.ai/code/artifact/43052d58-5492-404b-b6f1-ab163c937ee8 ）

---

## 一、重设计范围

不只是配色刷新，而是**设计语言 + 版式 + 信息架构 + 图标**全部重做：

- 统一的色彩 / 字体 / 图标 / 组件规范（原型底部附完整规范文档）
- 主页面版式重新规划（等高双栏，见下）
- 导航重构（图标窄栏 + 按"干活 / 维护"分组）
- 全局模型配置从常驻侧栏改为可折叠停靠面板
- 成套自绘线性图标（1.8px 描边、圆角端点、currentColor）

## 二、已拍板的设计决策（逐条经用户确认，勿擅自推翻）

| # | 决策 | 内容与理由 |
|---|------|-----------|
| 1 | 主题色 | 长春花靛蓝 `#5468FF`（呼应蓝发动漫应用图标）。语义色独立于主题色：ok `#12A366` / warn `#C77A00` / danger `#E5484D`，深色主题下提亮（`#3DD68C` / `#E8A33D` / `#FF6369`） |
| 2 | 双主题 | 浅色 + 深色都要。偏好存 `settings.appearance.theme`（system/light/dark），system 跟随系统配色，坏值回落浅色 |
| 3 | 字体栈 | `-apple-system / SF Pro Text / Segoe UI / PingFang SC / Microsoft YaHei / Noto Sans SC`。原 `Arial` 使中文落在无样式回退字体上，是最大单项可读性问题 |
| 4 | 主版式 | **方案 A「等高双栏」**：内容区定高；左侧任务表/日志在卡片内部滚动；右侧运行面板满高、主按钮锚底。解决用户明确抱怨的"左右高度参差"（根因：两栏是会提前结束的浮动卡片） |
| 5 | 侧栏分组 | 图标窄栏，横线上=文件处理入口（Excel / Word / PDF），横线下=配置与资源维护（记忆库 / 模型配置）。按用户视角"干活 vs 维护"分，而非"页面 vs 面板"的技术区分 |
| 6 | 模型配置 | 左侧**可折叠停靠面板**（非浮层）。放左的理由：右侧已被运行设置占用。空间逻辑=左:全局配置 / 中:操作对象 / 右:本次怎么跑。**默认折叠 + 记住上次状态**（字段 `settings.appearance.model_config_panel_open` 已就位）。顶栏放只显模型名的药丸（如 `● gpt-4o-mini`），不写"模型配置"四个字 |
| 7 | 顶栏 | 页面身份唯一出处：标题 + 状态徽章（紧跟标题）+ 一行简介。删除页面内重复大标题块（省约 90px 给表格）与 `EXCEL WORKSPACE` 英文小标 |
| 8 | 记忆库页 | 通栏（无右侧运行面板） |
| 9 | 「设计语言」页 | **不是产品功能**，是设计规范文档，归宿即本目录与 `docs/mockups/`，不进产品导航。用户面向只提供浅色/深色/跟随系统一个设置，不开放 token 自定义。若需在真实 Qt 里调试，用运行时环境变量开关（沿用 `TRANSLATOR_*` 约定），**不要用打包排除**（spec 里 `collect_submodules("native_app")` 整包扫入，排除易造成 release 版 ImportError 且 dev/release 行为分叉） |

## 三、实施进度

### ✅ 第 1 步：设计系统层（已完成，commit `1091f30`）

- 新增 `native_app/theme.py`：每主题一张 token 表 + 单一 QSS 模板。
  ⚠️ QSS 用 `{}` 做规则体，模板替换**必须用 `string.Template` 的 `$name`，不能用 `.format()`**。
- `native_app/style.py` 变薄为兼容层：`APP_QSS` 照旧可用，新增 `qss_for_theme()`。
- `settings.py` 新增 `AppearanceSettings`（theme + model_config_panel_open）。
- `native_app/main.py` 启动时 `apply_theme()`（settings 加载前先套一次默认，避免迁移弹窗裸奔）。
- 提示气泡是富文本、QSS 够不着 → `widgets.py` 改为从 `active_palette()` 取色。
- 新增深色勾选/单选图标 `assets/ui/check-dark.svg` / `radio-dot-dark.svg`（深色下 accent 提亮、on_accent 变深，白色勾会不可见）。
- 过程中修掉的两个真 bug：
  1. **深色对比度**：`tint_ink` 若不随主题提亮，会深蓝字压深蓝底（状态徽章/模型用途选择器发糊）。已数值验证（tint 底亮度 35 vs 字 178）。
  2. **测试全局污染**：测试里 `apply_theme` 给共享 QApplication 挂样式表，改变后续布局测试的控件高度（32→29px）。新增 `set_active_theme()` 只改模块状态不碰样式表。
- 两个冻结精确色值的旧测试重写为按 token 断言意图，且同时覆盖双主题。
- **验证**：380 tests pass、ruff clean、真实 Qt 窗口双主题离屏渲染截图确认（含深色勾选图标放大检查）。

### ⏸ 第 2 步：逐页版式改造（未开始，暂停中）

按定稿设计逐页落地，一页一提交：

1. 主窗口壳层：图标窄栏（分组 + 配置入口）、可折叠左侧配置停靠面板、顶栏合并（标题+状态+简介 / 右侧模型药丸+更新+主题切换按钮）。**注意：`appearance.theme` 字段已存在但界面上还没有切换控件**，原计划做在顶栏。
2. Excel 页：等高双栏（源路径条 → 指标块 → 任务表内滚 / 右侧满高运行面板主按钮锚底）。
3. Word 页：同版式，running 态（进度+日志）参照原型。
4. PDF 页：同版式,done 态参照原型。
5. 记忆库页：通栏。
6. 图标资源：原型内 27 个 SVG symbol 需导出为独立文件进 `assets/ui/`。

### 遗留待议（不混入本轮）

- **语言设置产品行为不一致**：Excel 与 Word 共用 `settings.source_lang/target_lang`（一处改另一处跟着变）；PDF 用独立 `settings.pdf.target_lang` 且无源语言（页图翻译由视觉模型识别原文，合理）。原型中已用小字注明共享关系，逻辑未动。要不要统一，界面定稿后单独议。

## 四、恢复施工指引

```bash
# 环境（.venv 已建好，PySide6 6.11.1 与 release 锁版一致）
cd XL_Translator && git checkout redesign/design-system

# 跑测试
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ -q

# 真机试跑（隔离数据目录，不碰真实设置）
TRANSLATOR_APP_DATA_DIR=/tmp/xlt-test .venv/bin/python scripts/launch_native.py
```

关键文件：`native_app/theme.py`（token 与模板）、`native_app/style.py`（兼容层）、`docs/mockups/2026-07-16_redesign_prototype.html`（设计真相源）。

**暂停原因**：第 2 步是纯 Qt 工作量（改大量 Qt 构建代码）。若整体重构决定更换 UI 壳层（见 refactor 评估文档），第 2 步应改为在新壳层上落地原型，Qt 版式工作全部跳过；设计本身（token/原型/图标）与壳层无关，零损失迁移。
