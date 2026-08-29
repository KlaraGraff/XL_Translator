# 全仓 Bug 审查报告(2026-08-29)

## 方法与范围

11 个独立审查代理并行覆盖全部子系统:PDF / Word / Excel 三条管线、任务编排、API 与配置、引擎与调度、翻译记忆 TM、覆盖率与残留修复、前端(工作区 + 设置)、更新流程与 Tauri 壳。每个代理先跑通该板块的既有测试做基线(全部绿),再逐条核实发现;标「已复现」的条目均写了一次性脚本实跑确认,不是读代码推断。

**统计:去重后 71 条(高 14 / 中 30 / 低 27)。** 既有测试约 700+ 项全部通过——所有发现都在现有测试覆盖之外,不是回归。

两条项目硬约束的专项核验:

- **旧数据兼容 / 写入出路**:settings 侧通过(原子写、状态机、adopted 接管都正确);但 keys.json 不走这套状态机(中-1),TM 旧库 `word_type='term'` 条目被深度清洗永久排除(中-4)——「能读」做到了,「能用」有两个洞。
- **互斥开关两半一起落盘**(c5ce235 / 83aed6b / 19f50b2 三个近期提交):专项复查,**无回归**。

XSS 专项:前端全部 `innerHTML` 均为静态字面量或清空,markdown 渲染纯 DOM API,**无 XSS 面**。更新签名校验、版本比较、下载中断处理:核验干净。

---

## 高危(14 条)

数据损坏、任务挂死、译文错误、进程残留。

### 译文正确性(覆盖率/过滤,4 条,全部已复现)

**高-1 日译中:日文汉字被当成「已经是中文」,整体跳过不译**
`core/translation_filter.py:316-318`。`_contains_chinese` 的 `一-龥` 同时匹配日文汉字。源 ja → 目标 zh 时,`工事契約書` 这类纯汉字标题/表头/条款名不抽取、不翻译、不进报告,整份文档只有假名句子被翻。

**高-2 中译日:含「年/月/日/分」的正确译文被残留检查打回,改写回中文原文**
`core/translation_filter.py:459-461, 609-624`。`_residual_cn_date_unit_issue` 未按 `residual_classifier.py:34` 的豁免表放过 ja。`本工事は2026年8月9日に完了する` 被判 fail → `engine_dispatcher.py:999-1022` 将结果重置为原文。中译日的日期/时长句几乎必踩。

**高-3 8 种文字系统不在语言预检白名单,自动检测下整份文件原样输出**
`core/language_preflight.py:133`。阿拉伯、希伯来、泰、老挝、缅甸、高棉、埃塞俄比亚、谚文(韩语)不在候选正则内。源语言选「自动」时预检零调用,回落默认 zh,补译模式下全部单元格判 `ignored`,输出与输入逐字相同。

**高-4 en/fr 之外的语言证据缺失:法语味单元格被静默 `ignored`,不译且不进报告**
`core/translation_coverage.py:189-205`。`_language_evidence` 只认 en/fr 一对标记词。英译中时 `Société Générale Contract`、`Café Manager` 等因 fr 证据更高被拒判 → `COVERAGE_IGNORED`,而补译计划日志不显示 ignored 类,用户无从发现。

### 数据丢失/损坏(3 条)

**高-5 PDF:终态任务单页重生成失败 → 已产出的翻译 PDF 被删且不再重建(已复现)**
`core/pdf_image_translation.py:1582→1847→1593-1594`。先删旧产物再重建,重建被 `ImageModelUnavailableError` 等跳过。模型 Key 失效时点「重新生成」某页 → 整份高清/压缩 PDF 从磁盘消失,record 回 `pending`,任务中心仍指向已删文件,兜底文案还说「输出文件未更新」。

**高-6 Excel:补译模式 + 关闭公式回填 → 公式被整条替换成字面文本(已复现)**
`core/excel_coverage.py:315-316` + `core/xlsx_patcher.py:1488-1489`。扫描端把公式源码串当源文本,写入端用同一 key 命中,`_set_cell_inline_text` 删掉 `<f>`。`=A1&"合计"` 变成纯文本,公式永久丢失,还白花一次 API 翻译公式源码。非补译路径安全(恒用 `data_only=True`)——两条路径对「源文本」定义不一致。

