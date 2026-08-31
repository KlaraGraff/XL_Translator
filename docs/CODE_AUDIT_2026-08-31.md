# 全仓 Bug 审查报告（2026-08-31 · V9.4.0 发布后）

本文由各审查线回传的结构化结果自动渲染，**不要手工编辑**——下一条审查线跑完重渲染时会覆盖。要补充结论请另开文档。

## 方法与范围

17 条独立审查线，逐条串行派发（不并行）。每条线拿到同一份预先算好的基线：`pytest -q` → **1664 passed, 249 subtests passed, 31s**，ruff / tsc / cargo clippy 均干净；因此所有发现都在现有测试覆盖之外，不是回归。每条线约 60 次工具调用的成本上限，到顶就回传已有结论（宁可少报，不许空手）。

审查线一律不得写入用户真实数据目录（`~/Library/Application Support/Translator`）：脚本必须在 `import config` **之前** 把 `TRANSLATOR_APP_DATA_DIR` 指到临时目录，并断言 `config.APP_DATA_DIR` 落在 /tmp、/var/folders 或 /private 之下。这条纪律是本轮开跑后补的——此前有代理直接写坏了用户的 `keys.json`，触发了「静默备份并重置为空」的路径。

**当前进度：14/17 条审查线回传，累计 50 条发现（高 13 / 中 25 / 低 12）。**

| 审查线 | 范围 | 发现 |
|---|---|---|
| R1 | 2026-08-29 审计 14 条高危的回归核验 | **0 条** |
| R2 | 上一轮审计中危 30 条 + 低危 27 条修复抽验 | 中 2 |
| C1 | 并发、线程、锁、竞态、死锁 | 高 1、中 2 |
| C2 | 资源生命周期：临时文件、子进程、数据库连接、文件句柄 | 中 1、低 2 |
| P1 | 持久化 / schema 迁移 / 旧数据兼容 | 高 1、中 2、低 3 |
| E1 | 错误处理、异常吞没、用户可见错误文案 | 高 1、中 1 |
| A1 | HTTP API 层（api/app.py、api/task_manager.py、api/launcher.py | 高 1、低 1 |
| G1 | 模型引擎 / 故障转移 / 调度 / Token 成本 | 高 3、中 6、低 1 |
| T1 | 翻译质量链路：过滤、覆盖率、残留、语言识别、续译 | 高 3、中 1 |
| M1 | 翻译记忆库（TM | 高 1、中 2、低 2 |
| F1 | 前端工作区、任务中心、客户端 | 中 1、低 1 |
| F2 | 前端设置页、记忆库页、更新流程、样式 | 中 1 |
| D1 | Excel 翻译管线 | 高 1、中 3、低 2 |
| D2 | Word 翻译管线 | 高 1、中 3 |

---

## 高危（13 条）

### 高-1 用户点了停止之后，三条「恢复类」链路仍会等到槽位并发出付费模型调用，还会自动重试一次

`C1` · 置信度 high · new

**位置**：`core/coverage_review.py:231`、`core/coverage_review.py:239`、`core/coverage_review.py:246`、`core/word_task_runner.py:3557`、`core/word_task_runner.py:3562`、`core/word_task_runner.py:3641`、`core/word_task_runner.py:3645`

**机制**：这三处（Excel/Word 补译复核的成对仲裁、Word 语义仲裁、Word 页眉页脚补译）拿并发槽位时写的是 `api_scheduler.slot(weight, category=API_REQUEST_CATEGORY_RECOVERY)`，**没有传 should_stop**。而 WeightedApiScheduler.acquire_lease / FairApiGroupScheduler.acquire_lease 里那行是 `self._condition.wait(timeout=0.1 if should_stop is not None else None)`——不传就退化成 `wait(timeout=None)`，整个等待过程既没有超时、也没有任何停止检查。等待结束拿到槽位后，`with` 体里直接 `raw = engine.chat(system_prompt, user_payload)`，中间**没有再检查一次停止**（对比 core/mixed_language.py:786 那处是有 `if should_stop and should_stop(): return None` 的）。出了异常再走 `handle_api_concurrency_limit(...)`，同样没传 should_stop，于是 `_interruptible_sleep(delay, None)` 睡满整段退避；coverage_review.py:246 拿到 decision 后还是 `if decision is not None:` 就无条件递归重发同一批。上游的 `arbitrate()`（coverage_review.py:97）确实查了 stop_event，但那只是**批次入队前**的闸门：已经进入 worker 线程、正卡在槽位上的那几批，停止信号一点都传不进去。

**后果**：用户在「补译复核 / 语义仲裁 / 页眉页脚补译」阶段按下停止后，已经在飞的每个 worker 线程都会：先不可中断地等槽位（实测停止后 3 秒仍在等），拿到后照常发一次完整的付费模型调用，遇到 429 再不可中断地睡最长 30 秒、然后再发一次。按 concurrency 8～16 算，就是停止后仍有十几次真金白银的调用被打出去，界面上任务还停在「停止中」十几秒到两分钟。这直接踩项目硬约束里的「白花钱」。

**复现**：已复现。c1_no_should_stop.py：把连接组用 normal 流量占满，同时起两条 recovery 线程——不带 should_stop 的那条在 stop 置位 3 秒后仍 `STILL BLOCKED`，最后 `('acquired', 3.32)`（说明它最终还是拿到槽位继续发请求）；带 should_stop 的那条 `('cancelled', 0.31)`。c1_backoff_stop.py：停止已置位的情况下连打 4 次 429，无 should_stop 版本分别阻塞 1.64/3.38/9.30/13.10 秒共 27.4 秒，有 should_stop 版本全部 0.00 秒。拿到槽位后直接 engine.chat（中间无停止分支）这一跳为代码路径确认。

**修法**：三处都按 core/pdf_image_translation.py:4109/4148 的写法补齐：(1) `slot(...)` 加 `should_stop=<停止回调>`，并捕获 ApiSchedulerAcquireCancelled 当成「这一批没判出来」返回；(2) 进入 `with` 之后、调 engine.chat 之前再查一次停止就返回（照抄 core/mixed_language.py:786 的那两行）；(3) `handle_api_concurrency_limit(...)` 补 `should_stop=`；(4) coverage_review.py:246 的重发前加停止检查。停止回调本来就有：coverage_review 已经通过 `stop_event` 参数拿到了（core/task_runner.py:2410 传的 self._stop_event），Word 两处需要把 _WordRecoveryPool 的 self._should_stop 透传进去。

### 高-2 settings.json 只要出现一个非 UTF-8 字节，读写全线 500，连维护页「重置设置」都 500——只剩会连带删光 Key/记忆库/历史的「完整重置」

`P1` · 置信度 high · new

**位置**：`settings.py:1500`、`settings.py:1501`、`settings.py:1497`、`api/app.py:592`、`core/maintenance.py:196`

**机制**：`_inspect_settings_file()` 用 `SETTINGS_PATH.read_text(encoding="utf-8")` 读文件，但只 `except OSError`（settings.py:1501）。`UnicodeDecodeError` 是 `ValueError` 的子类、不是 `OSError`，于是它从分类器里直接逃出去，`unusable/unreadable` 的备份-重建状态机根本没机会跑。所有入口都建立在这个函数上：`get_settings_schema_status()`、`load_settings()`、`recover_settings_file_if_needed()`、`save_settings()`（包括 `replace_incompatible=True` 这条「显式重置」的出路，它在 settings.py:1766 先调 `_inspect_settings_file()`）全部原样抛出。对照组能证明这是疏忽而不是设计：`core/task_history.py:132` 的 `except (OSError, ValueError, TypeError)` 就把这类错误接住了。

**后果**：设置页打不开（GET /api/settings 500）、任何保存 500、维护页 overview 500、维护页「重置设置」500。唯一还能走通的是「完整重置本地数据」，而那条路会把 API Key、翻译记忆库、任务历史一起删掉。直接违反硬约束「读不动就备份旧文件、新建可用的，绝不能停在报错不动」。触发路径不玄：settings.json 里本来就装着中文（custom_prompt、domain_name_overrides、自定义目标语言名），用户拿旧编辑器打开再按 GBK/ANSI 存回去，或者一次文件系统位翻转，就是这个状态。

**复现**：已复现。`p1_corrupt.py latin1`：status / load_settings / recover / save_settings / force_reset 五步全部 `RAISED UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9 in position 133`，backups 目录为空。`p1_api_nonutf8.py`（把一份合法 settings.json 用 gb18030 编码写盘，走真实 FastAPI TestClient）：`GET /api/settings -> 500`、`GET /api/maintenance/overview -> 500`、`POST /api/maintenance/clear settings(confirmation=true) -> 500`、`PUT /api/settings -> 500`、`POST /api/maintenance/reset-full -> 200`。既有 tests/test_data_schema_recovery.py 只覆盖 `"not json"` / `"{oops"` 这类合法 UTF-8 的坏内容，没有编码维度。

**修法**：settings.py:1500 改成 `SETTINGS_PATH.read_bytes().decode("utf-8")` 并把 `except OSError` 扩成 `except OSError` + `except (UnicodeDecodeError, ValueError)` 两支：解码失败属于「内容坏了但文件读得出来」，应当归到 `unusable`（可以备份后重建），而不是逃逸。顺手把 BOM 一并处理掉（`decode("utf-8-sig")` 或读前剥 `﻿`）——目前带 BOM 的文件被判 unusable 直接重建，用户配置白丢一次，虽然有备份但完全没必要。

### 高-3 OpenAI 兼容引擎把已经答对、已经付费的译文当解析失败静默丢弃，报错文案里唯一的线索也被擦成空白

`E1` · 置信度 high · new

**位置**：`engines/openai_engine.py:236`、`engines/base_engine.py:104-122`

**机制**：_extract_chat_completion_text 对 message.content 只接受纯字符串：`return content if isinstance(content, str) else ""`。只要某个 OpenAI 兼容中转/模型把 content 返回成非字符串（最常见是新协议下的 content-parts 数组，如 `[{"type":"text","text":"..."}]`），这一行就把模型已经答出来的译文原地丢掉换成空串。空串被传进 parse_response 后，json.loads("") 解析失败，抛出的 ValueError 文案是「OpenAI 响应解析失败...大模型原始内容：」，冒号后面是空的——用户和调试日志看到的都不是真实原因（content 格式不被本程序识别），而是一句被自己清空过的空话。DashscopeEngine / ZhipuEngine 都继承 OpenAIEngine 的同一条路径，同样会中招。

**后果**：用户看到一句「解析失败，原始内容：（空）」，既不知道是接口坏了还是本程序识别不了这种格式，也没法把这段报错拿去问客服；更关键的是这次 API 调用已经产生真实的模型输出（也就是已经真金白银付费成功），却被当成失败整批丢弃、计入未翻译，用户要重新发起才能拿到译文，等于重复付费。

**复现**：已复现：脚本 /private/tmp/claude-501/-Users-lijianwei-vibecoding-claude-XL-Translator/9bc2c740-3ad6-41c8-9853-d1ce6bd7a8c6/scratchpad/repro_openai_content_list.py，mock `_post_json` 返回 `content=[{"type":"text","text":"这是应该出现的译文"}]`（模拟模型已经正确回答），实测 `translate_batch` 抛出 `ValueError: OpenAI 响应解析失败 (无法匹配为包含 1 条记录的数组)，大模型原始内容：`，原始内容字段确认为空，尽管「模型」实际已经给出了译文。另确认此 ValueError 不带 status_code，engine_dispatcher 层面是否会为它重跑整批（可能造成二次付费）不在本文件范围内未查证。

**修法**：_extract_chat_completion_text 遇到非字符串 content 时不要直接转空串：先尝试从数组里按 type=="text" 拼出正文，实在提取不到再抛异常，且异常文案里带上 `repr(content)[:200]` 之类的真实原始返回，而不是让 parse_response 拿着空字符串报出一句自己都解释不清的话。

### 高-4 任务日志没有任何上限，整份写进 task_history.json，并被 GET /api/tasks 每 4 秒原样回吐——历史文件与轮询报文一起线性膨胀到 10 MB 量级

`A1` · 置信度 high · new

**位置**：`api/task_manager.py:820`、`api/task_manager.py:844`、`api/task_manager.py:1840`、`api/task_manager.py:1852`、`api/task_manager.py:1773`、`core/task_history.py:53`、`ui/src/views/tasks.ts:959`、`ui/src/views/tasks.ts:1665`

**机制**：三处叠加，根因是 ApiTask.logs / ApiTask.events 全程无上限。

(1) `_status_payload()`（task_manager.py:820）把 `list(task.logs)` 整份放进报文，runner 每写一行日志就 append 一条，全程不裁剪；`_retire_terminal_task()`（:1773）退休时只裁 `task.events` 到 TERMINAL_EVENT_TAIL=300，`task.logs` 一条都不裁。core/task_runner.py 里 `self._log(...)` 有 78 处调用点，pdf_image_translation.py 里 79 处，大多在按文件/按批/按页的循环体内——一次中等任务几百到几千行是常态。

(2) `_append_event()`（:1840-1852）在 `_history_write_due()` 为真时（HISTORY_WRITE_INTERVAL_SECONDS = 1.0，翻译期间日志密集，实际就是每秒一次）把这份带全量 logs 的记录交给 `TaskHistoryStore.upsert()`；而 `upsert()`（core/task_history.py:53-64）是「读整个文件 → 过滤 → 整个重写」，limit=200 条记录**每条都带自己的全量 logs**。于是翻译期间 sidecar 以约 1 Hz 的频率把一个越来越大的 JSON 全量读+全量重写。

(3) `list_tasks()`（:844）直接返回 `self._history.records()`——整个历史文件、200 条记录、每条的全部日志。前端 `ensureBackgroundLoop` 12 秒轮一次（应用启动即开始，与视图无关），任务中心挂载时 `fastPollTimer` 4 秒轮一次（tasks.ts:959 / 1665），每次都要 JSON.parse 这份报文并对 200 条记录逐条 upsert + renderList。

仓库里没有任何日志条数上限常量，tests/test_task_history.py 也没有相关断言——不是「登记为已知代价」，是漏了。

**后果**：用得越久越卡，且不可逆（除非用户去「清空任务历史」）。实测：200 条历史 × 每条 400 行日志 → task_history.json 10.72 MB；GET /api/tasks 返回 10.72 MB 报文，前端每 4 秒解析一次这么大的 JSON，WKWebView 里就是任务中心持续掉帧、切视图卡顿。翻译进行中还叠一层：每秒一次全量读写 10.7 MB 文件（实测单次 57–69 ms），等于整个翻译过程持续约 10 MB/s 的磁盘写放大，而且发生在 `_pump_runner` 送 SSE 事件的同一个线程上——事件推送和进度更新会被这 60 ms 顶住。线性可外推：50 行/条 → 1.36 MB / 8 ms；100 行/条 → 2.68 MB / 15 ms；400 行/条 → 10.72 MB / 57 ms。一个几百页 PDF 或几十文件的 Excel 批次单条记录就能顶到几千行，200 条上限意味着文件没有天花板。此外 12 条退休终态任务各自的全量 logs 一直留在内存里。

**复现**：已复现。
1) scratchpad/a1_logs.py：写 200 条各 400 行日志的记录 → 「history file size: 10.72 MB」「one upsert on a full history: 69 ms」「HISTORY_WRITE_INTERVAL_SECONDS = 1.0」。
2) scratchpad/a1_listtasks.py（走真实 FastAPI TestClient）→ 「GET /api/tasks -> 200, 10.72 MB, 49 ms」「recent[0] has logs: 400」，确认日志确实原样出现在轮询报文里。
3) scratchpad/a1_scale.py → 50/100/400 行三档的文件大小与单次 upsert 耗时，线性。
4) scratchpad/a1_sanitize.py → `_sanitize_task_data` 在 8000 行时 33 ms/次，确认脱敏不是主要成本，主要成本在文件全量读写和报文体积。

**修法**：三处都要动，缺一不可：
1) 落盘记录裁剪日志：`_persist_task` / `_status_payload(include_result=True)` 走持久化路径时只保留日志尾部（比如最后 200 条）加一个 `logs_truncated: true` + 总条数；内存里的 `task.logs` 也设一个上限（deque(maxlen=N)），并在 `_retire_terminal_task` 里像裁 events 一样裁 logs。
2) `list_tasks()` 不再回吐日志：`recent` 只回摘要字段（task_id/surface/state/terminal/时间戳/result 摘要），日志留给 `GET /api/tasks/{id}` 和 `/results` 按需取。前端 refreshRegistry 本来也只用 upsert 的摘要字段，detail 面板已经单独调 getTask/getTaskResult。
3) `TaskHistoryStore.upsert` 的「全量读+全量重写」在记录变小之后成本自然下来；如果仍嫌 1 Hz 太密，可以把运行中任务的节流从 1 s 放宽到 3–5 s（状态变化仍然立即写，语义不变）。
改完补一条测试：断言单条历史记录的 logs 长度有上限、且 GET /api/tasks 的报文里不含 logs。

### 高-5 连接链判定「全部失效」后没有熔断，后续每个批次继续重拨已知全死的连接，二分把请求放大到 45 次/批

`G1` · 置信度 high · new

**位置**：`core/failover_engine.py:32`、`core/failover_engine.py:91`、`core/failover_engine.py:186`、`core/engine_dispatcher.py:737`

**机制**：FailoverTranslationEngine._switch_from_locked 在 failover_candidates 返回空时只 `return False`，`_call_with_failover` 把原始异常原样抛出——`AllConnectionsExhaustedError`（failover_engine.py:32 定义）全仓库从未被 raise 过，是死代码。于是「整条链已全灭」这个信息从来没有传出去。上层 `_translate_batch_with_fallback` 收到的只是一个普通 503/超时，`_is_permanent_request_error` 判 False，于是照常二分重试；每个子节点又各自走一遍 failover（当前引擎已是耗尽状态，必然再失败），真实引擎里每个节点内部还有 tenacity 的 3 次尝试。单批 15 个二分节点 × 3 = 45 次 HTTP 请求，每次超时上限 CLOUD_REQUEST_TIMEOUT=120s。而且 self._exhausted 一旦集齐全部连接就再也不清空，后面每一个批次都从这个已死状态重新开始。

**后果**：网关宕机 / DNS 挂掉 / 账号整体被封时，任务不是快速失败，而是长时间假死：8000 格文件约 400 批，每批 45 次 120s 超时请求，并发 10 也要跑到小时级；界面上只滚「Excel 有 N 条未能翻译」，从头到尾不会出现一句「所有连接都已失效」。用户唯一的出路是自己按停止。

**复现**：已复现（g1_exhausted.py）：两条不同 Base URL 的连接都固定回 503，连续跑 5 个 8 条的批次，输出为 `after batch 1: cumulative engine dials = 16, exhausted=['c1','c2']` … `after batch 5: cumulative engine dials = 76`。即链路在第 1 批就已被判定全灭（exhausted 集齐两条），第 2～5 批仍各自重拨 15 次。测试里的 DeadEngine 是裸引擎，生产的 OpenAIEngine/ClaudeEngine 每次重拨内部还有 tenacity 3 次，实际是 45 次/批。

**修法**：在 FailoverTranslationEngine 里落地 AllConnectionsExhaustedError：`_call_with_failover` 在 `_switch_after` 返回 False 时抛这个异常（把原异常挂 __cause__），并置一个 self._dead 标志，之后所有调用直接快速抛出、不再发请求。`_translate_batch_with_fallback` 把 AllConnectionsExhaustedError 归入 `_is_permanent_request_error` 同级处理——不二分、不重试，直接把剩余批次全部计未翻译并向上抛一个明确的「所有连接均已失效」终止信号，让 task_runner 停止提交后续批次。

### 高-6 限流持续到阈值抛 ApiKeyTemporarilyUnavailableError 时，同一次 translate_texts 里已翻译并已计费的批次全部被丢弃

`G1` · 置信度 high · new

**位置**：`core/engine_dispatcher.py:690`、`core/engine_dispatcher.py:519`、`core/task_runner.py:2136`、`core/api_concurrency_control.py:310`

**机制**：`_translate_batch_with_fallback` 对 ApiKeyTemporarilyUnavailableError 直接 `raise`（engine_dispatcher.py:690）。在云端路径里这个异常从线程池的 `future.result()`（engine_dispatcher.py:519 的 `while future_map:` 循环内）抛出，直接穿出 `translate_texts`，函数体内已经累积在局部变量 `results` 里的所有成功批次随栈一起消失，一条都没有返回给调用方。task_runner.py:2136 只把它转成 fatal_error_message 结束任务。更糟的是异常抛出时线程池 `__exit__` 还会 wait 等已提交的 future 跑完——那些请求照发照计费，结果同样进不了任何地方。api_concurrency_control.py:28-33 的注释写着「Failing the whole task on the next 429 threw away every request already paid for, so the run now waits out a grace window」——宽限窗口只是把这个丢弃推迟了 120 秒，丢弃路径本身一行没改。

**后果**：上游持续限流两分钟，用户已经付过钱的几百上千条译文全部作废，任务以致命错误收场；下次续译要为同样的内容再付一次。这正好命中「停止后丢弃已付费成果」这条高危线。

**复现**：已复现（g1_lostwork.py）：121 条文本、batch_size=10、concurrency=2，中间一条触发 ApiKeyTemporarilyUnavailableError。输出 `items actually sent to (and billed by) the model: 80` / `translations returned to the caller: 0 -> every paid batch discarded`。

**修法**：不要让这个异常裸穿 translate_texts。在 `translate_texts` 的 `while future_map:` 里 try/except 捕获，记进 run_stats（新增一个 fatal 字段），停止 `_submit_next()`，把已收到的 `results` 正常返回；由 task_runner 读 stats 的 fatal 标志决定「部分完成 + 致命原因」而不是「全盘失败」。已翻译的部分要照常写盘和写 TM，未翻译的计入未翻译数，这样续译才只补差额。translate_texts_with_sources 同一处理。

### 高-7 纯 429 限流会触发批次二分，对正在限流的端点反而把请求数放大一个数量级

`G1` · 置信度 high · new

**位置**：`core/engine_dispatcher.py:692`、`core/engine_dispatcher.py:737`、`core/api_concurrency_control.py:172`

**机制**：`_translate_batch_with_fallback` 里限流的专用通道有一道硬上限 `concurrency_round < _MAX_CONCURRENCY_RETRY_ROUNDS`（=8）。跑满 8 轮之后（前 4 轮把并发从 10 降到 2，后 4 轮在最低档退避 2/4/8/16 秒），第 9 次失败就落到普通分支：429 不在 `_is_permanent_request_error` 的永久错误集合里，于是 `can_split` 成立，批次被一分为二重发。两个半批各自把 concurrency_round 重置为 0，又各自享有 8 轮限流重试 + 继续二分，三层二分共 15 个叶子节点。二分对限流毫无帮助——它不减少 token、只增加请求条数，等于在上游明确说「请求太多」的时候把请求数乘以 15。

**后果**：上游持续限流时本地变成请求风暴，把限流窗口拖得更长，还会连累同一个 key 上的其他任务；最后这些请求要么全部失败（20 条全部原样保留），要么在 120 秒宽限期到点后抛致命错误（叠加上一条，已付费结果一起丢）。

**复现**：已复现（g1_429split.py）：单个 20 条批次，引擎对每次调用都回 429（httpx.HTTPStatusError，body 为 OpenAI 风格 rate limit）。为了在秒级内数清请求把 `_backoff_sleep` 与 `_interruptible_sleep` 打桩为空。输出 `requests fired at an endpoint that answered 429 to every single one: 135` / `did the code SPLIT the batch under pure 429? -> yes | split retries recorded: 7` / `scheduler capacity walked to: 2`。生产环境退避是真实计时，但 8 轮上限一样会走到二分，只是时间被拉长。

**修法**：在 `can_split` 的判据里排除限流：`can_split = ... and not is_api_concurrency_limit_error(exc)`。限流耗尽 8 轮后应当保持原批次不动，交给上层「这个 key 暂时不可用」的路径（或直接触发换连接），而不是拆批再打。顺带把 429 的重试上限从「轮数」改成「累计等待时长」，与 MINIMUM_CAPACITY_GRACE_SECONDS 对齐，避免两套计时各说各话。

### 高-8 自动源语言预检没定出语言就回落 zh、多文件批次全局只取第一个语言，日/韩文件在补译模式下整份被判 ignored 不译

`T1` · 置信度 high · new

**位置**：`core/task_runner.py:1087`、`core/task_runner.py:1095`、`core/translation_filter.py:312`、`core/excel_coverage.py:236`

**机制**：阶段 2 的语言预检结果被压成**一个全局字符串**：`detected_sources` 是把所有文件的 `result.source_langs` 拍平去重后的列表，`source_lang = detected_sources[0]`（task_runner.py:1087）；一条都没有时回落 `get_default_source_lang()` = "zh"（:1095）。这个单一值随后被原样喂给 `_apply_deferred_resume_baselines` 和 `_rebuild_coverage_plans_after_preflight`，也就是喂给**每一个文件**的补译覆盖率计划。

而 `_is_source_script_text`（translation_filter.py:312-330）只在 `source_lang ∈ {ja, ko}` 时才把汉字当作待译原文。于是源语言一旦落成 zh 或 en，日文/韩文文件里的纯汉字内容（标题、条款名、表头）重新变回上一轮高-1 的「已经是中文 → 跳过」。

实测（t1_ja_sourcelang.py，日文单元格 `工事契約書` / `第一条 目的` / `本契約は、発注者と受注者との間の工事に関する。` / `設計図書`）：
```
source_lang=ja  summary={'covered':0,'source_only':4,'ignored':0}
source_lang=zh  summary={'covered':0,'source_only':1,'ignored':3}
source_lang=en  summary={'covered':0,'source_only':0,'ignored':4}   ← 连假名整句都 ignored
```
两条触发路径都很日常：
1. 预检请求失败 / 模型答 uncertain → `source_langs` 为空 → 回落 zh → 日文件 3/4 内容不译（上一轮高-3「整份原样输出」的同一形态，只是入口从候选正则换成了回落分支）；
2. 一次拖一个文件夹进来、里面英文件排在日文件前面 → `detected_sources[0]` = "en" → 那个日文件 **4/4 全部 ignored**，一个字都不翻。

