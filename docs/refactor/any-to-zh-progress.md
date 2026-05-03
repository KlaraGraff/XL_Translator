# 任意语言到中文改造进度

## 1. 背景与目标

本次改动范围仅限新增 `任意语言 -> 中文` 这一条翻译方向，不改动现有 `中文 -> 其他语言` 流程的行为与默认体验。

## 2. 总体策略

- 兼容策略基线：旧流程继续默认 `source_lang="zh"`，保证现有 `中文 -> 其他语言` 的调用链、默认值和页面布局不变。
- 新入口策略：只在翻译页中、且仅当目标语言为中文时，按需暴露 `source_lang` 选择器；目标不是中文时，页面保持改动前外观与交互。
- 签名升级策略：凡是新增语向参数的函数，一律保留默认值，优先使用 `source_lang="zh"`，避免强迫旧调用点同步修改。
- 语言对兼容策略：`lang_pair` 从“固定 `zh-*`”升级为“可表达 `source-target`”，但必须兼容解析历史 `zh-*` 字符串。
- 过滤策略：`core/translation_filter.py` 仅为 `target_lang == "zh"` 增加新分支，原有 `中文 -> *` 分支原样保留，避免扩大回归面。
- 本轮范围边界：不引入自动语言检测，不新增第三方依赖，不修改 `page_tm.py`、`tm_cleaner.py`、`task_logger.py`，不处理 UI 文案去中文化，不同步修改 `dist/XL_Translator_Distribution/**`。
- 执行约束：后续每次开始一个步骤前，先回看本文件；每完成一个 checklist item，立即更新勾选状态、变更日志与风险监控表。

## 3. 改动清单（Checklist）

- [x] `settings.py` (`AppSettings`, `_normalize_target_lang_state`) — 新增 `source_lang` 持久化状态，并保持旧流程默认 `source=zh`，否则新方向没有稳定语向入口
  - 风险点：默认值或迁移逻辑处理不当会影响旧设置文件加载，或让旧流程默认源语言漂移
  - 回归校验点：不设置 `source_lang` 的旧配置仍能加载；旧 `中文 -> 英文` 启动链路仍使用默认 `source=zh`

- [x] `core/language_registry.py` (`build_lang_pair`, `get_target_lang_display_from_lang_pair`, `get_target_lang_description_from_lang_pair`, `build_target_lang_note_block_from_lang_pair`) — 把语言对构造/解析升级为通用 `source-target`，同时兼容历史 `zh-*`
  - 风险点：若历史 `zh-*` 兼容丢失，现有 TM 会全部失去命中能力
  - 回归校验点：旧 `zh-en` 等字符串仍可解析；旧流程生成的默认语言对仍然与改动前语义一致

- [x] `config.py` (`SUPPORTED_LANGS` 定义区) — 允许中文进入可选语言集合，但绝不改变默认目标语言顺序和值
  - 风险点：若中文插入到集合头部，会把默认目标语言从英文改坏
  - 回归校验点：未手动选择目标语言时，旧翻译页默认目标语言仍保持改动前的值

- [x] `ui/target_language_selector.py` (`render_target_lang_selectbox`) — 让目标语言选择器可按页面场景决定是否暴露中文，同时不影响现有 TM 页使用
  - 风险点：若改成全局暴露中文，会把 TM 页面也带进 `zh-zh` 等无效范围
  - 回归校验点：翻译页在目标不是中文时外观不变；TM 页语言范围与改动前保持一致

- [x] `ui/page_translate.py` (`_render_translate_inspector`, `render_page`, `_start_translation_task`, `_render_action_buttons`) — 仅在目标语言为中文时显示 `source_lang`，并把 source/target 一起送入后台链路
  - 风险点：布局误改或按钮状态误判，会扩大旧 `中文 -> *` 页面回归面
  - 回归校验点：目标不是中文时页面布局与按钮行为不变；目标是中文且未选源语言时禁止启动；`source_lang == target_lang` 时被明确拦截