**高-7 TM:旧清洗建议永不失效 + 版本校验被绕开,重放会覆盖用户人工校对(已复现)**
`core/tm_manager.py:1749`(`mark_cleaning_suggestions` 全仓无调用方)+ `api/app.py:228-234`(payload 无 `expected_version`)+ `ui/src/views/library.ts:715`(默认全勾)。序列「跑清洗 → 手工校对某译文 → 再跑清洗 → 确认写入」会用上一轮旧建议静默盖掉人工校对,本该拦截的乐观并发校验因 payload 缺字段根本不执行。

### 任务挂死/崩溃(3 条)

**高-8 Word:停止落在恢复池重试轮之间 → 任务线程永久挂死(已复现)**
`core/word_task_runner.py:2754-2759, 2826-2839`。等待循环只看「全部完成」不看停止标志;停止又让 `_schedule_retry_locked` 不再排下一轮 → `complete()` 永假,线程无限空转。UI 永远「运行中」,`finally` 的临时文件清理、恢复池 shutdown 都不执行。同一修复点还有 `_future_done`(2872-2890)吞异常不复位 inflight 标志的第二条挂死路径。

**高-9 引擎:Responses 流式路由遇到任何非 2xx → `ResponseNotRead` 穿透崩掉整个任务(已复现)**
`engines/openai_engine.py:197` + `core/api_concurrency_control.py:392`。流式 `raise_for_status()` 产生的异常被限流分类器读 `.text` 时抛 `ResponseNotRead`(RuntimeError 子类,`getattr` 不吞),穿透批次二分和降级阶梯直达 `task_runner`。当前 custom_openai 配置(asxs Responses 路由)正在此路径上:一次 429/502 抖动 → 整个文件任务崩、已完成批次全丢,报错是英文内部错误,与真实原因无关。

**高-10 前端:PDF 逐页重跑期间 sidecar 重启过 → 每 2 秒一条报错 toast 永不停止,跨页面跟随(已确认)**
`ui/src/views/workspace.ts:2551-2553, 1727, 1989, 501-511`。快照拉取失败不更新 `rerun.active`,两个循环永不退出;计时器不随 `unmountWorkspace` 清理,toast 洪水跟到设置/记忆库,直到重启应用。批量重跑时 `waitForRerunSlot` 同样死循环,整批卡第一页。

### 更新与生命周期(3 条)

**高-11 正常退出(Cmd+Q)对 sidecar 是 SIGKILL,三层优雅关闭全部失效**
`src-tauri/src/main.rs:659`(`child.kill()` = SIGKILL)。`api/launcher.py:36-55`、`api/app.py:381` lifespan、`api/task_manager.py:1348-1356` 专门写的收尾一层都执行不到。Word 任务跑到一半退出 → headless soffice 进程被 reparent 到 launchd 长期存活并占着 TCP 端口,`/tmp/word_translator_lo_uno_*` 残留。watchdog 救不了(父进程活着时先杀的子进程)。

**高-12 macOS 就地更新把正在运行的 sidecar 的 bundle 整个换掉,界面还说「期间可以继续用」**
`ui/src/update-toast.ts:270, 296` + `ui/src/update-controller.ts:381`。updater 换 bundle 后,PyInstaller PYZ 每次惰性导入都按旧偏移重读新二进制 → 更新后新建 .xls/PDF 任务在首次导入 `core.xls_converter`/`pypdfium2` 时 `zlib.error`,报错文案完全误导(如「pypdfium2 未安装」)。

**高-13 Windows 更新路径上 `stop_sidecar` 永不执行,NSIS 在 sidecar 占用文件期间覆盖安装**
`src-tauri/src/main.rs:669, 722-726`。updater 插件 `exit(0)` 前只走 `cleanup_before_exit`,不发 `RunEvent::Exit`。带任务安装时 sidecar 及其 DLL 仍被占用十几秒,NSIS 撞上被占文件(半装状态为疑似,机制确认)。