另外注意 source_lang=en 那一行：`should_translate('本契約は…', 'zh', 'en')` 返回 True，但覆盖率计划把它判成 ignored——同一个判断在 translation_filter 和 excel_coverage 两处各有一套，结论不一致（这正是问询要点 6 说的「同一判断写了两遍」的实例）。

**后果**：用户选「自动识别」翻一批日文/韩文合同，预检抖一下或批次里混了别的语言文件，产出的文件跟原文逐字相同（或只翻了带假名的几句），任务显示成功。只有任务日志里一行「N 格未补译（默认跳过）」提示，用户对着几十页原文才发现。同时预检那一次模型调用的钱已经付了。

**复现**：已复现：t1_ja_sourcelang.py，输出见 mechanism 里的三行 summary（source_lang=en 时 4/4 ignored，source_lang=zh 时 3/4 ignored）。多文件批次取 detected_sources[0] 这一段为机制确认（读 task_runner.py:1080-1095 代码路径）。

**修法**：两处改：(1) 语言预检结果按文件保留而不是拍平成全局一个值——`file_language_preflights` 本来就是 per-file 的，`_rebuild_coverage_plans_after_preflight` / `_apply_deferred_resume_baselines` 应按 `result.source_langs[0]` 逐文件取源语言，而不是共用 `detected_sources[0]`；(2) 预检一条语言都没定出来时不要静默回落 zh：补译模式下应把该文件的源语言判定标成未知，并让 `_is_source_script_text` 在未知源语言 + 含假名/谚文/汉字时按「待译」处理（宁可多翻一次，也不要整份原样交付），同时在任务日志里出一条 WARN 而不是只在汇总行写「实际源语言=未确定」。

### 高-9 续译底稿资格核查只查「源文件新增内容」，不查「源文件删掉的内容」——用户删掉的段落/行原样留在新译文里

`T1` · 置信度 high · new

**位置**：`core/resume_detection.py:302`、`core/resume_detection.py:404`、`core/task_runner.py:794`、`core/word_task_runner.py:876`、`core/task_runner.py:2543`

**机制**：`baseline_missing_source_texts` 只做单向消耗式比对：遍历**源文件**计划里每条 `COVERAGE_SOURCE_ONLY` 文本，去底稿的文本多重集里消耗一次；剩下没消耗掉的算「源文件新增」。反方向——底稿里有、源文件里已经没有的文本——完全不看。

续译一旦通过核查，`process_path` 被整个换成上次的双语产物（task_runner.py:813 `process_path = candidate`，word_task_runner 同构）。也就是说**新产物是从旧产物文件长出来的**，源文件此后只用来做核查。源文件里被删掉的那些行/段落仍然完整地躺在底稿里，于是原样写进这一次的交付物，而且因为它在底稿里已经有译文，覆盖率计划判 covered，既不重译也不进任何报告。

实测（t1_excel_resume.py，底稿为 `工程概况/施工方案/质量保证措施` 三行双语，源文件做各种改动后跑核查）：
```
1 源文件未改                    missing=[]
2 源文件新增一行                 missing=['安全文明施工']
3 源文件删掉一行(施工方案)          missing=[]        ← 判定「底稿可用」
4 源文件改了一格文字              missing=['施工方案(修订)']
5 新增行文字与已有行重复           missing=['工程概况']
6 源文件重命名工作表              missing=[三条全部]
```
第 3 行就是缺口：用户把「施工方案」整行删掉后续译，核查放行、底稿被采用，新产物里「施工方案」及其译文照常出现。

模块 docstring 和给用户的 WARN 文案（「源文件比上次翻译时多了 N 处内容」）都只讲了新增这一半，产品对外承诺的却是「源文件改过就不拿旧产物当底稿」。

**后果**：用户在两次翻译之间删掉了作废条款、删掉了整张工作表、删掉了几个段落，然后点续译。交付出去的译文里这些内容仍然在——是「已经删掉的内容被当成正式交付件发出去」，而且报告里一个字都不提。合同/投标文件这类场景后果直接。

**复现**：已复现：t1_excel_resume.py，case「3 源文件删掉一行(施工方案)」输出 `missing=[]`（即核查判定底稿合格）。底稿被采用后旧内容随底稿进入产物这一段为机制确认（task_runner.py:813 `process_path = candidate`，产物由底稿文件生成）。

**修法**：在 `baseline_missing_source_texts` 里加反向核查：消耗完源文件全部 source_only 文本后，检查底稿多重集里是否还有**未被消耗、且属于源文段（非译文段）**的剩余项——Excel 按 (sheet, 源文半边) 计，Word 按段落源文半边计。有剩余就说明源文件删过内容，跟新增一样判 diverged 拒用底稿。给用户的文案也要分两种说法：「多了 N 处」/「少了 N 处」，别一律写成「多了」。若担心误杀（底稿里的译文半边被误算成源文），可以先只对 Excel 的整行/整表消失和 Word 的整段消失做检查，阈值放在「底稿里有源文半边但源文件里一次都没出现」这一条上。

### 高-10 自定义目标语言（繁体中文/粤语/日语变体）不在残留豁免表里，正确译文被判「残留中文」并被强制重置回原文

`T1` · 置信度 high · new

**位置**：`core/residual_classifier.py:36`、`core/translation_filter.py:519`、`core/residual_pipeline.py:70`、`core/engine_dispatcher.py:1027`、`core/tm_hygiene.py:56`

**机制**：`RESIDUAL_EXEMPT_TARGET_LANGS = frozenset({"zh", "ja"})` 是一张**只认内置语言码**的硬编码白名单。而自定义目标语言的码是 `x-custom-<base64>`（language_registry.py:89 `CUSTOM_TARGET_LANG_PREFIX`），永远进不了这张表。于是任何用汉字书写的自定义目标语言——繁体中文、粤语、文言文、日语的敬体/简体变体——都会被残留链路当成「译文里不该有中文」来处理。

两条后果叠加：
1. `translation_filter.py:519` 的 `_residual_cn_date_unit_issue` 对译文里的「2026年8月9日 / 18個月 / 500萬元」判 fail；`is_translation_redundant` 返回 True；`engine_dispatcher.py:1027` 的 `_apply_quality_filter` 执行 `results[src] = src`——**把正确的译文重置成中文原文**。这是上一轮高-2 的同一条重置路径，只是入口换成了自定义语言。
2. `residual_pipeline.py:70` 的豁免判断同样落空，整句被 `classify_residual_spans` 切成 `cn_date_unit + sentence_block`，进 `needs_review`，在报告里报成「残留未译」。

实测（t1_custom_lang.py，源文中文、译文繁体）：
```
=== custom target: 繁體中文 code: x-custom-57mB6auU5Lit5paH
  validate → fail ['residual_cn_date_unit']
  residual needs_review: [(('cn_date_unit','sentence_block'), ('本工程於','年','月','日完工','工期為','個月','總金額','萬元'))]
=== custom target: 粵語 / 日本語（敬体）  同上，全部 fail
ja baseline validate → pass []      ← 内置 ja 正常豁免
```
附带一处不一致：translation_filter.py:519 比对前做了 `_normalize_lang(target_lang)`，而 residual_classifier.py:199 / residual_pipeline.py:70 / tm_hygiene.py:56 三处都拿原始 `target_lang` 直接 `in` 判断——同一张豁免表四个调用点两套归一规则。

**后果**：用户新建一个「繁体中文」或「粤语」自定义目标语言去翻中文文档：凡是带日期、月数、金额的句子（合同里几乎每段都有），模型翻好的译文在写盘前被静默换回中文原文，报告里记成「质量校验回退」；剩下的句子被报成「残留未译」。用户为每一条都付过费，拿到的是原文。这是自定义语言功能里最典型的用法（要变体而不是要小语种），踩中概率很高。

**复现**：已复现：t1_custom_lang.py，三个自定义目标语言全部 `validate → fail ['residual_cn_date_unit']` + `needs_review` 非空，内置 ja 对照组 `pass`。从 fail 到 `results[src] = src` 的重置路径为机制确认（engine_dispatcher.py:1019-1030，逻辑与上一轮高-2 已确认的同一条）。

**修法**：豁免判断不能按语言码做集合比对，要按「目标语言是否使用汉字书写」来判。建议在 language_registry 里给自定义语言加一个「书写系统」标记（新建自定义语言时让用户选一次，或按显示名里是否含汉字/假名做默认推断），残留链路改为调 `target_lang_uses_han(target_lang, custom_target_langs)`。过渡期最低成本的兜底：`x-custom-` 前缀的目标语言一律豁免残留中文检查（自定义语言本来就没法给它定残留规则，宁可不查也不能把正确译文重置回原文）。同时把四个调用点统一走 `_normalize_lang` 归一，别留两套。

### 高-11 深度清洗只要有一个批次失败，其余批次已付费拿到的全部建议被直接丢弃，一条都不落库

`M1` · 置信度 high · new

**位置**：`core/tm_cleaner.py:805`、`core/tm_cleaner.py:821`、`core/tm_cleaner.py:608`、`core/tm_cleaner.py:624`、`core/tm_cleaner.py:510`

**机制**：`_run_cleaning_threaded`（云端引擎）与 `_run_cleaning_async`（Ollama）都是先把所有批次的建议收进内存里的 `suggestions`，最后统一 `tm_manager.persist_cleaning_suggestions(...)` 入库。但 `if batch_errors: raise TmCleaningBatchError(...)`（805/608）排在这句 persist（821/624）**前面**：只要 N 个批次里有任意 1 个抛异常（超时、5xx、连接断），函数直接抛出，那句 persist 永远执行不到，内存里那份成功批次的建议随栈一起消失。`_submit_batch` 里的 `handle_api_concurrency_limit` 只对「并发限流」类错误重试，超时/网络错误不重试，直接进 `batch_errors`。异常上带的 `partial_suggestions` 在 run_cleaning:510 只被赋值为 `convention_suggestions`（0 API 的确定性惯例归一建议），模型建议一条都不在里面——任务运行器的提示语「另有 N 条 0 API 惯例归一建议已生成并保存」也印证了：活下来的只有不花钱的那批。

**后果**：用户点一次深度清洗，钱已经按批次实打实付给模型了。默认 batch_size=20，一个 20 万条的语言对就是 1 万个批次；这 1 万次调用里只要有 1 次超时，前面 9999 批的建议全部作废，界面只给一句「1/10000 个清洗批次失败」，建议列表是空的。用户唯一的补救是重跑，也就是把这笔钱重新付一遍——而重跑同样只要再撞上一次瞬时失败就再次归零。库越大越必然踩中，正好是最需要清洗的老用户。直接违反「白花钱 / 停止后丢弃已付费成果算高危」这条硬约束。

**复现**：已复现（scratchpad/m1_batchfail.py）。5 个批次、假引擎让第 5 批抛 RuntimeError('模拟第 5 批超时')。实际输出：`engine calls actually made (paid): 5`、`RAISED: 1/5 个清洗批次失败`、`partial_suggestions attr: []`、`suggestions persisted in DB: 0`。前 4 批共 20 条有效建议全部丢失。

**修法**：把 `persist_cleaning_suggestions(suggestions)` 挪到 `if batch_errors: raise` **之前**（两条路径都要改：threaded 805/821、async 608/624），先落库再抛错；同时把 `batch_error.partial_suggestions` 从 `list(convention_suggestions)` 改成 `convention_suggestions + suggestions`，任务运行器 tm_cleaning_task_runner.py:103-108 的提示语相应改成「本次已生成并保存 N 条建议（其中 M 条来自模型），另有 K 个批次失败，可只对失败部分重跑」。停止路径（cancel_event）目前是正确的——已实测会走到 persist，不要一起改坏。

### 高-12 全量模式下「公式显示值回填」关闭时，公式格的显示值照样被送去翻译，一格都写不回去——钱白花一半

`D1` · 置信度 high · new

**位置**：`core/task_runner.py:2897`、`core/task_runner.py:922`、`core/xlsx_patcher.py:1610`、`core/excel_coverage.py:238`

**机制**：词条抽取有两条路。补译路（build_excel_coverage_plan → _classify_excel_cell，core/excel_coverage.py:238）明确判断 `is_formula and not formula_display_value_backfill` → 直接判 COVERAGE_IGNORED，注释里写得清清楚楚「送翻是白花一次 API 调用」。但全量路 `TaskRunner._collect_texts`（core/task_runner.py:2897）用 `load_workbook(data_only=True)` 无差别收所有字符串，压根没有 formula_display_value_backfill 这个参数——公式格的缓存显示值一律进待译词条。到了写回端 `_resolve_source_text`（core/xlsx_patcher.py:1610）在回填关闭时返回的是 `"=" + 公式源码`，而译文表的键是显示值，必然对不上，`_plan_cell_mutation` 返回 None，这一格原样不动。于是这批词条 100% 是付了钱、拿到译文、然后扔掉。

**后果**：用户为了保住公式而关掉「公式显示值回填」（这正是这个开关存在的理由），结果每次全量翻译都要额外为所有公式格的显示值付一遍模型费用，产出文件里一个字都用不上。公式列越多浪费越大：VLOOKUP/CONCAT 生成的中文列在国内表里极常见，实测夹具里 400 个词条有 200 个（50%）是纯浪费。同样的表走「只补未译」反而不花这笔钱——两条路对同一个开关的行为不一致。

**复现**：已复现。脚本 /private/tmp/claude-501/-Users-lijianwei-vibecoding-claude-XL-Translator/cef9c101-5dad-4a16-b030-8f75baf592e2/scratchpad/d1_waste.py（夹具 d1_shared_200.xlsx：A 列 200 个静态中文 + B 列 200 个共享公式、缓存显示值为中文）。输出：
  全量模式收词条总数 = 400  其中来自公式格显示值的 = 200
  backfill=False 写回改动格数 = 200 (只有 A 列静态文本)
  B1 仍是公式? True
  译文 EN_项目1件 是否出现在文件里? False
  补译模式收词条数 = 200  其中公式显示值 = 0

**修法**：给 `TaskRunner._collect_texts` 加上 `formula_display_value_backfill` 参数（两个调用点 core/task_runner.py:922 和 :979 都已经有 `excel_output.formula_display_value_backfill` 在手），回填关闭时改用 `data_only=False` 判 `cell.data_type == 'f'` 跳过公式格——判据可以直接复用 `excel_coverage._resolve_cell_text`，两条路共用一份规则才不会再漂移。同时把 DISPIMG 与错误值格的排除（补译路已有）一并搬过来：全量路目前同样在为这两类格子付钱。

### 高-13 Word 文本框 / 形状 / 脚注里的文字既不翻译，也不进「无法读取的内容」告警——Excel 早就会数会提示，Word 没有

`D2` · 置信度 high · new

**位置**：`core/word_document.py:390`、`core/word_document.py:689`、`core/word_coverage.py:92`、`core/word_task_runner.py:932`

**机制**：三条链路都只走 python-docx 的 `doc.paragraphs` + `doc.tables`：抽取（`extract_word_segments`，word_document.py:410/438）、写回（`write_bilingual_docx` 的 `body_paragraphs = list(doc.paragraphs)`）、体检（`word_coverage._classify_body_paragraphs` 的 `paragraphs = list(doc.paragraphs)`）。文本框/形状的文字住在 `w:pict/v:textbox/w:txbxContent`（旧式）或 `mc:AlternateContent → wps:txbx`（新式）里，脚注/尾注/批注住在独立的 footnotes.xml / endnotes.xml / comments.xml part，全都不是 `w:body` 的直接 `w:p` 子元素，python-docx 一个都返回不了。
本来有一道兜底：`detect_hidden_word_content`（word_document.py:689）的文档字符串明写「统计文档里 python-docx 扫不到、因而会被静默漏译的内容」，结果由 word_task_runner.py:932 变成质量报告里的一条「存在无法读取的内容」。可它只查两样东西——`w:sdt`（内容控件）和 `w:ins`（修订插入），对 `txbxContent` / 脚注 part 一个字都没查（全仓库 grep `txbx|footnote|endnote` 在 core/word_*.py 里零命中）。于是漏译发生在告警机制的盲区里。
对照组：Excel 侧的产品结论早就拍板过——docs/mockups/2026-08-06_lang-picker-focus-excel-notice.html 与 CHANGELOG.md:248「清点嵌入内容：列出文件里的图片数量和含文字的文本框 / 形状数量，提前说明这两类内容当前不会被翻译」。同一条产品标准在 Word 侧没有落地。

**后果**：中文工程/标书/合同类 docx 里，封面标题、图纸说明、流程图标注、印章旁注几乎全是文本框，脚注是法律与技术文档的标配。用户翻完拿到的产物里这些位置原样是中文，而任务日志、覆盖率体检、质量报告三处都显示这份文件已全部译完、无残留、无「无法读取的内容」。用户要么自己一页页翻出来，要么直接把带中文的文件发出去。这正好踩中「界面不许骗人」：报告声称覆盖完整，实际整块内容从未进过翻译范围。

**复现**：已复现。脚本 d2_textbox.py：用 python-docx 造一份含两段正文 + 一个 VML 文本框（内容「文本框里的中文说明文字。」）的 docx，输出——
segments: ['这是正文里的普通段落。', '末尾另一段正文。']  ← 文本框那句根本没进抽取
hidden report: {'content_control_count': 0, 'tracked_insertion_count': 0, 'toc_control_count': 0, 'total': 0} found= False describe=  ← 告警机制也判定「没有扫不到的内容」
脚注/尾注/批注部分为机制确认（core/word_*.py 内 grep `footnote|endnote|comment part` 零命中，无任何读取 footnotes.xml 的代码路径），未单独造夹具。

**修法**：最小改动是先把「不翻译」变成「说清楚不翻译」，与 Excel 对齐：在 `_detect_hidden_word_content`（word_document.py:689）里补两项统计——① 从 `doc.element.body.iter()` 找 `w:txbxContent`，统计其中含可见文字（复用现成的 `_element_has_visible_text`）的个数；② 直接读 zip 里的 `word/footnotes.xml` / `word/endnotes.xml`（可照抄 `count_text_bearing_header_footer_parts` 的 zipfile+正则写法，避开再解析一遍整份文档），数出带字母/汉字的注释条数；把两个计数并进 `WordHiddenContentReport`，让 `describe()` 与 word_task_runner.py:932 那条质量问题如实说出「N 个文本框 / M 条脚注不会被翻译，原样保留」。真要翻译文本框则是下一步的产品决策（`txbxContent` 里就是标准的 `w:p`，抽取与写回可以直接复用现有段落逻辑，只是要在遍历入口把 `body.iter(qn('w:txbxContent'))` 的段落并进 `all_paragraphs`，并注意保护边界与位置编号）。

---

## 中危（25 条）

### 中-1 中-28「共享公式让渡 O(n²)」只降了常数，复杂度没变：单个大共享组仍是平方增长，1 万行公式列实测卡死 48 秒

`R2` · 置信度 high · regression-of-prior-audit

**位置**：`core/xlsx_patcher.py:977`、`core/xlsx_patcher.py:1015`、`core/xlsx_patcher.py:1034`、`tests/test_audit_excel_fixes.py:86`、`tests/test_audit_excel_fixes.py:148`

**机制**：修复引入了 _SharedFormulaIndex，整表只扫一次建索引，注释写的是「之后每次让渡都是一次字典查询」。实际不是：dependents() 拿到那一组的 live 列表后，每次调用都要把整组元素重走一遍（cell.find(<f>) + get("si") 逐个复核过期），再回写 live；_promote_shared_formula 紧接着又对 dependents 做四次 min/max 全列表扫描（1035-1038 行）。一个含 n 个单元格的共享组，n 次让渡的总代价仍是 Σk ≈ n²/2 次 lxml 调用，只是把「整张分表重扫」换成了「本组重扫」，常数降了约 7 倍，指数没动。真实 Excel 的一列下拉公式就是一个 si 覆盖整段 ref 的大组，正是这个最坏形态。回归测试 test_donation_cost_no_longer_grows_quadratically 用的夹具是 _one_group_per_row —— n 个大小为 2 的组，组内代价恒定，这个形态无论实现怎么写都是 O(n)，所以它抓不到剩下的这一半。

**后果**：一列长公式的 Excel 表在写回阶段单线程空转：实测（.venv 3.13，单组）200 行 0.02s、800 行 0.28s、1600 行 1.09s、3000 行 3.95s、6000 行 16.35s、10000 行 48.34s —— 每翻一倍行数耗时翻四倍。模型钱已经花完了，卡在最后写文件这一步，界面没有进度、没有可解释的状态，用户看到的就是任务假死；上万行的工程量表（这类表恰恰最爱整列下拉公式）会卡到分钟级。

**复现**：已复现。脚本 /private/tmp/claude-501/-Users-lijianwei-vibecoding-claude-XL-Translator/cef9c101-5dad-4a16-b030-8f75baf592e2/scratchpad/perf_shared.py：手写一张分表 XML，B 列 1..N 行同属 si="1" 一个共享组（B1 为主控带 ref="B1:BN"，每格缓存显示值都是待译文本），调 write_bilingual_workbook(formula_display_value_backfill=True)。实测输出：rows=200 0.02s / 400 0.08s / 800 0.28s / 1600 1.09s / 3000 3.95s；perf_big.log：rows=6000 16.35s / rows=10000 48.34s。倍率 3.5～4.0，标准平方增长。对照现有回归测试的夹具（每行一组），同样行数只要毫秒级——所以测试是绿的。

**修法**：两处都要动。① dependents() 不要每次线性复核整组：让 _set_cell_inline_text 和让渡本身在改写单元格时主动从索引里摘掉该条目（索引持有 (row,col,cell) 时同步维护一个 已失效 id 集合，或让组用 deque/游标从头推进——同一组的让渡天然按文档顺序单向前进，游标只需前进不需回头），把单次让渡降到 O(1) 摊还。② min_row/max_row/min_col/max_col 不要每次对整个 dependents 重算：一个组的边界在建索引时就能一次算出，让渡后只需把左上角沿游标前移，右下角不变。③ 回归测试补一个「单个大共享组」的夹具（例如 B 列 400 行同 si vs 1600 行同 si），沿用现有的 8 倍阈值——当前实现在这个夹具上会得到约 16 倍，能真正锁住这条性质。

### 中-2 限流退避漏传停止信号：补译复核与混合语言仲裁两处没跟上 PDF 那次修复，停止后仍继续重试约 2 分钟，最后还报成「换 API Key」而不是「已停止」

`R2` · 置信度 high · regression-of-prior-audit

**位置**：`core/coverage_review.py:239`、`core/mixed_language.py:436`、`core/api_concurrency_control.py:338`、`core/coverage_review.py:97`、`core/pdf_image_translation.py:4148`

**机制**：上一轮低-PDF 那条「handle_api_concurrency_limit 漏传 should_stop，停止响应最多拖 30 秒/页」已在 pdf_image_translation.py:4148 修好（连注释一起补了），engine_dispatcher.py:699/847 也一直是传的。但同一个函数还有两个调用方没跟上：core/coverage_review.py:239（run_pair_arbitration_batch，Excel/Word 补译复核共用）和 core/mixed_language.py:436（混合语言仲裁）都没有 should_stop 参数。api_concurrency_control._wait_out_minimum_capacity_limit 最后一行是 _interruptible_sleep(delay, should_stop)，should_stop 为 None 时那个循环退不出来，睡满 delay（MINIMUM_CAPACITY_MAX_DELAY = 30s）。更糟的是 coverage_review 拿到 decision 后是直接递归调用自己重试，中间不看任何停止标志——stop_event 只在 coverage_review.py:97 的批次之间起作用，进了批次内部就完全失联；退避一直退到 MINIMUM_CAPACITY_GRACE_SECONDS = 120s 用完，抛出的是 ApiKeyTemporarilyUnavailableError（任务致命错误），不是停止。mixed_language 更明显：它下一行就写着 if should_stop and should_stop()，变量就在作用域里，只是没往下传，于是必须先睡满一觉才轮得到那句检查。

**后果**：用户在「补译复核」阶段遇到上游限流时按停止：界面上按钮点下去了，任务却继续往上游发请求（实测停止置位后又发了 4 次），最长要等到 120 秒的宽限窗口耗尽才停下；而且最终收尾不是「已停止」，是一条误导性的致命错误——「接口持续限流…请稍后重试，或在设置里换一条连接、更换 API Key 后重新开始」。用户主动停的，却被告知自己的 Key 出了问题。复核本身是按次计费的模型调用，停止后仍在发的那几次也在花钱（虽然多半被 429 挡回）。

**复现**：已复现。脚本 /private/tmp/claude-501/-Users-lijianwei-vibecoding-claude-XL-Translator/cef9c101-5dad-4a16-b030-8f75baf592e2/scratchpad/stop_latency2.py：假引擎每次 chat 都抛 429 too many concurrent requests，真 WeightedApiScheduler，0.5 秒后置位 stop_event，调 run_pair_arbitration_batch。为了让脚本跑得完把 GRACE 临时改成 12s、MAX_DELAY 改成 4s（真实值 120s / 30s，倍数关系不变）。实测输出：「[0.50s] 用户点了停止」→「[12.80s] 抛出 ApiKeyTemporarilyUnavailableError: 接口持续限流…请更换 API Key」→「stop 置位后仍继续调用模型的次数（含首次）: 5」。按真实常量外推即 ~120 秒不响应停止。

**修法**：① core/coverage_review.py：给 run_pair_arbitration_batch 加 should_stop: Callable[[], bool] | None = None 形参，从 arbitrate_coverage_units 已有的 stop_event 传下来（stop_event.is_set），既传给 handle_api_concurrency_limit，也在递归重试之前检查一次——停了就直接返回空 dict，让上游按 uncertain 处理，而不是继续退避到宽限期用完抛致命错。② core/mixed_language.py:436：把作用域里现成的 should_stop 直接传进去（下一行的检查保留即可）。③ 加一条测试锁住「三个调用方都把 should_stop 交出去」——这类漏传按调用点逐个修必然还会漏，最省事的做法是给 handle_api_concurrency_limit 的 should_stop 去掉默认值（改成必填关键字参数），让漏传在类型检查/导入期就暴露。

### 中-3 生产实际使用的 FairApiGroupScheduler 完全忽略 request category，恢复优先级是死代码

`C1` · 置信度 high · new

**位置**：`core/task_resources.py:358`、`core/task_resources.py:222`、`core/task_resources.py:250`、`core/api_scheduler.py:267`

