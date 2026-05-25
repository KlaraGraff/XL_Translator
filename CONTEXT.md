# Translator

Translator is a local document translation workspace. This glossary keeps the user-facing language precise when a translation task finishes with mixed file and content outcomes.

## Language

**文件级结果**:
The outcome of producing an output file for a selected source document. A file can be generated even when some translated content inside it still needs review.
_Avoid_: 成功

**内容级结果**:
The outcome of translating and validating the individual paragraphs or cells inside a generated document. Content-level results can include fully passed text, retried text, semantically accepted text, and text left for human review.
_Avoid_: 文件成功, 全部成功

**已生成**:
A file-level result meaning the output document was written successfully. It does not imply that every paragraph was fully translated or passed validation.
_Avoid_: 成功

**生成失败**:
A file-level result meaning no usable output document was written for that source file.
_Avoid_: 失败

**输出目录**:
The destination folder where generated translation documents and reports are written. It is path information, not a result metric.
_Avoid_: 结果指标

**全部通过**:
A content-level result meaning every relevant segment in the generated document has an accepted translation and no segment is left as needs-review. It can include segments that were 已自动处理.
_Avoid_: 成功

**成功**:
A task-level completion label used only when all generated content is 全部通过. It may include 已自动处理 segments, but it must not be used when any segment is 需复核.
_Avoid_: 已生成

**结果摘要**:
A concise task-level sentence shown after completion. It should include only non-zero content-level counts, except for the generated file count that anchors the task outcome.
_Avoid_: 成功但有提示

**需复核**:
A content-level result meaning the output contains retained or otherwise unresolved source content that should be checked by a person before being treated as final. Semantic arbitration accepted content is not counted as needs-review content.
_Avoid_: 成功但有提示

**已自动处理**:
A content-level result category for segments that initially triggered recovery but ended with an accepted translation. This includes semantic arbitration accepted segments and single-segment retry recovered segments.
_Avoid_: 需复核

**保留原文**:
A content-level result meaning a segment could not obtain an accepted translation and remains as source text in the output document. It always belongs to 需复核.
_Avoid_: 未插入译文, 成功

**语义仲裁接受**:
A content-level result meaning a candidate translation failed rule-based checks but was accepted because semantic arbitration judged it equivalent to the source. It belongs to 已自动处理, not 需复核.
_Avoid_: 校验通过

**单段重试恢复**:
A content-level result meaning a segment failed the initial translation path but obtained an accepted translation through single-segment retry.
_Avoid_: 成功

**执行监控摘要**:
A non-scrolling progress summary shown during a running task. It communicates current aggregate state, such as retry rounds, semantic checks, accepted counts, and unresolved counts.
_Avoid_: 日志

**执行监控布局**:
The running-task layout where the progress summary keeps its natural content height and the scrolling log occupies the remaining vertical space. Window resizing should change the log area height, not crop the summary or leave unused blank space.
_Avoid_: 固定高度监控区

**运行日志**:
A scrolling event history for a running task. Repeated recovery events should identify the affected document position so the user can tell which segment is being retried, arbitrated, accepted, or retained.
_Avoid_: 执行监控摘要

**段落位置**:
The user-facing location of a Word segment inside a source document, such as a body paragraph or a table cell. It should be shown when a segment enters retry, semantic arbitration, or needs-review handling.
_Avoid_: 词条, 条

**位置计数**:
The display count of affected Word segments based on document positions rather than unique source text. It is a readability cue for the UI; final locating is done by the result page issue list.
_Avoid_: 去重词条数

**重复段落位置**:
All document positions where the same source segment appears. When a repeated segment enters retry, semantic arbitration, or needs-review handling, the running log should show every affected position rather than only the first one.
_Avoid_: 另有 N 处相同内容

**结果定位清单**:
The completion-page list that locates translation issues or automatic handling by file, section path, paragraph or table cell position, source excerpt, problem, and handling result. Needs-review items appear before automatically handled items.
_Avoid_: 只看运行日志

**文件结果行**:
A compact completion-page row for scanning each selected file's file-level and content-level outcome. It should summarize counts but not duplicate the detailed positions from the result location list.
_Avoid_: 定位清单

**日志保留策略**:
The rule that limits how much running log history remains visible during long tasks. It controls UI volume without hiding the specific positions of retry, semantic arbitration, or needs-review events that remain in the retained window; diagnostic archives should retain complete or much larger log history.
_Avoid_: 少写位置

**模型配置**:
The user-facing area for configuring model access and model roles across the product. It includes cloud API configuration, local Ollama configuration, and the model role choices that use them.
In the first product version, it remains in the sidebar and uses a compact model-role selector to switch which role is being configured rather than becoming a separate settings page.
_Avoid_: 翻译引擎

