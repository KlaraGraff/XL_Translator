# 全仓 Bug 审查报告（2026-08-31 · V9.4.0 发布后）

本文由各审查线回传的结构化结果自动渲染，**不要手工编辑**——下一条审查线跑完重渲染时会覆盖。要补充结论请另开文档。

## 方法与范围

17 条独立审查线，逐条串行派发（不并行）。每条线拿到同一份预先算好的基线：`pytest -q` → **1664 passed, 249 subtests passed, 31s**，ruff / tsc / cargo clippy 均干净；因此所有发现都在现有测试覆盖之外，不是回归。每条线约 60 次工具调用的成本上限，到顶就回传已有结论（宁可少报，不许空手）。

审查线一律不得写入用户真实数据目录（`~/Library/Application Support/Translator`）：脚本必须在 `import config` **之前** 把 `TRANSLATOR_APP_DATA_DIR` 指到临时目录，并断言 `config.APP_DATA_DIR` 落在 /tmp、/var/folders 或 /private 之下。这条纪律是本轮开跑后补的——此前有代理直接写坏了用户的 `keys.json`，触发了「静默备份并重置为空」的路径。

**当前进度：4/17 条审查线回传，累计 12 条发现（高 2 / 中 5 / 低 5）。**

| 审查线 | 范围 | 发现 |
|---|---|---|
| R1 | 2026-08-29 审计 14 条高危的回归核验 | **0 条** |
| C1 | 并发、线程、锁、竞态、死锁 | 高 1、中 2 |
| C2 | 资源生命周期：临时文件、子进程、数据库连接、文件句柄 | 中 1、低 2 |
| P1 | 持久化 / schema 迁移 / 旧数据兼容 | 高 1、中 2、低 3 |

---

## 高危（2 条）

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

---

## 中危（5 条）

### 中-1 生产实际使用的 FairApiGroupScheduler 完全忽略 request category，恢复优先级是死代码

`C1` · 置信度 high · new

**位置**：`core/task_resources.py:358`、`core/task_resources.py:222`、`core/task_resources.py:250`、`core/api_scheduler.py:267`

**机制**：WeightedApiScheduler._can_acquire（core/api_scheduler.py:267）实现了恢复优先级：一旦有 recovery 在等或在跑，普通请求就被 normal_soft_limit（默认 80%）卡住，给重试/仲裁留出 20% 的槽位。但 api/task_manager.py:605/1145/1516 把 `lease.scheduler_for(group)` 交给 runner——runner 拿到的是 TaskGroupScheduler 门面，背后是 FairApiGroupScheduler，而 `FairApiGroupScheduler._can_acquire_locked(owner_key, weight)`（core/task_resources.py:358）只有两句：容量够不够、是不是队首任务，**category 参数根本没进这个函数**。`_waiting_recovery_count`（:222 自增）和 `_active_recovery_weight`（:250 累加）照常记账，也照常出现在 snapshot 里，但没有任何判定读它们。而 FIFO 是按 owner_key（任务）排的，同一个任务的 normal 线程和 recovery 线程共用同一个队列条目，等于在任务内部完全没有优先级——纯抢锁。tests/test_api_concurrency_control.py 里那 10 条恢复优先级测试全部只测 WeightedApiScheduler，tests/test_scheduler_waiters.py 只有 2 条且不涉及 category，所以这个缺口不会被现有测试照出来。

**后果**：Word 的恢复池是在主翻译「不再有新批次入队」时就 start 的（core/word_task_runner.py:1368 的 _MainTranslationDrainGate + defer_until_started=True），设计上就是要和主翻译尾巴并行跑；PDF 的逐页审核（category=RECOVERY）在审核模型与图像模型同连接时也和图像生成共用同一个组。现在这段重叠期里恢复请求拿不到任何优先级，会被主翻译流量压到几乎抢不到槽位，本该并行的仲裁/审核退化成串行等待，任务总时长变长。用户感知是「进度条走到最后几步不动了」。不丢结果、不多花钱，但设计意图被静默架空。

**复现**：已复现。c1_recovery_priority.py：capacity 8，8 条 normal 线程满载 4 秒 + 1 条 recovery 线程。WeightedApiScheduler → recovery 抢到 74 次，中位等待 0.0ms、最大 54.9ms；FairApiGroupScheduler 门面（生产实际路径）→ recovery 只抢到 1 次，等待 4022.5ms（等到我把 normal 停掉才进去）。