- [x] `core/task_runner.py` (`TaskRunner._run`, `TaskRunner._collect_texts`) — 让后台主链路贯穿 `source_lang`，并用真实语言对做 TM 查询/写入与扫描过滤
  - 风险点：若 runner 内默认值没保住，旧流程会因为签名变化或 TM pair 变化而失效
  - 回归校验点：旧 `中文 -> 英文` 仍命中 `zh-en` TM；新 `英文 -> 中文` 能生成 `en-zh` TM

- [x] `core/translation_filter.py` (`should_translate`, `is_translation_redundant`) — 为 `target_lang == "zh"` 增加独立规则，旧分支原样保留
  - 风险点：若直接重写通用规则，旧 `中文 -> *` 会把不该翻译的英文/代码/型号送进模型
  - 回归校验点：旧 `中文 -> 英文` 的扫描与质量拦截结果不变；新 `英文 -> 中文` 不会漏掉纯英文短句

- [x] `core/engine_dispatcher.py` (`get_system_prompt`, `translate_texts`, `_apply_quality_filter`) — 把 `source_lang` 传入 Prompt 与质量闭环，同时保住旧默认 `source=zh`
  - 风险点：共享调度层一旦失配，会同时影响所有引擎和旧流程
  - 回归校验点：旧流程不显式传 source 时仍生成与改动前语义等价的 Prompt；新流程能把真实语向传到底层

- [x] `engines/base_engine.py` (`TASK_INSTRUCTION`, `TranslationEngine.translate_batch`) — 把共享 Prompt 模板从“固定中文源”改成 source/target 参数化，同时保证 `source=中文` 时语义等价
  - 风险点：共享模板修改会波及所有引擎，若提示语退化会直接伤及旧流程质量
  - 回归校验点：旧 `中文 -> 英文` 生成的 Prompt 仍表达“从中文翻译为目标语言”；空字符串与替换前缀协议不变

- [x] `engines/openai_engine.py` (`translate_batch`) — 透传新的语向参数给共享模板与调度链路
  - 风险点：若签名与基类不一致，云端 OpenAI 兼容链路会直接报错
  - 回归校验点：旧 `中文 -> 英文` 与新 `英文 -> 中文` 都能正常请求并解析结果

- [x] `engines/claude_engine.py` (`translate_batch`) — 透传新的语向参数给共享模板与调度链路
  - 风险点：若签名与基类不一致，Claude 链路会直接报错
  - 回归校验点：旧 `中文 -> 英文` 与新 `英文 -> 中文` 都能正常请求并解析结果

- [x] `engines/dashscope_engine.py` (`translate_batch`) — 透传新的语向参数给共享模板与调度链路
  - 风险点：若签名与基类不一致，DashScope 链路会直接报错
  - 回归校验点：旧 `中文 -> 英文` 与新 `英文 -> 中文` 都能正常请求并解析结果

- [x] `engines/ollama_engine.py` (`translate_batch`, `_translate_async`, `_translate_chunk`) — 透传新的语向参数给共享模板与调度链路
  - 风险点：本地引擎有异步分支，若参数漏传会出现仅本地模式异常
  - 回归校验点：旧 `中文 -> 英文` 的本地模式仍能跑通；新 `英文 -> 中文` 本地模式不因漏参退回原文

- [x] `engines/zhipu_engine.py` (`translate_batch`) — 透传新的语向参数给共享模板与调度链路
  - 风险点：若签名与基类不一致，智谱链路会直接报错
  - 回归校验点：旧 `中文 -> 英文` 与新 `英文 -> 中文` 都能正常请求并解析结果

- [x] `core/bilingual_writer.py` (`write_bilingual_file`, `_write_with_openpyxl`) — 写回阶段继续使用语向过滤，确保 `-> 中文` 结果能实际落盘
  - 风险点：若只改扫描不改写回，模型返回了中文结果也会在写回层被重新跳过
  - 回归校验点：旧 `中文 -> 英文` 仍能正常双语回填；新 `英文 -> 中文` 会把命中的源文+中文译文写回单元格

## 4. 回归风险监控表