**高-14 前端:内置领域 Prompt 是过期硬编码文本,用户一编辑就把后端的数字/计量规则永久丢掉**
`ui/src/views/settings.ts:214-227` vs 权威 `config.py:134-201`。前端文本缺「一个字符都不能改动」和「中文计量用语换写」整行规则,还缺 `en` 键。用户改一个词保存 → 过期文本整段替换内置预设,此后所有翻译丢这两条规则,无任何提示。

---

## 中危(30 条)

### 任务与停止路径

**中-1 keys.json 损坏后保存与清空双双 500,界面无自救出路**(已复现)
`settings.py:1725-1726, 2065` + `core/maintenance.py:194-197` + `api/app.py:1760-1761`。maintenance overview 还把 keys 报成可清空、count 0,点了却 500。对照:`clear_tm()` 为同样情形专门写了出路(`maintenance.py:219-233`),keys 漏了。**硬约束「拒绝写入且不给出路」违规点。**

**中-2 停止时已付费拿到的译文既不写文件也不入 TM**(已复现)
`core/task_runner.py:1180`(停止检查在 TM 写入 1491-1507 之前)。e2a2c5e 专修「停止不拦 TM 写入」,但停止路径上那行修复从未执行到;重跑整批重新付费。注释与行为相反。

**中-3 Excel 复核仲裁标记被无条件清空**(已复现)
`core/task_runner.py:1039` 重绑定 `excel_review_marks = {}`,阶段 1 仲裁写入的标记全部丢弃 → 「疑似原文异常」底色不涂、review 计数 0。

**中-4 TM v2 旧库 `word_type='term'` 条目被深度清洗永久排除**(已复现)
`core/tm_manager.py:2117`(唯一不走归一化的字面比对)。老用户点「深度清洗」永远「未生成可写入的建议」且无提示;附带 stats 把旧 `'import'` 行算进 auto。**硬约束相关。**

**中-5 TM 入库把换行折成空格,二次跑同文档时多行单元格换行丢失**(已复现)
`core/tm_text.py:31` + `core/tm_manager.py:1046-1049`。首跑(API)保留换行,重跑(TM 命中)同格变单行,排版与首次交付不一致。

**中-6 手工新增词条被优先级挡下或写入异常时,界面仍弹「记忆条目已保存」**(已复现)
`core/tm_manager.py:830-840, 1296-1298` + `ui/src/views/library.ts:463-467`。pinned 冲突 → 实际没存,提示已保存。

**中-7 TM 清洗「外层噪声剥离」咬掉成对引号间正文**(已复现,中低)
`core/tm_cleaner.py:232-237, 267-269`。`"甲" 与 "乙"` → `甲" 与 "乙`,破坏后的建议默认勾选入库。

**中-8 `.xls` 转换临时文件在中断路径永久残留,无清扫代码**
`core/task_runner.py:733/1549/1717` + `core/xls_converter.py:86`。阶段 2 停止/致命失败、`already_failed: continue` 两条路径都漏删 `$TMPDIR/xl_translator_temp/*.xlsx`。(同主题:低-25 转换失败留半个 .xlsx。)

### 引擎与调度

**中-9 failover:候选连接构建失败被当成「整个 endpoint 宕机」,同网关全部连接被标耗尽**(已复现)
`core/failover_engine.py:207`。A 报 401、B 构建失败 → A/B/C 全进 `_exhausted`,可用的 C 一次没试。注释说 "try the next candidate",行为相反。

**中-10 模型返回对象数组时 `str()` 兜底,把 `{'translation': 'Valve'}` 字典字面量写进单元格并存入 TM**(已复现)
`engines/base_engine.py:103`(同源 `core/language_preflight.py:274, 292`)。TASK_INSTRUCTION 本身要求对象数组,普通 parse 路径长度校验通过后逐条 `str()`,污染输出与 TM。

### 覆盖率

**中-11 数字 token 不识别千分位与非 ASCII 数字体系,正确译文被判失败改回原文**(已复现)
`core/translation_filter.py:15, 85-87`(同源 residual_classifier/residual_repair)。`1500天 → 1 500 jours`(法语标准写法)fail `missing_number`;修复稿同样被 `verify_feedback_retranslation` 打回。