**修法**：把 WeightedApiScheduler._can_acquire 的那段软上限逻辑搬进 FairApiGroupScheduler._can_acquire_locked：给它加 category 形参（调用点 core/task_resources.py:245 已经算好了 normalized_category），维护一个 normal_soft_limit（在 _reset_capacity_locked / set_capacity / _next_reduced_capacity_locked 三处一起更新），并保留「_active_normal_weight == 0 时放行一个超重普通请求」那条防死锁的逃生口。同时给 tests/test_scheduler_waiters.py 补一条「normal 满载时 recovery 仍能在 N 毫秒内拿到槽位」的测试——现在这条契约在生产调度器上完全没有测试守着。

### 中-2 限流退避不响应停止：另有 3 处调用也没把停止标志交给 handle_api_concurrency_limit

`C1` · 置信度 high · new

**位置**：`core/tm_cleaner.py:747`、`core/mixed_language.py:440`、`core/mixed_language.py:748`

**机制**：core/api_concurrency_control.py 的 `_wait_out_minimum_capacity_limit` 在并发已经降到最低档时会 `_interruptible_sleep(delay, should_stop)`，delay 按 2×2^(n-1) 增长、上限 30 秒，整段宽限窗口 MINIMUM_CAPACITY_GRACE_SECONDS = 120 秒。`_interruptible_sleep` 只在 `if should_stop and should_stop()` 时提前返回——传 None 就是完全睡满。全仓 8 个调用点里，只有 core/engine_dispatcher.py:699/847（Excel/Word 主翻译）和 core/pdf_image_translation.py:4148 传了 should_stop；pdf 那处还专门写了注释说明为什么必须传（「限流退避里睡的是整整 30 秒，不把停止标志交给它，用户点了停止之后每一页都要先把这一觉睡完才肯回来」）。剩下的 core/tm_cleaner.py:747（TM 清洗）、core/mixed_language.py:440（混合语言）、core/mixed_language.py:748（混合语言重试）都没传——mixed_language 更可惜，should_stop 就在作用域里，:445 睡醒之后紧接着就在查它，只是没交给退避本身。（另三处 coverage_review / word_task_runner 已并入本次第 1 条发现。）

**后果**：用户在 TM 清洗或混合语言处理阶段按停止，遇上账号正被限流时，每个 worker 线程要先把当前这一觉（最长 30 秒）睡完才回来查停止；宽限窗口内会连睡几次，最坏能拖到接近 120 秒。任务中心停在「停止中」不动，看起来就是卡死。tm_cleaner 这条尤其难受——TM 清洗本来就是用户觉得「随时可以叫停」的后台整理动作。

**复现**：已复现。c1_backoff_stop.py：把 capacity 打到最低档后，在停止已置位的前提下连打 4 次 429。无 should_stop 版本分别阻塞 1.64s / 3.38s / 9.30s / 13.10s（累计 27.4 秒，日志里能看到「等待 13.1s 后重试当前批次，已持续 14s」）；传了 should_stop 的版本 4 次全是 0.00 秒。

**修法**：三处调用都补上 `should_stop=`：core/mixed_language.py:440 和 :748 直接把作用域里现成的 `should_stop` 传进去（:445 那句检查可以保留，但退避本身必须先能被打断）；core/tm_cleaner.py:747 把 runner 的停止回调透传到这一层。更稳的做法是给 handle_api_concurrency_limit 的 should_stop 改成必填关键字参数，让漏传在 tsc/ruff 之外靠签名本身兜住——现在 8 个调用点漏了 6 个，说明「可选参数」这个形状本身就是缺陷来源。

### 中-3 sidecar 看门狗的 20 秒强杀预算包不住内层 22 秒收尾链——壳被强退时 soffice 与临时目录照样残留

`C2` · 置信度 high · regression-of-prior-audit

**位置**：`api/launcher.py:21`、`api/launcher.py:78-84`、`src-tauri/src/main.rs:48-68`、`src-tauri/src/main.rs:1045-1063`