**机制**：WeightedApiScheduler._can_acquire（core/api_scheduler.py:267）实现了恢复优先级：一旦有 recovery 在等或在跑，普通请求就被 normal_soft_limit（默认 80%）卡住，给重试/仲裁留出 20% 的槽位。但 api/task_manager.py:605/1145/1516 把 `lease.scheduler_for(group)` 交给 runner——runner 拿到的是 TaskGroupScheduler 门面，背后是 FairApiGroupScheduler，而 `FairApiGroupScheduler._can_acquire_locked(owner_key, weight)`（core/task_resources.py:358）只有两句：容量够不够、是不是队首任务，**category 参数根本没进这个函数**。`_waiting_recovery_count`（:222 自增）和 `_active_recovery_weight`（:250 累加）照常记账，也照常出现在 snapshot 里，但没有任何判定读它们。而 FIFO 是按 owner_key（任务）排的，同一个任务的 normal 线程和 recovery 线程共用同一个队列条目，等于在任务内部完全没有优先级——纯抢锁。tests/test_api_concurrency_control.py 里那 10 条恢复优先级测试全部只测 WeightedApiScheduler，tests/test_scheduler_waiters.py 只有 2 条且不涉及 category，所以这个缺口不会被现有测试照出来。

**后果**：Word 的恢复池是在主翻译「不再有新批次入队」时就 start 的（core/word_task_runner.py:1368 的 _MainTranslationDrainGate + defer_until_started=True），设计上就是要和主翻译尾巴并行跑；PDF 的逐页审核（category=RECOVERY）在审核模型与图像模型同连接时也和图像生成共用同一个组。现在这段重叠期里恢复请求拿不到任何优先级，会被主翻译流量压到几乎抢不到槽位，本该并行的仲裁/审核退化成串行等待，任务总时长变长。用户感知是「进度条走到最后几步不动了」。不丢结果、不多花钱，但设计意图被静默架空。

**复现**：已复现。c1_recovery_priority.py：capacity 8，8 条 normal 线程满载 4 秒 + 1 条 recovery 线程。WeightedApiScheduler → recovery 抢到 74 次，中位等待 0.0ms、最大 54.9ms；FairApiGroupScheduler 门面（生产实际路径）→ recovery 只抢到 1 次，等待 4022.5ms（等到我把 normal 停掉才进去）。

**修法**：把 WeightedApiScheduler._can_acquire 的那段软上限逻辑搬进 FairApiGroupScheduler._can_acquire_locked：给它加 category 形参（调用点 core/task_resources.py:245 已经算好了 normalized_category），维护一个 normal_soft_limit（在 _reset_capacity_locked / set_capacity / _next_reduced_capacity_locked 三处一起更新），并保留「_active_normal_weight == 0 时放行一个超重普通请求」那条防死锁的逃生口。同时给 tests/test_scheduler_waiters.py 补一条「normal 满载时 recovery 仍能在 N 毫秒内拿到槽位」的测试——现在这条契约在生产调度器上完全没有测试守着。

### 中-4 限流退避不响应停止：另有 3 处调用也没把停止标志交给 handle_api_concurrency_limit

`C1` · 置信度 high · new

**位置**：`core/tm_cleaner.py:747`、`core/mixed_language.py:440`、`core/mixed_language.py:748`

**机制**：core/api_concurrency_control.py 的 `_wait_out_minimum_capacity_limit` 在并发已经降到最低档时会 `_interruptible_sleep(delay, should_stop)`，delay 按 2×2^(n-1) 增长、上限 30 秒，整段宽限窗口 MINIMUM_CAPACITY_GRACE_SECONDS = 120 秒。`_interruptible_sleep` 只在 `if should_stop and should_stop()` 时提前返回——传 None 就是完全睡满。全仓 8 个调用点里，只有 core/engine_dispatcher.py:699/847（Excel/Word 主翻译）和 core/pdf_image_translation.py:4148 传了 should_stop；pdf 那处还专门写了注释说明为什么必须传（「限流退避里睡的是整整 30 秒，不把停止标志交给它，用户点了停止之后每一页都要先把这一觉睡完才肯回来」）。剩下的 core/tm_cleaner.py:747（TM 清洗）、core/mixed_language.py:440（混合语言）、core/mixed_language.py:748（混合语言重试）都没传——mixed_language 更可惜，should_stop 就在作用域里，:445 睡醒之后紧接着就在查它，只是没交给退避本身。（另三处 coverage_review / word_task_runner 已并入本次第 1 条发现。）

**后果**：用户在 TM 清洗或混合语言处理阶段按停止，遇上账号正被限流时，每个 worker 线程要先把当前这一觉（最长 30 秒）睡完才回来查停止；宽限窗口内会连睡几次，最坏能拖到接近 120 秒。任务中心停在「停止中」不动，看起来就是卡死。tm_cleaner 这条尤其难受——TM 清洗本来就是用户觉得「随时可以叫停」的后台整理动作。

**复现**：已复现。c1_backoff_stop.py：把 capacity 打到最低档后，在停止已置位的前提下连打 4 次 429。无 should_stop 版本分别阻塞 1.64s / 3.38s / 9.30s / 13.10s（累计 27.4 秒，日志里能看到「等待 13.1s 后重试当前批次，已持续 14s」）；传了 should_stop 的版本 4 次全是 0.00 秒。

**修法**：三处调用都补上 `should_stop=`：core/mixed_language.py:440 和 :748 直接把作用域里现成的 `should_stop` 传进去（:445 那句检查可以保留，但退避本身必须先能被打断）；core/tm_cleaner.py:747 把 runner 的停止回调透传到这一层。更稳的做法是给 handle_api_concurrency_limit 的 should_stop 改成必填关键字参数，让漏传在 tsc/ruff 之外靠签名本身兜住——现在 8 个调用点漏了 6 个，说明「可选参数」这个形状本身就是缺陷来源。

### 中-5 sidecar 看门狗的 20 秒强杀预算包不住内层 22 秒收尾链——壳被强退时 soffice 与临时目录照样残留

`C2` · 置信度 high · regression-of-prior-audit

**位置**：`api/launcher.py:21`、`api/launcher.py:78-84`、`src-tauri/src/main.rs:48-68`、`src-tauri/src/main.rs:1045-1063`

**机制**：上一轮高-11 只修了「正常退出」这条路：Rust 侧把 SIDECAR_STOP_TIMEOUT 改成 ①uvicorn 排空 10s + ②task_manager.shutdown 12s + ③落盘余量 3s = 25s，明确写着「外层必须包住内层」。但「壳非正常消失」（Force Quit 整个 app、Tauri 崩溃、被系统杀）走的是另一条路——Rust 根本没机会发 SIGTERM，收尾由 api/launcher.py 的 parent watchdog 触发：它发现父进程没了 → 调 begin_shutdown（只置标志）→ server.should_exit = True → 然后 `deadline = time.monotonic() + WATCHDOG_FORCE_EXIT_SECONDS` 干等 20 秒，到点无条件 os._exit(0)。这个 20.0 从来没跟着高-11 一起改：内层最坏情况仍是 10.0（GRACEFUL_SHUTDOWN_SECONDS）+ 12.0（TranslationTaskManager.shutdown 默认 timeout）= 22.0 秒 > 20.0，而 runner 的 finally（删 LibreOffice profile、word_translator_temp、PDF 分页工作区）恰好就在②那一段里。更关键的是这个循环压根不观察收尾有没有做完——不看 server 状态、不看任务是否已 terminal，只是纯睡满 20 秒。Rust 侧那个 the_mirrored_sidecar_budgets_still_match_the_python_side 测试只读 GRACEFUL_SHUTDOWN_SECONDS 和 shutdown(timeout=)，完全没覆盖 WATCHDOG_FORCE_EXIT_SECONDS，所以这条不等式变红不了。

**后果**：Word / PDF 任务跑到一半，用户强退应用（或应用崩溃）：sidecar 在收尾走到一半时被自己的看门狗 os._exit 掉。UNO 那条路拉起的 headless soffice 是 Popen 长驻进程，terminate 写在 finally 里，这一杀就永远不执行——soffice 被 reparent 到 launchd 长期存活，占着 127.0.0.1 的 UNO 端口和 profile 目录；word_translator_temp 里那份几十 MB 的中间 docx、PDF 分页工作区也一起留下。任务历史那部分不受影响（下次启动 mark_active_tasks_interrupted 会兜住）。

**复现**：已复现（c2_watchdog.py + c2_watchdog2.py）。c2_watchdog.py 实测输出：GRACEFUL_SHUTDOWN_SECONDS = 10.0 / WATCHDOG_FORCE_EXIT_SECONDS = 20.0 / task_manager.shutdown default timeout = 12.0 / worst-case inner chain (drain+unwind) = 22.0 | watchdog force-exit = 20.0 | watchdog covers inner? False / watchdog waits on server state? False。c2_watchdog2.py 做等比例活体验证：把 WATCHDOG_FORCE_EXIT_SECONDS 调成 2.0、模拟收尾工作耗时 4.0s，进程在恰好 2 秒时退出（elapsed=2s），CLEANUP FINISHED 一次都没打印——证明看门狗到点即杀、完全不等收尾。

**修法**：把 api/launcher.py 的 WATCHDOG_FORCE_EXIT_SECONDS 改成和 Rust 同源的加法：GRACEFUL_SHUTDOWN_SECONDS + <task_manager.shutdown 默认 timeout> + 余量（即 25s），而不是写死 20.0；更稳的做法是循环里改成「轮询到 server 真的停了就立刻 os._exit，否则等到 deadline」，这样正常情况几百毫秒就退干净、异常情况才用满预算。同时把 WATCHDOG_FORCE_EXIT_SECONDS 补进 src-tauri/src/main.rs:1045 那个镜像测试的断言里（断言它 >= drain + unwind），否则下次改任何一段还是没人拦。

### 中-6 keys.json 上同一个洞：非 UTF-8 字节让保存、读取、以及显式出路「删除全部 API Key」三条路一起抛异常（中-1 的修复没修干净）

`P1` · 置信度 high · regression-of-prior-audit

**位置**：`settings.py:1809`、`settings.py:1810`、`core/maintenance.py:208`

**机制**：`_load_keys_unlocked()` 和 settings 侧犯同一个错：`KEYS_PATH.read_text(encoding="utf-8")` 外面只有 `except OSError`（settings.py:1810）。函数里为「内容损坏」写了完整的备份-重建分支、为 `force=True`（维护页「删除全部 API Key」）写了专门的放弃分支，但这两个分支都在 `json.loads` 那一层，解码错误在更早的一行就把整个函数掀了。于是 `load_keys()` / `get_key()`（非 strict，本该吞掉一切返回空表）、`save_key()`、以及 `delete_all_keys()` 里那句 `_load_keys_unlocked(strict=True, force=True)`——按注释「用户已经在按下它的那一刻放弃了旧文件，不该因为读取或备份失败就拒绝执行删除」——全部一起抛。

**后果**：和上一轮审计的中-1 是同一种形状：保存与清空双双失败、界面无自救出路。而且比中-1 更宽——`get_key()` 也炸，意味着不是「存不进 Key」，是每一次翻译在取 Key 那一步就直接失败。触发概率比 settings.json 低（Key 基本是 ASCII），但一旦发生用户完全出不来。中-1 的修复只覆盖了 JSON 解析失败和 OSError 两类，编码这一类漏了。

**复现**：已复现。`p1_keys_nonutf8.py`（keys.json 用 gb18030 写入 `{"openai::": "sk-中文备注"}`）：load_keys / get_key / save_key / delete_all_keys 四步全部 `RAISED UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd6 in position 17`。

**修法**：settings.py:1809-1810 与上一条同源修复：改用 `read_bytes().decode("utf-8")`，把 `UnicodeDecodeError` 并入下面那段已经写好的「内容损坏 → 非 strict 返回空表 / force 直接放弃 / 否则备份后按空表续写」逻辑里，不要让它绕过状态机。两处一起改，别只改 settings 那边。

### 中-7 「测试连接」跨网络往返持着旧快照，落盘时整份 connections 列表覆盖，把期间用户对另一条连接的修改静默吃掉（两个请求都返回 200）

`P1` · 置信度 high · new

**位置**：`api/app.py:1517`、`api/app.py:1557`、`settings.py:1645`、`settings.py:1629`

**机制**：`_settings_delta()` 对非 dict 的值一律记 `_SETTINGS_FIELD_SET`（settings.py:1645），列表也在内——所以 `engine.connections` 的合并粒度是「整份列表替换」，不是按元素合并。`check_model_role_connectivity` 在 api/app.py:1517 先 `load_settings()`（此时拍下 `_persisted_snapshot`），然后做一次真实的模型 API 往返（`check_connectivity`，秒级甚至到超时），最后在 api/app.py:1557 才 `save_settings(settings)`。这个窗口里用户在面板上改另一条连接、走 `PUT /api/models/roles/{role}/connections/{id}` 已经存进磁盘了；测试结果回来时，它的 delta 里 connections 是一整份基于旧快照的列表，直接把对方的改动盖掉。既有 tests/test_settings_concurrent_updates.py 的 10 个并发用例全是标量和嵌套 dict，没有一个覆盖列表元素。

**后果**：用户点「测试连接」后不干等着（这是最自然的操作），顺手改了另一条连接的模型名并保存，界面提示保存成功、接口返回 200、响应体里还带着新值；等测试结果一落盘，磁盘上悄悄退回旧值。下次打开设置页发现改动没了，且没有任何提示。这条连接如果后面被真的用来跑任务，跑的是用户以为已经改掉的旧模型——属于「用户以为配置生效了、实际在按旧配置花钱调 API」。

**复现**：已复现。`p1_api_lostupdate.py`：TestClient 起真实 app，把 `check_connectivity` 换成 sleep 1.2s 的桩（模拟网络往返），线程 A 打 `POST /api/models/connectivity/text {connection_id: c0}`，线程 B 在 0.4s 后打 `PUT /api/models/roles/translation/connections/c1 {model: "用户刚改的模型"}`。输出 `HTTP: {'edit': 200, 'test': 200}`，盘上 `[('c0','主模型'), ('c1','m1')]`——c1 的编辑消失。纯 settings 层的最小复现见 `p1_conn_lost3.py`：串行改生效（c1=X1），并发改两条不同连接后只剩一条（`[('c0','主模型'),('c1','并发改一'),('c2','m2')]`，「并发改二」丢失）。

**修法**：两条路选一条。轻量：给 `_settings_delta` 加一条列表特例——对元素带稳定主键的列表（connections 有 `id`）按 id 生成逐元素的增/删/改 delta，而不是整份 SET；`_apply_settings_delta` 对应按 id 合并，顺序变化仍记为整份 SET。稳妥：把 `check_model_role_connectivity` 的写盘窗口收窄——网络往返结束后重新 `load_settings()`，只把 availability_* 这几个字段写到目标连接上再保存，不要让一个跨秒级 I/O 的请求持有整份设置快照。推荐后者先落地（改动小、语义清楚），前者作为 delta 层的根治。

### 中-8 Ollama 引擎手写的重试循环不判断错误能否重试，把「模型名填错」这类永久性配置错误当瞬时故障重试三次

`E1` · 置信度 high · new

**位置**：`engines/ollama_engine.py:110-120`

**机制**：openai_engine.py / claude_engine.py 都用 tenacity 的 `@retry(..., retry=retry_if_exception(is_retryable_engine_error))`，命中 400/401/402/403/404/405/410/422 这类永久性错误时第一次失败就放弃重试、立即把真实原因交回上层（base_engine.py:50 的 _NON_RETRYABLE_STATUS 就是为此设的）。但 ollama_engine.py 的 `_translate_chunk` 是独立手写的 `for attempt in range(RETRY_MAX_ATTEMPTS): except Exception` 循环，完全没有导入也没有调用 is_retryable_engine_error，任何异常一律当瞬时故障重试到底。

**后果**：本地模型名填错、Ollama 服务鉴权被拒等这类重试也不会自愈的场景下，用户要多等 3 次退避（实测约 4.8 秒）才能看到真实报错，期间日志连打三条「第 N 次重试」的警告，容易让人误以为是网络抖动而去调网络设置，而不是去检查模型名/权限——正是关注点 5 里点名的「重试掩盖配置错误」。本地模型不花钱，所以只是体验问题，不是资金问题，故定为中危。

**复现**：已复现：脚本 /private/tmp/claude-501/-Users-lijianwei-vibecoding-claude-XL-Translator/9bc2c740-3ad6-41c8-9853-d1ce6bd7a8c6/scratchpad/repro_ollama_retry.py，把 `_call_ollama` 替换为始终抛出 `status_code=404`（"model 'bogus' not found"）的异常，实测 `translate_batch` 耗时 4.76 秒、重试 3 次后才把原始异常抛给调用方；对照 base_engine.is_retryable_engine_error(同一个 404 异常) 返回 False，说明 openai/claude 引擎在同等场景下会立即放弃重试。

**修法**：`_translate_chunk` 的 except 分支里先调用 `is_retryable_engine_error(e)`，不可重试就直接 break/reraise，和 openai_engine/claude_engine 保持一致的策略，不必接入 tenacity，一行判断即可。

### 中-9 自适应并发只降不升：一次瞬时 429 之后整段任务永久跑在 20% 速度，还会拖慢同组的其他任务

`G1` · 置信度 high · new

**位置**：`core/api_scheduler.py:214`、`core/api_scheduler.py:264`、`core/task_resources.py:303`、`core/task_resources.py:339`

**机制**：`register_concurrency_limit_hit` 沿 build_adaptive_capacity_levels 的 0.8/0.6/0.4/0.2 阶梯单向下调 self.capacity，全模块没有任何回升路径——grep `restore|recover|raise` 在两个调度器上都是零命中，`set_capacity` 只在任务加入/退出时被调用。FairApiGroupScheduler 更进一步：容量是整个连接组共享的，任务 A 撞到的 429 直接把 B 的可用并发一起削掉；只有 `_reset_capacity_locked`（成员变动时）才会恢复。也就是说，只要在长任务的前几分钟遇到一次限流抖动，剩下的几小时都跑在最慢档。

**后果**：8000 格文件约 400 批，并发 10 降到 2 意味着后续吞吐掉到 1/5，用户看到的是「越跑越慢而且再也不快起来」，且没有任何界面提示解释原因（限流通知每轮最多两条，之后只有心跳）。

**复现**：已复现（g1_repro.py 段 (b)）：`WeightedApiScheduler(10)` 连打 6 次限流信号，输出 `hit1: reduced 10->8 … hit4: reduced 4->2 … hit5: unavailable`，随后执行 500 次完全成功的 acquire/release，`after 500 successful requests capacity: 2 (initial was 10)`，`any restore/raise API on scheduler? NONE`。组调度器同样：`add_task(A,8)+add_task(B,8)` → capacity 16，A 连撞 6 次后 `taskA's 429 burst lowered SHARED group capacity to: 3`。

**修法**：加一条对称的回升路径：记录最近一次限流命中时间，在连续 N 次成功释放（或安静 60~120 秒）后沿同一阶梯回升一级，并 `_generation += 1` 让在途请求的旧信号被判为过期。回升要有上限（不超过 initial_capacity）和阻尼（回升步长小于下降步长），避免与上游限流窗口共振。

### 中-10 Claude 引擎硬编码 max_tokens=8096，而连通性测试只发 8：输出上限 4096 的模型「测试全绿、每批 400、整份文档原样退回」

`G1` · 置信度 high · new

**位置**：`engines/claude_engine.py:74`、`core/connectivity_check.py:284`、`core/engine_dispatcher.py:730`、`core/model_catalog.py:14`

**机制**：claude_engine.py:74 对所有 Claude 模型固定发 `max_tokens: 8096`。Anthropic 对 claude-3-opus / claude-3-haiku / claude-3-sonnet（以及不少第三方中转网关）的输出上限是 4096，超限直接 400 invalid_request_error。而 Claude 不在 OPENAI_COMPATIBLE_MODEL_PROVIDERS 里（model_catalog.py:14-22 没有 claude），模型名是用户手输的，没有任何白名单拦得住。连通性测试（connectivity_check.py:284）发的是 `max_tokens: 8`，必然通过。真正翻译时 400 落进 `_is_permanent_request_error`（400 在永久错误集合里），走 engine_dispatcher.py:730 的「不可重试，已停止拆分批次」——不重试、不二分、不换连接（400 被 classify_connection_failure 归为 transient，`should_switch_connection` 返回 False），整批原样退回。

**后果**：用户在设置里填了一个老 Claude 模型，点「测试连接」显示绿色可用，跑翻译却一格都没翻，错误文案是「Excel 翻译请求不可重试：Client error '400 Bad Request'」，完全指不到「模型输出上限对不上」这个真因。

**复现**：已复现（g1_repro.py 段 (a)）：MockTransport 模拟 Anthropic 对 max_tokens>4096 回 400（原文即 Anthropic 的真实报错措辞）。输出 `max_tokens actually sent: [8096]` / `result == source text? True` / `untranslated recorded: 3 | retries: 0` / 错误文案 `Excel 翻译请求不可重试，已停止拆分批次：Client error '400 Bad Request'`。同一模型走 connectivity_check 的 max_tokens=8 则通过。

**修法**：两处一起改：(1) claude_engine 的 max_tokens 按批次预估输出量动态给，并给一个保守下限（比如 max(2048, 预估×1.3)），或从模型名映射一张上限表；(2) 更重要的是连通性测试要用与真实翻译一致的 max_tokens，否则测试绿灯没有任何保证价值；(3) 400 响应体里带 `max_tokens` 关键字时，把 humanize 后的文案改成「所选模型的输出上限低于本次请求」。

### 中-11 Claude 响应只取 content[0]，首块不是 text 就静默返回空串，一个 30 条批次要烧掉 15 次真实计费的 200 OK 才放弃

`G1` · 置信度 high · new

**位置**：`engines/claude_engine.py:91`、`engines/base_engine.py:99`、`core/engine_dispatcher.py:737`

**机制**：`_extract_claude_text` 只看 `content[0]`，且末尾是 `return text if isinstance(text, str) else ""`——首块是 thinking / redacted_thinking / tool_use 等非 text 块时，真正的译文块（content[1]）被整个丢掉，函数返回空串而不是报错。空串进 `parse_response` 触发 JSONDecodeError → ValueError。注意 parse_response 是在 `_call_api` 之外调用的，所以 tenacity 不会重试（这点是对的），但 dispatcher 的三层二分会把同一批文本重发 15 次，每一次上游都返回 200 并照常计费。

**后果**：这 15 次调用是成功计费的（不是错误响应），token 全部白花，最终这一批还是 100% 原样保留计未翻译。对第三方中转网关尤其现实——不少网关会对特定模型强制打开思考模式，用户这边看到的只是「翻译全失败但账单在涨」。

**复现**：已复现（g1_amplify.py）：MockTransport 让 Claude 返回 `content=[{type:thinking,...},{type:text,text:'["T0",...]'}]`，30 条一批。输出 `thinking-block first: paid API calls for ONE 30-item batch = 15` / `all untranslated? True | untranslated: 30`。对照组：持续 HTTP 500 的同一批是 45 次（tenacity 3 × 二分 15）。

**修法**：`_extract_claude_text` 改成遍历 content、拼接所有 `type == "text"` 的块；一个 text 块都没有时抛 ValueError 并把 stop_reason / 首块 type 写进异常文案，而不是返回空串。顺便校验 `stop_reason == "max_tokens"` 时给出「输出被截断」的明确错误，别让它伪装成解析失败。

### 中-12 Responses API（asxs 网关，生产实际路径）的 SSE 解析不看事件 type、也不校验终态：非正文 delta 会污染 JSON，失败/截断事件被当成功

`G1` · 置信度 high · regression-of-prior-audit

**位置**：`engines/openai_engine.py:38`、`engines/openai_engine.py:186`

**机制**：`_extract_text_from_responses_events` 完全不读 `data["type"]`，只要事件里有字符串 `delta` 就拼进正文。Responses API 里带 `delta` 字段的事件不止 `response.output_text.delta`——还有 `response.reasoning_summary_text.delta`、`response.refusal.delta`、`response.function_call_arguments.delta` 等，全部会被无差别拼进去。同时终态事件一个都不校验：`response.failed`、`response.incomplete`（含 max_output_tokens 截断）、以及流被中途掐断都不会被识别，只要拼出来的字符串非空，`_call_responses_api` 就当成功返回。payload 里也没有设 max_output_tokens，截断由网关默认值决定。

**后果**：轻则每批解析失败 → 三层二分 15 次重发（付 15 次钱后整批未翻译），重则一段被截断的响应恰好能解析（例如只丢了尾部若干项）时，长度校验只在 parse_response 里按数组长度卡——长度对不上会报错，但污染型（reasoning 文本 + 正确 JSON 拼接）一律解析失败。这条路是当前生产实际在走的路由（上一轮高-9 就出在这里）。

**复现**：已复现（g1_repro.py 段 (c)）：喂入含 `response.reasoning_summary_text.delta` 的事件流，`extracted: 'Let me think about these terms.\n["Valve", "Flange"]'`、`parses as JSON? False`。中途掐断（无 response.completed）→ `extracted: '["Valve", "Fla' | accepted as success? True`。`response.failed` 事件在完整正文之后 → 仍返回正文、失败事件被忽略。`response.incomplete(max_output_tokens)` → 返回半截 JSON。

**修法**：按 type 白名单收 delta（只收 `response.output_text.delta`，done 只认 `response.output_text.done`）；显式处理 `response.failed` / `response.error` / `response.incomplete`，抛带上游 code/message 的异常；流结束时若没见到 `response.completed`（或 output_text.done）就判为截断并抛错，而不是把半截正文当成功。另外给 payload 补一个显式的 max_output_tokens，别让网关默认值决定截断点。

### 中-13 全仓库从不读取 Retry-After 响应头，退避节奏完全无视上游明确给出的等待时间

`G1` · 置信度 high · new

**位置**：`core/api_concurrency_control.py:308`、`core/api_concurrency_control.py:382`、`engines/openai_engine.py:141`、`core/engine_dispatcher.py:65`

**机制**：限流退避一律用本地公式：`MINIMUM_CAPACITY_BASE_DELAY * 2**attempt`（封顶 30s）和 `_backoff_sleep`（封顶 8s）。`_collect_response_texts`（api_concurrency_control.py:382）只从响应里取 status_code / text / content / json()，`headers` 一次都没碰过。全仓库 grep `retry.after|retry_after` 只有一句无关的英文注释命中。OpenAI/Anthropic/DashScope 在 429 和 529 上都会给 Retry-After 或 x-ratelimit-reset-*，这些信息被完整丢弃。