| 风险项 | 等级 | 是否已规避 | 如何规避 |
| --- | --- | --- | --- |
| `config.py` 若把中文放到默认目标语言前面，会改坏旧默认目标语言 | 高 | 是 | 已把中文拆到 `OPTIONAL_TARGET_LANGS`，并单独新增 `SUPPORTED_SOURCE_LANGS`；`SUPPORTED_LANGS` 原有顺序与默认值保持不变 |
| `engines/base_engine.py` 共享 Prompt 若失去旧 `source=zh` 语义，会直接影响现有 `中文 -> *` | 高 | 是 | 已把共享任务模板参数化为 `source_lang_name` / `target_lang_name`，并通过静态校验确认 `source=中文` 时仍生成“从中文翻译为英文”这类旧语义等价提示 |
| `core/translation_filter.py` 若全局放宽规则，会把旧 `中文 -> *` 扫描面扩大 | 高 | 是 | 已把新规则严格包裹在 `target_lang == "zh"` 分支内；旧 `中文 -> *` 分支保留原始中文优先扫描与质量拦截逻辑 |
| `core/language_registry.py` 若不兼容历史 `zh-*`，旧 TM 会全部失忆 | 中 | 是 | 已让 `build_lang_pair()` 保留默认 `source="zh"`，并把 `lang_pair` 解析改成“首个 `-` 分割”；历史 `zh-*` 可继续解析，`zh-x-custom-*` 也兼容 |
| `ui/target_language_selector.py` 若全局暴露中文目标，会把 TM 页面带入无效 `zh-zh` 范围 | 中 | 是 | 已把“是否显示中文”做成 `include_optional_target_langs` 场景参数，默认值仍为 `False`；仅翻译页显式开启，TM 页保持原有语言范围 |
| `core/bilingual_writer.py` 若不改写回过滤，新 `-> 中文` 结果可能生成了但不落盘 | 中 | 是 | 已让写回阶段沿用 `target_lang` / `source_lang` 感知过滤，确保扫描与落盘使用同一套方向判断 |
| 新 `en-zh` / `fr-zh` TM 本轮暂不可在 `page_tm.py` 检视，形成后台可写前台不可见 | 中 | 否 | 本轮记为遗留项，不在范围内实现；交付时明确说明 |
| 旧 UI 文案仍写“原始中文工作表”等，可能误导用户但不阻塞功能 | 低 | 否 | 本轮不动文案，交付时列入遗留项 |

## 5. 实施顺序

1. **补语向建模，但保持旧默认不动**
   - 优先修改 `settings.py`、`core/language_registry.py`、`config.py`
   - 目标：让 `source_lang` 与通用 `lang_pair` 成为可用底座，但旧调用仍默认 `source=zh`
2. **只在翻译页新增 `-> 中文` 入口，不重做共享 UI**
   - 修改 `ui/target_language_selector.py`、`ui/page_translate.py`
   - 目标：目标语言不是中文时页面完全不变；目标语言是中文时才出现 `source_lang`
3. **打通主翻译闭环**
   - 修改 `core/engine_dispatcher.py`、`engines/base_engine.py`、各引擎实现、`core/translation_filter.py`、`core/task_runner.py`、`core/bilingual_writer.py`
   - 目标：从页面选择到 Prompt、翻译、TM、写回都能跑通 `任意语言 -> 中文`
4. **补自检与遗留项确认，不扩大范围**
   - 不实现 `page_tm.py`、`tm_cleaner.py`、`task_logger.py`
   - 目标：完成静态/动态验证，确认旧 `中文 -> *` 未回归，并把第二批必须处理的遗留项明确记录

## 6. 变更日志

- 预留区域，待开始编码后逐条追加：
  - 时间戳
  - 完成的 checklist item
  - 关键改动摘要
  - 是否通过自检

- 2026-04-11 22:33:47 +0100 | 完成 `config.py` (`SUPPORTED_LANGS` 定义区)
  - 关键改动摘要：新增 `OPTIONAL_TARGET_LANGS={"中文":"zh"}` 与 `SUPPORTED_SOURCE_LANGS`，把中文作为可选补充项引入，同时保持原 `SUPPORTED_LANGS` 顺序不变
  - 是否通过自检：已通过静态自检；确认默认目标语言仍来自原 `SUPPORTED_LANGS` 首项，旧 `中文 -> *` 默认体验未被改动

