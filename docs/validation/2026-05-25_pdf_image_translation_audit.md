# PDF 版式图像翻译实现审计报告

审计日期：2026-05-25

审计对象：模型配置重构、图像生成模型角色、PDF 版式图像翻译首版实现。

审计结论：**未完全通过**。当前实现已经完成主体架构和大部分用户确认项，且现有单元测试全部通过；但仍有若干需求级缺口，尤其是图像模型可用状态的持久化、首次可用性强制校验、模型级异常时的清单完整性，需要继续修正后再视为完成。

## 审计依据

- `CONTEXT.md`
- `docs/refactor/pdf-image-translation-design-notes.md`
- 本轮代码实现文件：
  - `core/model_roles.py`
  - `core/image_generation.py`
  - `core/pdf_image_translation.py`
  - `native_app/main_window.py`
  - `native_app/pages/pdf_translate.py`
  - `settings.py`
  - `core/diagnostics.py`
  - 相关测试文件

## 已执行验证

命令：

```bash
.venv/bin/python -m unittest discover -s tests
```

结果：

```text
Ran 139 tests in 0.647s
OK
```

说明：测试通过只能证明当前测试覆盖范围内行为成立；本报告仍按需求文档做了静态和语义审查。

## 总体完成度

| 模块 | 审计结论 | 说明 |
| --- | --- | --- |
| 模型用途重构 | 基本完成 | 已有翻译模型、深度清洗模型、图像生成模型，支持配置跟随与链式跟随拦截。 |
| 图像生成模型能力 | 部分完成 | 有独立能力列表和连接测试，但首次强制校验与运行时状态持久化不完整。 |
| PDF 翻译 UI | 基本完成 | 独立 PDF 翻译页已加入主导航，风格大体复用 Word/Excel 页，源语言未暴露，目标语言可选。 |
| PDF 图像翻译流水线 | 基本完成 | 已实现逐页渲染、逐页请求、生图质检、失败占位、应急归一化、PDF 合成。 |
| 输出包结构 | 基本完成 | 输出目录复制源 PDF，译文 PDF 放在相同相对目录，页图像归档放在根级 `_pdf_pages/`。 |
| 报告与清单 | 部分完成 | 全局 Markdown 报告和 JSON manifest 已实现，但模型级异常中断时可能丢失当前文件的部分素材记录。 |
| 受保护 PDF | 符合暂缓范围 | 已记录为暂不支持，代码未实现密码/绕过方向。 |
| 诊断归档 | 部分完成 | 不打包 PDF/PNG 和密钥，符合轻量原则；但 PDF 专属 manifest 摘要不足，停止/异常任务的输出目录信息不足。 |

## 需要修正的问题

### P1：运行时记录的图像模型可用状态没有持久化

证据：

- `core/pdf_image_translation.py:516` 到 `core/pdf_image_translation.py:522` 在模型级不可用时调用 `record_image_model_availability(...)`。
- `core/pdf_image_translation.py:796` 到 `core/pdf_image_translation.py:802` 在真实 PDF 页生成成功后调用 `record_image_model_availability(...)`。
- 但 `native_app/pages/pdf_translate.py:956` 到 `native_app/pages/pdf_translate.py:981` 处理 `DoneMsg` / `ErrorMsg` 时没有调用 `save_settings(self.settings)`。
- 相比之下，左侧“测试连接”路径在 `native_app/main_window.py:844` 到 `native_app/main_window.py:846` 会测试后保存设置。

影响：

用户要求“只要读取到记录，这个模型之前调用成功了，就不会弹窗拦截”。当前真实业务调用虽然会修改内存里的 `settings.image_model_role.availability_status`，但应用重启后可能丢失运行时成功/失败状态，历史状态语义不可靠。

建议修正：

在 PDF 任务结束、模型级异常、或 runner 发出状态变更时持久化设置。最小改法是在 `PdfTranslatePage._poll_runner()` 收到 `DoneMsg`、`ErrorMsg`、`StoppedMsg` 后调用 `save_settings(self.settings)`，同时确保线程内只负责更新内存状态，UI 线程负责落盘。

### P1：首次使用未知状态图像模型时没有强制可用性校验

证据：

- `native_app/pages/pdf_translate.py:899` 到 `native_app/pages/pdf_translate.py:925` 的 `_handle_image_model_history_prompt()` 只在 `availability_status == "unavailable"` 且签名匹配时弹窗。
- 当状态为 `unknown` 时直接返回 `True`，随后进入真实 PDF 生成流程。

影响：