**后果**：上游说「60 秒后再来」，本地 2 秒后就重打，把限流窗口不断刷新；反过来上游说「1 秒即可」时又白等 30 秒。叠加上面的 8 轮上限，本地更容易提前走到二分（第 3 条）和 120 秒宽限到点的致命错误（第 2 条）——这两条的触发概率都被这一项直接抬高。

**复现**：机制确认：`grep -rni "retry.after|retry_after" --include='*.py' --include='*.ts' --include='*.rs'` 在非 .claude 路径下只命中 core/api_concurrency_control.py:185 的一句英文注释，无任何读取代码；`_collect_response_texts` 的属性遍历列表为 ('text','content')，不含 headers。

**修法**：在 `_collect_exception_texts` 旁加一个 `extract_retry_after(exc) -> float | None`，读 `exc.response.headers` 的 Retry-After（秒数与 HTTP-date 两种格式都要认）以及 x-ratelimit-reset-requests/tokens；`_wait_out_minimum_capacity_limit` 与 `_backoff_sleep` 取 `max(本地退避, 上游给的秒数)`，并对上游值设一个上限（比如 120s）防止恶意/异常头把任务挂死。

### 中-14 每批重发的系统提示词比正文本身还长：8000 格文件里 60% 的输入字符是重复的固定提示

`G1` · 置信度 high · new

**位置**：`engines/base_engine.py:23`、`core/engine_dispatcher.py:41`、`core/engine_dispatcher.py:411`、`config.py:207`

**机制**：每个批次都把完整的 system（领域提示 225 字符 + TASK_INSTRUCTION 372 字符 = 599 字符）重新发一遍。批次大小受两道限制：CHUNK_CLOUD_DEFAULT=20 条（config.py:207，UI 上限 30）和 `_EXCEL_CLOUD_BATCH_CHAR_BUDGET=3200` 字符。实测典型 Excel 词条平均约 20 字符，所以卡死在「20 条」这道限制上，每批正文只有约 400 字符——3200 的字符预算连 1/8 都没用到。Claude 侧也没有任何 cache_control（claude_engine.py 全文无该字段），OpenAI 侧 599 字符约 500 token，低于 1024 token 的自动缓存门槛，所以两家的 prompt 缓存都吃不到。

**后果**：8000 格文件产生 400 个批次，系统提示词累计重发 239,600 字符，正文只有 158,890 字符——输入 token 里 60.1% 是重复的模板。把每批条数放到 40、或把字符预算真正用满，可以直接砍掉三到四成的输入 token 开销。用户为每次调用付费，这是可直接换算成钱的浪费。

**复现**：已复现（g1_prompt.py）：`system prompt chars: 225` / `task instruction chars: 372` / `full system chars: 599`；8000 条平均 19.9 字符的词条经 `_build_text_batches(max_items=20, max_chars=3200)` 得 `batches: 400, avg items/batch: 20.0`；`system-prompt chars resent: 239600 vs body chars: 158890 -> prompt overhead ratio: 60.1%`；`cache_control present in claude_engine? False`。

**修法**：这项涉及批次大小这个用户可见设置，建议先出数字给用户拍板。技术侧的具体动作：把 CHUNK_CLOUD_MAX 从 30 提到 60~80（现代模型上下文早已不是瓶颈，3200 字符预算仍然兜底），默认值从 20 提到 40；同时因为批次变大会放大单批失败的影响面，要配套第 3 条（429 不二分）和第 1 条（耗尽熔断）一起改。Claude 侧若把 system 补齐到 1024 token 以上再加 cache_control 反而更贵，不建议。

### 中-15 目标语言为中文时，URL / 邮箱 / 文件路径被当成待译文本送模型：既白花钱，模型真译了还会把网址替换成中文

`T1` · 置信度 high · new

**位置**：`core/translation_filter.py:390`、`core/excel_coverage.py:236`、`core/engine_dispatcher.py:1027`

**机制**：`should_translate` 的 target=zh 分支（translation_filter.py:376-396）保护规则只有两条：纯数字符号、以及「无空格 + 含字母 + **含数字**」的型号码。URL、邮箱、Unix/Windows 路径这类字符串不含数字时两条都躲过去，落到兜底的 `letter_count >= 2 → return True`（:390-392），一律判为要翻。

反方向（target=en）却是跳过的——同一批样本在 zh→en 下全部 False，en→zh 下全部 True，两个方向对「什么算可翻文本」的定义不一致。

实测（t1_filter_matrix.py，列为 zh→en / en→zh / ja→zh）：
```
https://example.com/a/b     False  True  True
user@example.com            False  True  True
C:\Users\Tom\file.docx      False  True  True
/usr/local/bin/python       False  True  True
```
补译计划也照单全收（t1_url_zh.py）：`summary={'covered':0,'source_only':5,...}`，5 条里 4 条是 URL/邮箱/路径。

下游两种结局都不好（t1_url_zh.py 第一段输出）：
- 模型原样返回 → `same_as_source` 判 fail → `_apply_quality_filter` 重置为原文并记一条 `quality_reset`，报告里堆出一串假的「质量校验回退」；
- 模型真把它译了（`is_translation_redundant('https://example.com/a/b', '示例网址', 'zh')` → **False**，校验放行）→ 网址/邮箱/路径被中文短语替换写进产物，没有任何一道闸拦得住。

**后果**：英译中（以及任何 X→中文）的表格/文档里，每个网址、邮箱、文件路径都是一次自费的模型调用；报告里多出一批看不懂的「质量校验回退」条目误导用户去查译文质量；模型一旦按字面翻译，交付件里的链接和联系邮箱就被替换成中文词，用户点不开也发不出去。含供应商联系表、参考链接的文件必踩。

**复现**：已复现：t1_filter_matrix.py（39 个样本的三向判定表）+ t1_url_zh.py（补译计划 5 条 source_only 全含 URL/邮箱/路径；`is_translation_redundant(url, '示例网址', 'zh')` 返回 False，即译坏了也放行）。

