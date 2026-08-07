# 全项目代码审计报告

审计日期：2026-08-07 ｜ 审计基线：`f05afca`（main）｜ 版本：V9.1.0

审计方式：10 路并行子代理分域审计（UI↔API 契约、Excel、Word、PDF/图片、引擎与调度、
TM/语言、设置与模型配置、后端任务生命周期、打包发布、机械核查），主会话对每条高危结论
做独立复验。**本报告只收录复验通过或代码可直接指认的条目**，子代理报告中的假阳性、
定级错误和标签误用已在第 6 节单独列出，不进修复清单。

---

## 0. 一句话结论

代码质量的**下限很高**（585 测试全绿、ruff 零问题、tsc 零错误、无 XSS、无 SQL 注入、
无假数据 UI、无死端点），但**上限被三类问题压住**：

1. **静默产出错误结果** —— 失败被"安全兜底"伪装成成功，用户拿到的文件看起来完全正常。
   这是本次审计最危险的一类，共 7 条。
2. **双平台不对称** —— Windows 支持是 V9.0.0 新加的，但 CI 不跑 Windows 测试，
   导致多个 Windows-only 缺陷从未被发现。
3. **契约漂移** —— 前端读的字段名、设置项、接口路径与后端实际提供的对不上，
   代码两侧各自都"对"，合起来失效。

---

## 1. P0 · 静默产出错误结果（用户无从察觉）

这一类的共同特征：**不报错、不中断、界面显示成功**，但产物是错的。危害高于崩溃。

### 1.1 多连接池 + 自动识别语言 → 整份文档原样输出，界面报"完成"

**触发条件**：Excel 翻译 + 源语言选「自动识别」+ 模型连接池配了 ≥2 条连接。

三段缺陷扣在一起：

1. `core/engine_dispatcher.py:210` —— 连接链去重后 `len(unique) >= 2` 时，引擎被包进
   `FailoverTranslationEngine`。单连接不触发。
2. `core/failover_engine.py:66` —— 包装器靠 `__getattr__` 把未实现的方法转发给真实引擎。
   但 `chat()` 与 `translate_batch_with_sources()` **定义在基类 `TranslationEngine` 上**
   （`engines/base_engine.py:166`、`:174`），Python 属性查找会先在继承链上命中它们，
   `__getattr__` 根本不触发 → 直接执行基类的 `raise NotImplementedError`。
3. `core/engine_dispatcher.py:722` —— 自动识别走 `translate_texts_with_sources`，
   异常落进兜底：二分拆批重试（每次同样失败）→ 拆到单条 → `return {text: text for text in batch}`，
   **把原文当译文返回**。

**主会话复验（已实测）**：同一底层引擎单独调 `chat()` 正常；包进 `FailoverTranslationEngine`
后 `chat()` 与 `translate_batch_with_sources()` 均抛 `NotImplementedError`。

**为什么 585 个测试没拦住**：`tests/test_failover_engine.py` 只覆盖 `translate_batch`
（该方法包装器**自己实现了**，走的是正常路径），从未对 failover 引擎调用 `chat` 或
`translate_batch_with_sources`。

**同根因波及**：
- `core/task_runner.py:706` 语言预检 100% 失败，被 `preflight_files` 静默吞掉。
- `core/mixed_language.py:876`、`core/word_task_runner.py:2150` —— `_engine_supports_chat`
  对包装器返回 False，混合语言处理与语义复核在多连接模式下被静默禁用，用户侧无提示。

### 1.2 Excel 单元格超 32767 字符 → 生成的 xlsx 打不开

`core/xlsx_patcher.py:873` 写入单元格文本时无长度上限守卫。**主会话复验**：全仓库
grep `32767` / `MAX_CELL` 零命中，全链路无任何截断点。

**触发**：长文本单元格（如 2 万字条款）翻译后拼成「原文\n译文」超过 Excel 的 32767
字符硬上限 → 输出文件 Excel 提示需修复。子代理实测写入 40001 字符无任何拦截。

### 1.3 非法 XML 字符 → 留下一个"看起来成功"的未翻译文件

`core/xlsx_patcher.py:873` + `core/bilingual_writer.py:171`。输出副本在打补丁**之前**
已经 `shutil.copy2` 落盘；译文含 `\x0b` 之类控制字符时 lxml 抛 `ValueError` 中止，
但那个副本留在输出目录里，文件名是正常的 `双语(xx)_xxx.xlsx`。用户极易误发。