**中-12 中译日补译:含汉字的日文译文被认成源文,全表复译**(已复现)
`core/translation_coverage.py:164-186, 294-304`。`has_incidental_cjk` 对 target ja 直接 False → 已有日文译文的格子再插一条重复译文。

**中-13 英译德等同字母语言对:双行都是原文时第二行必然被当译文,covered 漏译**(已复现)
`core/translation_coverage.py:262-291, 307-364`。仅模型仲裁能救,引擎不支持 chat 或仲裁失败时整格漏译。

### 前端

**中-14 数值框上下限比后端宽,越界提交 422 英文报错,输入框留着没存的值**(已复现;两个代理独立发现,互相印证)
`ui/src/views/settings.ts:896-913, 2451-2465` vs `config.py:216-257` + `api/app.py:594-597`。五个字段(每批段落数/字符上限/拆分阈值/单页重试/页图并发)区间夹缝里全 422 pydantic 英文原文;catch 分支不重绘,表单值与磁盘不一致。

**中-15 领域覆盖按预设名存、按当前目标语言显示,换语言后拿错语言的 Prompt 翻译**
`ui/src/views/settings.ts:2537-2538` vs `settings.py:931` + `core/engine_dispatcher.py:359-364`。fr 下保存的覆盖,切到 en 翻译时系统 Prompt 还是法语那份。

**中-16 工作区开关/领域/输出目录落盘是发射后不管,PUT 失败则界面骗人、任务按磁盘旧值跑**
`ui/src/views/workspace.ts:2776, 2863, 2887, 2906`(四处 `void persistSettings` 无 catch)。先改内存后落盘,失败无回滚无提示;任务开关后端读磁盘,「锁定行高时缩字号」界面显示关、实际按开跑,持续整个会话。

**中-17 主题先写 localStorage 再落后端,后端失败则本地/后端永久分叉**
`ui/src/views/settings.ts:2661-2695` + `ui/src/app.ts:35-56`。重启后界面深色(localStorage)、设置页高亮「跟随系统」(后端)。

**中-18 任务中心暂停/继续/结束按钮无错误处理,422 时零反馈**
`ui/src/views/tasks.ts:946-969, 1331-1334`。列表最多滞后 4 秒,任务已终态时点「继续翻译」→ unhandled rejection,按钮没反应没原因。对照 workspace 同组操作全部有 catch+toast,属漏网。

**中-19 停止落在 PDF 预处理阶段 → 「已停止 · 已生成 3 个文件 — 全部通过」,实际零产出**(已确认)
`ui/src/views/workspace.ts:3500`。`fileResults` 为空且 state=stopped 时 `generated = 选中文件数`。Excel/Word 不受影响(有 unstarted 补录)。

**中-20 一次 `listTasks()` 失败就永久关掉该 surface 的任务自动接管**
`ui/src/views/workspace.ts:523-524`。`bootstrapAdopted.add` 在网络调用之前且 catch 静默;此后该工作区永远显示设置态,看不到跑着的任务。

**中-21 SSE 终态与列表快照竞态 → 空 body 200 无限重连,任务中心永动重绘**(机制确认,窗口窄)
`ui/src/views/tasks.ts:754-768` + `ui/src/api-client.ts:236-278`。旧快照把 terminal 打回 false 重开流;终态流立即 return 空 body,重试上限只写在 catch 里。注释承认过「infinite fast reconnect」,修了 fast 没修 infinite。

### 更新与壳

**中-22 手动检查更新失败 → 绿色对勾「已经是最新版本」**(已确认)
`ui/src/update-toast.ts:433-436, 480-488`。不看 `result.status === "error"`(后端把网络故障包成 200+error);同屏设置页画「检查失败」chip,两边打架。

**中-23 校验和兜底请求不跟随 302,走到即整次检查报废**(当前潜伏)
`core/update_checker.py:311, 350`。httpx 默认不跟重定向,GitHub 下载链接必 302 → `ok=False` 清空 download_url。仅 asset 缺 `digest` 时触发(当前 release 全带,故未发作);测试假 client 不模拟重定向,CI 永绿。