**云端 API 配置**:
The cloud service access configuration used by cloud-backed model roles. It identifies the service provider access, while each model role chooses the model it actually calls.
_Avoid_: 翻译引擎, 模型配置

**本地 Ollama 配置**:
The local model access configuration used when a model role runs through Ollama on the user's device.
_Avoid_: 云端 API 配置

**模型用途**:
A user-facing role for why a model is called, such as translation, deep TM cleaning, or image generation. Multiple model roles can share the same cloud API configuration while using different model names.
_Avoid_: 引擎类型, 服务商类型

**模型用途选择器**:
The compact sidebar control used to choose which model role is currently displayed in the shared model configuration fields.
_Avoid_: 三个常驻板块按钮

**模型能力**:
The kind of work a model service can perform for a model role, such as text generation or image generation. A service provider can appear in more than one capability list only when it supports those capabilities.
_Avoid_: 服务商列表, 模型用途

**翻译模型**:
The model role used to translate document or spreadsheet text.
It requires text generation capability.
_Avoid_: 主引擎

**深度清洗模型**:
The model role used to review and improve translation memory entries.
It is a cloud-backed role in the current product boundary and requires text generation capability.
_Avoid_: 清洗引擎

**图像生成模型**:
The model role used to generate translated page images from visual page inputs.
It is a cloud-backed role in the current product boundary and requires image generation capability.
_Avoid_: 图片引擎, 生图引擎

**配置跟随**:
A relationship where one model role continuously uses another role's cloud API configuration instead of keeping a copied snapshot. Changing the source configuration changes every role that follows it.
Model roles can only follow earlier source roles in the product's configuration order; the translation model is the default source role and does not follow another role.
For cloud-only model roles, following applies only to cloud API configuration, not to local Ollama mode.
Followed configurations should be visibly marked in the shared model configuration area, such as with a subtle highlight or status marker.
When a role follows another role, service provider, API key, and Base URL are read-only for that role; the model name remains editable. Changing those cloud access fields requires switching the role to independent configuration.
_Avoid_: 一键复制, 同步一次, 循环跟随

**共用模型操作**:
The shared actions outside the model-role-specific fields, such as fetching model lists and testing connectivity. They operate on the effective configuration of the currently selected model role.
_Avoid_: 每个用途重复按钮

**独立配置**:
A model role configuration that does not follow another role's cloud API configuration. It owns its cloud access choice for that role.
_Avoid_: 断开同步

**服务商凭据**:
The credential used to access a cloud model service provider. In the current product language, credentials belong to the service provider and are shared by model roles that use that provider.
_Avoid_: 模型凭据, 配置档案

**PDF 版式图像翻译**:
A PDF translation route that renders each source PDF page to an image, generates an equal-scale translated page image, and merges the translated page images back into a PDF. Its primary promise is visual layout preservation, not editable text output.
It does not read from or write to translation memory because its unit of work is a page image, not paired source and target text segments.
It lets users choose the target language, but does not expose a source-language setting because the image route asks the model to translate the page's visible text directly.
_Avoid_: PDF 转 Word 翻译, 可编辑 PDF 翻译

**PDF 图像翻译提示词**:
The concise general prompt used for PDF 版式图像翻译. It asks the image-generation model to translate page text in context while preserving original placement, visual style, and page structure; it does not reuse the product's professional-domain text translation prompt.
The first product version keeps this prompt fixed rather than exposing user customization.
_Avoid_: 专业领域 Prompt, 文本批量翻译 Prompt

**PDF 翻译**:
The product workspace for PDF-specific translation routes and PDF output packages. It is a separate main navigation area from Word translation.
Its visual language and result presentation should stay consistent with the existing Excel and Word translation pages.
_Avoid_: Word 翻译模式

**PDF 翻译输出包**:
The output folder produced by PDF 版式图像翻译. It presents the final translated PDF as the primary artifact and keeps page image archives so a problematic page can be inspected or regenerated without rerunning the whole document.
The final translated PDF uses a stable translated-file name; review status and failed-page counts belong in the result view and report, not in the PDF filename.
The first product version does not provide an in-app single-page regeneration workflow; the output package preserves page artifacts so that workflow can be added later.
_Avoid_: 临时目录, 诊断归档

**PDF 页级结果指标**:
The result metrics for PDF 版式图像翻译, such as file count, total page count, generated page count, failure placeholder page count, emergency ratio-normalized page count, retry count, and output directory. Translation memory counts do not apply to this route.
_Avoid_: 记忆库命中, 新增词条, API 翻译条数

**原始页图像归档**:
The retained page images rendered from the source PDF before translation. It is used for page-level inspection and regeneration.
_Avoid_: 临时图片