### 1.4 PDF 质检只看宽高比，内容完全不校验

`core/pdf_image_translation.py:513-540` 的 `check_page_quality` 只比较输出图与源页的
宽高比（1% 容差）。模型返回**纯白页、文字全糊、漏译半页**——只要比例对就判「通过」，
直接进最终 PDF。

### 1.5 图像模型填 `gpt-image-1` → 每页每次必判不合格

`core/image_generation.py:436-443`：非 `gpt-image-2` 的所有 `gpt-image-*` 一律请求固定的
1024x1536 / 1536x1024，与 1.4 节的 1% 宽高比容差**数学上不可能同时满足**。

子代理实测比例误差：A4 5.70%、Letter 13.73%、Legal 9.80%、B5 5.31%、A5 5.42%、A3 5.72%。
后果：每页每次判 `ratio_error` → 跑满全部重试（**API 用量 = 页数 ×(重试+1)**）→
最后强行加白边、整份文件标 needs_review。

同类：`gpt-image-2` 的 16px 量化在长宽比 ≈0.60 的窄长页（1216×2048）误差 1.03% > 1%，
同样白白耗尽重试。

### 1.6 TM 恢复备份到非空库 → 静默丢词条

`core/tm_manager.py:1566-1578`。`mode='overwrite'` 对 `word_type='auto'` 的来源实际走
跳过分支，返回 `inserted:0 / skipped:1`，旧值保持不变。

**触发**：用户导出备份 → 误操作 → 导入备份恢复。凡当前库已存在的同源词条，备份里的
译文一律不生效，返回结果只体现为"重复"，用户以为恢复成功。

### 1.7 `delete_all_entries` 实际是 `delete_unpinned_entries`

`core/tm_manager.py:1041` 与 `:1024` 两个函数**函数体逐字相同**，都传 `only_unpinned=True`。
docstring 写着「所有词条」，实际固定词条一条都删不掉。**主会话复验：已确认。**

用户点「清空词库」后以为空了，其实没有。叠加 `core/tm_manager.py:1521` 导入时把
`pinned=None` 映射成 1 的缺陷 → 导入的外部 JSON 全部被静默固定 → 之后永远删不掉。

---

## 2. P0 · 功能 100% 失效

### 2.1 TM 批量固定/取消固定：恒定 422

`api/app.py:857` 的 `/api/tm/entries/{entry_id}/pin` 声明在 `:863` 的
`/api/tm/entries/bulk/pin` **之前**，Starlette 按声明顺序匹配，`bulk` 被当成
`entry_id: int` 解析失败。

**主会话复验（实测）**：
```
POST /api/tm/entries/bulk/pin    → 422  int_parsing: input='bulk'
POST /api/tm/entries/bulk/delete → 200
```
`ui/src/views/library.ts:330` 的批量固定按钮 100% 失败。批量删除不受影响（DELETE 方法、
路径形状不冲突）。

同类路由遮蔽：`api/app.py:1307/1314/1321` 的三条 connectivity 路由被 `:1196` 的
`/{role}` 挡死。行为恰好等价所以没暴露，但是 20 行不可达代码。

### 2.2 Windows 上"打开文件 / 打开链接"全线失效

`src-tauri/src/main.rs:136`、`:161` 两个 Tauri 命令**无条件**调用 macOS 的
`Command::new("open")`。**主会话复验**：整个文件只有一处 `cfg!(target_os = "windows")`
（`:195`，用于选 sidecar 可执行文件名），这两个函数里没有平台分支。Windows 上 `open`
不是可执行程序，`spawn()` 必然失败。

受影响入口（已核对调用方）：
- 设置页「查看 Release / 下载」`ui/src/views/settings.ts:2034`
- 设置页「打开本地数据目录」`ui/src/views/settings.ts:2024`
- 任务中心 / 工作区「在文件夹中显示」`ui/src/views/tasks.ts:622`、`ui/src/views/workspace.ts:2450`
- 帮助页外链 `ui/src/views/help.ts:16`

附带：错误文案硬编码 `"The referenced output no longer exists on this Mac."`
（`main.rs:134`）——Windows 用户会看到一句说自己电脑是 Mac 的英文报错。

这条直接击穿 README 第 60 行「更新只会提示并打开官方 GitHub Release 下载页」在 Windows 上的可用性。

### 2.3 工作台阶段文案与完成横幅计数：读错字段名