**修法**：在 `should_translate` 的 target=zh 分支里，把 zh→X 方向已有的「结构化字面量」保护补齐并抽成一个共用判据（两个方向共用一份，别再各写一套）：无空格且匹配 URL（`^\w+://` 或 `^www\.`）、邮箱（`^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$`）、绝对/相对路径（含 `/` 或 `\` 且无空格）、纯扩展名文件名的一律 return False。同时在 `_validate_translation_strict` 里加一条对称守卫：原文整体是 URL/邮箱/路径而译文不是同一串时判 fail，堵住「模型真把网址翻了」这条没人拦的路。

### 中-16 清洗建议写回把多行译文折成单行，多行单元格的换行在「深度清洗」这条路上又被吃掉了

`M1` · 置信度 high · regression-of-prior-audit

**位置**：`core/tm_cleaner.py:226`、`core/tm_cleaner.py:214`、`core/tm_cleaner.py:310`

**机制**：`_sanitize_clean_suggestion`(310) 先调 `_normalize_clean_target`(214)，后者最后一句是 `_MULTISPACE_RE.sub(" ", cleaned)`，而 `_MULTISPACE_RE = re.compile(r"\s+")` 会连换行一起吃。之后才调 `normalize_tm_text_for_storage` —— 可那个函数自 9.3.x 起专门改成保留换行（tm_text.py 的 docstring 写死了「绝不把换行折成空格——那会让二次跑同一份文档时多行单元格塌成单行」），此时换行早就没了，它只能把一个已经是单行的串再规整一遍。也就是说中-5 的修复在「入库」这条路上守住了，在「清洗写回」这条路上没守住：tm_cleaner 用的是自己那套 `\s+` 折叠，没跟着换成 tm_text 的行内空白正则 `[^\S\n]+`。注意只有「模型确实改了内容」才会出问题：模型原样返回时 `normalize_tm_text_for_compare` 比较相等，建议被丢弃，看不出症状。

**后果**：库里凡是多行的译文（Excel 多行单元格、Word 里带软换行的段落），一旦被深度清洗改写并由用户确认写入，换行就永久变成一个空格。下次翻同一份文档时 TM 命中回填的是单行译文，排版与首次交付不一致——正是中-5 当初要消灭的那个症状，只是入口换成了清洗。用户在复核面板上看到的 diff 也不会提示「顺便把你的换行去掉了」。

**复现**：已复现（scratchpad/m1_clean.py）。current='第一行\n第二行'，模型建议 '第一行改\n第二行改'：`normalize_tm_text_for_storage` 保留换行（'第一行改\n第二行改'），但 `_sanitize_clean_suggestion` 返回 '第一行改 第二行改'，`_build_clean_suggestion(...).new_target` 同样是折平的 '第一行改 第二行改'。

**修法**：`_normalize_clean_target` 末尾那句改成只折行内空白：复用 tm_text 里的 `[^\S\n]+` 口径（或直接 `return normalize_tm_text_for_storage(cleaned)`，它已经做了「统一换行符 + 折行内空白 + 逐行 strip」），别再用 `\s+`。`_looks_like_meta_output`(286) 里那句 `_MULTISPACE_RE.sub` 只用于判定、不落库，可以不动。补一条测试：多行译文经清洗建议写回后 `\n` 仍在。

### 中-17 「深度清洗」一键起跑，不告诉用户要跑多少条、要花多少次模型调用，也没有任何上限

`M1` · 置信度 high · new

**位置**：`ui/src/views/library.ts:1424`、`ui/src/views/library.ts:716`、`api/task_manager.py:359`、`core/tm_cleaner.py:420`

**机制**：`tmClean()`(716) 只做了「更新后没重启」的拦截，随后直接 preflight → 起任务；`openTaskRiskModal` 只有在「与别的活动任务共用 API 连接」时才弹，且弹的内容全是并发风险，一个字没提规模。服务端 `_prepare` 里 tm_clean 分支（task_manager.py:359-386）构造的 task_snapshot 写死 `"selected_file_count": 0`，既不查词条数也不估批次数。`run_cleaning`(420) 拿 `get_all_entries_for_cleaning(lang_pair)` 的**全部**未固定 auto 词条，无上限、无采样、无分段确认，按 batch_size（默认 20，settings.py:486）切批全部提交。界面上唯一的反馈是状态条一句「正在分析未固定条目…」。

**后果**：老用户库里几万到几十万条是常态。实测 20 万条的库，get_all_entries_for_cleaning 全量返回，按默认 batch_size=20 就是 1 万次模型调用，用户在点下按钮之前完全不知道这个数字，点完才发现账单。配合上面那条「一个批次失败全丢」，这笔钱还可能白花。按项目硬约束「用户为每一次模型调用真金白银付费」，一个不可预估、不可分段、无上限的一键花钱入口是需要拍板的产品缺口。

**复现**：机制确认。代码路径已逐段核对（library.ts 无规模提示、task_manager tm_clean 分支 selected_file_count 恒为 0、run_cleaning 无 limit）；实测部分：scratchpad/m1_scale.py 造了 20 万条库，`get_all_entries_for_cleaning` 口径下这 20 万条全部合格（word_type=auto、pinned=0），除以默认 batch_size=20 即 1 万批。未真跑模型调用（不花钱）。

**修法**：起任务前先算一次规模：在 task_manager.py:359 的 tm_clean 分支里查 `len(get_all_entries_for_cleaning(lang_pair))` 与 `ceil(n / batch_size)`，塞进 task_snapshot；前端在 `tmClean()` 里无条件弹一次确认框，把「本次将分析 N 条词条，约 M 次模型调用」说清楚（现在的共享连接风险框可以合并进去）。是否再加「只清洗最近 N 条 / 分批跑」的上限选项属产品拍板，建议至少先把数字亮出来。

### 中-18 运行中每来一条 SSE 事件就整屏重建工作区，「逐页审核」表和文件表的滚动条被打回顶部——任务跑着的时候根本没法翻表

`F1` · 置信度 high · new

**位置**：`ui/src/views/workspace.ts:4337`、`ui/src/views/workspace.ts:727`、`ui/src/views/workspace.ts:1455`、`ui/src/views/workspace.ts:2472`、`core/headless_pdf_translate.py:139`、`api/task_manager.py:1788`

**机制**：handleTaskEvent() 在 switch 末尾无条件 `rerender(surface)`（workspace.ts:4337），而后端每一条 LogMsg 都原样转成一个 SSE `log` 事件、没有任何合并或节流（headless_pdf_translate.py:139 的 while 循环里逐条 _emit_event；api/task_manager.py:1788 的 _append_event 同样逐条入队）。rerender → renderInto()（workspace.ts:727）第一件事就是 `while (container.firstChild) container.removeChild(container.firstChild)`，把整屏拆光再重建：文件表（buildTableCard）、逐页审核表（buildPdfReviewCard，snapshot 里**每一页**都建一个 tr，收起的分组也照建、只加 .hid 类）、200 行日志、右栏全部控件。
关键点在滚动容器本身也是每次新建的：buildTableCard 里 `const tableWrap = el("div"); tableWrap.style.cssText = "flex:1;overflow:auto"`（workspace.ts:1455），buildPdfReviewCard 里 `tableWrap.style.cssText = "flex:1;min-height:0;overflow:auto"`（workspace.ts:2472）。新建的 div 其 scrollTop 定义上就是 0，所以用户翻到的位置每次事件都归零。
对照证据：这个代码库对**日志面板**专门写了 captureLogScroll / restoreLogScroll（workspace.ts:684/705，含锚点行 data-seq 的完整方案），说明作者清楚整屏重建会毁掉滚动位置——但这套保护只认 `container.querySelector(".log")` 一个元素，两张表一个都没覆盖。

**后果**：1) 交互：PDF 任务跑到一半，用户想在「逐页审核」里翻到第 150 页看某一页的状态——每来一条日志（后端逐条发、不节流）表格就跳回第 1 页，实际等于翻不动；Excel/Word 批量几十上百个文件时，运行中的文件清单同样翻不动。而运行中恰恰是用户最想盯着表看的时候。
2) 性能：实测每次重建 300 页 ≈ 16 ms、800 页 ≈ 38 ms（Chrome；WKWebView 更慢）。日志密集时段这些开销直接压在主线程上，表现为整屏发涩、按钮点击迟滞。

**复现**：已复现（机制部分为代码路径确认，成本部分为实测）。脚本：/private/tmp/claude-501/-Users-lijianwei-vibecoding-claude-XL-Translator/cef9c101-5dad-4a16-b030-8f75baf592e2/scratchpad/bench_rerender.html，Chrome 实测输出 {"pdf_300页_200日志":"16.0 ms/次","pdf_100页_200日志":"6.9 ms/次","pdf_800页_200日志":"38.0 ms/次","excel_0页_200日志":"2.1 ms/次"}。滚动归零这一半是定义级确定：tableWrap 每次 renderInto 都是 `el("div")` 新建的节点，新节点 scrollTop 恒为 0，无需实测。附带做的对照实验 repro_detail_scroll.html 证明「同一元素清空再填」**不**丢滚动（600→600），正说明问题出在「容器被换掉」而不是「内容被清空」。

**修法**：两步，第一步就能解决可用性：
(1) 把 captureLogScroll/restoreLogScroll 那套推广成通用的「按 CSS 选择器保存/还原滚动位置」——renderInto 开头对 `.log`、`.tablecard [style*=overflow]`（或给两个 tableWrap 加上稳定的 class，如 `.ws-scroll`，并各配一个 data-scroll-key）统一 capture，重建后统一 restore。表格行不像日志会被 200 条上限挤掉，直接还原 scrollTop 即可，不需要日志那套锚点行方案。
(2) 给 rerender 加一层 requestAnimationFrame 合帧：handleTaskEvent 只置脏标记，同一帧内的多条 log/progress 事件只重建一次。这样 800 页那档 38 ms/次的开销从「按事件数」降到「按帧」，也顺带减少滚动被打断的次数。
（更彻底的做法是逐页审核表改成只重建变化的行，但那要给行做 key 索引，工程量大得多；先做 (1)+(2)。）

### 中-19 记忆库切换语言对失败时，源/目标语言等模块状态不回滚也不重渲染，导致「已选」与实际选中 ID 集合脱节，后续全选/批量删除可能作用在用户未看到的语言对上

`F2` · 置信度 medium · new

**位置**：`ui/src/views/library.ts:368-394`、`ui/src/views/library.ts:328-354 (handleSelectAllTm)`、`ui/src/views/library.ts:428-452 (confirmBulkDelete)`、`ui/src/views/library.ts:264-296 (applyTmQueryChange，对照用的“已修好”版本)`

**机制**：saveLangPair() 一进函数体就直接把 sourceLang/targetLang/page 改成新值、清空 selectedIds、并把新 pair 塞进 recentPairs（368-380 行），然后才 try persistSettings(PUT /api/settings)。一旦这次 PUT（或紧随其后的 refreshTm/refreshConflicts）失败，catch 分支（391-393 行）只弹一条 toast，不回滚 sourceLang/targetLang/page，也不调用 rebuildToolbar()/renderTable()——表格 DOM 停在旧数据，但模块级 sourceLang/targetLang 已经是新值。文件内其余所有用 tmLangPair() 取当前语言对的调用点（handleSelectAllTm 的 fetchAllMatchingTmIds、openCleanReviewForCurrentPair 等）此后都会用这个“未落盘、未渲染”的新 pair 去查询。confirmBulkDelete() 直接拿 selectedIds 里的原始 id 发 /api/tm/entries/bulk/delete，完全不校验这些 id 是否属于当前表格显示的语言对。对照同文件 264-278 行 applyTmQueryChange() 的写法——那里明确写了大段注释说明“旧代码请求前清空选中集合、失败不回滚不提示”是要修的 bug，并做了完整的 snapshot/restore；但 saveLangPair()（语言对切换，产品含义与搜索/翻页完全同类）没有套用同一套修法，是同一 bug 类在另一入口的漏网实例。

**后果**：用户切换记忆库语言对时如果这次保存失败（网络抖动、后端瞬时 5xx），界面上的语言选择控件（自定义下拉组件）大概率已经视觉上显示了新选项（点击即改自身展示态），但下方表格仍是旧语言对的数据，之前勾选的行的“已选”计数被清零但复选框视觉状态未刷新——已经是一次可感知的不一致。更严重的是：只要用户此后点了一次“选择全部”，实际发出的查询用的是新（未提交）语言对，selectedIds 里装的 id 属于新语言对，而屏幕上仍呈现旧语言对的行；此时点“批量删除”，删除请求按 selectedIds 原样发送，删掉的是用户根本没有在看的那个语言对下的记录，且没有任何界面文案提示“这些 id 不对应你现在看到的表格”。属于会造成非预期数据丢失的记忆库操作，触发条件是“语言对切换保存失败 + 用户不理会失败 toast 继续操作”的复合场景，不常见但一旦触发后果是不可逆删除。

**复现**：机制确认（未做实测复现）。已完整读取 saveLangPair()（368-394 行）确认状态在 try 之前无条件修改、catch 分支无回滚无重渲染；已读取 handleSelectAllTm()/fetchAllMatchingTmIds()（307-352 行）确认其查询参数来自 tmLangPair() 这一模块级可变状态；已读取 confirmBulkDelete()（428-452 行）确认删除请求直接使用 selectedIds 原始值、不做语言对归属校验。未搭建 PUT /api/settings 失败的实际前端环境去触发（项目前端目前没有任何测试基建，ui/package.json 只有 tsc --noEmit / vite build，没有 vitest/jest，临时补一套 DOM+mock 环境的成本超出本轮预算），故标“机制确认”而非“已复现”。

**修法**：把 saveLangPair() 改成和 applyTmQueryChange() 一样的 snapshot/try/catch-rollback 模式：进入前拍一份 { sourceLang, targetLang, page, selectedIds, recentPairs } 快照；PUT 或后续 refreshTm/refreshConflicts 任一步失败时，把这几个变量原样恢复成快照值再调用 rebuildToolbar()/renderTable()，让 DOM 与失败后的真实状态对齐，toast 之外不留任何“看不见的状态漂移”。最省事的做法是直接让 saveLangPair 复用 applyTmQueryChange 的 change 回调机制（把 sourceLang/targetLang 的赋值也塞进 change() 闭包里），避免同一套回滚逻辑维护两份。

### 中-20 共享公式主控权让渡仍然是 O(n²)——上一轮中-28 只压小了常数，没改掉复杂度，3200 行 4.4 秒、上万行仍是分钟级假死

`D1` · 置信度 high · regression-of-prior-audit

**位置**：`core/xlsx_patcher.py:977`、`core/xlsx_patcher.py:1001`、`core/xlsx_patcher.py:1035`

**机制**：`_SharedFormulaIndex.dependents`（core/xlsx_patcher.py:977）确实把「整表重扫」换成了「只扫这一组」，但漏了一件事：公式格被改写成静态双语文本之后，让渡的主控权会落到组里下一个格子，而那个格子紧接着也会被改写，于是 `_promote_shared_formula` 对同一组要被调用 n 次，每次都把当前存活的 n、n-1、n-2 … 个成员整个走一遍（`for row_num, col_index, cell in entries` + 后面 1035-1038 行四个 `min`/`max` 生成器又各扫一遍）。总量仍是 n²/2。cProfile 实证：n=3200 时 `dependents` 被调用 3200 次、tottime 4.99s，占总耗时 7.46s 的 67%，min/max 再吃 0.8s。

**后果**：一张只有一列长共享公式的表，写回阶段的耗时按行数平方涨。实测每翻一倍行数耗时涨约 4 倍，外推 12800 行约 71s、25600 行约 4.7 分钟——这是在所有模型调用都已完成、界面停在「正在写入」之后才发生的纯 CPU 空转，用户看到的就是任务假死。上一轮审计的 中-28 判为已修，但只是把常数从 800 行 2.08s 降到 0.27s，量级没变。

**复现**：已复现。脚本 d1_perf_shared.py（同上目录）实测：n=200 → 0.10s；n=800 → 0.27s；n=1600 → 1.07s；n=3200 → 4.44s（每翻倍 ×3.9~4.1，标准 O(n²) 特征）。脚本 d1_prof.py 的 cProfile：`3200 calls, tottime 4.986s  xlsx_patcher.py:977(dependents)` 排第一。

**修法**：别在每次让渡时重扫整组。给每个组维护一个游标（下标）而不是每次重建 `live` 列表：`dependents` 从上次停下的位置往后推进，跳过已失效条目就永久前移游标；同时把组的 min_row/min_col/max_row/max_col 在 `_build` 时算一次并随游标增量维护，别每次让渡都 4 次全组扫描。更彻底的做法是在 `_build` 时就把每组的成员按文档顺序存成 deque，让渡时 popleft 到第一个仍带 `<f>` 的成员即可，整组总代价降到 O(n)。

### 中-21 「锁定行高」模式对默认行高的行永远缩不进去，结果是整表字号被无差别压到 6pt 下限，并逐格刷警告

`D1` · 置信度 high · new

**位置**：`core/xlsx_patcher.py:1736`、`core/xlsx_patcher.py:614`、`core/xlsx_patcher.py:1545`、`config.py:402`

**机制**：双语文本一定含换行（config.py:407 `BILINGUAL_SEPARATOR = "\n"`），所以 `estimate_required_lines` 恒 ≥ 2。可见行数 `estimate_max_visible_lines(row_height, size) = int(row_height / (size * 1.35))`，在 Excel 默认行高 15pt（或真实文件常见的 14.4pt）下：size=11 → int(11.11/11)=1，size=6（PRINT_GUARD_FONT_FLOOR）→ int(11.11/6)=1。整个 [6, 11] 区间可见行数恒为 1，永远追不上 required≥2。于是 `_shrink_font_for_locked_row` 的 while 循环必然一路走到 `current_size <= min_size` 才 break，`reached_floor` 恒为 True。要装下 2 行需要字号 ≤ 5.55pt，正好被 6.0 的下限卡在门外。

**后果**：用户打开「锁定行高」（用意是保住打印版式），拿到的是一张**每一格译文都变成 6pt** 的表——几乎读不了；同时任务日志对每一个被翻译的单元格刷一条「缩至最小字号 6.0pt 仍可能无法完全显示」，万格的表就是万条警告，真正需要关注的告警被淹没。更糟的是这次缩字号没有换来任何收益：可见行数从头到尾都是 1 行，只是把用户的字号毁了。

**复现**：已复现。脚本 d1_merge.py，lock_row_height=True 那一轮输出：A1 字号 11.0 → 6.0；A5（普通单元格、无合并、列宽 12、行高未设即默认 15pt）同样报 `[WARN] Sheet!A5 缩至最小字号 6.0pt 仍可能无法完全显示`。手算复核：int(15/(1.35*11))=1，int(15/(1.35*6))=1，required=2，恒不满足。

**修法**：在 `_shrink_font_for_locked_row` 里先算一次「下限字号下的可见行数」：如果 `estimate_max_visible_lines(row_height, min_size) <= estimate_max_visible_lines(row_height, original_size)`，说明这一格在允许区间内缩多少都不会多出一行，直接返回 `(None, True)`——不动字号，只记一次「装不下」。另外警告要按分表聚合成一条（「本表 N 格在锁定行高下装不下，已保持原字号」），不要逐格刷。产品上还得拍板：默认行高的行本来就只放得下一行，锁定行高 + 双语是一对天然矛盾，是否该在开关旁边直接说明。

### 中-22 合并单元格的排版估算只按左上角那一列的宽度算，合并标题行被撑成几倍高（或在锁行高模式下直接压到 6pt）

`D1` · 置信度 high · new

**位置**：`core/xlsx_patcher.py:1793`、`core/xlsx_patcher.py:1545`、`core/xlsx_patcher.py:622`

**机制**：`xlsx_patcher.py` 全文没有出现过 `mergeCell`（grep 零命中），`_SheetGeometry` 也只读 `<col>`/`<row>`。行高自适应 `_auto_adjust_row_heights`（core/xlsx_patcher.py:1793）用 `geometry.col_width(col_index)` 只取锚点格自己那一列的宽度；锁行高的 `_shrink_font_for_locked_row` 调用点（core/xlsx_patcher.py:1545）同样只传单列宽。合并区的实际可用宽度是区内所有列宽之和，估算值因此系统性偏小若干倍，required_lines 被高估同样倍数。

**后果**：中文表里「A1:H1 合并的大标题」是最常见的版式。实测一个 8 列合并（总宽 96 字符位）、55 字符双语标题：自适应模式把第 1 行行高从默认撑到 77pt（按 5 行算，实际 1 行就够），整张表顶上多出一大块空白；锁行高模式下同一格字号被从 11pt 压到 6pt 并报「仍可能无法完全显示」。两种模式都是把用户明确排过的版式弄坏，且方向相反、都错。

**复现**：已复现。脚本 d1_merge.py：夹具 A1:H1 合并、每列宽 12（合计 96）、译文合成后共 55 字符。lock=False → 第1行 height=77.0；lock=True → A1 字号 6.0 且报 `[WARN] Sheet!A1 缩至最小字号 6.0pt 仍可能无法完全显示`。对照组 A5 未合并，行为符合预期。

**修法**：在 `_SheetGeometry.__init__` 里顺手解析 `<mergeCells>/<mergeCell ref=...>`，建一张「锚点坐标 → 合并区列范围」的表，新增 `effective_col_width(row, col)`：命中合并区就返回区内各列 `col_width` 之和，否则返回本列宽。`_auto_adjust_row_heights` 与 `_shrink_font_for_locked_row` 的调用点都改用它。跨多行的合并区（ref 高度 > 1）同理要把可见高度按区内行高求和，否则纵向合并的格子会被判成装不下。

### 中-23 替换模式把整段译文塞进第一个 run，段首若是上标脚注号 / 彩色强调字，整段译文继承那个格式（实测整句变成红色上标）

`D2` · 置信度 high · new

**位置**：`core/word_document.py:2494`、`core/word_document.py:1354`、`core/word_document.py:632`

**机制**：`_replace_paragraph_text`（word_document.py:2494）的策略是：取 `_paragraph_content_runs` 的第一个 run 当锚点，把整段译文写进去，其余 run 一律清空。锚点选择在 `_paragraph_text_anchor_run`（:1354）里，唯一的特判是「段落以超链接开头就在超链接前插一个新 run」——只解决了「译文别变成链接」，没管锚点自身的字符格式。
run 分裂在真实 docx 里是常态，而分裂出来的第一个 run 恰恰经常是格式异类：段首的上标脚注号/角标、被标红的强调词、定义式段落开头加粗的术语（「**安全生产**：指……」）、首字下沉。`Run.text = ...` 只换 `w:t`，`w:rPr` 原封不动留着，于是整句译文戴上了本来只属于一个字符的格式帽子。页眉页脚走同一个函数（word_document.py:632 的 `apply_header_footer_translations`），同样中招。

**后果**：产物观感直接坏掉，而且是整段而不是一个字：段首有脚注号的条款，整条译文变成缩小的上标；段首有红字的段落，整段译文变红。用户会认为程序把文档格式搞乱了。这类段落在规章、合同、技术规范里成片出现，一份文档往往不止一处。

**复现**：已复现。脚本 d2_runfmt.py：段落 = run1「1」(superscript=True, color=FF0000) + run2「 本条款适用于全体员工及外聘人员。」，走 `write_bilingual_docx` 的 `__XL_REPLACE__::` 路径。输出——
'1 This clause applies to all employees and outsourced staff.' super= True color= FF0000
'' super= None color= None
整句英文译文继承了上标 + 红色。

**修法**：锚点位置可以不动（保住词序与超链接规避），但字符格式要改成跟「主体」走：在 `_replace_paragraph_text` 里先按 `len(run.text)` 找出原段落里文字最多的那个 run 作为「主体格式源」，若它不是锚点，就把它的 `w:rPr` 深拷贝覆盖到锚点上再写文字（现成的 `_copy_run_shape` 已经在做类似的事，可以复用其取「统一格式」的判定）。更保守的最小改法：只在锚点承载的原文字数占全段不足某个比例（比如 1/3）时，从锚点的 `w:rPr` 上剔除 `w:vertAlign`（上标/下标）、`w:color`、`w:highlight` 这几个最容易致灾的属性。

### 中-24 书签只圈住段落中间一段文字时，替换模式把文字全挪到 bookmarkStart 之前，书签范围内只剩空 run，REF 交叉引用刷新后变空

`D2` · 置信度 high · new

**位置**：`core/word_document.py:2494`、`core/word_document.py:1354`

**机制**：同样出在 `_replace_paragraph_text` 的「锚点吃掉全部文字、其余 run 清空」。`w:bookmarkStart` / `w:bookmarkEnd` 是 `w:p` 的直接子元素，函数完全不看它们：锚点固定取第一个内容 run。当书签的起点排在第一个 run 之后（用户选中段落中间一个短语插书签、Word 为交叉引用自动生成的 `_Ref…` 书签圈住的是被引用的那截文字），译文就落在 bookmarkStart 之外，书签区间里只剩一个被清空的 `<w:r/>`。书签坐标还在，但范围内零字符。
注：书签在段首（`<w:bookmarkStart/><w:r>正文</w:r><w:bookmarkEnd/>`，Word 给标题生成 `_Toc…` 时的常见形状）不受影响，锚点正好落在区间内。

**后果**：文档里所有 `REF 该书签` 的交叉引用（「详见 第三章 安全总则」里的那半句）在 Word 更新域后取到空串，读者看到「详见 」后面什么都没有；`\* MERGEFORMAT` 缓存被刷掉之前不显形，用户交付出去以后才炸。PAGEREF 不受影响（书签位置还在）。

**复现**：已复现。脚本 d2_bookmark.py：段落 = 「参见 」+ bookmarkStart(_Ref100) + 「第三章 安全总则」+ bookmarkEnd，走 `__XL_REPLACE__::` 路径。产物 XML——
<w:r><w:rPr>…</w:rPr><w:t>See Chapter 3 General Safety Rules</w:t></w:r>
<w:bookmarkStart w:id="1" w:name="_Ref100"/>
<w:r/>
<w:bookmarkEnd w:id="1"/>
译文落在书签外，书签内是空 run。

**修法**：在 `_paragraph_text_anchor_run`（word_document.py:1354）里加一条书签感知：先扫段落里的 `w:bookmarkStart`/`w:bookmarkEnd` 配对，若存在一个区间既不是从段首开始、也没有覆盖整段（即真正圈住段落中间一截），就把锚点选在该区间之内的第一个 run（同时保持躲开 `w:hyperlink` 的现有规则）；这样译文整体落进书签范围，REF 至少能取到完整译文而不是空串。段落里有多个互不相交书签的情形无法用单锚点全部满足，那种情况保持现状但值得在质量报告里留一条痕（与 `issue_callback` 同一条通道）。

### 中-25 `paragraph.style.name` 打在热路径上，每次都触发 python-docx 全样式表扫描——9000 段文档写回 11.5s 里 9.2s 花在这一件事上

`D2` · 置信度 high · new

**位置**：`core/word_document.py:1823`、`core/word_document.py:1744`、`core/word_document.py:1830`

**机制**：`_paragraph_style_name`（word_document.py:1823）用的是 `paragraph.style.name`。段落没有显式 `w:pStyle` 时（正文段落的常态），python-docx 会退到 `styles.default(WD_STYLE_TYPE.PARAGRAPH)` → `CT_Styles.default_for()`，那个函数每次都把 styles.xml 里的全部样式过一遍，并对每个样式做一次 `WD_STYLE_TYPE` 的枚举反解（`enum/base.py:from_xml`）。Word 默认模板带 ~160 个样式，于是一次「读段落样式名」= 160 次枚举转换。
这个调用挂在 `_is_toc_or_field_paragraph`（:1744，正文循环里逐段调）和 `_is_heading_style`/`_detect_heading_level`（:1830，抽取时逐段调）上，等于每段付一次。结果不是 O(n²)，但常数大得离谱，而且完全可以缓存掉。

**后果**：一份 9000 段（3000 正文 + 60 张 20×5 表格）的 docx，`write_bilingual_docx` 实测 11.54s，其中 9.17s（79%）耗在 `default_for`；`extract_word_segments` 另花 3.57s，覆盖率体检再花 2.06s。真实的 500 页文档段落数只多不少，用户在全部 API 调用都结束之后，还要盯着界面干等十几到几十秒的纯 CPU 空转，且这段时间没有任何进度反馈。批量翻译时每个文件都付一遍。

**复现**：已复现。脚本 d2_perf.py / d2_callers.py：造 3000 段正文 + 60 张 20×5 表格的 docx（9000 段落、14220 个待译单元），cProfile 结果——
write 11.54 s；docx/oxml/styles.py:292(default_for) ncalls=25920 cumtime=9.171s
print_callers 显示其中 12300 次（cumtime 4.529s）直接来自 core/word_document.py:1823(_paragraph_style_name) ← :1744(_is_toc_or_field_paragraph)，其余来自写回时新建段落取默认样式。
extract 3.57 s / coverage 2.06 s 同一份文档。

**修法**：两处一起改：① `_paragraph_style_name` 改成先直接读 XML——`paragraph._p.pPr` 下的 `w:pStyle/@w:val`，取到就返回（注意这是 styleId 不是 name，`_is_toc_or_field_paragraph` 里判 'toc'/'目录' 对 styleId 同样成立，`_is_heading_style` 的判定要一并按 styleId 校准并补测试）；② 取不到 pStyle 时才回落到 `paragraph.style.name`，并把「本文档的默认段落样式名」按 `id(doc.part)` 或直接在调用方按文档缓存一次，不要每段重算。粗估写回从 11.5s 降到 ~2.4s（约 4.8×），抽取同比例受益。

---

## 低危（12 条）

### 低-1 Word 转换失败留下的半成品临时 docx 没人回收——Excel 侧修了（低-25），Word 侧从来没修

`C2` · 置信度 high · new

**位置**：`core/word_converter.py:50-110`、`core/word_converter.py:186-214`、`core/word_converter.py:268-292`、`core/word_converter.py:768-771`、`core/word_task_runner.py:2597-2657`、`core/word_task_runner.py:918`

**机制**：两处叠加。(a) core/word_converter.py 整个文件里一次 unlink / _discard_partial_output 都没有（grep 实证）。convert_doc_to_docx 和 convert_numbering_to_text_with_native_apps 都是「多策略依次试」：每个策略先用 _get_temp_docx_path 在 $TMPDIR/word_translator_temp 下生成唯一路径、落盘产物，再由 _validate_docx 校验；校验不过或落盘后抛异常时，except 只把错误文案记进 errors 列表就 continue 下一个策略，那份已经写到盘上的 docx 谁都不删。对照 core/xls_converter.py:169-176 的 _discard_partial_output——Excel 那条路上一轮已经补了这个动作，Word 这条路没有。(b) core/word_task_runner.py 的 _prepare_word_source_for_translation 把 temp_paths 攒在函数局部，只有正常 return 后调用方（:918）才 extend 进 converted_temp_paths，:2214 的 finally 才清得到。只要这个函数在中途抛出（.doc 已经转成 docx 之后 normalize_docx_automatic_numbering 再失败，是最典型的一条），先前生成的那一两份临时 docx 就永远没被登记，谁都清不掉。

**后果**：用户每翻一批 .doc / 需要编号预处理的 Word，只要有策略失败（本机没装 Word、LibreOffice 自带 Python 起不来、转出来的东西 python-docx 读不动），就在系统临时目录留下一份和原文档同量级的 docx，且不在任何清理路径上。单次几十 MB 量级，长期只靠 macOS 自己的临时目录老化回收（Windows 上连这个都没有）。不影响译文正确性，是磁盘慢性泄漏。

**复现**：已复现（c2_word_leak2.py）：造一个带 OLE 头的坏 .doc，注入 _validate_docx 失败（模拟「转换器落了盘但产物不是有效 docx」这个真实条件），调 convert_doc_to_docx(prefer_native_word=False, allow_compatibility_fallback=True)。输出：convert_doc_to_docx failed: WordConversionError / LEAKED temp files: 2 / broken2_31a5b36a.docx 3531 bytes / broken2_d9cc37ad.docx 5113 bytes——LibreOffice 和 textutil 两次尝试各留一份。(b) 那一半是机制确认，没构造出 normalize 抛错的输入。

**修法**：在 core/word_converter.py 里补一个和 xls_converter._discard_partial_output 同形的 helper，convert_doc_to_docx / convert_numbering_to_text_with_native_apps 的 except 分支里对该次尝试的 output_path 调一次（_validate_docx 失败那条也要走到）。(b) 处把 _prepare_word_source_for_translation 改成 try/except：抛出前先删掉本次已经生成的 temp_paths，或者改成接收调用方传进来的 converted_temp_paths 列表、边生成边登记，让 :2214 的 finally 能兜住。

### 低-2 维护页「临时工作区」是死条目：永远 0 项，而真正会堆积的临时目录既不显示也无处清

`C2` · 置信度 high · new

**位置**：`core/maintenance.py:31`、`core/maintenance.py:67`、`core/maintenance.py:250-263`、`api/app.py:1905`、`api/app.py:1924-1925`、`core/word_converter.py:769`、`core/xls_converter.py:234`

**机制**：clear_owned_workspaces 只删 APP_DATA_DIR/workspaces 下带 .translator-workspace.json 标记的目录。全仓库 grep：写这个标记的地方只有 tests/test_phase8_maintenance_contracts.py，没有任何生产代码创建 APP_DATA_DIR/workspaces 或那个标记文件（PDF 分页存档实际落在用户的输出目录，见 core/pdf_image_translation.py:1121-1126 resolve_pdf_page_archive_dirs，不在 APP_DATA_DIR 下）。于是 data_overview 里这一栏恒为 0 B / 0 项，而 api/app.py:1905 还把它和 keys/settings/tm 一样归进「必须二次确认」的危险类别——用户被弹一次确认框，换来删掉 0 个东西。与此同时，真正在堆积的是 $TMPDIR/xl_translator_temp、$TMPDIR/word_translator_temp 和异常退出留下的 xl_translator_lo_* / word_translator_lo_uno_* profile 目录，它们既不在 data_overview 里，也没有任何清扫入口或启动清扫。

**后果**：两层。用户侧：维护页多一栏永远为空的假条目加一个永远无效的确认弹窗，是误导性文案；而真正占盘的临时残留他看不见也清不掉。工程侧：_get_temp_docx_path / _get_temp_xlsx_path 写死 tempfile.gettempdir()，不认 TRANSLATOR_APP_DATA_DIR，测试也隔离不掉——本机 $TMPDIR 就攒了实测 1325 个文件 / 47 MB。

**复现**：已复现（c2_ws.py）。APP_DATA_DIR 指向 mktemp 目录后输出：{'id': 'workspaces', 'label': '临时工作区', 'size_bytes': 0, 'count': 0, 'clearable': True} / WORKSPACES_DIR: .../workspaces exists: False / clear result: {'category': 'workspaces', 'removed_count': 0}。同时 ls 本机 $TMPDIR 实测：word_translator_temp 1325 个 .docx 共 47M（最早 2026-08-28）、xl_translator_temp 10 个 .xlsx、word_translator_lo_uno_7no3wp5l 与 _xep3pzrn（2026-08-28）、xl_translator_lo_498y5qt8 与 _hq9o445_（2026-08-31）——后四个是 mkdtemp + finally rmtree 的目录，它们还在就说明 finally 没跑到，即进程被杀过。据文件名判断这批多数由测试套件产生，但没有任何生产代码会清它们这一点是一样的。

**修法**：二选一，建议后者：(1) 最省事——把「临时工作区」这一栏和 clear_owned_workspaces 一起删掉（含 api/app.py:1905 的确认名单、Literal 类型、前端那一行），别在维护页留死按钮；(2) 更有价值——把这一栏改成真的指向 $TMPDIR/xl_translator_temp、$TMPDIR/word_translator_temp、$TMPDIR/{xl,word}_translator_lo*，统计真实体积并支持清理，同时在 sidecar 启动时顺带清一次超过 N 天没动过的残留（启动清扫必须只删本 app 自己 prefix 的路径，且跳过当前进程正在用的）。顺带把 _get_temp_docx_path / _get_temp_xlsx_path 的根目录改成可注入，测试才隔离得掉。

### 低-3 settings.json 的位置被目录占住时，连维护页的显式重置都抛 IsADirectoryError，一条出路都不剩

`P1` · 置信度 high · new

**位置**：`settings.py:1547`、`settings.py:1327`、`settings.py:1533`

**机制**：目录会让 `SETTINGS_PATH.exists()` 为真、`read_text()` 抛 IsADirectoryError（属 OSError）→ 判 `unreadable`，这一步是对的。但 `_recreate_settings_file(force=True)` 走到 settings.py:1547 的 `_write_text_atomic()` 时，settings.py:1327 的 `os.replace(temp_path, path)` 目标是个目录，POSIX 下 rename 文件到已存在的目录必失败 → IsADirectoryError 直接抛出。也就是说，被设计成「最后一条出路」的显式重置本身也堵死了。

**后果**：数据目录被同步工具（iCloud / OneDrive / 坚果云的冲突目录）或一次异常写入留下同名目录时，应用彻底进不去设置也重置不了，只能让用户自己去 Finder 里删。概率低，但性质是硬约束里最禁止的那一类：拒绝写入且不给出路。

**复现**：已复现。`p1_corrupt.py dir`：status → unreadable；save_settings → SettingsSchemaError；`force_reset` → `RAISED IsADirectoryError: [Errno 21] ... '.settings.json.rhqbl49a.tmp' -> '.../settings.json'`；backups 目录为空。

**修法**：在 `_recreate_settings_file` 的 force 分支里（settings.py:1533 之后、写新文件之前）加一句：目标存在且 `is_dir()` 就先 `shutil.move` 到 backups（挪不动再 `shutil.rmtree`），再走 `_write_text_atomic`。同样的判断建议在 `_inspect_settings_file` 里也加一个 `is_dir()` 分支，让它归 `unusable` 而不是 `unreadable`——目录里没有用户配置，没有「不能覆盖」的理由。

### 低-4 core/maintenance.py 的 reopen_quick_start 是死代码，却借用了 replace_incompatible=True——一旦接线就会在 settings.json 暂时读不到时无备份地把整份配置换成默认值，还报成功

`P1` · 置信度 high · new

**位置**：`core/maintenance.py:201`、`settings.py:1533`、`ui/src/views/settings.ts:512`

**机制**：`replace_incompatible=True` 的契约是「维护页的显式重置，用户已经放弃旧文件」，所以 `_recreate_settings_file(force=True)` 在备份失败时只 log 一句就继续覆盖（settings.py:1533-1543）。`reopen_quick_start()` 做的却是「把 quick_start_completed 翻成 false」这种良性操作，也传了 force。而 `unreadable`（Windows 上杀软/备份软件占住文件、或一次 EACCES）恰好是「读不出来 ⇒ 也复制不出来 ⇒ 备份必然失败」的组合，于是这条良性操作会静默销毁用户全部配置。恢复事件里 `backup_path` 写成空串，前端 ui/src/views/settings.ts:512 又用 `|| "备份目录"` 兜底，横幅会告诉用户「已备份到 备份目录」——而实际上一份备份都没有。当前 grep 全仓（排除 .venv/.runtime/dist/worktree）只有定义、没有任何调用方，也没有测试覆盖，所以今天影响为零。

**后果**：今天为零（无调用方）。留着的风险是：这是个看起来人畜无害的函数名，谁把「重新打开快速上手」接上按钮就踩雷，而雷的效果是无备份地清空用户全部设置并显示成功。附带一个独立的小问题：`_recreate_settings_file` 的 force 分支允许 `backup_path=""`，与 `_load_keys_unlocked` 里「这也保证恢复事件里的 backup_path 永不为空」的注释自相矛盾，前端的兜底文案因此会撒谎。

**复现**：已复现。`p1_quickstart_wipe.py`：先存一份 `engine.cloud_model="用户珍贵的自定义模型"、concurrency=9`，`chmod 000` 模拟占用（status 确认为 unreadable），调 `maintenance.reopen_quick_start()` → 正常返回不抛异常；恢复权限后盘上变成 `model='' concurrency=20`（默认值），`backups/settings` 目录为空，`recovery.json` 里 `backup_path: ''`。

**修法**：两件事分开做。① 直接删掉 `reopen_quick_start`（真实路径是 `PUT /api/updates/preferences`，api/app.py:1876-1878，走的是普通 `save_settings`，行为正确）；如果要留就把 `replace_incompatible=True` 去掉。② 独立于死代码：settings.py:1533 的 force 分支在备份失败时不要再写 `record_recovery_event(backup_path="")`，或者给恢复记录加一个 `backup_ok: false`，让前端说「旧文件无法备份，已直接重建」而不是编一个不存在的备份目录出来。

### 低-5 任务历史的原子写少了 fsync（settings 侧有），断电/强杀后整份历史可能变空并被静默当作空列表

`P1` · 置信度 medium · new

**位置**：`core/task_history.py:146`、`core/task_history.py:151`、`settings.py:1315`

**机制**：`TaskHistoryStore._write_locked` 是 `write_text` → `chmod` → `replace`，中间没有 `flush + os.fsync(fd)`，替换后也没有 fsync 父目录。对照 settings 侧的 `_write_text_atomic`（settings.py:1315-1334）：写完 fsync 文件、rename 后再 fsync 目录，两步都做了。APFS/ext4 上 rename 的元数据可以先于文件内容落盘，掉电后就会看到一个已改名但内容是全零或半截的 task_history.json。而且每次 upsert 都是整份 200 条重写，所以这一次撕裂丢的是全部历史，不是一条。`_read_locked`（core/task_history.py:132）把 `ValueError` 一并吞掉返回 `[]`，于是这种丢失完全静默——界面上就是「任务记录空了」，没有任何提示。

**后果**：强制关机、Cmd+Q 收尾超时被 SIGKILL、或掉电之后，任务中心的历史整份消失且没有任何解释。历史里挂着输出文件路径、单页重生成的「上一版」引用等信息，丢了以后已完成任务的产物在界面上就找不回来了。发生概率不高，但代价是整份而不是一条。

**复现**：机制确认。代码路径确定（write_text/replace 无 fsync，与 settings.py:1315-1334 逐行对照可见差异），未构造掉电环境实测——需要真机断电或 fs 故障注入才能观察到撕裂，不在本次可复现范围内。

**修法**：把 `_write_locked` 换成复用 `settings._write_text_atomic`（它已经处理了唯一临时文件名、fsync 文件、fsync 父目录、Windows ACL），只把 `file_mode` 传 0o600 即可，顺带去掉这里手写的 chmod 和 `_stray_temp_paths` 里对命名格式的重复约定。若不想跨模块依赖，至少在 `temporary.write_text` 之后补 `flush + os.fsync`、`replace` 之后补一次父目录 fsync。

### 低-6 401 响应绕过了 CORSMiddleware，没有 Access-Control-Allow-Origin——浏览器层直接拦掉，前端拿到的是不可辨识的网络错误而不是 401

`A1` · 置信度 high · new

**位置**：`api/app.py:404`、`api/app.py:414`、`api/app.py:429`、`ui/src/api-client.ts:150`

**机制**：`app.add_middleware(CORSMiddleware, ...)`（app.py:404）先注册，`@app.middleware("http") require_loopback_token`（:414）后注册。Starlette 的 add_middleware 是往 user_middleware 头部插的，后注册的在**外层**——实测 `app.user_middleware` 顺序是 `['BaseHTTPMiddleware', 'CORSMiddleware']`，即 token 中间件包在 CORS 外面。于是 token 不匹配时 `return Response(status_code=401)`（:429）这条响应根本不经过 CORSMiddleware，不带任何 CORS 头。webview 的 origin 是 tauri://localhost，请求打的是 http://127.0.0.1:PORT，属于跨源；一个没有 ACAO 的跨源响应会被 WebView 在网络层直接丢弃，fetch 以 TypeError 拒绝。另外这个 401 的 body 是空的，即使同源也没有 detail/reason 可读。
（OPTIONS 预检本身是对的——中间件显式放行 OPTIONS，由内层 CORSMiddleware 应答，实测 200 带 ACAO。路由内抛出的 404/422 也在内层，实测带 ACAO。只有 401 这一条漏在外面。）

**后果**：鉴权失败这一类故障在前端完全不可辨识：`ApiClient.request` 抛出的是 `TypeError: Failed to fetch` 而不是 `ApiError(status=401)`，`apiErrorReason()` 返回空串，界面只能报「连不上」。触发场景有限（token 来自 Tauri 的 sidecar_info，正常不会不匹配；sidecar 重启时端口也一起变，TCP 层就先失败了），所以定低。真正的代价是排障：真出现 token 不一致时，日志和界面都看不出是鉴权问题。

**复现**：已复现。scratchpad/a1_cors401.py 输出：`middleware order (outermost first): ['BaseHTTPMiddleware', 'CORSMiddleware']`；带 Origin: tauri://localhost 且 token 正确 → `200 ACAO= tauri://localhost`；token 错误 → `401 ACAO= None body= ''`；OPTIONS 预检 → `200 ACAO= tauri://localhost`；路由内 404 → `404 ACAO= tauri://localhost`。

**修法**：两选一。简单的：把 CORSMiddleware 改成在 token 中间件之后注册（即调换 app.py:404 与 :414 两块的顺序），让 CORS 包在最外层，401 也就带上头了。稳妥的：token 中间件不再自己造 Response，改成 `raise HTTPException(401, ...)` 之外的路径不好走，那就手动给这条 401 补上 `Access-Control-Allow-Origin`（按 `_allowed_origins()` 匹配请求的 Origin）并把 body 换成 `_json_error(401, "鉴权令牌不匹配，请重启应用。", reason="invalid_token")`，前端才能按 reason 分支。顺带补一条测试断言 401 带 ACAO 且 body 有 reason。

### 低-7 五个引擎对超时/重试/永久错误/连接复用各写各的，Ollama 一路最脆：401 也重试、chat() 完全不重试、失败 chunk 会连带丢弃同批已成功的结果

`G1` · 置信度 high · new

**位置**：`engines/ollama_engine.py:108`、`engines/ollama_engine.py:88`、`engines/ollama_engine.py:141`、`engines/openai_engine.py:134`、`engines/claude_engine.py:60`、`engines/claude_engine.py:78`

