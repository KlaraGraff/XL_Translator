# ASXS Responses 适配修复进度

## 任务目标

修复当前项目在 `https://api.asxs.top/v1` 上使用 `gpt-5.4` 时，`/v1/chat/completions` 返回 `200` 但 `choices[0].message.content` 为空，导致翻译链路批量解析失败的问题；将链路切换到可正常返回正文的 `/v1/responses` 主线路，并尽量保持现有调用方与 UI 行为稳定。

## 当前结论基线

- 已确认当前 `custom_openai` 配置使用 `https://api.asxs.top/v1`。
- 已确认 `/v1/chat/completions` 在该服务上会返回 `200`、有 token 计费，但 `message.content` 缺失。
- 已确认 `/v1/responses` 主线路在流式模式下能正常返回正文。
- 本轮目标优先修复 `custom_openai / gpt-5.4 / asxs.top` 实际翻译链路，不顺手扩散到无关 provider。

## 任务清单

- [x] 创建任务追踪文档并初始化内容
- [x] 启动防休眠程序并记录 PID
- [x] 审查当前 `OpenAIEngine` 与调度层的改动范围，确定最小切入点
- [x] 为 `custom_openai` / ASXS 主线路接入 `/v1/responses`
- [x] 保持现有批量翻译返回协议兼容，不破坏调用方
- [x] 为 Responses 返回增加可解析正文抽取逻辑
- [x] 为异常场景补充日志，能区分“空正文 / 非法正文 / 上游错误”
- [x] 增加至少 1 个与本次修复直接相关的动态测试
- [x] 运行 `quality_gate.ps1`
- [x] 运行动态测试并确认覆盖结果
- [x] 更新文档收尾总结
- [ ] 关闭防休眠程序

## 风险与约束

- 不修改与本轮无关的 provider 实现，避免扩大回归面。
- 如需改动共享抽象层，优先保持旧调用签名和旧 provider 行为不变。
- 测试必须使用项目 `.venv`，涉及用户配置/keys 的探针需要继续隔离。
- 若发现 `responses` 适配必须扩大到多 provider 共享抽象，先在本文档记录再继续实施。

## 运行信息

- 防休眠状态：运行中
- 防休眠 PID：`4989`

## 进度日志

- 2026-04-12 01:46: 已创建任务追踪文档，写入目标、清单、风险与日志区。
- 2026-04-12 01:46: 已启动防休眠程序 `caffeinate -dimu`，PID=`4989`。
- 2026-04-12 01:52: 已确认最小切入点只需调整 `engines/openai_engine.py`，无需扩散到调度层与其他 provider。
- 2026-04-12 01:52: 已为 `api.asxs.top` 接入 `/v1/responses` 流式读取，并保持其他 OpenAI 兼容 provider 继续走原 `chat.completions` 路线。
- 2026-04-12 01:52: 已补充 Responses SSE 正文抽取与“成功但无正文”异常提示，同时让 `chat()` 共享同一条 ASXS Responses 主线路。
- 2026-04-12 02:05: 已新增动态测试脚本 `.runtime/self-tests/asxs-responses-engine/check_asxs_responses_engine.py`，用于隔离环境下直连当前 ASXS 配置做法语转中文 smoke。
- 2026-04-12 02:07: 已运行 `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1`，结果：`All checks passed!`。
- 2026-04-12 02:08: 已运行隔离动态测试 `Run-IsolatedVenvPython.ps1 -TaskSlug asxs-responses-engine ...`，返回结果：
  - `Débit 30 à 120m3/h -> 流量 30 至 120m3/h`
  - `Avertisseur d'Alarme -> 报警器`
- 2026-04-12 02:11: 收尾结论：
  - 当前故障根因已绕开：`api.asxs.top` 上的 `/v1/chat/completions` 对 `gpt-5.4` 返回 `200` 但缺失 `message.content`。
  - 当前修复策略：对该主机直连 `/v1/responses` 并按 SSE `response.output_text.delta` / `response.output_text.done` 抽取正文。
  - 回归面控制：仅 `api.asxs.top` 走新路径，其他 OpenAI 兼容 provider 仍保留旧实现。