**主会话复验（已确认）**：后端 `core/task_runner.py:91,98` 发的是 `phase_name` /
`phase_desc`；`ui/src/views/workspace.ts:2268,2276` 读的是 `data.phase` / `data.stage` /
`data.message`——一个都不匹配。而 `ui/src/views/tasks.ts:525,531` **读对了**，
可证是 bug 而非设计。

后果：
- 整轮翻译期间工作台阶段文案冻在「正在准备任务」。
- `markActiveFile`（`workspace.ts:2325-2337`）靠匹配阶段文案定位当前文件，因此
  逐文件「进行中」高亮**永不点亮**。
- `finishTask`（`workspace.ts:2342-2347`）读 `result.summary.*` 与顶层
  `generated_count` / `review_count` / `auto_fixed_count`，后端无 `summary` 对象，
  前两个字段全仓 Python 零出现 → 完成横幅的「生成 N 个文件」退化成"已选文件数"，
  需复核与自动处理**恒为 0，永远拼成「全部通过」**。真有复核项时用户看不到。

### 2.4 「页图并发」设置项从来没人读

`core/pdf_image_translation.py:3009-3017` 的 `_resolve_pdf_concurrency` 零调用方。
用户可见的「页图并发（留空自动）」（`ui/src/views/settings.ts:1310-1319`）和 CLI 的
`--pdf-page-concurrency`（`core/headless_pdf_translate.py:82`）写进设置后无人读取，
运行时并发只来自 `core/model_throughput.py:120-155`。用户调到 8 期望提速，实际仍按默认 2 跑。

### 2.5 关于页显示错误版本号

`ui/src/views/settings.ts:2044` 硬编码 `APP_VERSION_FALLBACK = "8.1.2"`，实际版本 9.1.0
（`app_meta.py:4`）。**主会话复验：已确认。** 用户打开「设置 → 更新与关于」但还没点
「检查更新」时（默认进入状态），直接显示「当前版本：8.1.2」。

`scripts/verify_release_metadata.py` 的版本一致性门检查 app_meta / tauri.conf.json /
Cargo.toml / package.json 四处，**唯独不扫 UI 源码**，所以漏过去了。

---

## 3. P1 · 数据安全与稳定性