文档中“图像生成可用性校验”要求首个可用配置必须通过带重试的图像生成校验；当前未知状态不会强制测试，也不会提示用户先验证。这样首个 PDF 任务可能直接消耗真实页面请求来发现配置不可用，偏离已确认的状态记录方案。

建议修正：

在 PDF 任务启动前增加 `unknown` 且签名未成功记录的处理。推荐弹窗提供：

- `测试连接`
- `继续生成`
- `取消`

如果严格按“首次必须校验”执行，则 `unknown` 状态不应默认直接继续；至少应明确提示“尚未完成图像生成可用性校验”。如果用户坚持继续，可按你们已确认的“继续生成”路径放行，但这需要在产品文案中与“强制校验”重新统一。

### P1：模型级异常发生在单个文件处理中时，manifest/report 可能丢失当前文件的部分素材记录

证据：

- `core/pdf_image_translation.py:495` 到 `core/pdf_image_translation.py:506` 只有 `_process_file(...)` 正常返回后才把 `record` 加入 `file_records`。
- `core/pdf_image_translation.py:655` 到 `core/pdf_image_translation.py:657` 中，页面 future 抛出 `ImageModelUnavailableError` 时直接设置 stop 并重新抛出。
- 外层 `core/pdf_image_translation.py:514` 到 `core/pdf_image_translation.py:523` 捕获后只记录 fatal error，再用已有 `file_records` 写 manifest/report。
- `core/pdf_image_translation.py:931` 的 `partial_artifacts_available` 只检查已进入 `file_records` 的记录。

影响：

如果第一个 PDF 的第一页已经复制源 PDF、渲染源图像，随后图像模型返回明确不可用错误，当前文件的 `PdfFileRecord` 不会进入 summary。最终 `pdf_translation_manifest.json` 可能显示没有部分素材，但磁盘上实际已有源 PDF 或源页图像。这违反“清单保留足够页面和素材信息，支持未来恢复/重组”的要求。

建议修正：

让 `_process_file()` 在模型级异常时也返回包含已复制源 PDF、已渲染页面和错误状态的 `PdfFileRecord`；或者在外层调用前先创建并登记 record，再由处理函数逐步填充。manifest/report 应明确记录当前文件状态为 `failed` 或 `stopped`，并保留已有素材路径。

### P2：图像连接测试对明确模型级错误没有执行三次默认重试

证据：

- `core/image_generation.py:214` 到 `core/image_generation.py:244` 实现了最多三次连接测试。
- 但 `core/image_generation.py:233` 到 `core/image_generation.py:234` 遇到 `is_model_unavailable_error(exc)` 会提前 `break`。

影响：

用户确认过“强制校验默认三轮，当三轮都失败才弹出不可用”。当前对 invalid API key、quota、model not found 等明确错误会单次失败即记录不可用。工程上可以理解为节省时间，但与已确认文案不完全一致。

建议修正：

如果要严格满足用户确认项，连接测试不应在模型级错误时提前退出，而应完成配置的默认三次尝试后再记录不可用。若团队决定保留“明确错误快速失败”，需要回写文档，说明这是产品策略的有意变更。

### P2：PDF 页生成并发输入未在 UI 写回时按安全上限归一化

证据：

- `settings.py:230` 到 `settings.py:233` 声明 `page_generation_concurrency` 最大值为 `PDF_PAGE_CONCURRENCY_SAFETY_CAP`。
- `native_app/pages/pdf_translate.py:797` 到 `native_app/pages/pdf_translate.py:805` 只做 `max(1, int(raw))`，没有按上限裁剪。
- `core/pdf_image_translation.py:901` 到 `core/pdf_image_translation.py:909` 运行时会裁剪到安全上限。

影响：

运行时不会突破安全上限，但 UI/配置文件可能保存大于 Pydantic 字段上限的值。由于 settings 加载时会做 Pydantic 校验，保存了超限值后，下一次启动存在配置解析失败并回退默认配置的风险。

建议修正：

在 `_on_params_changed()` 中按 `PDF_PAGE_CONCURRENCY_SAFETY_CAP` 裁剪，并把裁剪后的值写回输入框。空值继续表示默认跟随。

### P2：PDF 诊断归档缺少 PDF 专属输出/manifest 摘要

证据：

- `core/diagnostics.py:72` 到 `core/diagnostics.py:76` 只从 `DoneMsg` 提取 `output_dir`、`file_results`、`issues`。
- `core/diagnostics.py:89` 到 `core/diagnostics.py:106` 的诊断 manifest 没有 PDF manifest/report 路径、PDF 任务状态、页级统计、占位页数、应急归一化页数等 PDF 专属摘要。
- `native_app/pages/pdf_translate.py:982` 到 `native_app/pages/pdf_translate.py:993` 在停止任务时归档没有 `DoneMsg`，因此诊断归档里的 `output_dir` 为空，只能从日志文本里间接找。

