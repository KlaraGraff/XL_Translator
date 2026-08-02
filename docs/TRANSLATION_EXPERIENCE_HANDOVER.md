# Skill 翻译与多轮纠偏经验交接

更新日期：2026-08-02  
适用项目：`/Users/lijianwei/vibecoding/claude/XL_Translator`

## 这份交接的范围

本文件整理的重点不是 Translator 应用自己产生的 TM、任务历史或日志，而是另一条已经被频繁使用的工作流：

```text
用户在对话中提出翻译任务
  -> Codex 按文件类型调用 skill / 脚本 / 模型
  -> 用户查看真实产物，指出不对之处
  -> Codex 按反馈局部或全量修改
  -> 再检查产物，继续迭代
```

这才是现在最有价值的“经验资产”。它不自动进入 Translator；绝大部分仍散落在 Codex/Claude 本地会话、skill 规则、最终文件和用户后续反馈中。

## 结论

有可用的历史经验，但它当前不是一个干净的数据集，而是三类混合材料：

1. **会话事实**：原始请求、修改意见、前后版本、工具调用和最终结论。它保留在本机本地会话中，可能含敏感文件内容，不能整库导入软件。
2. **可执行方法**：文件路由、翻译脚本、产物清单、质量检查和模型选择，保留在 skill 的 `SKILL.md`、`scripts/` 与项目测试中。
3. **已经被人确认的偏好/规则**：例如“局部补译不能损坏整段双语内容”“不能用抽样宣布全量审校通过”。这是最适合先产品化的部分。

现有软件有 TM，但它解决的是术语和短文本复用；它**不能**保存一轮修订的原因、适用范围、文档结构、视觉验收或多轮上下文。因此不能把这批经验简单等同为“往 TM 多写几条”。

## 可访问的历史来源

| 来源 | 本机位置 | 含有什么 | 是否适合直接导入软件 |
| --- | --- | --- | --- |
| Codex 会话 | `/Users/lijianwei/.codex/sessions/` | 请求、回复、工具执行、局部反馈和任务产物线索 | 否，需按任务筛选、脱敏、结构化 |
| Claude 项目会话 | `/Users/lijianwei/.claude/projects/-Users-lijianwei-vibecoding-claude-XL-Translator/` | 关于翻译软件、模型路由、跑批和验收的完整讨论 | 否，适合人工复盘和抽取决策 |
| Claude 项目记忆 | 上述目录的 `memory/` | 已明确记录的长期工作偏好 | 可以人工转为产品默认规则 |
| Codex 翻译 skills | `/Users/lijianwei/.codex/skills/{file-translate,excel-translate,word-translate,pdf-image-translate}/` | 路由、模型/CLI、输出约定、验证规则 | 可以抽取为产品工作流规范 |
| 最终交付物及 report/manifest | 每个任务当时指定的输出目录 | 真实的已交付译文、输入指纹、机械检查结果 | 只导入经人工确认的样本 |
| Translator 应用 TM | `~/Library/Application Support/Translator/tm.db` | 术语和短文本翻译对 | 仅作为辅助词汇资产，不是本交接的主来源 |

会话文件是本机数据，不等同于稳定的产品 API，也不保证永久格式兼容。它们更像待筛选的原始档案。原始会话、`keys.json`、`config.env`、模型响应和涉及项目文件的正文都不应被直接提交到 Git 或批量送给模型。

## 已确认的经验规则

以下规则来自历史任务中用户的实际纠偏。它们的共同特征是：不是通用“翻译要准确”口号，而是已经暴露出具体失败模式、可以写成产品约束的规则。

### 1. 全量审校不能用预览或抽样代替

当用户要求“检查整个文件/整套 PPT 是否漏译或不匹配”时，预览文本、固定字符上限或抽样读取只能用于定位，不足以给出“已全量检查”的结论。

历史中出现的失效方式包括：长文本被截断、表格/图表/组合对象未覆盖、只拿到每页摘要却对全文件下结论。

产品化要求：

- 审校任务必须保存 `total_units`、`read_units`、`covered_units`、`omitted_units` 和 `unsupported_units`。
- 任何 `covered_units < total_units` 的任务，只能报告“部分审校”，不得报告“无漏译”。
- 视觉节点、表格单元格、脚注、文本框、演讲者备注等应分别计数，不能只以“页数”代替覆盖率。
- 对超长或不支持节点，产物中必须给出明确待复核清单。

### 2. 局部补译必须是保留性修改

用户曾遇到：一大段已有双语内容里只缺一小段翻译，修正时却把整个 shape/段落替换成“缺失片段 + 译文”，导致此前正确的双语内容丢失。