**机制**：差异表：(a) 重试判据——OpenAI/Claude 用 tenacity + `is_retryable_engine_error`（400/401/403/404 等不重试），Ollama 是手写 for 循环（ollama_engine.py:108-121），对任何异常都重试满 RETRY_MAX_ATTEMPTS，包括永远不会变的 401/404，而且最后一次失败后仍会 `await asyncio.sleep(1.5**attempt)` 白等；(b) chat() ——OpenAI/Claude 的 chat 走带重试的 `_call_api`，Ollama 的 chat（ollama_engine.py:141）直接调 `_call_ollama`，一次失败就抛，同一个引擎两条路径重试策略相反；(c) 部分成功——Ollama 的 `_translate_async` 在任一 chunk 失败时 `raise errors[0]`，把同批已经成功的 chunk 结果（merged）整个丢掉，交给 dispatcher 二分重译（本地不花钱，但重复算力和耗时是实打实的）；(d) 连接复用——三个引擎（含 model_catalog、connectivity_check）都是每次调用 `with httpx.Client(...)` 新建客户端，没有任何共享连接池，400 个批次 = 400 次 TCP+TLS 握手；(e) 停止响应——没有任何引擎接受 should_stop，一个已发出的请求在 120s 超时前无法中断，停止只能等在途请求自己结束。

**后果**：本地模型路径下配置写错（模型名不存在）会硬等三轮重试加退避才报错；Ollama 的 chat 路径（复核/清洗等结构化调用）一次网络抖动就整条失败，与 translate_batch 的行为不一致，排查时很难对上；每批新建 TLS 连接在远端 API 上按每次 100~300ms 计，400 批就是 40~120 秒纯握手开销。

**复现**：机制确认：逐行比对四个引擎文件（ollama_engine.py:108-121 的 for 循环无异常类型判断、:141-144 的 chat 无重试、:88-102 的 gather 后 `raise errors[0]` 丢弃 merged；openai_engine.py:134/222 与 claude_engine.py:60/78 的 `with httpx.Client(...)` 均在调用内构造）。未构造 Ollama 服务端做端到端复现。

**修法**：三件事，按性价比排序：(1) Ollama 的重试循环接上 `is_retryable_engine_error`，并把最后一次失败后的 sleep 去掉；chat() 复用同一条重试路径。(2) 把 `httpx.Client` 提到引擎实例上（引擎本身就是每任务构造一次），或用一个模块级的共享 Client + limits，拿回 keep-alive。(3) Ollama 的 `_translate_async` 改成返回 (merged, errors)，让 dispatcher 只重译失败的 chunk，而不是整批重来。

### 低-8 外层噪声剥离会把下划线包裹的工程标识符当 Markdown 强调剥掉

`M1` · 置信度 high · new

**位置**：`core/tm_cleaner.py:191`、`core/tm_cleaner.py:261`

**机制**：`_HIGH_CONFIDENCE_OUTER_WRAPPERS` 把 `("_", "_")` 和 `("*", "*")` 当高置信 Markdown 强调。`_wraps_whole_text` 对对称定界符的判据是「内部不再出现同一个字符」，`_reserved_` 内部确实没有下划线，于是判定为「整段被包住」，剥成 `reserved`。中-7 的修复解决的是「首尾恰好各有一个引号但不是一对」的情况，对「首尾确实是一对、但那不是标记而是标识符正文」这一类没有防线。

**后果**：译文里出现 `_reserved_`、`_internal_`、`*ID*` 这类工程标识/占位符时，清洗建议会给出剥掉包裹符的版本。复核面板默认全不勾（library.ts:856 已确认），用户逐条看得见，所以不会静默污染；但一次大批量清洗里混进这类建议，用户按整体印象快速勾选就会连带改坏标识符。命中面窄（要求整段译文就是那一个标识符），故定低。

**复现**：已复现（scratchpad/m1_clean.py）：`_normalize_clean_target('_reserved_')` → `'reserved'`，`_normalize_clean_target('*星号*')` → `'星号'`。同一脚本里 `'「甲」与「乙」'`、`'"甲" 与 "乙"'`、`'《书》和《报》'` 均正确保持原样（中-7 的修复有效）。

**修法**：给 `_` 和 `*` 这两对加一条附加条件：只有当内部文本不是「纯标识符形态」（`^[A-Za-z0-9_]+$`）时才剥，或者干脆要求内部含空白/CJK 才认作强调标记。`**`/`__` 双字符那两对不受影响，可保留现状。

### 低-9 清洗建议写回的乐观并发版本按 entry_id 归并，同一词条挂多条建议时版本校验会张冠李戴

`M1` · 置信度 high · new

**位置**：`core/tm_cleaner.py:1017`、`core/tm_manager.py:1706`

**机制**：`apply_suggestions_detailed` 把逐条建议的 `expected_version` 收成 `{s.entry_id: s.expected_version}` 一个 dict（1017），同一 entry_id 的多条建议里只有**最后一条**的版本活下来；`bulk_update_detailed`(1706) 再按 entry_id 取这个版本去校验**每一行**。结果是：基于旧版本译文的过期建议会被拿当前版本去校验从而通过并写入，而基于当前版本的正确建议反被判成 stale。`_settle_suggestion_rows` 的注释本身就把「同一词条挂两条建议」当成明确存在的情形来处理，说明这个前提不成立。

**后果**：触发后果很重（过期建议盖掉人工校对，正是高-7 的失效模式），但当前动线兜得住：`/api/tm/clean/suggestions` 每次 GET 都先跑 `expire_stale_cleaning_suggestions`，而复核面板的建议**只**来自这个 GET（tm_cleaning_task_runner 的 DoneMsg 不带 suggestions，前端 library.ts:790 必然回退到 fetched.suggestions），expire 之后能留在 pending 的建议版本必然与当前词条一致，所以真实 UI 下同一词条不会出现两个不同版本的待审建议。属于「靠外层不变量兜住的内部错误」，任何一次动线调整（比如让任务结果直接带回建议、或去掉 GET 里的 expire）都会让它变成真 bug。

**复现**：已复现（scratchpad/m1_apply.py CASE2，绕过 GET/expire 直接构造两条 pending）：词条「Pump casing」先有建议 A（版本 V1）→ 用户人工校对成「泵体外壳（人工校对）」→ 再生成建议 B（版本 V2）→ 两条一起确认。实际输出 `{'applied': 1, 'skipped': 1, outcomes: [{2:'updated'},{3:'stale'}]}`，库里译文变成「旧建议A」——过期建议写入成功，人工校对被覆盖，当前版本的建议 B 反被拦下。

**修法**：别再用 entry_id 当 key：给 `bulk_update_detailed` 增加一个按提交行序对齐的 `expected_versions: list[str | None]`（与 `updates`、`rows` 同序），逐行取版本；旧的 dict 形参保留给其它调用方即可。顺带把 api/app.py:245-251 里 `expected_version: str = ""` / `suggestion_id: int = 0` 的默认值收紧成必填（或在 `_resolve_pending_suggestions` 里对「既查不到 pending 行、客户端又没给版本」的建议直接判 stale 拒写），现在这两个字段一旦为空就等于关掉整个乐观并发检查。

### 低-10 PDF「查看对比」弹窗：关闭之后才下载完的页图，其 blob URL 永远不会被 revoke

`F1` · 置信度 high · new

**位置**：`ui/src/views/workspace.ts:2771`、`ui/src/views/workspace.ts:2792`、`ui/src/views/workspace.ts:2846`

**机制**：openPdfPageCompareModal 里的 load() 是异步的：`const blob = await c.getPdfPageImage(...)`，拿到后才 `const url = URL.createObjectURL(blob); objectUrls.push(url);`（workspace.ts:2792-2796）。而回收只发生在两个同步时刻——closeCompare()（2782）和「关闭」按钮的 onClick（2846-2848），两者都是对**当时**的 objectUrls 数组做一遍 revoke。如果用户在图片还没下载完时就点了关闭（或在「当前版/上一版」切换后立刻关闭），revoke 循环先跑完、fetch 后落地，新建的 blob URL 被 push 进一个再也没人遍历的数组，对应的 blob 就在 document 存活期内一直占着内存。注意 components.ts 的 openModal 没有 Esc / 点遮罩关闭，所以只有这一条异步竞态路径会漏，不是所有关闭路径都漏。

**后果**：逐页审核一份大 PDF 时，用户往往连开连关很多页的对比弹窗；A3 幅面、200 dpi 的页图单张可达数 MB。急着翻页（图还在转圈就关掉）的操作习惯下，泄漏的 blob 会一张张累积，长时间审核后应用内存持续上涨，直到重启程序才释放。不会导致数据错误，只是内存不回收。

**复现**：机制确认（代码路径推导：await 之后才 createObjectURL/push，而 revoke 只遍历同步时刻的数组；未构造真实 PDF 任务复现）。

**修法**：在 openPdfPageCompareModal 里加一个 `let closed = false;` 闭包标志，closeCompare() 和「关闭」按钮的 onClick 都置 true；load() 在 `await c.getPdfPageImage(...)` 返回后先判 `if (closed) return;`，再 createObjectURL。或者更省事：load() 里改成拿到 blob 就先 push url、再在 `closed` 为真时立刻 revoke 掉自己这一条。两种都只改 5 行以内。

### 低-11 自动行高会把用户手工设过的行高往小里改写——120pt 的行被压成 61.6pt

`D1` · 置信度 high · new

**位置**：`core/xlsx_patcher.py:1802`

**机制**：`_auto_adjust_row_heights` 算出 `new_height = max_lines * BASE_FONT_SIZE_PT * LINE_HEIGHT_RATIO` 后**无条件** `row.set("ht", ...)`，没有和原有 `ht` 取 max。函数的 docstring 只承诺保护「这次一个字都没翻的行」，但一旦这行里有任意一格被翻译，用户手工设的行高就被换成一个纯按文字行数估出来的值。而且估算固定用 `BASE_FONT_SIZE_PT`（11pt 常量）而不是这一格的真实字号，18pt 标题行会被算矮。

**后果**：为了摆图片、留版心而特意拉高的行（在报价单/施工方案这类表里很常见），只要行内有一格被翻译，行高就被压回文字估算值，悬浮图片与版式被挤乱。用户没做任何要求改行高的动作，也没有任何日志提示。

**复现**：已复现。脚本 d1_merge.py，lock_row_height=False 那一轮：夹具第 3 行手工设 `height=120`，A3 有一格被翻译，输出 `第3行 height = 61.6 (原 120)`。同一脚本 lock=True 时该行保持 120（锁行高模式不动行高），可见差异确实来自 `_auto_adjust_row_heights`。

**修法**：`new_height` 改成 `max(new_height, old_height)` —— 自动行高的职责是「不让译文被截掉」，不是「把行压到刚刚好」。另外把估算基准从常量 `BASE_FONT_SIZE_PT` 换成该行内被改写格的实际最大字号（`styles.font_size(base_index)` 已经在 _process_sheet 里拿得到，顺手记进 row_texts 即可）。

### 低-12 数据验证下拉列表里的中文选项永远不翻译，且扫描风险提示里一个字都没提

`D1` · 置信度 high · new

**位置**：`core/xlsx_patcher.py:1372`、`core/file_scanner.py:125`

**机制**：写入端只遍历 `<sheetData>` 里的单元格，`<dataValidations>` 里 `formula1="甲,乙,丙"` 这种内联清单从来不在处理范围内；引用式清单（formula1 指向某个区域）里的选项因为在单元格里，反而会被翻译，于是同一份文件里两种下拉的行为不一致。file_scanner 的 `risk`（core/file_scanner.py:125）只披露 .xls 兼容转换，`FileItem` 也只有 image_count / shape_text_count / comment_count 三个「数得出来但不翻」的计数，下拉选项既不计数也不提示。

**后果**：外方拿到的双语表，正文全是双语、点开下拉却是一串纯中文，而任务日志与扫描摘要都显示「一切正常」。批注和形状文字至少还在扫描摘要里报了数量、用户知道要自己补；下拉选项连这个知情权都没有。

**复现**：已复现。脚本 d1_fidelity.py：夹具含 `DataValidation(type="list", formula1='"甲,乙,丙"')`，翻译前后 `dv` 快照完全一致（`['D2:D10|"甲,乙,丙"']`），且整个运行的 log_callback 只输出两条「分表已处理」，无任何提示。

**修法**：最小成本先补知情权：在 `_scan_one_excel_file` 里数一遍 `<dataValidation type="list">` 且 formula1 为内联字面量、含 CJK 的条数，加进 `FileItem`（与 comment_count 同一套 None 语义）并在扫描摘要里报出来。真要翻的话属于新功能、要产品拍板：内联清单是逗号分隔的字符串，翻完还得保证不引入逗号、总长不超 255 字符，这两条约束不解决就会写出 Excel 打不开的文件。

---

## 各线的基线、假阳性排除与备注

审查线自己排除掉的假阳性记在这里，**是为了不让下一轮重复查同一处**。

### R1（2026-08-29 审计 14 条高危的回归核验）

**基线与复现脚本**

板块基线：`./.venv/bin/python3 -m pytest tests/test_audit_*.py -q` → **338 passed, 108 subtests passed, 6.83s**（绿）。
全仓门禁复核：`./.venv/bin/python3 -m pytest tests/ -q` → **1664 passed, 249 subtests passed, 29.25s**（绿）。
PDF 单项：`pytest tests/test_audit_pdf_fixes.py tests/test_pdf_page_review.py tests/test_pdf_resume.py -q` → 36 passed。
自写复现脚本（全部只在 scratchpad，未改动仓库任何文件）：
- `r1_h1_h2_h4.py`（高-1/高-2/高-4 过滤与覆盖率）
- `r1_h8.py` + `r1_h8b.py`（高-8 Word 恢复池，6 条确定性用例 + 200 轮随机停止/异常时序）
- `r1_h9.py`（高-9 httpx 未读流 ResponseNotRead）
- `r1_h5_cycle.py`（高-5 新增「上一版」功能的完整循环：跑批→重生成→换回→再重生成→再换回）
- `r1_h7.py`（高-7 TM 乐观并发；DB_PATH 改指临时目录，未触碰用户真实 tm.db）

## 结论：14 条高危逐条核验，无 (b) 漏修 / (c) 修出新问题 / (d) 未修。findings 为空是实测结论，不是没查。

### 逐条状态（全部判定为「真修好了」）

| 条目 | 现在的代码位置 | 核验方式 | 证据 |
|---|---|---|---|
| 高-1 / 高-2 ja↔zh 过滤 | `core/translation_filter.py` `_is_source_script_text` / `_HAN_SHARING_SOURCE_LANGS` / `should_translate`；残留豁免 `RESIDUAL_EXEMPT_TARGET_LANGS` 在 `:519` 生效 | **已复现**（r1_h1_h2_h4.py） | 假名/谚文为硬证据，汉字共享只对 ja/ko 源放行；中→中不再自我循环 |
| 高-3 语言预检 | `core/language_preflight.py:141-149` | 机制确认 | 候选正则已覆盖阿拉伯/希伯来/泰/老挝/缅甸/高棉/埃塞俄比亚/谚文 |
| 高-4 语言证据 | `core/translation_coverage.py` `looks_like_source_text`（注意函数名不是审计原文里的 `is_source_language_text`） | **已复现**（r1_h1_h2_h4.py） | 只有 `_language_evidence(...) is False` **且** `looks_like_target_text(...)` 同时成立才拒收，door/van/el 这类短词不再被判「确定非英文」 |
| 高-5 PDF 单页重生成 | `core/pdf_image_translation.py` `_execute_page_rerun`（事务边界：`page_backup`/`_file_artifact_snapshot`/`_page_counters_snapshot` + `_apply_page_regenerate(transactional=True)` 改名入栈而非删除） | **已复现**（r1_h5_cycle.py）+ 既有回归测试 | 失败路径 `_restore_page_image_stash` + `_restore_page_after_failed_rerun` 整体回滚；装配走 `.{name}.building` + `os.replace`；`_discard_superseded_compressed_pdf` 已挪到状态落定之后 |
| 高-6 Excel 补译公式格 | `core/excel_coverage.py:238`、`core/xlsx_patcher.py:1461-1465`、`core/resume_detection.py`、`core/task_runner.py`、`api/app.py:844` | 机制确认 + 全链路测试 | a235f84 把「一律跳过」改成跟随「公式显示值回填」开关；写入层还留了一道独立兜底（`tests/test_excel_backfill_formula.py` 白盒直测）；生产侧两个 `write_bilingual_workbook` 调用点都显式传参，没人吃到 `=False` 的默认值 |
| 高-7 TM 乐观并发 | `core/tm_manager.py` `_entry_version` / `bulk_update_detailed`；`expire_stale_cleaning_suggestions` 已被 `GET /api/tm/clean/suggestions`（app.py:1257）真正调用 | **已复现**（r1_h7.py） | 过期 `expected_version` → `rows=['stale']`，手工改动 `HAND EDITED` 未被盖掉；刷新版本后重写 → `rows=['updated']` 成功。**不构成「拒绝写入且不给出路」** |
| 高-8 Word 挂死 | `core/word_task_runner.py:2833-3480` `_WordRecoveryPool` | **已复现**（r1_h8.py + r1_h8b.py，200 轮随机） | 延迟提交 `_pending_submits` → 锁外 `_flush_pending_submits`，ABBA 环断开；`_executor_shutdown` 后不再碰 executor；停止时 `attempts_done = _max_attempts` 让 `wait_for_completion` 有第二个出口；票据 `settled` 保证 inflight 记账不漏 |
| 高-9 ResponseNotRead | `engines/openai_engine.py:196-206`（`if response.is_error: response.read()` 后再 `raise_for_status()`）+ `core/api_concurrency_control.py:386-402` | **已复现**（r1_h9.py） | `ResponseNotRead` 是 `RuntimeError` 子类，`getattr(..., default)` 吞不掉，现在两处都真的接住了 |
| 高-10 前端 toast 洪水 / 死循环 | `ui/src/views/workspace.ts`：`scheduleRerunTick`（setTimeout 自排 + 退避封顶 20s + 连败 5 次停）、`waitForRerunSlot`（同款退避，返回 false 让整批收工）、`unmountWorkspace` 里 `stopPdfRerunTicker` | 机制确认（逐行读完三段） | 三条退出条件都在：taskId 不符 / `rerun.active` 转 false / 连败上限；停的那一刻只弹一句话，不再每轮弹 |
| 高-11 Cmd+Q SIGKILL | `src-tauri/src/main.rs` `request_child_termination`（Unix 发 SIGTERM）+ `stop_child` 限时兜底 | 机制确认 + 仓库自带 Rust 回归测试 | `stopping_a_sidecar_lets_it_unwind_instead_of_sigkilling_it`、`a_wedged_sidecar_is_killed_after_the_timeout...`、`the_exit_budget_outlasts_the_whole_sidecar_shutdown_chain`（外层 25s 预算 > 内层 12s，且用测试钉住 `api/task_manager.py` 的 `shutdown(timeout=)` 数值同步） |
| 高-12 macOS 就地更新 | `ui/src/update-toast.ts:270-273`、`openRestartBeforeTaskModal`；`ui/src/update-controller.ts` `consumePendingRestartWarning` | 机制确认 | 误导文案已改成「**下载**期间可以继续用，装好后会提醒你重启」；拦截真的接进了新建任务的两个入口（`workspace.ts:3935`、`library.ts:717`），不是只定义没接线 |
| 高-13 Windows 更新不停 sidecar | `src-tauri/src/main.rs` `SidecarShutdownHook`（挂 Tauri 资源表，靠 `cleanup_before_exit()` 清表触发 `Drop`） | 机制确认 | 顺带盖住 `AppHandle::restart()`（macOS 更新后重启）；`stop_running_sidecar` 用 `take()`，被走两次也只生效一次 |
| 高-14 前端硬编码领域 Prompt | `ui/src/views/settings.ts:210-238` + `renderDomainPromptCard` | **已复现**（上一轮） | 硬编码文案已删，改从 `/api/domains/builtin-prompts` 拉；拉失败沿用上次缓存并只 `console.warn`，不阻断设置页；只有 `promptArea.value !== defaultPrompt` 才落覆盖 |

### 我排除掉的假阳性（都实际追到底了，请勿再让人重查）

1. **「切换任务会让 PDF 重跑计时器丢失」** —— 我一度认为 `startPdfRerunTicker` 开头的 `if (rerunTickers[surface] !== undefined) return;` 会在「任务 A 计时器还没触发时切到任务 B」的窗口里让 B 拿不到计时器。追下去发现 `focusTask` 全仓只有两个调用点（`workspace.ts:635` 的 `adoptExistingTask`、`:4115` 的 `sendTaskStart`），**工作区内不存在「在两个终态任务之间横向切换」这条路**：`adoptExistingTask` 只在挂载时跑（此前 `unmountWorkspace` 已清空计时器槽），`sendTaskStart` 起的是非终态新任务（快照 `rerun.active` 必为 false，本来就不该起计时器）。不可达，撤回。

2. **「换回上一版会让候选图/压缩版产物错位」** —— `_PAGE_VERSION_FIELDS` 含 `candidate_artifacts`，换回时会把旧版的候选图路径一起搬回来，而那些评审候选图文件已被重生成覆盖。但候选图既不进 API 也不进界面，且不参与装配，**没有可观察后果**；压缩版产物名不漂已有测试钉住。不报。

3. **「重生成→换回→再重生成 会攒出孤儿页图」** —— r1_h5_cycle.py 实测五个节点，每一步磁盘上都只有一份 `.previous.*`，`.stash.*` / `.swap.*` 中转名零残留，页记录指着的两个文件都真实存在，输出 PDF 中心像素每一步都跟着换对（white→red→white→blue→white），**模型总调用次数 3 = 重生成次数**（换回确实不计费）。命名机制也确认过：`.previous.` 的点前缀让 `page_NNN*` 与 `page_NNN.*` 两处 glob（`:1253`、`:2552`）都扫不到它，续读/续译不会把上一版当正版捡回来。

4. **「TM 数据库降版会被清空」** —— `_inspect_db` 对 `version > TM_SCHEMA_VERSION` 判 `unusable` → 备份后重建。但 `TM_SCHEMA_VERSION = 3` 追溯到 `5c6960c`（很早的提交），**本次 9.4.0 没有动过它**，不是新引入的风险；且该路径备份文件留在 `backups/tm/`，不属于「拒绝写入且不给出路」。不在本线范围，仅备案。

### 需要主会话知道的上下文（都不是新缺陷，只是账要记着）

- **Windows 的 sidecar 仍然是硬杀。** `request_child_termination` 在非 Unix 直接 `return false`，`stop_child` 于是立刻 `TerminateProcess`。高-13 的「安装器动手前文件句柄已释放」达成了，但 Windows 上退出/更新时**在途的已付费翻译结果照样丢**。这一条已在 `AUDIT_FIX_PLAN` 里登记为「需 sidecar 加 shutdown 端点（新对外 API，立 issue 待拍板）」，按简报要求不重复开单——但它确实还是那条「付费成果被丢弃」的口子，issue #1 别让它沉底。
- **`runPdfBatchRerun` 里 `rerunPdfPage` 抛异常那一支**（`workspace.ts:2226`）弹完 toast 就 `batch.done += 1` 继续下一页，不等槽位。若异常是「请求超时但后端其实已经开跑」，下一页会撞 409。属于既有实现的边角，后果只是多一条错误 toast、这一页没跑，不丢数据不多花钱，我判定够不上开单，记在这里备查。
- **全量模式 + 「公式显示值回填」关闭** 时，`_resolve_source_text`（`xlsx_patcher.py:1610-1626`）返回的是 `"=" + 公式源码`，命中译文就会把 `<f>` 换成静态文本；而补译侧 `excel_coverage` 明确把公式格判 `ignored` 并注明「公式源码不送翻，不白花 API 调用」。两侧口径不一致，但这是 a235f84 **之前**就存在的全量侧老行为，且不在 14 条高危里，我没有越界处理——如果要拉齐口径，是产品拍板的事。

### R2 — 上一轮审计中危 30 条 + 低危 27 条修复抽验

**基线与复现脚本**

未跑全量（按纪律沿用给定基线 1664 passed + 249 subtests）。只跑了与本线相关的定向复现脚本，全部在临时数据目录下执行（每个脚本开头 os.environ["TRANSLATOR_APP_DATA_DIR"]=tempfile.mkdtemp() 并断言 config.APP_DATA_DIR 落在 /var/folders 下，实测输出确认；未读也未写 ~/Library/Application Support/Translator）：
1) /private/tmp/.../scratchpad/perf_shared.py + perf_big.log —— 共享公式让渡耗时随行数的增长关系。
2) /private/tmp/.../scratchpad/keys_repro.py —— keys.json 损坏后 save_key / clear_keys / 维护路径。
3) /private/tmp/.../scratchpad/stop_latency2.py —— 补译复核在上游限流时对停止信号的响应。
读代码核验（未构造运行环境）：中-3 中-8 中-14 中-19 中-29 中-30 低-TM(32766) 低-Excel(让渡失败入任务日志)。

【已核验确实修干净，不报】
- 中-1 keys.json 损坏无出路：keys_repro.py 实测。损坏文件 → save_key 成功（自动备份到 backups/keys/keys_unusable_*.json 后按空表续写并记 recovery event）；再损坏 → maintenance.clear_keys() 成功（force 路径不留含明文 Key 的副本，这个取舍在注释里说清了，是对的）。strict/force、unusable/unreadable 四象限都区分开了，硬约束合规。
- 中-2：core/task_runner.py:1408 与 1771 两处都明确注释「不拦停止信号，已付费结果必须落库」，TM 写入不再被停止旁路。
- 中-3：excel_review_marks 顶部声明一次，1264 行有「不许重绑定」的守卫注释，全部调用点共用同一个 dict。
- 中-4：get_all_entries_for_cleaning 改成先取全部再用 _normalize_word_type 过滤，旧库 'term' / 'import' 归一正确；get_stats 同样按归一后计数。
- 中-5：tm_text 拆成 storage（保留换行）/ compare（折换行，兼容旧库存储形态）两种形态，lookup_batch 三种哈希齐查（raw / storage / compare），新旧数据都命中。
- 中-6：insert_manual_entry_detailed 把 written / unchanged / blocked_pinned / invalid / error 分开回报，不再压成一个布尔。
- 中-8：task_runner finally 里对 self._files 全量兜底扫描删临时件，_cleanup_excel_conversion_temp 可安全重复调用（不存在直接跳过，不刷假告警），process_paths / resume_baseline_used 预声明成空列表防未赋值。
- 中-14：settings.ts 五个 numberField 的 min/max（1–16 / 800–12000 / 1500–30000 / 1–8 / 0–8）与 config.py:216-255 常量逐条对齐，422 夹缝已闭合。
- 中-19：workspace.ts:4409 `fileResults.length > 0 ? produced : stateNotProduced ? 0 : st.selected.size`，PDF 停止零产出不再谎报文件数。
- 中-29：_plan_cell_mutation 去掉了写入端的 should_translate 重判，注释把「复核改判格含中文被误杀」讲透了。
- 中-30：_cell_value_by_ctype 按 ctype 逐类还原（DATE→date/time/datetime、BOOLEAN→bool、ERROR→xlrd.error_text_from_code 文本、认不出的错误码留空不编造），日期/布尔不再变序列号。
- 低-TM sqlite 32766：_SQL_VARIABLE_CHUNK = 900 + _chunked()，bulk_pin_entries / _set_pinned_by_ids / set_all_pinned / lookup_batch / 清洗建议更新五处全部分片；单句最大绑定变量数 901，远低于 3.11 运行时 SQLite 的 999 下限。注：.venv311 存在（Python 3.11.15）但没装 pytest，无法在 3.11 下跑测试，这条按代码路径核验 + 变量数计数确认。
- 低-Excel「让渡失败只写 loguru」：xlsx_patcher.py:1481-1488 已补 log_callback 的 [WARN] 行，并且判定挪到了涂色之前，不会再出现「标了色没译文」。