- 2026-04-11 22:36:48 +0100 | 完成 `core/language_registry.py` (`build_lang_pair`, `get_target_lang_display_from_lang_pair`, `get_target_lang_description_from_lang_pair`, `build_target_lang_note_block_from_lang_pair`)
  - 关键改动摘要：新增源语言辅助函数；让目标语言显示/描述与语言说明块支持通用 `source-target` 语言对；`build_lang_pair()` 保留默认 `source="zh"` 以兼容旧调用
  - 是否通过自检：已通过静态自检；确认旧调用仍可只传 `target_lang` 生成 `zh-*`，历史 `zh-*` 字符串仍可解析

- 2026-04-11 22:38:10 +0100 | 完成 `settings.py` (`AppSettings`, `_normalize_target_lang_state`)
  - 关键改动摘要：为 `AppSettings` 新增持久化字段 `source_lang`，并在模型归一化阶段对非法或缺失值回退到默认 `zh`
  - 是否通过自检：已通过静态自检；确认旧设置文件缺少 `source_lang` 时仍会安全回退到 `zh`，不会改变旧 `中文 -> *` 启动链路

- 2026-04-11 22:39:40 +0100 | 完成第 1 步阶段自检
  - 涉及文件：`config.py`、`settings.py`、`core/language_registry.py`
  - 关键改动摘要：已建立 `source_lang` 与通用 `lang_pair` 底座，并保留旧调用默认 `source=zh`
  - 是否通过自检：已通过；`./.venv/bin/python3 -m py_compile config.py settings.py core/language_registry.py` 成功，且静态确认旧调用 `build_lang_pair('en')` 仍生成 `zh-en`、默认目标语言仍为 `en`

- 2026-04-11 23:00:25 +0100 | 完成 `ui/target_language_selector.py` (`render_target_lang_selectbox`)
  - 关键改动摘要：为目标语言选择器新增 `include_optional_target_langs` 场景参数，并把最近目标语言的记忆/回退逻辑同步接入该参数；默认行为仍不暴露中文
  - 是否通过自检：已通过静态自检；确认默认参数仍为 `False`，旧调用方无需传入 `source_lang` 或额外开关即可保持原有目标语言范围

- 2026-04-11 23:00:25 +0100 | 兼容补强 `settings.py` / `core/language_registry.py`
  - 关键改动摘要：让 `recent_target_langs`、`target_lang` 的归一化也识别可选中文目标，避免新方向在 rerun 或设置重载后把 `zh` 目标悄悄回退到旧默认值
  - 是否通过自检：已通过静态自检；确认默认目标语言顺序未变，且 `AppSettings(target_lang="zh")` 现在会保留 `zh` 而不是回退为 `en`

- 2026-04-11 23:00:25 +0100 | 完成 `ui/page_translate.py` (`_render_translate_inspector`, `render_page`, `_start_translation_task`, `_render_action_buttons`)
  - 关键改动摘要：翻译页仅在目标语言为中文时显示 `source_lang` 选择器；未选源语言时禁用开始按钮；显式拦截同语种启动；启动与 `.xls` 兼容模式确认弹窗都会把 `source_lang` 一起带入后台链路
  - 是否通过自检：已通过静态自检；确认目标语言不是中文时不会出现 `source_lang` 控件，且旧流程启动入口仍会落回默认 `source=zh`