**中-24 握手超时不 kill 子进程,孤儿 sidecar 活到用户点掉对话框**(中低)
`src-tauri/src/main.rs:605-607`。`?` 提前返回只 drop `Child`;紧邻的健康检查失败路径记得 kill,属遗漏。Gatekeeper 首启扫描超 30 秒即触发。

### Word/PDF 文档结构

**中-25 页眉整行替换遇到 STYLEREF/SEQ 等域段落 → 退化成追加,原文整行重复**(已复现)
`core/word_document.py:572-580`。`replace_only` + 段落含域时把「整行成品」追加而非替换;页眉高度固定,跑版或被裁切。

**中-26 末尾「只含修订删除」的段落被当空段删除,修订记录丢失**(已复现)
`core/word_document.py:2280-2298`。判空白名单缺 `w:delText`/`w:del`;输出上「拒绝所有修订」也拿不回原文。

**中-27 PDF 停止时已入队未开跑的页被烧成「图像生成失败」占位页,任务报「已完成、未截断」**(已复现)
`core/pdf_image_translation.py:3276-3280→3585-3595→2876-2887`。零模型调用的页进了交付 PDF,无 StoppedMsg、无 resume 入口;与测试断言的设计意图(中止不生成占位版)直接冲突。

### Excel

**中-28 共享公式让渡 O(n²),长公式列上任务假死**(已复现:200 行 0.13s→800 行 2.08s,外推 8k 行数分钟)
`core/xlsx_patcher.py:938-945`。每改写一个主控格全表重扫一遍。

**中-29 目标中文时复核改判过的格子被静默丢弃,译文和底色都没有**(已复现)
`core/xlsx_patcher.py:1505` + `translation_filter.py:317-319`。仲裁改判格的 key 是完整双语文本,含中文 → `should_translate` False → return None;标记逻辑在 gate 之后。付了复核的 API 钱,文件一字未改。

**中-30 xls 兼容转换把日期变序列号、布尔变 1/0,风险提示未涵盖**(已复现)
`core/xls_converter.py:154`。`row_values` 不看 ctype:2023-07-15 → `45122`,True → `1`,带进最终双语文件;提示只说样式/图片/宏。

---

## 低危(27 条)

按板块归并,均已由代理核实(置信度标注见括号):

**PDF**(5):审核异常保留候选图时扩展名不跟随实际格式(`pdf_image_translation.py:3428`);前轮 blocking issue 通过后不清,面板「通过」与问题描述并存(`:3497-3499`);三个统计计数器在工作线程无锁自增(`:3281, 3368, 3833`);`handle_api_concurrency_limit` 漏传 `should_stop`,停止响应最多拖 30 秒/页(`:3303-3309`);`_process_file` 是生产死代码但 6 个用例当真、判定已与生产分支漂移(`:2931-3140`)。

**任务编排**(4):`_pump_runner` 兜底把英文内部串/裸异常名当用户文案(`api/task_manager.py:1582, 1589`);`text_source_scopes` 用预检前数据构建,重建后新词条保守不入 TM(`core/task_runner.py:884-891`,疑似);两处删 `task_history.tmp` 是死代码、路径永远对不上(`core/task_history.py:115`、`core/maintenance.py:312`);维护模块与历史库对数据目录的绑定时机不一致(`core/maintenance.py:30`,疑似)。

**API/配置**(1):`PUT /api/settings` 改 `engine.cloud_model` 返回 200 但被服务商记忆覆盖回,当前无调用方走此路径(`settings.py:550-559, 776-785`)。

**引擎**(1):数组中 `null` 元素转空串后不写入也不计未译,报告显示全部成功(`engines/base_engine.py:103`)。

**TM**(3):模型建议每跑一次重复入库一次(`core/tm_cleaner.py:783, 586`);`bulk_pin_entries`/`set_all_pinned` 超 32766 条抛 `too many SQL variables`——发布用 3.11 运行时才炸,开发机 3.13 复现不了(`core/tm_manager.py:1560-1592`);编辑固定条目被拒时报「与现有原文冲突」,与真实原因无关(`:1332-1334` + `api/app.py:966-968`)。