根因不在翻译质量，而在写入接口默认把“局部意图”解释成“整段覆盖”。

产品化要求：

- 局部修正一律需要明确的范围定位：段落、run、字符范围、表格单元格或结构化节点 ID。
- 全量覆盖必须是单独动作，并要求调用方提交完整替换内容和明确 `replace_all=true`。
- 写入前验证目标内容版本/哈希；写入后验证未修改范围仍存在。
- 生成修改前后 diff，允许用户逐项接受。不能只以“调用成功”作为正确性证明。

### 3. 多轮“同上/继续/另一页”必须有显式任务状态

用户在同一对话的下一轮说“同上处理另一页”时，曾因路由只识别少数固定表达而没有暴露上轮可用的编辑工具。自然语言续写不应依赖脆弱的关键词正则。

产品化要求：

- 每次翻译/审校建立稳定的 `job_id`，保存文件、页/工作表/段落范围、语言对、风格、术语集、模型、输出版本和未完成事项。
- 用户的“同上”“继续”“另一页”“按刚才方式”等表达应先尝试恢复最近的兼容任务，而不是重新走无状态的通用路由。
- 无法唯一判断承接哪个任务时，展示最近任务/页面让用户点选，不能静默降级成普通聊天。
- 会话压缩、重开或更换模型后，任务状态仍须可恢复；状态不能只活在对话上下文中。

### 4. 用户反馈是有结构的验收，不是新的自由 Prompt

真实反馈通常在修正以下维度：遗漏、错译、术语、双语排列、对象范围、版式保真、输出目录、是否真正覆盖全文件。把它们作为一段自由文本塞回模型会丢失可统计性。

建议的反馈分类：

| 类别 | 例子 | 软件应产生的资产 |
| --- | --- | --- |
| 术语/译法 | 专名、工程词、法语表达不符合项目用法 | 审核后的 TM/术语条目 |
| 语气与体裁 | 工程函件、台账、施工方案、邮件的表达不对 | 风格 profile 规则与正反例 |
| 完整性 | 漏译、误判为已翻译、未覆盖节点 | 评测案例与覆盖率规则 |
| 写入安全 | 局部修改损坏周围内容 | 回归样本与写回保护测试 |
| 版式与结构 | 表格、段落、双语层级、PDF 页面结构被破坏 | 格式保真评测与视觉 QA 样本 |
| 流程/续写 | “同上”无法继续、复核工具未暴露 | 状态机和路由测试 |

反馈入库前至少应有：`issue_type`、`document_type`、`language_pair`、`scope`、`before`、`after`、`accepted_by`、`source_job_id`、`style_profile_version`、`privacy_level`。`before/after` 默认仅保留用户批准的最小片段。

### 5. 机械通过与人工验收是两层不同结论

历史工作流已经形成了一个有效分工：翻译跑批可交给成本较低的独立模型，检查文件是否生成、页数/工作表数/报告是否齐全；排版审美、技术准确性和是否符合用户项目语境，留给主对话和人工判断。

当前长期偏好已记录在：

- `/Users/lijianwei/.claude/projects/-Users-lijianwei-vibecoding-claude-XL-Translator/memory/delegate-test-runs-to-sonnet.md`
- [`../.claude/agents/translation-smoke.md`](../.claude/agents/translation-smoke.md)

具体规则是：测试跑批委派给 Sonnet + medium；不要为了省钱改用 Haiku；真实模型 API 成本由 Translator 的文本/图像/审核角色及 CLI 覆盖决定；排版和语义验收不能因机械核查通过而跳过。

## 当前 style 在哪里

这里的 “style” 不是模型内部不可导出的个性，而是可见、可版本化的三层：

1. **软件内风格**：[`config.py`](../config.py) 的 `DOMAIN_PRESETS`，加上 `settings.json` 中 Excel/Word 分别保存的领域选择和用户 Prompt 覆盖；组合逻辑在 [`core/engine_dispatcher.py`](../core/engine_dispatcher.py) 的 `get_system_prompt()`。
2. **skill 工作流风格**：各 `SKILL.md` 对文件路由、模型、输出、复核和交付格式的约束。这层是目前直接 skill 翻译的主要行为来源。
3. **用户校正后形成的项目风格**：目前散落在会话与成品中，尚未被结构化保存。这正是软件最缺的一层。

因此，优化目标不应是“复制 Codex 的 style”，而是把第三层沉淀成用户可见、可编辑、可审计的 `style profile`：领域、语言对、目标读者、保留规则、术语集、示例、版本和生效范围。

## 建议的软件数据模型

建议新增独立的“修订经验库”，与 TM 并列而非混写：