- 2026-04-11 23:00:25 +0100 | 完成第 2 步阶段自检
  - 涉及文件：`ui/target_language_selector.py`、`ui/page_translate.py`、`settings.py`、`core/language_registry.py`
  - 关键改动摘要：翻译页的新入口已经收敛到“仅 `target=zh` 才显示 `source_lang`”；共享选择器继续默认隐藏中文目标；可选中文目标的状态在 rerun / 设置归一化阶段不再丢失
  - 是否通过自检：已通过；`./.venv/bin/python3 -m py_compile ui/target_language_selector.py ui/page_translate.py core/language_registry.py settings.py` 成功，且 `./.venv/bin/python3` 直接构造 `AppSettings(target_lang="zh")` 后仍保留 `zh`，`get_ordered_target_lang_codes(..., include_optional=True)` 结果以 `zh` 开头，旧 `中文 -> *` 路径仍可依赖默认 `source=zh`

- 2026-04-11 23:18:06 +0100 | 完成 `core/engine_dispatcher.py` (`get_system_prompt`, `translate_texts`, `_apply_quality_filter`)
  - 关键改动摘要：调度层新增 `source_lang` 透传，并把质量闭环的 `is_translation_redundant()` 调用升级为 source/target 双参入口；旧调用继续默认 `source=zh`
  - 是否通过自检：已通过静态自检；确认旧 `translate_texts(..., target_lang, system_prompt)` 仍可用默认 `source=zh` 跑通

- 2026-04-11 23:18:06 +0100 | 完成 `engines/base_engine.py` (`TASK_INSTRUCTION`, `TranslationEngine.translate_batch`)
  - 关键改动摘要：共享任务模板从“固定中文源”改成 `source_lang_name + target_lang_name` 参数化，并保留空字符串/替换前缀协议不变；抽象签名新增默认 `source_lang="zh"`
  - 是否通过自检：已通过静态自检；确认 `source=中文`、`target=英文` 时模板仍包含“从中文翻译为英文”的旧语义等价表述

- 2026-04-11 23:18:06 +0100 | 完成 `engines/openai_engine.py` (`translate_batch`)
  - 关键改动摘要：OpenAI 兼容链路已透传 `source_lang`，并使用共享模板生成新的 source/target Prompt
  - 是否通过自检：已通过静态自检；确认函数新增参数保留默认值，不会破坏旧 `中文 -> *` 调用

- 2026-04-11 23:18:06 +0100 | 完成 `engines/claude_engine.py` (`translate_batch`)
  - 关键改动摘要：Claude 链路已透传 `source_lang`，并复用共享模板生成新的 source/target Prompt
  - 是否通过自检：已通过静态自检；确认函数新增参数保留默认值，不会破坏旧 `中文 -> *` 调用

- 2026-04-11 23:18:06 +0100 | 完成 `engines/dashscope_engine.py` (`translate_batch`)
  - 关键改动摘要：DashScope 链路已透传 `source_lang`，并复用共享模板生成新的 source/target Prompt
  - 是否通过自检：已通过静态自检；确认函数新增参数保留默认值，不会破坏旧 `中文 -> *` 调用

- 2026-04-11 23:18:06 +0100 | 完成 `engines/ollama_engine.py` (`translate_batch`, `_translate_async`, `_translate_chunk`)
  - 关键改动摘要：本地 Ollama 链路在同步入口与异步批次拆分之间完整透传 `source_lang`，避免仅本地模式漏参
  - 是否通过自检：已通过静态自检；确认新增参数保留默认值，旧本地 `中文 -> *` 仍可沿用 `source=zh`

- 2026-04-11 23:18:06 +0100 | 完成 `engines/zhipu_engine.py` (`translate_batch`)
  - 关键改动摘要：智谱链路已透传 `source_lang`，并复用共享模板生成新的 source/target Prompt
  - 是否通过自检：已通过静态自检；确认函数新增参数保留默认值，不会破坏旧 `中文 -> *` 调用

- 2026-04-11 23:18:06 +0100 | 完成 `core/translation_filter.py` (`should_translate`, `is_translation_redundant`)
  - 关键改动摘要：为 `target_lang == "zh"` 新增独立扫描/质量判断分支，中文内容在新方向下直接跳过；旧 `中文 -> *` 分支保持原逻辑不动
  - 是否通过自检：已通过静态自检；确认 `should_translate('Valve body', target_lang='zh', source_lang='en')` 为真、`should_translate('阀体', target_lang='zh', source_lang='en')` 为假，旧分支未被改写