【排除的假阳性】
- key_origins.json 损坏时被当成空集且在「本地保存」路径上不自愈（settings.py:1918-1922 / 1948-1958），后果是已导入的密钥退回成「自己的」、可被导出。代码注释明确把这条登记为可接受的降级（「最坏结果只是退回没有这份文件时的老行为，而不是让用户存不进 Key」），是有意设计，不报。
- normalize_tm_text_for_compare 把换行折成空格作哈希，会让「甲\n乙」和「甲 乙」撞同一条 TM 记录 —— 这是为兼容旧库存储形态的有意取舍，注释写明，不报。
- 按纪律排除：低-覆盖率 5% CJK 阈值、复制路径、Cmd+Q 25s、ko/ja 四处特判。

【未及验证，留给下一轮】
工具预算用尽，以下中危只做了「代码路径存在对应修复」的粗查，没有逐条构造复现：中-7（引号剥离）、中-9（failover 候选构建失败）、中-10（对象数组 str() 兜底）、中-11/12/13（覆盖率三条）、中-15/16/17/18/20/21/22/23/24（前端与更新/壳，共 9 条）、中-25/26/27（Word 与 PDF 三条）。其中中-21（SSE 终态竞态无限重连）和中-27（PDF 停止烧占位页，直接对应「白花钱」硬约束）我认为最值得下一轮优先补上实测。

### C1 — 并发、线程、锁、竞态、死锁

**基线与复现脚本**

未跑全量（按纪律沿用已给基线 1664 passed + 249 subtests）。本线只跑了三个一次性复现脚本，全部落在 /private/tmp/claude-501/-Users-lijianwei-vibecoding-claude-XL-Translator/cef9c101-5dad-4a16-b030-8f75baf592e2/scratchpad/：

1) c1_group_stress.py —— FairApiGroupScheduler 压力测试（2 个任务 / 11 个线程 / 660 次抢占，含 recovery 类别）。
   输出：elapsed 0.37s，alive(hung): []，peak concurrent 6 == capacity 6，errors 0，counts {'A': 480, 'B': 180}。
   结论：公平 FIFO 本身不挂死、不超发、不漏账。

2) c1_recovery_priority.py —— 对比两个调度器的恢复优先级（capacity 8，8 条 normal 线程满载 4 秒，1 条 recovery 线程）。
   输出：WeightedApiScheduler → recovery 抢到 74 次，中位等待 0.0ms，最大 54.9ms；
        FairApiGroupScheduler（生产实际用的那个）→ recovery 抢到 1 次，等待 4022.5ms（其实是等到我把 normal 停掉才进去）。

3) c1_no_should_stop.py —— 停止后槽位等待是否可中断（capacity 4，4 条 normal 满载）。
   输出：with_should_stop（PDF 那种写法）→ ('cancelled', 0.31)；
        no_should_stop（补译复核 / Word 语义仲裁那种写法）→ 停止后 3 秒仍 STILL BLOCKED，
        直到 normal 流量停掉才 ('acquired', 3.32)——也就是说它最后还是拿到槽位、继续往下发请求。

4) c1_backoff_stop.py —— 停止后限流退避是否可中断（把 capacity 打到最低档后连打 4 次 429）。
   输出：无 should_stop → 1.64s / 3.38s / 9.30s / 13.10s（共 27.4 秒，全程 stop 已置位）；
        有 should_stop → 0.00s / 0.00s / 0.00s / 0.00s。

所有脚本开头都先 os.environ["TRANSLATOR_APP_DATA_DIR"] = tempfile.mkdtemp()（在 import config 之前），
并 assert config.APP_DATA_DIR 落在 /tmp、/var/folders 或 /private 下面，未触碰用户真实数据目录。

**排除掉的假阳性（都实测或读码确认过，不用再看）**

- `_WordRecoveryPool`（core/word_task_runner.py:2831-3300）的锁序修复是扎实的：`_submit_locked` 只记账、`_flush_pending_submits` 出锁后才 submit、`_abandon_unsubmitted_ticket_locked` / `_settle_abandoned_ticket_locked` 两条兜底退账路径都在，`_settle_abandoned_ticket_locked` 里也确实没有在 shutdown 期间回头调 `_schedule_retry_locked`。WT1-M1 / WT1-M2 没有回归。
- `FairApiGroupScheduler` 的 FIFO 本身没问题：c1_group_stress.py 跑 2 任务 / 11 线程 / 660 次抢占，0 挂死、峰值并发严格等于 capacity、结束时 active_total_weight 归零、0 异常。队列条目不会变成陈旧条目（唯一的 `_add_waiter_locked` 在等待线程内，`_remove_waiter_locked` 在 finally 里且必 notify_all）。
- api/task_manager.py 的锁序是单向的：`task.condition → self._lock`（_finish_if_needed → _retire_terminal_task → _evict_retired_tasks）。反向嵌套一处都没有——`list_tasks` / `delete_task_record` / `_evict_retired_tasks` 只拿 self._lock，`begin_shutdown` / `shutdown` / `flush_history` / `mark_active_tasks_interrupted` 都是「先拿 self._lock 抄一份列表、放锁、再逐个拿 task.condition」。没有 ABBA。
- `flush_history` 里 `continue` 跳过 `self._history.upsert(record)`，`record` 不会串到下一轮——看着可疑，实际正确。
- PDF 的三个计数器（`_api_call_count` / `_review_api_call_count` / `_rate_limit_reduction_count`）现在确实都在 `_counter_lock` 下自增（core/pdf_image_translation.py:4700-4712），上一轮那条低危已修干净。`_review_lock` / `_page_status_lock` 下的 `_queue.put` 是无界队列，不会在持锁时阻塞。
- PDF 页生成里图像租约和审核槽位**没有嵌套**：`finally: scheduler.release(lease)`（:4171）在 `review_scheduler.slot(...)`（:4209）之前执行。审核模型与图像模型同组时 `review_scheduler is scheduler`，如果嵌套就是必然自锁——这里是安全的。
- `_begin_page_review` / `_finish_page_review` 在 except / except / else 三条分支上都配平了。
- PDF `_process_prepared_pages` 的 `while not producer_done or futures` 有 `if not futures: break` 出口，停止后生产端不再产出，能正常收敛；`wait(..., timeout=0.2)` 有超时。
- core/connection_pool.py 全是纯函数，无共享状态。

**未及验证的（留给后续或别的线）**

1. `_pump_page_rerun`（api/task_manager.py:1268）在 `self._shutdown.is_set()` 时 break 并放锁，但 `_run_page_rerun` 是 daemon 线程且任务已终态——`begin_shutdown` 的 `if terminal: continue` 会跳过它，所以它不会被通知停止，`shutdown()` 也不等它。sidecar 退出时单页重生成可能正写到一半的输出 PDF。我没去查 `_execute_page_rerun` 的写盘是不是原子（临时文件 + replace），所以没写成发现。值得单独确认。
2. core/model_throughput.py 完全没有锁，`set_model_throughput` 直接改 AppSettings 上的字典，而 `get_model_throughput` 会被 worker 线程读。看着像典型的裸共享状态，但我没构造出具体的错误后果（大概率只是读到旧值），所以没报。
3. `_record_review_api_call()`（core/pdf_image_translation.py:4207）在拿到审核槽位**之前**自增。停止导致 ApiSchedulerAcquireCancelled 时，这次没发生的调用照样计进报告的「审核接口调用次数」。真实但极轻微，不值一条发现，顺手记一下。
4. api/task_manager.py `_retire_terminal_task` 会把 `task.events` 截断到 TERMINAL_EVENT_TAIL。慢速 SSE 消费者会跳过被截掉的事件（id 出现跳跃）。是运行日志的观感问题，不影响终态事件（它永远在尾部），没往上报。
5. 没审 api/app.py 的事件推送侧（SSE 端点本身），只审到 task_manager 的 `_iter_task_sse`。

**成本**：约 55 次工具调用，未跑全量 pytest。

### C2 — 资源生命周期：临时文件、子进程、数据库连接、文件句柄

**基线与复现脚本**

沿用交付的全量基线（1664 passed + 249 subtests，未重跑）。自己跑的板块相关测试：`TRANSLATOR_APP_DATA_DIR="$(mktemp -d)" ./.venv/bin/python3 -m pytest -q tests/test_phase8_maintenance_contracts.py tests/test_word_converter.py tests/test_api_launcher.py` → 19 passed，全绿（说明本次三条发现都落在既有覆盖之外）。所有复现脚本都先把 TRANSLATOR_APP_DATA_DIR 指到 mktemp 目录并断言 config.APP_DATA_DIR 落在 /var/folders 下，未触碰任何用户真实数据目录。脚本留在 /private/tmp/claude-501/-Users-lijianwei-vibecoding-claude-XL-Translator/cef9c101-5dad-4a16-b030-8f75baf592e2/scratchpad/（c2_soffice_timeout.py、c2_timeout2.py、c2_watchdog.py、c2_watchdog2.py、c2_ws.py、c2_word_leak.py、c2_word_leak2.py）。

【实测排除的假阳性，别再重复查】
1. soffice 超时不会留僵尸/孤儿。c2_timeout2.py 用真实 .doc 夹具（.runtime/self-tests/phase-05-word/artifacts/legacy.doc）跑生产同形命令，分别用 0.4s / 0.8s 触发 subprocess.run 的 TimeoutExpired，3 秒后 `pgrep -f LibreOffice.app` 两次都是空。macOS 上 /Applications/LibreOffice.app/Contents/MacOS/soffice 是就地 exec 的，不像 Linux 包装脚本那样另起 soffice.bin，所以 subprocess.run 自带的 kill 足够。注意 `pgrep -f soffice` 会误命中 wpsoffice（本机装了 WPS），排查时必须按 LibreOffice.app 过滤——我第一次就被这个骗了一下。
2. core/xls_converter.py:126-180 的 LibreOffice 路径是干净的：work_dir 在 finally 里 rmtree，失败时 _discard_partial_output 删半成品，profile 目录逐次独立。
3. 上一轮「疑似」的 excel_coverage.py 第二次 load 泄句柄已修（core/excel_coverage.py:61-68 有 try/except BaseException: wb.close(); raise）。
4. task_history 的 stray temp glob 已修，`.{name}.*.tmp` 和 _write_locked 的命名对得上（core/task_history.py:119-129），maintenance._task_history_temp_paths 同步改走 default_history_path()。
5. tm_manager 的连接生命周期没问题：_get_conn 是 contextmanager，finally close；:409 的探测连接也在 finally close；WAL sidecar 文件被 maintenance._tm_paths 通过 db_sidecar_paths 完整列出。
6. 磁盘上限存在且合理：diagnostics 有 _DIAGNOSTIC_MAX_RECORDS=80 / _DIAGNOSTIC_MAX_TOTAL_BYTES=256MB，日志 max_files=5 / max_file_bytes=5MB，任务摘要 retention_limit=200。PDF 页图落在用户自己的输出目录（是续译/复核要用的产物，不是 app 私有缓存），不算无界增长。
7. maintenance.py:362 _remove_owned_path 用模块级 APP_DATA_DIR 做越界校验，而清单里多数路径是晚绑定的（default_history_path()、settings_module.RECOVERY_PATH、tm_manager.DB_PATH）——生产上 APP_DATA_DIR 稳定所以不炸，只在测试单独 patch settings 侧路径时会抛 MaintenanceError。这就是上一轮低危提过的那一条，没恶化，不重复报。

【本次预算内没查完的，供主会话决定要不要另派】
- core/headless_translate.py / headless_word_translate.py / headless_pdf_translate.py 三个 CLI 入口只做了 grep（无 Popen / 无裸 open / 无 tempfile），没有逐行读，也没跑起来验证它们的中断路径。
- PDF 分页工作区在「任务中途停止」时的清理是否完整（core/pdf_image_translation.py 那 4700 行只按 grep 命中点抽读了几段）。
- core/excel_automation.py 里 xlwings App 的进程生命周期（Excel 自动化只在装了 Excel 的机器上才走得到，本机没构造出环境）。
- Windows 独有路径（COM、pythoncom.CoUninitialize、文件占用导致 unlink 失败）全部没验证，本机是 macOS。
- word_converter.py:325 那个 Popen 用了 stdout/stderr=PIPE 却从不读取：理论上 soffice 输出超过管道缓冲（64KB）会把子进程写阻塞住，但 headless soffice 正常几乎不输出，我判断站不住，没写进 findings。
- core/task_resources.py 是 API 并发调度（连接位/租约），不涉及文件句柄或子进程，与本审查线主题不符，只快速通读未深挖。

### P1 — 持久化 / schema 迁移 / 旧数据兼容

**基线与复现脚本**

`./.venv/bin/python3 -m pytest tests/test_data_schema_recovery.py tests/test_settings_persistence.py tests/test_settings_concurrent_updates.py tests/test_settings_api_keys.py tests/test_config_crypto.py tests/test_task_history.py tests/test_api_key_export_origins.py tests/test_model_config_round_trip.py tests/test_document_config_bundle.py tests/test_audit_config_maint_fixes.py -q` → **149 passed + 2 subtests，0 failed**（2.26s）。审查全程只读，结束时 `git status --porcelain` 为空。所有脚本写在 /private/tmp/claude-501/-Users-lijianwei-vibecoding-claude-XL-Translator/cef9c101-5dad-4a16-b030-8f75baf592e2/scratchpad/p1_*.py，均通过 TRANSLATOR_APP_DATA_DIR 指向 tempfile 临时目录，没碰用户真实数据目录。

**排除掉的假阳性（都实测过，不构成发现）：**

1. **旧版本 settings.json 升级** —— 从 v8.1.0 / v9.1.0 / v9.2.0 / v9.3.0 四个 tag 用 `git archive` 导出到临时目录、各自生成当时的默认 settings.json，再用当前构建读（`p1_old_load.py`）：四份全部 `state=current`、load 正常、save 后无备份产生。唯一丢的字段是 `auto_pin_after_clean`、`output.enable_print_guard`、`output.enable_task_log`、`pdf_output.retain_page_materials`、`appearance.model_config_panel_open`、两处 `mark_colors.semantic`——grep 确认这些功能已整体移除、没有改名后的接收方，属于有意删除。

2. **「用户真的改过东西」的整份 round-trip** —— `p1_roundtrip.py` 把默认 settings 的所有布尔全部翻转后写盘再读回保存，被校验器改回的字段数为 **0**，没有静默重置。

3. **`SETTINGS_SCHEMA_VERSION` 从 v9.2.0 起一直是 26** —— 意味着 `adopted` 分支在现实里基本走不到，但这不构成缺陷（加法改动本来就靠 pydantic 默认值补齐，实测通过）。附带一点供主会话知晓：`AppSettings` 没设 `extra=`，pydantic 默认 `ignore`，所以版本号冻结 + 忽略未知字段 = 如果用户从新版回滚到旧版再升回来，新版独有的字段会被旧版静默抹掉。当前没有回滚路径，我没把它写成发现。

4. **`engine.connections[0]` 的编辑会被 `cloud_model` 覆盖回去** —— 我一开始误判为数据丢失，追到 `_sync_connection_pool`（settings.py:308-320）确认是「entry 0 就是旧版单连接字段的镜像」的有意设计，注释写得很清楚。第三条发现已改用非首位连接复现，避开了这个设计。

5. **`_seed_packaged_default_api_key` 在 `load_settings` 里可能把删掉的 Key 种回来** —— 查了 `DEFAULT_CUSTOM_OPENAI_API_KEY = \"\"`（config.py:47），且 scripts/packaging/.github 里都没有注入它的地方，函数实际是 no-op，不成立。

6. **TM 迁移** —— `_inspect_db` / `_recreate_db` / `discard_database` 的 unusable / unreadable / busy 三分法、备份前置、维护页 `clear_tm` 的兜底删除，逐条读过，没找到破口；`_backfill_source_hashes` 与 `_ensure_hash_index` 的顺序依赖和降级为普通索引的处理也是对的。既有 tests/test_data_schema_recovery.py 对 TM 侧的覆盖相当密。

7. **config_crypto / _embedded_key** —— 上一轮已核验通过（AAD 对称、五种解封状态），本轮抽查未见新问题，没有重复投入。

**主会话需要知道的上下文：**

- 第 1 条和第 2 条是**同一个 bug 的两处**（`read_text(encoding=\"utf-8\")` 外面只 catch `OSError`），修的时候两处一起改，只改 settings 那边会留下 keys 这一半。
- 第 4 条和第 1 条共享 `_recreate_settings_file` / `_write_text_atomic` 这条路径，建议归到同一个修复集群，避免两个代理撞车。
- 第 3 条如果按我推荐的「收窄写盘窗口」修，动的是 api/app.py 的连接测试端点；如果按「delta 支持列表主键」修，动的是 settings.py 的 `_settings_delta`/`_apply_settings_delta`，后者会影响全部并发写路径，需要补测试覆盖 connections 之外的其它列表字段（`custom_target_langs` 等）。
- 现有 tests/test_settings_concurrent_updates.py 的 10 个并发用例全部是标量与嵌套 dict，**列表元素维度零覆盖**；tests/test_data_schema_recovery.py 的损坏样本全是合法 UTF-8，**编码维度零覆盖**。这两处是本轮四条主要发现能存活到 V9.4.0 的原因。

### E1 — 错误处理、异常吞没、用户可见错误文案

**基线与复现脚本**

未重跑全量（按指示信任已给基线：1664 passed + 249 subtests，2026-08-31 实测全绿）。本线只跑了两个针对性一次性复现脚本（见下），未跑既有 pytest 套件文件，因为两条发现都落在既有测试覆盖之外（grep 确认无测试命中 `_extract_chat_completion_text` / ollama 重试路径）。

排除/未深挖的点：(1) claude_engine.py:102 `_extract_claude_text` 的 `first_block.get(\"text\")` 非字符串时同样转空串，理论上和 openai_engine 是同一类缺陷，但当前请求体没有开 extended thinking（没传 thinking 参数），正常情况下 content[0] 应该就是 text 块，没能构造出真实触发场景，置信度不够没有单独立项，只是提醒后续如果这个应用支持切换到带 thinking 的 Claude 模型要重新看这里。(2) api/task_manager.py `_pump_page_rerun`（1298-1299 行）把 pump 循环自身崩溃时的 `str(exc)` 直接写进 SSE 事件 `pdf_page_rerun` 的 message 字段、未过 humanize_error，看起来像是「重生成留上一版」新功能里复刻了此前 `_pump_runner` 那条已修问题的同款反模式；但实测追了一遍前端（ui/src/views/workspace.ts:1773 及 dist 里的构建产物）确认这个 SSE 事件类型目前没有任何前端代码消费，用户看到的重跑失败文案走的是另一条已经用了 user_facing_reason 的字段（`snapshot.rerun.error`，来自 core/pdf_image_translation.py:1757-1764，处理得很规范）。所以这是一条真实存在但当前不会被用户看到的死代码/技术债，没有计入 findings（不满足「用户会看到什么错误信息」的门槛），仅供参考，以后要是给这个 SSE 事件接上前端消费者，记得先把这两行改成走 user_facing_reason。(3) api/app.py 里大量 `except Exception as exc: raise HTTPException(422, str(exc))` 通用模式（PUT /api/settings 等）会把 pydantic ValidationError 的英文原文透传给前端，这正是上一轮审计「中-14」已经报过、已排进 B2 修复计划（ui/src/views/settings.ts + api/app.py）的问题，本轮没有重复上报。(4) core/user_facing_errors.py 的 timeout/502/504 规则顺序确实会让网关超时被「等接口响应超时了」这条更早命中，但文件里 130-137 行的注释已经明确讨论过这个取舍并给出理由（并非疏漏），不重复上报。(5) core/connectivity_check.py 里「测试连接」失败时把原始 HTTP 状态码+响应体（截断 300 字符）直接放进 message，判断这是连接测试这个诊断工具本身故意保留的技术细节（给会看日志的人用），未见明显误导，未列入 findings。工具调用数约 48 次，在预算内完成，未出现被打断的情况。

### A1 — HTTP API 层（api/app.py、api/task_manager.py、api/launcher.py）

**基线与复现脚本**

未跑全量（按指示沿用已给基线 1664 passed + 249 subtests）。本轮全部为只读审查 + 一次性复现脚本，全部在临时数据目录下运行：每个脚本第一行 `os.environ["TRANSLATOR_APP_DATA_DIR"]=tempfile.mkdtemp()`（在 import config 之前），并 assert `config.APP_DATA_DIR` 落在 /var/folders 下，实测输出确认（例：/var/folders/y6/.../T/a1data_st0dl67p）。未读、未写用户真实数据目录。脚本留在 scratchpad：a1_boot.py / a1_routes.py / a1_fuzz.py / a1_logs.py / a1_listtasks.py / a1_sanitize.py / a1_scale.py / a1_cors401.py。

**排除掉的假阳性（都实测过，不要再查）：**

1. *「pausing 状态在 sidecar 重启后不会被扫成 interrupted」* —— 看起来像 高-10 toast 洪水的复活（`core/task_history.py:93` 的 active_states 集合里确实没有 "pausing"），但 grep 全仓确认 **"pausing" 是纯前端状态**，后端 task_manager 从不写它。后端实际会持久化的状态只有 running / stopping / paused / error / interrupted / done / completed_with_issues / stopped，全部被 active_states 或终态覆盖。无缺口。

2. *事件循环阻塞（板块 5）* —— api/app.py 里**没有一个 `async def` 路由**（只有 lifespan、鉴权中间件和 6 个异常处理器是 async，全都不做同步 IO）。所有端点都是同步 `def`，FastAPI 自动丢线程池。SSE 也是同步 generator 走 `iterate_in_threadpool`。不存在「大文件翻译时 API 假死」。并发 SSE 流最多 4 条（surface_busy 保证每种 surface 同时只有一个活动任务），离 anyio 默认 40 线程上限很远。

3. *路径穿越* —— `/api/tasks/{id}/pdf-pages/image` 的 `file` 参数不落到文件系统拼接上：`resolve_page_image_path`（core/pdf_image_translation.py:1577）先 `_find_prepared_file(relative_path)` 在任务已扫描的文件清单里查表，查不到返回 None → 404；kind 白名单三选一，page 有上下界。诊断包路由 `/api/diagnostics/{record_id}.zip` 实测 `..%2F..%2Fetc%2Fpasswd.zip` 返回 404 Not Found（路由都没匹配上）。

4. *绑定与鉴权* —— launcher.py 绑 `("127.0.0.1", 0)` 随机端口，端口和 32 字节 token 一起从 stdout 交给 Tauri；token 用 `secrets.compare_digest` 比对，避免时序泄漏。CORS 白名单只有 tauri://localhost / http://tauri.localhost，dev origin 靠 `TRANSLATOR_DEV_ORIGIN` 环境变量且必须以 `http://127.0.0.1:` 或 `http://localhost:` 开头——发布构建不设这个变量，开发口子没有漏进去。

5. *端点健壮性* —— 对 33 个 GET（含空 lang_pair、`page=-5&page_size=100000`、`lang_pair=../../etc`、未知 surface / role / 不存在的 task_id / 诊断 id）做了一轮 fuzz（a1_fuzz.py），**零 500**，边界值都被 clamp 或返回带中文 detail 的 404/422。非法 task_id 上的 stop/pause/resume/end-paused/delete 一律干净 404（a1_boot.py）。

6. *中-21（SSE 终态重连风暴）* —— 已修干净：api-client.ts:277 那条「干净 200 但没见终态事件」的分支现在也走 `attempt >= 7` 封顶，不再绕过重试上限。

7. *启动残留* —— `TaskHistoryStore.mark_active_interrupted()` 在 TranslationTaskManager.__init__（:256）里就跑，重启后遗留的 running/paused/stopping 记录会被改成 interrupted + terminal=true，前端不会去 watch 它们。`task_status` / `task_results` 在内存任务被驱逐后都回落到历史记录（:786-797、:897-906），不会因为 MAX_RETAINED_TERMINAL_TASKS=12 的驱逐而 404。

**没查完的（留给后续或其他线）：**
- 板块 6 的「响应体 vs 前端类型契约」只对了 TaskStatus / TaskList / PdfPagesSnapshot 三组，**没有**逐字段核对 settings / model-roles / maintenance / diagnostics 这几组更大的报文与 ui/src 里对应的 interface。上一轮报的 `TaskStatus.result` 那条现在已在 api-client.ts:20 用注释显式改成可选，属于已修。
- 板块 3 的并发只做了代码路径确认（`_start_prepared` 用 `expected_revision` 做乐观并发，双发 POST /api/tasks 会被 reserve_task 的 revision 校验拦成 409 stale），**没有**真起两个线程压测。
- `list_tasks()` 在持有全局 `self._lock` 的情况下对每个活动任务做全量 logs 拷贝+脱敏（8000 行实测 33 ms），会和 start/stop/pause 抢同一把锁——这是发现 1 的同一个根因，修了日志上限就一起消失，所以没有单列。

### G1 — 模型引擎 / 故障转移 / 调度 / Token 成本

**基线与复现脚本**