**机制**：上一轮高-11 只修了「正常退出」这条路：Rust 侧把 SIDECAR_STOP_TIMEOUT 改成 ①uvicorn 排空 10s + ②task_manager.shutdown 12s + ③落盘余量 3s = 25s，明确写着「外层必须包住内层」。但「壳非正常消失」（Force Quit 整个 app、Tauri 崩溃、被系统杀）走的是另一条路——Rust 根本没机会发 SIGTERM，收尾由 api/launcher.py 的 parent watchdog 触发：它发现父进程没了 → 调 begin_shutdown（只置标志）→ server.should_exit = True → 然后 `deadline = time.monotonic() + WATCHDOG_FORCE_EXIT_SECONDS` 干等 20 秒，到点无条件 os._exit(0)。这个 20.0 从来没跟着高-11 一起改：内层最坏情况仍是 10.0（GRACEFUL_SHUTDOWN_SECONDS）+ 12.0（TranslationTaskManager.shutdown 默认 timeout）= 22.0 秒 > 20.0，而 runner 的 finally（删 LibreOffice profile、word_translator_temp、PDF 分页工作区）恰好就在②那一段里。更关键的是这个循环压根不观察收尾有没有做完——不看 server 状态、不看任务是否已 terminal，只是纯睡满 20 秒。Rust 侧那个 the_mirrored_sidecar_budgets_still_match_the_python_side 测试只读 GRACEFUL_SHUTDOWN_SECONDS 和 shutdown(timeout=)，完全没覆盖 WATCHDOG_FORCE_EXIT_SECONDS，所以这条不等式变红不了。

**后果**：Word / PDF 任务跑到一半，用户强退应用（或应用崩溃）：sidecar 在收尾走到一半时被自己的看门狗 os._exit 掉。UNO 那条路拉起的 headless soffice 是 Popen 长驻进程，terminate 写在 finally 里，这一杀就永远不执行——soffice 被 reparent 到 launchd 长期存活，占着 127.0.0.1 的 UNO 端口和 profile 目录；word_translator_temp 里那份几十 MB 的中间 docx、PDF 分页工作区也一起留下。任务历史那部分不受影响（下次启动 mark_active_tasks_interrupted 会兜住）。

**复现**：已复现（c2_watchdog.py + c2_watchdog2.py）。c2_watchdog.py 实测输出：GRACEFUL_SHUTDOWN_SECONDS = 10.0 / WATCHDOG_FORCE_EXIT_SECONDS = 20.0 / task_manager.shutdown default timeout = 12.0 / worst-case inner chain (drain+unwind) = 22.0 | watchdog force-exit = 20.0 | watchdog covers inner? False / watchdog waits on server state? False。c2_watchdog2.py 做等比例活体验证：把 WATCHDOG_FORCE_EXIT_SECONDS 调成 2.0、模拟收尾工作耗时 4.0s，进程在恰好 2 秒时退出（elapsed=2s），CLEANUP FINISHED 一次都没打印——证明看门狗到点即杀、完全不等收尾。

**修法**：把 api/launcher.py 的 WATCHDOG_FORCE_EXIT_SECONDS 改成和 Rust 同源的加法：GRACEFUL_SHUTDOWN_SECONDS + <task_manager.shutdown 默认 timeout> + 余量（即 25s），而不是写死 20.0；更稳的做法是循环里改成「轮询到 server 真的停了就立刻 os._exit，否则等到 deadline」，这样正常情况几百毫秒就退干净、异常情况才用满预算。同时把 WATCHDOG_FORCE_EXIT_SECONDS 补进 src-tauri/src/main.rs:1045 那个镜像测试的断言里（断言它 >= drain + unwind），否则下次改任何一段还是没人拦。

### 中-4 keys.json 上同一个洞：非 UTF-8 字节让保存、读取、以及显式出路「删除全部 API Key」三条路一起抛异常（中-1 的修复没修干净）

`P1` · 置信度 high · regression-of-prior-audit

**位置**：`settings.py:1809`、`settings.py:1810`、`core/maintenance.py:208`

**机制**：`_load_keys_unlocked()` 和 settings 侧犯同一个错：`KEYS_PATH.read_text(encoding="utf-8")` 外面只有 `except OSError`（settings.py:1810）。函数里为「内容损坏」写了完整的备份-重建分支、为 `force=True`（维护页「删除全部 API Key」）写了专门的放弃分支，但这两个分支都在 `json.loads` 那一层，解码错误在更早的一行就把整个函数掀了。于是 `load_keys()` / `get_key()`（非 strict，本该吞掉一切返回空表）、`save_key()`、以及 `delete_all_keys()` 里那句 `_load_keys_unlocked(strict=True, force=True)`——按注释「用户已经在按下它的那一刻放弃了旧文件，不该因为读取或备份失败就拒绝执行删除」——全部一起抛。