| # | 位置 | 缺陷 | 后果 |
|---|---|---|---|
| 3.1 | `api/app.py` 约 25 处（`496-538`、`1122-1135`、`1147-1161` 等） | 设置的读-改-写全程无锁、无版本/ETag 校验。这些是同步 `def` 端点，FastAPI 丢进 anyio 线程池并行执行 | 快速连点两个不同开关 → 后完成的那次把先完成的改动**整份覆盖**（lost update）。文件不会损坏（原子写与跨进程锁都是真的），丢的是内容 |
| 3.2 | `core/task_runner.py:1087` + `:1357-1361` | `insert_batch` 抛的 `sqlite3.OperationalError('database is locked')` 无人捕获，直接冲出工作线程 | 翻译收尾写 TM 时用户在词库页做长写事务 → **API 费用已全部花完、译文文件尚未写盘**，任务崩掉，整批 TM 丢失 |
| 3.3 | `core/tm_manager.py:117-122` vs `core/maintenance.py:179-181` | 连接从不设 `journal_mode=WAL`（只设了 `busy_timeout=5000`），但维护模块已把 `tm.db-wal`/`tm.db-shm` 列为待清理文件——**同一版本内自相矛盾，有人以为已经开了 WAL** | delete 模式下读者也挡写者，直接放大 3.2 的锁冲突窗口 |
| 3.4 | `api/task_manager.py:1357` | 每条 SSE 事件触发一次**全量历史落盘**：重新 sanitize 含全部 logs 的整个 payload → 读整个 `task_history.json` → dumps 最多 200 条 → 写临时文件 → rename。O(n²)，全程持有 `task.condition` | 实测单任务 3000 条事件累计 **19.5 秒纯开销**（前 200 条 0.12s，后 2000 条 17.2s）。Excel 按批发、PDF 按页发，几千条是常态。持锁期间该任务的 SSE 与 `GET /api/tasks/{id}` 全部阻塞 |
| 3.5 | `api/task_manager.py:183` | `self._tasks` 只增不减，终态任务永不移除。每个 `ApiTask` 长期持有 runner（含 `_files`、整份 settings 深拷贝、PDF 全部页记录）、events 全量、logs 全量 | sidecar 与 app 同生命周期，一天跑几十个任务内存单调增长 |
| 3.6 | `api/task_manager.py:1052` + `src-tauri/src/main.rs:338` | 完全没有优雅关闭：`shutdown()` 零调用者、无 FastAPI shutdown 事件、Tauri 退出直接 SIGKILL | 关窗口时正在跑的任务被硬杀。LibreOffice profile、`word_translator_temp` 临时 docx、PDF 页图工作区都不清理；历史停在 `running` |
| 3.7 | `core/word_task_runner.py:321-1259` `_run` | 只捕获 `TaskStopped` 和 `ApiKeyTemporarilyUnavailableError`，**无兜底 `except`、无 `finally`** | 写回阶段任何未预期异常（如 `doc.save` 权限失败）→ 线程静默死亡，清理被跳过导致临时 docx 泄漏，队列不发终止消息，**UI 侧任务永久挂起** |
| 3.8 | `core/word_task_runner.py:1775`、`1909/1912` | `_WordRecoveryPool._executor` 只在 `wait_for_completion()` 内 shutdown，无 `try/finally` | 主翻译抛异常时 executor 的非守护 worker 线程永久阻塞，每次泄漏 `concurrency` 个线程 |
| 3.9 | `core/tm_manager.py:1464-1499` | `get_full_export` 两次 SELECT 之间无显式事务，属两个不同快照 | 导出备份时后台在写 TM → **备份文件本身可能自相矛盾**（冲突候选指向不存在的词条）。备份不可信比没有备份更危险 |
| 3.10 | `core/engine_dispatcher.py:597-608`、`:628-639` | 重试放大 + 零退避。一批 30 条全失败时二分递归产生 59 个节点，每节点内 tenacity 再重试 3 次 → **单批最多 177 次请求**；并发降级分支立即递归，中间不 sleep | 上游 429/5xx 抖动时本地以最大速度把请求量放大近两个数量级 |
| 3.11 | `core/api_concurrency_control.py:98-104` + `core/engine_dispatcher.py:583-584` | 普通 429 判死整个任务：命中并发模式 → 自适应降档 4 级 → 到 minimum 后再来一次 429 抛 `ApiKeyTemporarilyUnavailableError`，该异常被显式 re-raise 穿透所有 fallback | 并发 20 打一个限速较紧的 key，几秒即可走完 4 级降档，Excel 任务整体中止 |
| 3.12 | `core/pdf_image_translation.py:1966-1969` | 渲染循环的 except 包住了**整个文件的所有页** | 一份 200 页 PDF 第 180 页对象损坏 → 整份文件置 FAILED，前 179 页已翻译好的结果**不装配成 PDF** |

---

## 4. P1 · 排版与产物正确性

### Word

| # | 位置 | 缺陷 | 触发 |
|---|---|---|---|
| 4.1 | `core/word_document.py:1624-1638` `_replace_paragraph_text` | 只改 `paragraph.runs[0].text` 并清空其余 run，而 `paragraph.runs` **不含超链接内的 run**，`paragraph.text` 却包含超链接文字 | 段落含超链接 + replace_only 模式 → 原文残留、超链接文字被甩到译文尾部，同段中英混杂 |
| 4.2 | `core/word_document.py:1499-1509` `_prepend_paragraph_text` | 同上，编号标签写进 `runs[0]` 跳过超链接 run | 段落以超链接开头 + 自动编号 → 编号掉到段落中间 |
| 4.3 | `core/word_document.py:555-579` | `all_paragraphs = [*doc.paragraphs, *表格内段落]`，表格段落全排在正文之后，**不是文档真实顺序** | 正文列表 → 表格列表 → 正文列表（同 numId）→ 编号顺序错乱 |
| 4.4 | `core/word_document.py:408` vs `:386` | 先算 front matter 保护边界，但 `_flatten_automatic_numbering` 对**含受保护区在内的全部段落**执行 | 勾选「保护封面和目录」→ 封面/目录虽不翻译，但自动编号被扁平化成正文文字并删除 `numPr`，**受保护区域仍被改写** |
| 4.5 | `core/word_document.py:489-531` | replace_only 模式对单元格用 `cell.text = ...` 赋值 | 单元格含嵌套表格/多段落/局部加粗 → 整个单元格被压成一个纯文本段落 |
| 4.6 | `core/word_batching.py:589-615` `_pack_sentences` | 多个句子打包进同一 part 时 `"".join(current)`，句间空格与换行丢失 | 段落超 `split_paragraph_chars`（默认 3000）才触发。**主会话复验**：`'A.\nB.\nC.'` → `'A.B.C.'`。丢的是**送给模型的原文**（译文侧 `_join_translated_parts:543` 会补空格），影响翻译质量而非产物可读性 |
| 4.7 | `core/word_document.py`（全链路） | python-docx 的 `p_lst`/`tbl_lst` 只取 body 直接子节点，`w:sdt`（内容控件）与 `w:ins`（修订插入）包裹的内容完全不可见 | 带内容控件的模板文档、或开启修订未接受的文档 → **静默漏译且无任何告警** |