**覆盖率**(1):5% 附带 CJK 阈值对短译文过严,含中文日期的短法语译文被复译——产品已声明「宁可多翻」,登记为已知代价(`core/translation_coverage.py:164-186`)。

**前端**(4):记忆库搜索/翻页失败时选中集已清、表格不刷新且无提示(`ui/src/views/library.ts:1133-1142`);重跑完成瞬间轮询显示「第 0 页正在重新生成」(`workspace.ts:1645-1646`);`TaskStatus.result` 类型声明与 `list_tasks` 实际报文不符,直读即炸(`api-client.ts:20` vs `task_manager.py:826`);「复制路径」在 WKWebView 失焦时静默失败(`tasks.ts:994-998`,疑似)。

**更新/壳**(1):每次更新检查失败落一条诊断,离线用户启动 80 次挤掉全部任务诊断;`_safe_error_code` 还把关键错误码折叠成泛型(`api/app.py:1702-1706` + `core/diagnostics.py:250-267, 373-388`)。

**Word**(2):开启页眉页脚翻译时向无页眉文档注入 6 个空 part + 6 条 reference(`core/word_document.py:494`,python-docx 副作用);合成 `w:pPr` 子元素顺序违反 CT_PPr schema 序列,现实 Word 容忍(`:2360-2375, 2105`,疑似)。

**Excel**(5):锁行高模式下小于 6pt 的字号被反向放大(`core/xlsx_patcher.py:1613-1622`);共享公式让渡失败只写 loguru、任务日志无痕,且出现「标了色没译文」(`:963-965, 1385-1392`);让渡后 `ref` 左上角不是新主控格,Excel 真机未验(`:957, 967-973`,疑似);`excel_coverage.py:61-68` 第二次 load 抛错泄第一个句柄(疑似);`xls_converter.py:103-113` 转换失败留半个 .xlsx 无人回收(与中-8 同主题)。

---

## 各板块「已查无发现」与专项核验(摘要)

- **api/task_manager.py 状态机主体**:停止/暂停/恢复、租约释放、SSE 回放、终态落盘、退休驱逐逐条追踪,无竞态,锁序一致。
- **word_converter.py(b307e3a)**:签名读取、约束判定、缓存策略边界正确,本机实测 codesign 输出命中判定。
- **word_batching / word_coverage / header_footer_channel**:拆包完整性双重校验、插入验证逐片段回定位,自洽。
- **excel_automation / bilingual_writer / headless_translate**:COM 生命周期、原子替换、暂存回拷正确。
- **engines 四个非 OpenAI 引擎、api_scheduler、connection_pool、connectivity_check、model_* 全家**:无发现。
- **translation_protocol / coverage_arbitration / coverage_review / residual_pipeline / residual_replay / mixed_language / language_registry**:无发现。
- **unit_ledger / tm_hygiene / tm_text**(本体)、**task_resources / task_logger / tm_cleaning_task_runner**:无发现。
- **launcher / app_paths / config_crypto / document_config / user_facing_errors / config.py / app_meta**:无发现;config_crypto 的 AAD 对称与失败路径核验通过。
- **前端**:markdown.ts(XSS 面)、components.ts、model-pill、save-file、quickstart、help、router、shell、excel/word/pdf 薄封装、task-state-labels:无发现。
- **更新**:版本比较(整数元组,`9.3.10 > 9.3.4` 正确)、minisign 验签、内存下载验签后落盘、update-service、dev-tauri-shim:无发现。

各代理还记录了主动排查后证伪的假线索(灾难性回溯、无限重试、生成器泄漏、双重提交窗口等约 30 项),不计入清单;明细见各代理输出。

## 测试基线

各板块既有测试全绿:PDF 108、Word 172、Excel 79、TM 65、编排 60、引擎 87、覆盖率 202+44 子测试、设置/API 108、生命周期 31、更新 29 + cargo 8,前端 `npx tsc --noEmit` 通过(无单测)。全部发现均无现有测试覆盖。