**后果**：和上一轮审计的中-1 是同一种形状：保存与清空双双失败、界面无自救出路。而且比中-1 更宽——`get_key()` 也炸，意味着不是「存不进 Key」，是每一次翻译在取 Key 那一步就直接失败。触发概率比 settings.json 低（Key 基本是 ASCII），但一旦发生用户完全出不来。中-1 的修复只覆盖了 JSON 解析失败和 OSError 两类，编码这一类漏了。

**复现**：已复现。`p1_keys_nonutf8.py`（keys.json 用 gb18030 写入 `{"openai::": "sk-中文备注"}`）：load_keys / get_key / save_key / delete_all_keys 四步全部 `RAISED UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd6 in position 17`。

**修法**：settings.py:1809-1810 与上一条同源修复：改用 `read_bytes().decode("utf-8")`，把 `UnicodeDecodeError` 并入下面那段已经写好的「内容损坏 → 非 strict 返回空表 / force 直接放弃 / 否则备份后按空表续写」逻辑里，不要让它绕过状态机。两处一起改，别只改 settings 那边。

### 中-5 「测试连接」跨网络往返持着旧快照，落盘时整份 connections 列表覆盖，把期间用户对另一条连接的修改静默吃掉（两个请求都返回 200）

`P1` · 置信度 high · new

**位置**：`api/app.py:1517`、`api/app.py:1557`、`settings.py:1645`、`settings.py:1629`

**机制**：`_settings_delta()` 对非 dict 的值一律记 `_SETTINGS_FIELD_SET`（settings.py:1645），列表也在内——所以 `engine.connections` 的合并粒度是「整份列表替换」，不是按元素合并。`check_model_role_connectivity` 在 api/app.py:1517 先 `load_settings()`（此时拍下 `_persisted_snapshot`），然后做一次真实的模型 API 往返（`check_connectivity`，秒级甚至到超时），最后在 api/app.py:1557 才 `save_settings(settings)`。这个窗口里用户在面板上改另一条连接、走 `PUT /api/models/roles/{role}/connections/{id}` 已经存进磁盘了；测试结果回来时，它的 delta 里 connections 是一整份基于旧快照的列表，直接把对方的改动盖掉。既有 tests/test_settings_concurrent_updates.py 的 10 个并发用例全是标量和嵌套 dict，没有一个覆盖列表元素。

**后果**：用户点「测试连接」后不干等着（这是最自然的操作），顺手改了另一条连接的模型名并保存，界面提示保存成功、接口返回 200、响应体里还带着新值；等测试结果一落盘，磁盘上悄悄退回旧值。下次打开设置页发现改动没了，且没有任何提示。这条连接如果后面被真的用来跑任务，跑的是用户以为已经改掉的旧模型——属于「用户以为配置生效了、实际在按旧配置花钱调 API」。

**复现**：已复现。`p1_api_lostupdate.py`：TestClient 起真实 app，把 `check_connectivity` 换成 sleep 1.2s 的桩（模拟网络往返），线程 A 打 `POST /api/models/connectivity/text {connection_id: c0}`，线程 B 在 0.4s 后打 `PUT /api/models/roles/translation/connections/c1 {model: "用户刚改的模型"}`。输出 `HTTP: {'edit': 200, 'test': 200}`，盘上 `[('c0','主模型'), ('c1','m1')]`——c1 的编辑消失。纯 settings 层的最小复现见 `p1_conn_lost3.py`：串行改生效（c1=X1），并发改两条不同连接后只剩一条（`[('c0','主模型'),('c1','并发改一'),('c2','m2')]`，「并发改二」丢失）。

**修法**：两条路选一条。轻量：给 `_settings_delta` 加一条列表特例——对元素带稳定主键的列表（connections 有 `id`）按 id 生成逐元素的增/删/改 delta，而不是整份 SET；`_apply_settings_delta` 对应按 id 合并，顺序变化仍记为整份 SET。稳妥：把 `check_model_role_connectivity` 的写盘窗口收窄——网络往返结束后重新 `load_settings()`，只把 availability_* 这几个字段写到目标连接上再保存，不要让一个跨秒级 I/O 的请求持有整份设置快照。推荐后者先落地（改动小、语义清楚），前者作为 delta 层的根治。

---

## 低危（5 条）

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