### Excel

| # | 位置 | 缺陷 | 触发 |
|---|---|---|---|
| 4.8 | `core/xlsx_patcher.py:1166-1173` `_sync_shape_extent` | `anchor.iter(a:ext)` 是**递归遍历**，组合图形内每个子形状的 `a:ext` 都被覆盖成整个锚点的 cx/cy。**主会话复验：已确认递归语义**（虽有 `xfrm` 父节点过滤，但组内子形状各自都有 `a:xfrm/a:ext`） | 表内有组合图形 + 行高变化 → 组内所有子形状被撑成整组大小，图形彻底变形叠在一起 |
| 4.9 | `core/xlsx_patcher.py:1530-1556` | 行高自动调整对**任何**估算出换行的行无条件写 `ht` + `customHeight="1"`，不检查该行是否真被翻译过 | 某行手工设了行高、本次一字未翻 → 行高被强行改写（实测 20 → 292.6），未翻译区域排版被破坏 |
| 4.10 | `core/xlsx_patcher.py:1257` | 行高估算把公式的**源码文本**（`"=" + 公式串`）当成内容参与换行计算 | 长嵌套公式 → 该行被无谓撑高 |
| 4.11 | `core/excel_coverage.py:256` | `ws_values[cell.coordinate].value` 在 `read_only` 表上每次重新解析整张 sheet XML，O(n²) | 补译模式打开含大量公式的表 → 实测 200/800/1600 个公式格耗时 0.18s / 2.07s / 7.69s，万级公式表卡死到分钟级 |
| 4.12 | `core/excel_coverage.py:260-263` | `_is_generated_original_sheet` 用 `sheet_name[:-3]` 反推原名，不处理被截断到 31 字符或加了 `_2` 去重后缀的克隆名 | 原分表名 >27 字符 → 补译时把自己生成的 `_原文` 分表当成待译内容重复翻译 |

### PDF

| # | 位置 | 缺陷 | 触发 |
|---|---|---|---|
| 4.13 | `core/pdf_image_translation.py:2412` `_pdf_render_scale()` = 300/72，无像素上限 | 渲染 DPI 固定，页面尺寸不设封顶 | 关闭「跳过大幅面页」（**默认就是关**）后处理 A0 图纸 → 单页 1.4 亿像素，pdfium 位图 ~558MB + PIL 副本 ~418MB，render-ahead 允许 并发+2 页同时在内存 → 数 GB 峰值，低配机 OOM |
| 4.14 | `core/pdf_image_translation.py:3355-3374` | 占位页字体候选只有 macOS 和 Linux 路径，兜底 `ImageFont.load_default()` | Windows 上任何一页失败 → 占位页中文说明渲染成方框/乱码，用户看不懂失败原因 |
| 4.15 | `core/pdf_image_translation.py:3706-3718` | 全部页都判为大幅面的 PDF，产物只是源文件原样副本，状态仍报 `completed` / `success=true` | 整份 A3 图纸集 + 开启跳过 → 用户拿到"翻译完成"的文件，一个字没翻 |

---

## 5. P2 · 发布流程与工程纪律

### 5.1 流水线自己降级还标"正式版"

`.github/workflows/build-distributions.yml:43-56` 在缺 Apple secrets 时，稳定 tag
**不是失败**，而是走 `temporary_signing=1` 分支，`:311-315` 仍以 `prerelease: false`
发布正式 Release。

`scripts/sign_macos_app.sh:32-35` 内部的 `FORMAL_RELEASE=1` 硬门是真的（缺 identity/notary
profile 直接退出），但**分类在更上游就降级了，硬门根本没被触发**。

README.md:29 写的是「正式 Release 均使用 Developer ID 签名和 Apple 公证」——这个「均」
在密钥缺失或证书过期时不成立。`AGENTS.md` 第 9 条已记录 V8.1.0 和 V8.0.1 栽过这个坑，
但堵的是"发布说明别照抄"，没堵住"流水线自己降级还标正式版"。

