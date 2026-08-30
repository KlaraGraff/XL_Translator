# 审计缺陷修复计划（2026-08-29）

对应 `docs/CODE_AUDIT_2026-08-29.md` 的 71 条发现。高-6 已修（ef35b4b）。
分工按用户指示：**技术问题直接修；产品问题 5 个一组交用户拍板。**

## 分类结论

- **技术直接修：67 条**（高 12 / 中 29 / 低 26），按文件归属分 13 个集群并行修复，
  每个集群一个修复代理 + 一个独立对抗审查代理，主会话统一验收、统一提交。
- **押后待拍板（产品形态）：3 项**
  1. 高-12：就地更新期间的 UX（是否禁新任务、是否提示重启、文案）。
  2. 中-15：领域自定义按「预设+语言」重构后，**已有旧自定义**算哪个语言的（迁移语义）。
  3. 高-7 残余：清洗建议**默认全勾**是否保留、是否跳过人工改过的条目
     （技术部分——建议过期失效 + 乐观并发校验——直接修，不等拍板）。
- **登记为已知代价，不修：1 项**——低-覆盖率（5% 附带 CJK 阈值过严致短译文复译），
  审计原文即注明「产品已声明宁可多翻」。

## 集群划分（文件互斥，避免并行代理撞车）

| 集群 | 条目 | 文件 | 代理配置 |
|---|---|---|---|
| A1 过滤/残留 | 高-1 高-2 中-11 | translation_filter, residual_classifier, residual_repair | opus/high |
| A2 覆盖率 | 高-4 中-12 中-13 | translation_coverage | opus/high |
| A3 预检/解析 | 高-3 中-10 低-引擎null | language_preflight, engines/base_engine | sonnet/high |
| A4 PDF | 高-5 中-27 低-PDF×5 | pdf_image_translation | opus/high |
| A5 传输/故障转移 | 高-9 中-9 中-23 | openai_engine, api_concurrency_control, failover_engine, update_checker | sonnet/high |
| A6 任务编排 | 中-2 中-3 中-8 低-text_source_scopes | task_runner | sonnet/high |
| A7 TM | 高-7(技术部分) 中-4 中-5 中-6 中-7 低-TM×3 低-前端library | tm_manager, tm_cleaner, tm_text, api/app.py, library.ts | opus/high |
| A8 配置/维护 | 中-1 低-API 低-编排×3 低-诊断洪水 | settings.py, maintenance, task_history, diagnostics, task_manager, api/app.py | sonnet/high，**排在 A7 之后**（共享 api/app.py） |
| A9 Excel | 中-28 中-29 中-30 低-Excel×5 | xlsx_patcher, xls_converter, excel_coverage | opus/high |
| B1 工作区前端 | 高-10 中-16 中-19 中-20 低-第0页 | workspace.ts (+app.css) | sonnet/high |
| B2 设置前端 | 高-14 中-14 中-17 | settings.ts (+必要时 api/app.py) | sonnet/high，**排在 A8 之后** |
| B3 任务中心/客户端 | 中-18 中-21 中-22 低-类型 低-复制路径 | tasks.ts, api-client.ts, update-toast.ts, update-controller.ts | sonnet/high |
| R1 Tauri 壳 | 高-11 高-13 中-24 | src-tauri/src/main.rs | opus/high |
| WD1 Word 文档 | 中-25 中-26 低-Word×2 | word_document.py | opus/high |
| WT1 Word 挂死 | 高-8 | word_task_runner.py | opus/high |

## 执行机制

- 修复代理只许改名下文件 + 新建 `tests/test_audit_*` 测试；不许 commit。
- 每个集群完成后立刻由独立 opus 审查代理对抗核查（逐条否证 + 跑该板块测试）。
- 主会话收齐后跑全量 pytest + tsc，按集群分批提交进 main。
- 基线（f6768f6，含并行会话的 TOC 提交）：1310 passed + 139 subtests，tsc 干净。
- 代理执行中发现的新产品问题收集进下一批「产品 5 条」。

## 第一轮结果（2026-08-30 追记）

- 15 个修复集群全部完成（A8 的修复落盘但代理死于报告环节，B2 未跑到）；
  13 份对抗审查共开出 **22 条 mustFix**，其中数条为审查代理实测复现的严重问题：
  - WT1：修复把一个 ABBA 死锁挪到了「停止」主路径，另有 shutdown 后 submit 必炸的挂死路径；
  - WD1：域段落替换绕过超链接保护、清掉页眉制表符（三段式页眉中招）；
  - A4：终态单页重生成不再清理旧压缩版产物且仍挂在任务记录上；
  - A2：语言证据词表把 door/van/el 等常见英文短词判成「确定非英文」；片假名区间误含・ー。
  - A5（传输/故障转移）唯一一次通过，零 mustFix。
- 第二轮：12 个集群按审查意见返修＋复审；A8 由收尾代理盘点补齐后首审；B2 从零跑全流程。

## 第一轮产品问题分流（33 条）

按既定立场直接消化、不占用户决策的：
- 「宁可多翻」立场覆盖：ja/ko 源文里真中文段被重翻；中译日纯汉字译文重翻；
  数字千分位歧义从宽（1,500 双解读任一命中即过——严格化会重新制造把正确译文打回的假失败）；
  WD1 不配平域页眉保持追加不漏译。
- 「最小事实文案」立场覆盖：B1 回滚 toast、停止自动刷新 toast 保持通用措辞；
  B3 更新失败 toast 复用后端中文 message；A9 错误值格还原成 #DIV/0! 文本（不含中文不会送翻）。
- 发布说明手工写：A3 新增 8 种自动检测语言写进 release notes 待办，不改代码文案。
- 纯技术处置：auto+纯汉字盲区随预检修复（高-3）自然收窄；WT1 重试异常计预算继续（保恢复率）。
- 工程待办：tauri 大版本升级检查单加一条「cleanup_before_exit 清资源表的实现事实」（高-13 依赖）；
  Windows 优雅关闭需 sidecar 加 shutdown 端点（新对外 API，立 issue 待拍板）。

**待用户拍板（批次 2，随收工报告给）**：停止可见性小包（中止页状态区分/停止横幅带未完成数/
失败提示带「旧产物已保留」/停止未恢复单独文案）；Word 停止时已恢复译文是否写盘（与 中-2 的
Excel 立场对齐）；replace 模式含域段落是否切原地替换；补译计划日志是否记 ignored 及理由；
Excel 两条告警文案是否换后果导向说法。
**批次 3（更低风险）**：Cmd+Q 收尾上限 12s 是否压短/加提示；清洗建议跳过清单/失效样式/
blocked_pinned 动线；复制路径失败兜底形态；ko 是否与 ja 同待遇。