影响：

当前诊断归档符合“不打包 PDF/PNG/密钥”的安全边界，但对 PDF 任务排障不够好。特别是中止或异常时，用户和开发者无法直接从诊断 manifest 定位输出目录和页级结果，只能翻日志。

建议修正：

让 runner 在 `StoppedMsg` / `ErrorMsg` 中携带 `output_dir`、`report_path`、`manifest_path` 或简化 summary；或者让 UI 归档时传入这些字段。诊断归档只写摘要和路径，不复制 PDF/PNG。

### P3：失败占位页视觉醒目程度不足

证据：

- `core/pdf_image_translation.py:253` 到 `core/pdf_image_translation.py:271` 使用白底、默认字体、黑字绘制失败占位页。

影响：

占位页包含了页码、失败序号、错误摘要、原始页图像路径、占位页路径，字段完整；但设计要求是“明确的失败占位图”和“醒目警示”。当前黑字默认字体在视觉上不够醒目，用户快速翻阅最终 PDF 时可能不够明显。

建议修正：

保持白底，但增加红色或深色警示标题条、更大的标题字体、明显边框。仍需避免遮挡字段信息。

### P3：PDF 图像翻译提示词偏长且含语言特例

证据：

- `core/image_generation.py:36` 到 `core/image_generation.py:50` 的 `PDF_IMAGE_TRANSLATION_PROMPT` 包含大量细项约束，并包含 French `"sis à [place]"` 的特例。

影响：

用户曾强调 PDF 图像路线不要复用专业领域词，也不要给稠密模型叠加过多专业提示，只需要通用地结合语境翻译并保持原位。当前没有复用 Word/Excel 的专业提示词，但提示词仍较长，且出现特定语言例子，可能偏离“简洁通用”的方向。

建议修正：

压缩成更通用的固定提示词：翻译可读文本到目标语言；保留版面、比例、位置、样式、表格、标记、数字、专名和已是目标语言的文本；不要增删解释或重设计页面。避免语言特例。

## 需求符合项

以下需求已找到明确实现证据：