**这条属于产品/发布决策，需要用户自行判断如何处理。**

### 5.2 Windows 构建不跑任何测试

`.github/workflows/build-distributions.yml:190-257`：Windows job 没有 `quality_gate.ps1`、
没有 `unittest discover`、没有 `npm run check`、没有 `cargo test`/`cargo check`，
也没有 macOS 那样的产物验证（对比 `:163-175` 与 `scripts/build_macos_package.sh:105-114`）。

后果：只在 Windows 上复现的回归（2.2 的 `open`、5.3 的控制台窗口）在 CI 里永远不会被发现。
Windows 安装包在**没有任何自动化检查**的情况下被打进正式 Release。

### 5.3 Windows 启动会多弹一个黑色命令行窗口

`packaging/sidecar/translator_sidecar.spec:138` 的 `console=True` + `src-tauri/src/main.rs:250-262`
没有 `creation_flags(CREATE_NO_WINDOW)`。冻结 sidecar 是 console 子系统程序，被
`windows_subsystem = "windows"` 的 GUI 父进程启动时，Windows 会为它分配新控制台窗口。
用户关掉那个窗口会杀掉后端。

### 5.4 sidecar 启动失败 = 静默闪退

`src-tauri/src/main.rs:353-357`、`:364-365`：`spawn_sidecar` 失败时 `setup` 返回 `Err`，
`build().expect(...)` 直接 panic，**没有任何用户可见的错误对话框**（`tauri-plugin-dialog`
已装但没用上）。

12 秒启动超时（首次启动 Gatekeeper 要扫描整个 PyInstaller onedir，很容易超）、被 quarantine
拦截、或健康检查失败 → 应用静默闪退，用户看不到任何原因。

### 5.5 冻结产物冒烟测试写好了但从未接入

`scripts/run_frozen_smoke.py` 全仓库无调用方（workflow、build 脚本、测试都不引用）。
任何 PyInstaller 打包缺失（hiddenimports 漏项、spec `excludes` 误删依赖——注意
`:75-107` 排除了 cryptography / lxml 子模块 / PIL 插件等一大批）都要等用户运行时才暴露。
附带：`run_frozen_smoke.py:26` 还留着 Qt 时代的 `QT_QPA_PLATFORM` 残留。

### 5.6 temporary-test 渠道会永久掐断 macOS 更新检查

`.github/workflows/build-distributions.yml:284-288` 产出的资产名带 `_TEMP_SIGNED_TEST`
后缀，而 `core/update_checker.py:114-126` 的 `_expected_asset_name` 只接受
`Translator_macOS_arm64_<ver>.dmg` 精确匹配。

一旦以 temporary-test 渠道发过版，所有 macOS 用户的更新检查永久返回 `release_not_ready`，
既不报错也不提示新版存在。Windows 侧因文件名不随渠道变化反而正常——同一次发布两平台行为分裂。

### 5.7 日常 push / PR 无 CI

`.github/` 只有 `build-distributions.yml`，触发条件是 `workflow_dispatch` + `push: tags: v*`。
回归要到发版当天才在构建机上暴露。`AGENTS.md:18` 用"打标签前必须手动跑
`run_release_env_tests.sh`"兜底，靠人执行。

### 5.8 测试套件的机制性盲区

`tests/` 里 **0 个 skip**，不是因为什么都测到了，而是**没有 skip 机制**。所有 Windows COM /
Office 自动化路径（`core/excel_automation.py`、`core/word_converter.py`）都用
`patch.object(platform, "system", return_value="Windows")` 配合假 `pythoncom` 模块伪造，
77 个测试文件中 50 个用了 mock。

即：`.xls` / `.doc` 旧格式转换这类能力在开发机上**从未真正端到端跑过一次**。绿灯不等于这条路能走通。

---

## 6. 需要修正的子代理结论（不进修复清单）

审计过程中主会话复验发现的**假阳性、定级错误与标签误用**，记录在此以免误导后续修复优先级。