未跑全量（按纪律沿用主会话基线 1664 passed + 249 subtests）。本线全部结论走独立复现脚本，均在 `TRANSLATOR_APP_DATA_DIR` 指向 mktemp 的隔离环境下执行（每个脚本开头断言 config.APP_DATA_DIR 落在 /var/folders，实测输出 `/var/folders/y6/.../tmpzu7bu3b7`），未读也未写用户真实数据目录。脚本位于 /private/tmp/claude-501/-Users-lijianwei-vibecoding-claude-XL-Translator/cef9c101-5dad-4a16-b030-8f75baf592e2/scratchpad/：g1_probe.py（429 分类矩阵，7 种响应体全部正确识别，无发现）、g1_repro.py（Claude max_tokens / 调度器容量 / SSE 解析）、g1_amplify.py（重试放大计数）、g1_exhausted.py（连接链耗尽后行为）、g1_429split.py（429 下的批次二分计数）、g1_lostwork.py（致命错误丢弃已付费结果）、g1_prompt.py（系统提示词开销实测）。

**排除掉的假阳性（都实测证伪过，不要再查）：**
1. 「裸 429 因为响应体没有关键词而被漏判成普通错误」——不成立。g1_probe.py 跑了 7 种 429 响应体（空 body、OpenAI code-only、智谱中文并发限制、通用中文流量提示、DashScope Throttling.RateQuota、空 JSON、HTML 网关页），`is_api_concurrency_limit_error` 全部返回 True。原因是 httpx 的 `raise_for_status()` 异常文案本身就带 "Too Many Requests"，正好命中 `\btoo\s+many\s+requests\b`。唯一漏判的是「自定义异常只带 status_code=429、文案里无任何关键词」，这在当前五个引擎里不存在（都走 httpx）。
2. 「同一文档内重复文本没去重、被翻译两次」——不成立。task_runner.py:718/1034/1196 用 `global_unique_texts: set[str]` 做了跨文件全局去重，日志也明说「相同内容只翻一次」。
3. 「二分时前半批已成功的结果丢失」——不成立。engine_dispatcher.py:758-771 的 left/right 是 `{**left, **right}` 合并，成功的一半会保留。
4. 「解析层把 dict 用 str() 写进单元格 / null 转空串」——上一轮已修干净，base_engine.py:99-118 现在对 dict/list/None 一律抛 ValueError，实测行为正确。
5. 「候选连接构建失败牵连整个 endpoint」（上一轮中-9）——已修干净，failover_engine.py:196-214 改成了循环跳过单个候选，不再牵连同网关其他连接。
6. 「FailoverTranslationEngine 的 exhausted 状态跨任务共享」——不成立。build_role_engine 每个任务各建一个实例，`_exhausted` 是实例级的。

**未及验证、留给下一轮的（预算到了）：**
- `FairApiGroupScheduler._can_acquire_locked` 的 FIFO 队头阻塞：队头任务的 weight 装不下时，后面权重更小、本可放行的任务也一并卡住，直到有 lease 释放。看代码是成立的，但没构造多线程场景压出来，不敢按已复现报。
- `core/model_throughput.py`、`core/model_api_identity.py`、`core/model_roles.py`（1011 行）只做了 grep 级扫描，没有逐段读。
- `_estimate_api_request_weight` 的权重换算（4000 字符 = 1 槽）与真实 provider 限额之间是否有量纲错配，没有验证。
- OpenAI Chat Completions 路径不校验 `finish_reason == "length"`：截断响应会伪装成解析失败并触发 15 次二分，机制与第 6 条同型，但因为 payload 没设 max_tokens、由服务端默认值决定，我没能构造出确定的触发条件，故未单列。

**跑批建议：** 第 1、2、3 条互相咬合（耗尽不熔断 → 二分放大 → 宽限到点丢弃已付费结果），修的时候建议放同一个集群、一次改完再验，分开改容易出现「熔断加了但丢弃路径还在」这种半吊子状态。

### T1 — 翻译质量链路：过滤、覆盖率、残留、语言识别、续译

**基线与复现脚本**

未跑全量（按指示复用给定基线 1664 passed）。只跑本板块直接相关的测试文件：`./.venv/bin/python3 -m pytest -q tests/test_resume_detection.py tests/test_excel_resume.py tests/test_word_resume.py tests/test_audit_filter_fixes.py tests/test_residual_pipeline.py tests/test_language_preflight.py tests/test_coverage_arbitration.py` → **154 passed, 89 subtests passed in 1.09s**（全绿，本报告 4 条发现全部落在既有覆盖之外）。

自建复现脚本（均先 `os.environ["TRANSLATOR_APP_DATA_DIR"]=tempfile.mkdtemp()` 再 import config，并断言 `config.APP_DATA_DIR` 落在 /var/folders 下；全程未读写用户真实数据目录）：
- /private/tmp/claude-501/-Users-lijianwei-vibecoding-claude-XL-Translator/cef9c101-5dad-4a16-b030-8f75baf592e2/scratchpad/t1_ja_sourcelang.py
- .../t1_excel_resume.py
- .../t1_custom_lang.py
- .../t1_filter_matrix.py
- .../t1_url_zh.py

**排除掉的假阳性 / 查过没问题的：**
1. `coverage_arbitration` 的批次对齐——id 用 `str(index)`、每批 `allowed` 白名单过滤越界 id、缺项按 uncertain，仲裁结果直接改 `unit.status` 且写入端读的是同一批 CoverageUnit 对象（内存同一引用，不存在上一轮中-29 那种 key 对不上）。没有发现。
2. 仲裁「限流抖动 → 整批 uncertain → 全部重译」是**设计上的取舍**（coverage_review.py 注释明写「宁可多翻，不可漏翻」，且 uncertain 数单独上报），不当发现报。
3. `RESIDUAL_EXEMPT_TARGET_LANGS` 缺 `ko`：现代韩文译文不写「2026年8月9日」，构造不出误判样本，不报。
4. Excel/Word 产物恒为双语（`bilingual_output_name`，无替换模式），所以「双语/替换模式切换」这条边界在当前产品形态下不存在。
5. 续译换语言：Excel/Word 靠文件名里的语言片段天然错开；PDF 有 `_pdf_manifest_lang_matches` 语言闸。核对过，没问题。
6. 续译目录发现：`_glob_output_dirs` 用 iterdir 字面量前缀比对（不受 `[ ] * ?` 影响），`file_scanner.py:166` 会把带 `_翻译输出_` 的路径整条排除，不会把上次产物当新源文件重扫。没问题。
7. 高-4 说的「ignored 类不进报告」现已修好（`format_ignored_coverage_report`，tests/test_audit_ignored_coverage_log.py 覆盖）——这也是发现 1 严重度没标到「完全静默」的原因：用户至少能在任务日志里看到一行「N 格未补译」。
8. `language_preflight` 的候选正则确实按高-3 补全了阿拉伯/希伯来/泰/老挝/缅甸/高棉/埃塞俄比亚/谚文。仍缺亚美尼亚(0530-058F)、格鲁吉亚(10A0-10FF)、藏文(0F00-0FFF)、蒙文传统字(1800-18AF)——但这几种都不在 `SUPPORTED_SOURCE_LANGS` 里，构造不出可达路径，不报。

**未及验证（预算到线，留给下一轮）：**
- `residual_repair.py` / `residual_replay.py` 两个模块只做了接口层扫读，没构造样本实跑；`mixed_language.py`（899 行）完全没查。
- 续译的 PDF 分支（`_classify_pdf` 的体积闸 + `reusable_pdf_pages` 计数口径）只读了代码，没造 manifest 实跑。
- 发现 2 的反向核查我只在 Excel 上实测；Word 侧（`word_task_runner.py:876`）是同构代码，按机制推定同样中招，未单独构造 .docx 复现。
- `unit_ledger.py`（200 行）没读，问询要点 6 里「同一判断写了几遍」只查到 translation_filter vs excel_coverage 这一处不一致（已写进发现 1）。

### M1 — 翻译记忆库（TM）

**基线与复现脚本**

未跑全量（按要求沿用给定基线 1664 passed）。只按需读取并对照了 tests/test_tm_cleaner_failures.py（2 个用例，只断言「批次失败不报 completed」，不涉及本次发现）。所有复现脚本均先 `os.environ["TRANSLATOR_APP_DATA_DIR"]=tempfile.mkdtemp()` 再 import config，并断言 `config.APP_DATA_DIR` 落在 /var/folders 下，全程未触碰用户真实数据目录。脚本落在 /private/tmp/claude-501/-Users-lijianwei-vibecoding-claude-XL-Translator/cef9c101-5dad-4a16-b030-8f75baf592e2/scratchpad/：m1_scale.py（20 万条压测）、m1_clean.py（清洗规则反例）、m1_apply.py（建议写回并发校验）、m1_conc.py（并发写 + 批量固定 + 导入）、m1_lock.py（10 万条导入与实时写入争锁）、m1_page.py（分页稳定性）、m1_batchfail.py（批次失败丢建议）。

**排除掉的假阳性（都实测过，不要再查）**

1. 规模性能全部合格，索引够用。20 万条库（scratchpad/m1_scale.py）：count 5ms、get_stats 37ms、search 第 1 页 19ms / 第 2000 页 49ms、关键词搜索 35ms、lookup_batch 200 条 2ms、导出 20 万条 161ms、set_all_pinned 全库 501ms、delete_unpinned 11ms。`search_entries` 的 `ORDER BY updated_at DESC` 虽然走 SCAN + TEMP B-TREE，绝对耗时仍在几十毫秒量级，不值得加索引。
2. 分页在 updated_at 全部相同（时间戳只有秒精度，批量入库必然大量并列）时依然稳定：1000 条翻 20 页，seen=1000 / unique=1000 / missing=0（m1_page.py）。
3. 并发没有 database is locked。翻译任务写入 + 用户全库固定/解固 + 8000 条导入三线程并发跑完 0.86s 无异常（m1_conc.py）；10 万条 `import_entries`（单事务）只占锁 2.6s，期间实时 `insert_batch` 10 轮全部成功，远在 busy_timeout=5000ms 之内（m1_lock.py）。WAL + busy_timeout 的配法是对的。
4. sqlite 变量上限：`core/tm_manager.py` 里全部 4 处动态 `IN (...)` 都已按 `_SQL_VARIABLE_CHUNK = 900` 分片（lookup_batch:1122、_set_pinned_by_ids:1783、bulk_pin_entries:1798、mark_cleaning_suggestions:2069），900 < 999，3.11 老运行时也安全。上一轮的低危已修干净。
5. 停止（cancel_event）路径**不丢**已付费建议：取消后停止投新批次、在飞批次跑完、仍会走到 `persist_cleaning_suggestions`。这一条我本来怀疑是硬约束违规，实测是对的。真正丢结果的是「批次失败」那条路（发现 1）。
6. 中-4（旧库 term 被排除）、中-5（入库端换行）、中-6（pinned 冲突谎报已保存）、中-7（引号剥离咬正文）在各自原始位置都已修好并实测有效；复核面板默认全不勾（library.ts:856），高-7 的「默认全勾 + 无版本校验」组合已不成立。批次 3 的三件套（常驻待复核提示、stale_count 窗口口径、逐行去向 outcomes）逻辑核对无误，`count_stale_suggestions_in_review_window` 的两个绑定参数顺序也是对的。
7. `delete_entries` 每条 id 开一个新连接（tm_manager.py:1568），5000 条实测 2.5s，且非原子（中途失败留半个删除）。慢得有限、界面单页选中最多 50 条，没到值得报的程度，仅记在此。

**没来得及验的（留给下一轮或主会话决定）**

- TM 导出的文件格式（xlsx/csv 写出端）对多行译文的处理没查，只验到 `get_all_entries_for_export` / `get_full_export` 这一层的数据是对的。
- 完整备份还原（api/app.py:1150-1233）是先 `save_settings` 保存自定义语言、再按语言对逐个 `import_entries`，中途某个语言对失败会留下「设置已改、词条只还原一半」的中间态，且没有还原前快照。没构造用例证死，也没算准这算不算硬约束覆盖范围内，故未成条。
- `_upsert_entry` 走哈希兜底命中旧库单行写法时，只更新 `source_hash` 不更新 `source_text`（tm_manager.py:900-918），库里会长期留着单行原文配多行译文的行。看着是有意的（改 source_text 会撞 UNIQUE），未视为缺陷。
- `run_cleaning` 产出的建议在 `list_cleaning_suggestions` 里一次性全量返回、不分页；20 万条库一次清洗可能产出上万条建议，一个 JSON 全推给前端渲染成弹窗列表。有卡顿嫌疑但没实测前端渲染，不敢定性。

### F1 — 前端工作区、任务中心、客户端

**基线与复现脚本**

按纪律未跑全量 pytest（基线已给：1664 passed + 249 subtests）。本线所有验证在浏览器实测 + 静态定位完成：
1) 起了一个隔离的静态服务器（/private/tmp/.../scratchpad，端口 8931，测完已 pkill）跑两份自写页面，全程未触碰 ~/Library/Application Support/Translator，未起 sidecar、未写任何仓库文件。
2) repro_detail_scroll.html —— 验证「同一个元素 innerHTML='' 后同步重填」是否丢滚动位置。实测输出 {"scrollTop_before_rebuild":600,"scrollTop_after_rebuild":600}，**滚动位置不丢**。据此推翻了我最初对 tasks.ts renderDetail 的假设，未上报（记录在 notes）。
3) bench_rerender.html —— 按 buildPdfReviewCard / buildLogCard 的真实节点结构（每页 1 tr + 4 td + chip + 2~3 个带 click 监听的 linklike；日志 200 行 × 3 节点）测整屏拆建 + 强制布局的耗时，Chrome 实测：
   100 页 + 200 日志 = 6.9 ms/次；300 页 + 200 日志 = 16.0 ms/次；800 页 + 200 日志 = 38.0 ms/次；0 页 + 200 日志 + 30 文件行 = 2.1 ms/次。
   （WKWebView 只会更慢，且真实代码还多出 createChip/icon 的 SVG use、captureLogScroll 的逐行 getBoundingClientRect、updateTopbar 与整个右栏控件。）

**被我自己推翻、明确不上报的假设（避免下一轮重复挖）**
- tasks.ts:1315 `detailRootEl.innerHTML = \"\"` + 整块重建，由 4 秒前台轮询（tasks.ts:1665）和 12 秒后台巡检（tasks.ts:959）无条件触发 touch()。我最初判定「用户翻到的任务详情每 4 秒被打回顶部」。**实测证伪**：同一个元素清空后在同一个同步块里重填、期间没有强制布局读取，浏览器不会把 scrollTop 夹到 0（实测 600 → 600）。renderDetail 全程无 getBoundingClientRect，故不触发夹取。已放弃。
  - 残留的小问题（未验证，留给后续）：整块重建会让用户在详情面板里选中的文本（比如「产物文件」表里那一列没有复制入口的错误原文）每 4 秒被清掉一次。若要报，需要先实测 Selection 在节点被移除后的行为。

**看过、判定不构成发现的点**
- 定时器/监听器生命周期整体是干净的：fastPollTimer（mount/unmount 配对）、silenceTickers、rerunTickers（unmountWorkspace 里显式 stop，且注释挂着高-10 的来由）、components.ts 的 popover/hint 都成对解绑；router.navigate 保证 unmount 一定先于下一次 mount，没有重复启动同一循环的第二入口。ensureBackgroundLoop 的 12 秒 interval 是刻意常驻（徽标要跨视图准确），不是泄漏。
- 异步竞态防护到位：runScan 有 scanTokens 单调递增丢弃旧结果（含 catch 和 finally 两处判 token）；preflightAndSubmit / submitTaskStart 各自守 submittingSurfaces 防连点；watchTask / refetchTask / fetchPdfPagesSnapshot 都在 await 后复查 task_id。
- XSS 面：全仓 innerHTML 仅 3 处非空赋值，均为静态字面量（icons.ts:163 内置 sprite、settings.ts:3158 表头、markdown.ts 明确禁用 innerHTML）。V9.4.0 新增的续译横幅/弹窗、上一版对比弹窗、逐页审核表全部走 createElement + textContent。**上一轮「无 XSS 面」的结论现在仍然成立。**
- workspace.ts:4744 与 tasks.ts:1009 的 window.confirm（结束暂停）是带注释的刻意选择（「与 main.ts / tasks.ts 一致」），按纪律不报。

**预算内没查完的（下一轮可接手）**
1. runPdfBatchRerun（workspace.ts:2198）：单页 rerunPdfPage 抛错时走 catch 弹 toast，随后**跳过 waitForRerunSlot 直接发下一页**，且 batch.done 照常 +1。若那次失败是超时/网络抖动而后端其实已经开跑，下一页会撞 409。需要构造后端故障注入才能确认，未做。
2. tasks.ts watchTask 的重连上限：streamTask 内部 7 次退避后抛出，catch 里若 getTask 成功且非终态就 setTimeout(0) 重新 watchTask，等于开启新一轮 7 次——「事件流一直 404 但任务详情接口一直正常」这种状态下是无上限的循环重连。需要造后端故障才能验，未做。
3. api-client.saveBinaryDownload（api-client.ts:194）直接把服务端 Content-Disposition 里的 filename 当原生保存框的 defaultPath。本地 sidecar 可信，判为不值得报，但如果将来允许接远端服务需要复查。
4. 大列表：记忆库（library.ts）上万条的渲染路径本线未覆盖（不在 F1 文件清单里）。

### F2 — 前端设置页、记忆库页、更新流程、样式

**基线与复现脚本**

未重跑全量（按指示复用既定基线：pytest 1664 passed + 249 subtests，ruff/tsc/cargo clippy 干净）。本线是纯前端 TS 审查，未执行任何测试命令；核对手段为静态代码读取 + 跨文件比对（settings.ts vs config.py/settings.py 的字段范围、update-controller.ts 状态机逐分支读取、markdown.ts 全文读取确认无 innerHTML/无链接渲染）。

本线大量既有前端逻辑（settings.ts 五个数值字段区间、领域 Prompt 拉取回退、update-controller.ts 的下载/安装/签名/磁盘满/lastCheckOk 状态机、markdown.ts 的发布说明渲染、settings.css/workspace.css/tokens.css 的暗色 token）在读码核对后确认与上一轮审计（CODE_AUDIT_2026-08-29 中-14/中-17/中-22/高-12/高-14）记录的修复状态一致，未发现新问题或回归，因此未逐条重复列为 finding：(1) settings.ts 里 word_batch 四个字段 + pdf.page_retry_attempts + pdf.page_generation_concurrency 的 min/max 已与 config.py 的 WORD_BATCH_*/PDF_PAGE_RETRY_ATTEMPTS_*/PDF_PAGE_CONCURRENCY_SAFETY_CAP 逐项核对一致（settings.ts:2598-2630 vs config.py:216-257）。(2) update-controller.ts 的 runUpdateCheck()/startInstall() 对“请求异常”与“200+status:error”两种失败分开记录 lastCheckOk，disk-full/signature/permission/network 四类诊断码分流清楚，失败文案统一带“当前版本没有被改动”，未发现中-22 类回归。(3) markdown.ts 全文读取确认发布说明渲染器完全不解析/不生成 `<a>`，[text](url) 语法只保留纯文本、丢弃链接地址，不存在 href 注入面，也没有任何 innerHTML 使用，维持“无 XSS 面”结论。(4) library.ts 的清洗建议“三件套”（常驻待复核提示、面板顶部失效汇总 staleCount、写入后逐行去向 outcomes）在零条/全部失效/部分失败三种边界下都有对应文案与 chip 展示，未发现新问题。(5) settings.css / workspace.css 未见硬编码颜色；app.css 里少量字面量色值（#fff、渐变、rgb(0 0 0/…) 阴影）都是画在纯色强调背景上的白字/白色圆点，或已经按 :root[data-theme="dark"] 显式覆盖过的渐变终止色，与 tokens.css 的 --surface 暗色值逐一核对数值一致，未发现暗色下看不清的组合。以下方向本线因预算已用去约 45 次工具调用、未及深入，供后续补查：settings.ts 里模型角色（cloud/local）切换与厂商预设联动的全部分支（只抽查了数值字段，没有走完整个角色矩阵）；library.ts 大数据量（万级）渲染路径的实测性能（本线与之前的 F1 都未覆盖，08-31 审计文档明确记了这一空档）；quickstart.ts、help.ts、model-pill.ts、dev-tauri-shim.ts 四个小文件只做了体量确认（89~186 行），未逐行核对。

### D1 — Excel 翻译管线

**基线与复现脚本**

未跑全量（按指令复用给定基线 1664 passed + 249 subtests）。只跑了本板块相关文件：`./.venv/bin/python3 -m pytest -q tests/test_xlsx_patcher.py tests/test_audit_excel_fixes.py tests/test_excel_coverage.py` → 62 passed, 4 subtests passed in 0.69s（全绿）。所有复现脚本都先 `os.environ["TRANSLATOR_APP_DATA_DIR"]=tempfile.mkdtemp()` 再 import，并断言 `app_paths.get_app_data_dir()` 落在 /var/folders 下（实测输出 `/var/folders/y6/.../tmp8lbqsfkl`），全程未读写用户真实数据目录。

复现脚本都在 /private/tmp/claude-501/-Users-lijianwei-vibecoding-claude-XL-Translator/cef9c101-5dad-4a16-b030-8f75baf592e2/scratchpad/ 下：d1_perf_shared.py（共享公式 O(n²) 计时 + 夹具生成器）、d1_prof.py（cProfile 定位）、d1_waste.py（回填关闭时的白花钱）、d1_merge.py（合并格排版 + 锁行高触底 + 手工行高被压）、d1_fidelity.py（写回保真度全项对照）。未修改仓库任何文件，未 git add/commit。

**已实测通过、无发现的项**（d1_fidelity.py 逐项对照翻译前后）：合并区、数据验证区域定义、条件格式、自动筛选、冻结窗格、超链接、批注内容、隐藏行/隐藏列、自定义数字格式（`#,##0.00\"元\"`）、打印区域、工作表保护、名称管理器、公式（缓存值为数字时不动）、keep_original_sheets 生成的 `_原文` 分表——全部原样保留。中-28 之外的其它 O(n²) 未发现（cProfile 里除 dependents 外没有超线性热点）。

**因预算未及验证的项**（留给下一轮或后续代理）：
1. `.xls` 路线（xls_converter）只做了代码走读，没有真 .xls 夹具实跑（本机无 xlwt，造 .xls 夹具成本高）。两处可疑但未证实：`convert_with_fallback` 把 xlrd 文本值直接喂给 `ws_out.cell(value=...)`，openpyxl 会把以 `=` 开头的**文本**当公式写、遇到非法控制字符（老 .xls 常见）会抛 IllegalCharacterError 让整个转换失败并丢弃产物；以及 `_xls_date_value` 还原成 date 后原表的自定义日期格式（如 `yyyy年m月d日`）会退成 openpyxl 默认格式。
2. LibreOffice 路线（docs/LIBREOFFICE_XLS_ROUTE_2026-08-30.md）与超 65536 行、密码保护 .xls 均未触及。
3. 数组公式 / 动态数组（`<f t=\"array\" ref=...>`）被改写成 inlineStr 后 spill 区的行为未构造夹具验证。
4. 富文本（单格内多段不同格式）改写后格式统一丢失——机制确认（`_set_cell_inline_text` 整格重建为单个 `<t>`），但这大概率是产品已知代价，未列入 findings。
5. 扫描/写回 key 一致性（关注点 6）只验证了公式格这一条路径（即高危那条），富文本与跨 sheet 同名场景未单独构造。

### D2 — Word 翻译管线

**基线与复现脚本**

沿用交付的全量基线（1664 passed + 249 subtests，未重跑）。本线自跑板块测试：`TRANSLATOR_APP_DATA_DIR="$(mktemp -d)" ./.venv/bin/python3 -m pytest -q tests/test_word_document.py tests/test_word_coverage.py tests/test_audit_word_doc_fixes.py tests/test_word_defect_fixes.py` → 128 passed, 23 subtests passed in 2.00s（全绿）。所有复现脚本与夹具写在 /private/tmp/claude-501/-Users-lijianwei-vibecoding-claude-XL-Translator/cef9c101-5dad-4a16-b030-8f75baf592e2/scratchpad/d2_*，未改动仓库任何文件，未 git add/commit；每个脚本开头先 `os.environ["TRANSLATOR_APP_DATA_DIR"]=tempfile.mkdtemp()` 再 import config 并断言 APP_DATA_DIR 落在 /var/folders 下（实测输出 /var/folders/y6/.../tmpXXXX），全程未触碰真实数据目录。

工具预算内没查完、留给后续的部分：\n1. **停止与恢复（关注点 5）没查。** `_WordRecoveryPool`（word_task_runner.py:2833-3477）的 `_flush_pending_submits` / `_abandon_unsubmitted_ticket_locked` / `_settle_abandoned_ticket_locked` 三者的锁序与「停止时已恢复译文是否写盘」都没验证。AUDIT_FIX_PLAN 把后者列在「待用户拍板批次 2」，且仓库里已有 tests/test_audit_word_stop_write.py，看起来已有结论，所以按纪律没往下挖——但第一轮修复审查报过的 ABBA 死锁与 shutdown 后 submit 两条回归，我没有独立复核过。\n2. **.doc 老格式（关注点 4）没查。** core/word_converter.py（793 行，LibreOffice 转换路径）与失败/残留清理 `_cleanup_converted_word_paths` 完全没读。\n3. **结构保真度只覆盖了一部分。** 已实测：文本框、书签、run 分裂+格式、超链接锚点（读代码确认已修）、合并单元格/嵌套表（`_iter_unique_table_cells` 按 `w:tc` 去重，python-docx 1.2.0 的 vMerge 委派机制下正确，未见问题）。**未实测**：脚注/尾注/批注（只做了机制确认，见发现 1）、内容控件 SDT 正文（走 `detect_hidden_word_content` 告警，属已知）、OMML 公式、SmartArt、图注 SEQ 域的 `_replace_paragraph_text_around_fields` 实跑、分节与首页/奇偶页眉的三段式制表位、多级列表跨表格的编号连续性。\n4. **双语模式（关注点 3）只读了代码没实跑。** 插行路径 `_insert_translation_paragraph_after` / `_append_translation_to_cell` 的结构保真没有独立夹具验证；页眉页脚的 `_append_translation_inline` 同理。\n5. 发现 2、3、4 都根植于同一个函数 `_replace_paragraph_text`（word_document.py:2494）的「锚点吃掉全部文字」策略，改动时建议一起设计，别分三次打补丁。