- 2026-04-11 23:18:06 +0100 | 完成 `core/task_runner.py` (`TaskRunner._run`, `TaskRunner._collect_texts`)
  - 关键改动摘要：Runner 现在显式持有 `source_lang`，并用真实 `source-target` 语言对执行 TM 查询/写入、Prompt 构造、词条扫描和写回
  - 是否通过自检：已通过静态自检；确认默认构造仍可回退 `source=zh`，而 `TaskRunner([], AppSettings(target_lang='zh', source_lang='fr'))` 会持有 `fr`

- 2026-04-11 23:18:06 +0100 | 完成 `core/bilingual_writer.py` (`write_bilingual_file`, `_write_with_openpyxl`)
  - 关键改动摘要：写回层新增 `source_lang` 透传，并按方向感知过滤决定是否落盘；同时让 `target=zh` 时输出文件名显示为“中文”而不是语言代码
  - 是否通过自检：已通过静态自检；确认旧调用仍可使用默认 `source=zh`，新方向不会因为写回层复用旧过滤而丢结果

- 2026-04-11 23:18:06 +0100 | 完成第 3 步阶段自检
  - 涉及文件：`core/engine_dispatcher.py`、`engines/base_engine.py`、`engines/openai_engine.py`、`engines/claude_engine.py`、`engines/dashscope_engine.py`、`engines/ollama_engine.py`、`engines/zhipu_engine.py`、`core/translation_filter.py`、`core/task_runner.py`、`core/bilingual_writer.py`、`ui/page_translate.py`
  - 关键改动摘要：Prompt、调度、过滤、TM pair、Runner 与写回已经全部贯穿 `source_lang`；所有新增参数都保留了默认 `source=zh`，旧 `中文 -> *` 路径不要求调用方同步改签名
  - 是否通过自检：已通过；`./.venv/bin/python3 -m py_compile core/engine_dispatcher.py engines/base_engine.py engines/openai_engine.py engines/claude_engine.py engines/dashscope_engine.py engines/ollama_engine.py engines/zhipu_engine.py core/translation_filter.py core/task_runner.py core/bilingual_writer.py ui/page_translate.py` 成功，且静态脚本确认 `build_lang_pair('en') == 'zh-en'`、`build_lang_pair('zh', source_lang='fr') == 'fr-zh'`、`should_translate('Valve body', target_lang='zh', source_lang='en')` 为真、`TaskRunner([], AppSettings(target_lang='zh', source_lang='fr'))._source_lang == 'fr'`

- 2026-04-11 23:27:31 +0100 | 补充修正 `ui/target_language_selector.py`（目标语言显示）
  - 关键改动摘要：把下拉框 `format_func` 也接入 `include_optional_target_langs`，确保翻译页里新增的 `zh` 目标以“中文”显示，而不是裸语言代码
  - 是否通过自检：已通过动态自检；隔离 AppTest 已确认默认目标列表包含“中文”且不再出现 `zh`

- 2026-04-11 23:27:31 +0100 | 完成第 4 步阶段自检
  - 涉及文件：`ui/target_language_selector.py`、`.runtime/self-tests/any-to-zh-ui/check_any_to_zh_ui.py`
  - 关键改动摘要：在不扩大范围的前提下补齐了目标语言展示一致性，并用隔离 `AppTest` 覆盖了旧方向无 `source_lang` 控件、新方向必须选择源语言、同语种保护三条核心 UI 约束；`page_tm.py`、`tm_cleaner.py`、`task_logger.py` 继续保留为第二批遗留项，未在本轮实现
  - 是否通过自检：已通过；`powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1` 通过，且 `powershell -ExecutionPolicy Bypass -File ./agent/testing/Run-IsolatedVenvPython.ps1 -TaskSlug any-to-zh-ui -ScriptPath .runtime/self-tests/any-to-zh-ui/check_any_to_zh_ui.py` 通过，验证了“目标不是中文时无源语言控件”“目标为中文且未选源语言时开始按钮禁用”“`中文 -> 中文` 会被明确拦截”