- 模型配置改为按模型用途切换：`native_app/main_window.py:364` 到 `native_app/main_window.py:375`。
- 翻译模型不显示配置来源；深度清洗和图像生成模型显示配置来源：`native_app/main_window.py:622` 到 `native_app/main_window.py:635`。
- 不允许链式跟随并弹窗：`native_app/main_window.py:637` 到 `native_app/main_window.py:652`。
- 跟随配置时服务商、API Key、Base URL 只读，模型名仍可编辑：`native_app/main_window.py:801` 到 `native_app/main_window.py:821`。
- 图像生成模型有独立能力列表：`config.py:30` 到 `config.py:34`，`core/model_roles.py:148` 到 `core/model_roles.py:152`。
- 深度清洗使用模型用途配置：`native_app/workers.py:99` 到 `native_app/workers.py:112`。
- PDF 翻译页加入主导航：`native_app/main_window.py:228` 到 `native_app/main_window.py:230`，`native_app/main_window.py:974` 到 `native_app/main_window.py:976`。
- PDF 页面不暴露源语言，只选择目标语言：`native_app/pages/pdf_translate.py:280` 到 `native_app/pages/pdf_translate.py:286`。
- PDF 页级重试默认为 3 且可配置：`config.py:153`，`settings.py:224` 到 `settings.py:229`，`native_app/pages/pdf_translate.py:287` 到 `native_app/pages/pdf_translate.py:292`。
- PDF 页生成并发空值表示默认跟随：`settings.py:230` 到 `settings.py:245`，`native_app/pages/pdf_translate.py:293` 到 `native_app/pages/pdf_translate.py:299`。
- 固定 300 DPI 渲染：`config.py:152`，`core/pdf_image_translation.py:1007` 到 `core/pdf_image_translation.py:1011`。
- 逐页渲染、逐页请求，无批量页请求：`core/pdf_image_translation.py:618` 到 `core/pdf_image_translation.py:645`。
- 渲染提前量受限：`core/pdf_image_translation.py:616`。
- 运行时并发安全上限：`config.py:156`，`core/pdf_image_translation.py:901` 到 `core/pdf_image_translation.py:909`。
- 页质检顺序为解码、比例、最低分辨率：`core/pdf_image_translation.py:179` 到 `core/pdf_image_translation.py:219`。
- 比例容差 1%：`config.py:158`，`core/pdf_image_translation.py:199` 到 `core/pdf_image_translation.py:206`。
- 最低可读分辨率固定为短边 1200、长边 1600：`config.py:159` 到 `config.py:160`，`core/pdf_image_translation.py:208` 到 `core/pdf_image_translation.py:217`。
- 比例失败重试耗尽后执行应急比例归一化：`core/pdf_image_translation.py:816` 到 `core/pdf_image_translation.py:830`。
- 普通页失败重试耗尽后生成失败占位页：`core/pdf_image_translation.py:832` 到 `core/pdf_image_translation.py:877`。
- 失败占位页记录 `当前失败序号/总失败数`：`core/pdf_image_translation.py:849` 到 `core/pdf_image_translation.py:856`。
- 最终 PDF 使用源 PDF 每页原始页面尺寸：`core/pdf_image_translation.py:879` 到 `core/pdf_image_translation.py:897`。
- 输出包复制源 PDF，只复制 PDF，不复制非 PDF：`core/pdf_image_translation.py:575` 到 `core/pdf_image_translation.py:578`；测试见 `tests/test_pdf_image_translation.py:85` 到 `tests/test_pdf_image_translation.py:153`。
- 页图像归档位于根级 `_pdf_pages/source_pages/` 和 `_pdf_pages/translated_pages/`：`core/pdf_image_translation.py:352` 到 `core/pdf_image_translation.py:357`。
- 译文 PDF 命名为 `译文(目标语言)_原文件名.pdf`：`core/pdf_image_translation.py:284` 到 `core/pdf_image_translation.py:292`。
- 应用管理输出目录内采用 R1/R2 修订：`core/pdf_image_translation.py:295` 到 `core/pdf_image_translation.py:322`；测试见 `tests/test_pdf_image_translation.py:160` 到 `tests/test_pdf_image_translation.py:205`。
- 扫描跳过生成目录、`_pdf_pages/`、隐藏路径和应用生成物：`core/pdf_image_translation.py:978` 到 `core/pdf_image_translation.py:989`。
- 全局 Markdown 报告和 JSON manifest：`core/pdf_image_translation.py:360` 到 `core/pdf_image_translation.py:370`，`core/pdf_image_translation.py:1056` 到 `core/pdf_image_translation.py:1128`。
- PDF 结果页使用页级指标，不使用 TM 统计：`native_app/pages/pdf_translate.py:467` 到 `native_app/pages/pdf_translate.py:490`。
- 中止任务不再提交新页并不合成当前最终 PDF：`core/pdf_image_translation.py:512` 到 `core/pdf_image_translation.py:513`，`core/pdf_image_translation.py:673` 到 `core/pdf_image_translation.py:675`。
- 受保护 PDF 本轮暂不支持：文档 `docs/refactor/pdf-image-translation-design-notes.md` 已列为 Deferred；代码 `core/pdf_image_translation.py:608` 到 `core/pdf_image_translation.py:612` 返回失败记录。
- 诊断归档不打包 PDF/PNG 且脱敏：测试 `tests/test_pdf_image_translation.py:349` 到 `tests/test_pdf_image_translation.py:403`。

## 建议补充测试

- `unknown` 图像模型状态启动 PDF 任务时必须测试/提示的 UI 测试。
- PDF 真实任务成功后，`availability_status=available` 被保存到 settings 的测试。
- PDF 真实任务遇到明确模型级不可用后，`availability_status=unavailable` 被保存到 settings 的测试。
- 模型级异常发生在第一页生成中时，manifest 仍包含当前文件、已复制源 PDF、已渲染源页图像的测试。
- PDF 并发输入超出安全上限时，UI 写回值被裁剪且 settings 文件不会保存非法值的测试。
- 诊断归档在 PDF stopped/error 状态下仍包含 output_dir/report_path/manifest_path 摘要的测试。
- 失败占位页包含醒目视觉元素的图像属性或像素级 smoke test。

## 修复优先级

建议另一个实现窗口按以下顺序处理：

1. 先修复运行时图像模型可用状态持久化。
2. 再修复 unknown 状态首次使用的校验/提示逻辑。
3. 再修复模型级异常时 manifest/report 丢失当前文件部分素材的问题。
4. 然后处理 PDF 并发输入裁剪和诊断摘要增强。
5. 最后处理占位页视觉和提示词压缩。

完成后至少重新运行：

```bash
.venv/bin/python -m unittest discover -s tests
```

并补充上方缺失测试后再回归审查。