| 子代理结论 | 修正 |
|---|---|
| TM「模糊匹配」「术语一致性」= **假的** | **不是 bug。** 全量搜索 UI / README / CHANGELOG 零命中——产品从未声称过这两个功能。TM 就是精确哈希匹配。「代码里没有」≠「假的」 |
| Word `_pack_sentences` = **致命** | **降级为中。** 子代理称"译文黏连不可读"不成立——`_join_translated_parts:543` 在重新拼接译文时对非 CJK 目标语言用 `" ".join` 会补空格。真正丢失的是**送给模型的原文**分隔符，影响翻译质量，不影响产物可读性。且触发门槛窄（需单段超 3000 字符） |
| PDF「文字层提取与翻译」= **假的** | **标签误用。** 该链路本就是「整页截图 → 图像模型重画 → 图片拼回 PDF」的设计，CHANGELOG 一直称其为"版式翻译"/"整页译图"。**但仍需让用户知道一个真实边界：输出 PDF 全是位图，不可搜索、不可复制、无可选文字。** 这是设计取舍，不是缺陷 |
| Excel `task_runner.py:1148-1150` 输出路径撞车 | **不可达（子代理已自行排除）。** `source_root` 恒为扫描根目录或单文件父目录，`relative_to` 不可能失败 |
| Excel `excel_coverage.py:256` 抛 `AttributeError` | **不成立（子代理已自行改判）。** `ReadOnlyWorksheet._get_cell` 存在且能取值，只是慢——已改列为性能问题（4.11） |
| 双击「开始翻译」造成重复提交 | **降级为低。** 后端 `start_task` 在 `self._lock` 内串行化并抛 `TaskConflictError`，数据无损，仅是用户看到一条无意义的 409 错误 toast |
| `core/image_detector.py` = 死代码 | **属实但是有意为之。** `docs/KNOWN_ISSUES.md` 的 VAL-005 / VAL-006 已明确记录"保留源码但不启用"的决策。不应作为缺陷计入 |
| 引擎默认模型 ID 过时（`claude-sonnet-4-6`、`gpt-4o`、`glm-4`、`qwen-max`） | **属实但不可达。** `core/engine_dispatcher.py:136/144/150/154` 永远显式传 `model=cloud_model`。属误导性死默认，优先级低。唯一用户可见的是 `ui/src/views/settings.ts:843` 的占位符"例如 gpt-4o-mini" |

---

## 7. 明确确认为健康的部分

为避免这份报告造成"项目到处是坑"的错误印象，以下是**经复验确认做得扎实**的部分：

**安全面**
- **无 XSS**：用户可控数据（文件名、译文、错误消息、TM 条目）一律走 `textContent` /
  `createTextNode`；`innerHTML` 写入点只有清空和一段静态表头。
- **无 SQL 注入**：TM 全部参数化，f-string 只用于拼 `?` 占位符。
- 只监听 `127.0.0.1`（不是 0.0.0.0）；CORS 白名单无通配符，`TRANSLATOR_DEV_ORIGIN` 只接受回环 origin；
  token 每次启动随机 32 字节。
- `api/launcher.py:51-58` 端口分配：`bind(("127.0.0.1", 0))` 拿系统随机端口后把 socket
  直接交给 uvicorn，**无 TOCTOU、无占用问题**。
- Tauri 权限面干净：`capabilities/default.json` 只开 `core:default` + `dialog:allow-open/save`，
  没有 shell/fs 插件，`Cargo.toml` 也没引入。
- `key_overrides` 是 thread-local，并发任务凭据不串。

**README 承诺核实成立**
- **「诊断不含 API Key、原文、译文或完整 Prompt」成立**：`core/diagnostics.py:53-120`
  开头就 `del source_root, task_artifacts`，只写 manifest/metrics/runtime 三个文件，
  定位符是 sha256[:16]，错误码是有限枚举。有意思的是该文件里还有 15 个会写出原文和
  单元格坐标的函数——全部零调用，正是因为它们被架空，这个承诺才成立。
- **「更新只提示不自动下载替换」成立**：`core/update_checker.py` 全文只 GET release JSON
  和 `.sha256` 文本，无 `tauri-plugin-updater`，外链被 `main.rs:148-166` 限制在四个
  GitHub 域名前缀。
- **「仅接受与本机架构匹配且带 SHA-256 的完整包」成立**：`update_checker.py:91-154`
  架构映射 + 精确文件名匹配 + 同名 `.sha256` 唯一性校验。
- **「首次启动不迁移旧数据」成立**：`core/app_paths.py:16-48` 无任何旧路径探测，
  全仓库无迁移代码路径。