**PDF 渲染 DPI**:
The resolution used when rendering PDF pages into source page images for PDF 版式图像翻译. The first product version uses a fixed 300 DPI default to prioritize page clarity over lower cost or smaller output size.
_Avoid_: 标清, 高清

**页面比例一致**:
The PDF page image requirement that the generated translated page keeps the same width-to-height ratio as the source page. Pixel dimensions may differ when the generated image has a different resolution, as long as the page ratio is preserved.
Generated pixel dimensions may be higher or lower than the rendered source page; resolution alone is not the pass/fail criterion.
Aspect-ratio checks must allow a small tolerance so harmless pixel rounding or resolution changes are not treated as failures.
_Avoid_: 像素尺寸一致

**最低可读分辨率**:
The fixed minimum pixel-size requirement used to reject obviously unreadable generated PDF page images. It is not calculated by comparing against the rendered source page resolution, because source PDFs may be vector-based or unusually high-resolution scans.
The first product version requires the generated page image to have a short edge of at least 1200 px and a long edge of at least 1600 px. Images below either threshold enter page-level recovery and become failure placeholder pages if recovery is exhausted.
_Avoid_: 原图分辨率对比

**译后页图像归档**:
The retained page images used to reassemble the translated PDF. Successful pages contain model-generated translated page images; failed pages can contain locally generated failure placeholder pages.
It contains only the final page image artifact for each PDF page, not intermediate retry attempts or debug files.
_Avoid_: 临时图片, 缓存

**页级恢复**:
The automatic recovery path for a PDF page image that does not produce an acceptable translated page image on the first attempt. A page is marked for review only after this recovery path is exhausted.
Its retry count is configurable for PDF tasks and defaults to three attempts.
_Avoid_: 直接失败

**页级生成请求**:
One image-generation request for one PDF page image. PDF 版式图像翻译 does not batch multiple pages into one model request, and each page request counts as one scheduling unit.
It does not apply extra image-size weighting; upstream concurrency or rate-limit feedback can still reduce the task's active concurrency.
_Avoid_: 批量页请求, 字符权重请求

**PDF 页生成并发数**:
The number of PDF page image-generation requests that may run at the same time. When left blank, it follows the default cloud concurrency; when a number is entered, it overrides concurrency for PDF page generation only.
_Avoid_: 批次大小, 图像权重

**失败占位页**:
A clearly marked page image inserted when PDF 版式图像翻译 cannot produce a translated page image after page-level recovery. It prevents the final PDF from being mistaken for a fully translated result while preserving page order.
It is generated locally by the application and saved as the page artifact used for PDF reassembly; it is not a model-generated translated image.
It should display the failure ordinal as current failed page over total failed pages, such as "1/3", so users can navigate all failed pages during review.
_Avoid_: 原图占位, 静默跳过

**应急比例归一化**:
The fallback handling used only after page-level recovery is exhausted for a generated page image whose page ratio still does not match the source page. The image is scaled proportionally onto the source page canvas with a white background, and the result must be marked in the report as an emergency handling result.
_Avoid_: 常规尺寸处理, 静默缩放

**PDF Markdown 文本翻译**:
A future PDF translation route that extracts or converts PDF content into Markdown and then translates the Markdown through the text translation model. It is a separate optional route from PDF 版式图像翻译.
It can use translation memory because its unit of work can become text segments.
It can expose both source-language and target-language settings because it uses the text translation route.
_Avoid_: PDF 版式图像翻译

## Example Dialogue

Dev: 这个 Word 文件显示“已生成”，那是不是可以告诉用户成功了？

Domain expert: 不能。文件级只能说“已生成”。如果里面还有保留原文，内容级要同时显示“需复核 N 段”；如果只是语义仲裁接受或单段重试恢复，归到“已自动处理”。

Dev: 如果没有任何需复核段落，但有自动处理段落呢？

Domain expert: 可以显示“成功”。成功是任务级标签，表示没有需复核内容；文件级仍然叫“已生成”。

Dev: 恢复过程里的数字应该写到日志里吗？

Domain expert: 汇总数字放在执行监控摘要；运行日志要写清楚具体段落位置，比如“正文段落 18 正在单段重试”或“表格 2 / 单元格 6 正在语义仲裁”。

Dev: 如果同一段原文在多个位置出现，日志只写第一个位置可以吗？

Domain expert: 不可以，重复段落位置要全部显示。日志过大时用日志保留策略处理，不要通过省略位置来压缩语义。

Dev: 运行中显示的“段数”是去重后的原文数量吗？

Domain expert: 不是，是位置计数。它主要让界面状态好读；最终定位靠结果定位清单。