```text
TM / glossary
  解决：这个词、短语或重复句应该怎么译

Style profile
  解决：这类文件对谁说、用什么语气、保留哪些结构和格式

Revision case
  解决：某次真实错误为什么错、如何改、适用范围是什么

Evaluation case
  解决：新版模型/Prompt/代码是否重犯历史错误

Translation job state
  解决：多轮“同上/继续/局部修改”如何安全承接
```

一个最小的 `revision_case` 可以是：

```json
{
  "case_id": "rev-...",
  "source_job_id": "job-...",
  "document_type": "docx|xlsx|pdf|pptx",
  "language_pair": "zh-fr",
  "scope": {"page": 3, "node_id": "...", "range": "..."},
  "issue_type": "omission|terminology|style|layout|unsafe_replace|routing",
  "instruction": "用户确认的修订要求",
  "before": "经批准保留的最小必要片段",
  "after": "经批准的修订结果",
  "style_profile_id": "engineering-fr-v1",
  "accepted_by": "user",
  "created_at": "..."
}
```

## 建议的沉淀流程

```text
外部 skill 翻译完成
  -> 产物 + manifest/report + job snapshot
  -> 用户给出反馈
  -> 选择反馈类型、影响范围和是否已接受
  -> 生成最小 revision case
  -> 人工审核后：写入 TM / style profile / eval case（可多选）
  -> 后续任务复用，并在回归测试中验证
```

关键约束：默认不自动学习。一次模型输出、一次口头意见或一次未确认的修正都不能自动污染 TM 和风格库。

## 第一批应抽取的经验

不要试图把所有会话一次性清洗。建议先从 20 到 40 个已经完成且用户实际给过修订意见的任务中，人工抽取下列高价值案例：

1. 工程中法/中英术语和固定表达的最终确认版本。
2. 施工方案、工程联系函、图纸清单、台账、报告等不同体裁的语气与双语排版样例。
3. “漏译”“局部覆盖丢内容”“表格或文本节点未覆盖”“同上无法续做”等失败样例。
4. 每类至少一个“原始输出 -> 用户反馈 -> 最终接受输出”的完整闭环。
5. 模型/Prompt/Skill 版本、人工验收结论与产物质量的对应关系。

这些案例先构成小而可信的评测集，之后再决定是否批量挖掘历史会话。比起上万条未经审核的历史消息，几十条结构完整、已接受的修订案例对软件改进更有效。

## 近期落地顺序

1. 新增 `translation_job` 与 `revision_case` 的本地 schema，并使每次软件内翻译可导出 manifest。
2. 在结果页增加“接受修订并沉淀经验”：选择写入 TM、style profile 或 eval case；默认都不写。
3. 为审校增加真实覆盖率，禁止对抽样/截断内容给全量结论。
4. 为局部修改增加范围写入、前后 diff 和保留性验证；禁止含糊的整段替换。
5. 实现可恢复的任务状态，让多轮“同上/继续另一页”可靠承接。
6. 从外部 skills 的已验收任务中手动建第一批 20 到 40 个 revision/eval cases，再用它们驱动 Prompt、路由和写入逻辑的回归测试。

## 数据安全与边界

- 原始会话可以用于人工检索和案例抽取，但不要整体导入、提交或上传。
- API Key、`config.env`、私有 Base URL、未经授权的项目正文和模型原始响应不进入经验库。
- 经验库只保存完成目标所需的最小片段，并记录来源、授权状态和删除路径。
- 输出文件是否“翻译完成”与内容是否“已被用户接受”必须分别记录。
- Translator 现有 TM 是补充资产；本机现存库为旧 schema，另行迁移时应只读备份、审核后导入，不能篡改版本号强行启用。

## 交接入口

- 直接 skill 的工作流：
  - `/Users/lijianwei/.codex/skills/file-translate/SKILL.md`
  - `/Users/lijianwei/.codex/skills/excel-translate/SKILL.md`
  - `/Users/lijianwei/.codex/skills/word-translate/SKILL.md`
  - `/Users/lijianwei/.codex/skills/pdf-image-translate/SKILL.md`
- 软件现有风格与 Prompt：[`../config.py`](../config.py)、[`../core/engine_dispatcher.py`](../core/engine_dispatcher.py)
- 已记录的跑批模型策略：[`../.claude/agents/translation-smoke.md`](../.claude/agents/translation-smoke.md)
- 本地项目偏好记录：`/Users/lijianwei/.claude/projects/-Users-lijianwei-vibecoding-claude-XL-Translator/memory/`

本文件是经验交接和产品设计依据，不包含原始翻译内容或密钥。后续的数据整理应以“用户已接受的修订闭环”为单位，而不是以聊天记录或单句模型输出为单位。