- **macOS 12.0 最低版本是全项目做得最扎实的一条**：声明（`tauri.conf.json:42`、`app_meta.py:13`）、
  编译期 `MACOSX_DEPLOYMENT_TARGET`/`RUSTFLAGS`、产物 Mach-O 逐切片扫描
  （`scripts/verify_macos_minimum_version.py`）、静态校验四层都在。

**设置持久化**
- `settings.py:1203-1238` 原子写：mkstemp 同目录 → write → flush → `os.fsync` →
  `os.replace` → 目录 fsync，**不可能截断**。
- `settings.py:1174-1200` 跨进程 `fcntl.flock` / `msvcrt.locking` + 进程内 per-path RLock。
- schema 版本闸门真实：版本不符时 `can_write=False`，旧文件原样保留。
- Key 掩码不泄漏长度；`core/model_catalog.py` **没有任何写死的模型 ID**，实时拉 `/models`。

**PDF（经实机验证，非仅读代码）**
- **坐标系正确**：`PdfMatrix().scale()` + `new_page`，非对称测试图往返后方向不变，无上下翻转。
- **旋转页正确**：手工构造 `/Rotate 90` 的 A3+A4 页，`page.get_size()` 已归一化 `/Rotate`
  和 CropBox，大幅面判定与渲染比例均正确。
- **大幅面跳过不丢内容**：矢量 `import_pages` 直传、页序正确、计入 summary、
  快照带 `skipped_oversize`、UI 有「按幅面跳过」标记。
- 页码 off-by-one 在渲染/装配/导入三处一致，无错位；临时文件用 `TemporaryDirectory` 管理。

**Word**
- **`e0fd1e4` 的 `id()` 身份修复是正确的**：`write_bilingual_docx:388-402` 在同一次遍历里
  同时建列表和 id 集合，强引用活到函数结束，id 不会被回收复用。`AGENTS.md` 第 8 条记的
  那个教训这次没有残留隐患。
- `write_untranslated_docx` 补译模式的按位置回写是安全的：重新快照列表并在插入前比对文本，
  索引错位会被跳过而非写错位置。

**Excel**
- 补丁式写入器名副其实：实测富样式工作簿回写后只有 `styles.xml` + 被改的 sheet XML 变动，
  其余 part **字节不变**。
- 共享公式主控权让渡（`_promote_shared_formula`）实测正确，`calcChain` 正确丢弃。
- 数字/日期单元格不被转文本；`mergeCells` 字节保留。

**其他**
- `core/word_converter.py` 所有 subprocess 都带 timeout 且 `finally` 里 terminate/kill。
- 后台 runner 线程即使异常死亡也不会让任务永远 running（`needs_poll()` 转 False 后
  `_pump_runner` 兜底发 error 终态）——注意这与 3.7 的 Word 场景不同。
- 585 测试 / ruff 0 问题 / tsc 0 错误 / TODO 全仓仅 3 条且都是故意的防御性 `NotImplementedError`。

---

## 8. 建议的处理顺序

按「用户会不会拿到错东西」而非「修起来难不难」排序：

**第一批（静默错误，必须先修）**
1. 1.1 failover `chat` 转发 —— 影响面最大，且已确认测试盲区
2. 1.2 32767 字符守卫 —— 一行判断的事，后果是文件打不开
3. 1.3 非法 XML 字符留下假成功文件
4. 1.7 + 1.6 TM `delete_all_entries` 与恢复丢词条 —— 数据安全
5. 1.4 / 1.5 PDF 质检与 gpt-image-1 尺寸 —— 前者是质量兜底缺失，后者会烧钱

**第二批（100% 失效的功能）**
6. 2.1 bulk/pin 路由遮蔽 —— 调整声明顺序即可
7. 2.2 Windows `open` —— 配合 5.2 一起做，否则改完也没有 CI 能验证
8. 2.3 工作台字段名 —— 抄 `tasks.ts` 的读法
9. 2.5 版本号硬编码 + 把 UI 源码纳入 `verify_release_metadata.py`

**第三批（稳定性与数据）**
10. 3.1 设置 lost update
11. 3.2 + 3.3 TM 锁与 WAL（一起做）
12. 3.4 + 3.5 SSE 落盘 O(n²) 与任务字典泄漏
13. 3.7 Word `_run` 缺兜底 except/finally

**第四批** 其余排版正确性与工程纪律条目。

**需要用户决策而非技术修复**：5.1（流水线降级仍标正式版）、以及是否要把
「输出 PDF 不可搜索」写进产品说明。
