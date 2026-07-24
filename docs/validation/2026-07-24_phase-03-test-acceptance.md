# Phase 3 模型模块：测试线验收记录

状态：`passed（L3 测试线；真实服务 Key 与 Windows 验收按已冻结范围暂缓）`

日期：2026-07-24
对应决策：`M3A-01–09`、`M3B-01–10`、`M3C-01–11`

## 已验证行为

- 使用本地 Mock OpenAI 兼容服务验证翻译角色连通性。成功状态绑定当前有效签名；变更模型后，持久化的已测试签名会清空并显示为未测试。
- 使用 Mock 清洗引擎验证复用翻译连接时仍使用清洗模型名，并校验最小 JSON 数组、`id`、`suggested` 协议；状态、签名和时间写回原始清洗角色，而非临时翻译配置副本。
- 验证图像生成和 PDF 审核角色各自通过 Mock 客户端的专属协议测试；模型目录或文本能力不能替代角色能力检查。
- 验证文本能力服务商不能保存为 PDF 图像生成角色；一层复用、禁止链式复用、云端专用角色禁止跟随本地翻译模型由回归集覆盖。
- 验证 Excel/Word 独立领域状态、空自定义 Prompt 拒绝、翻译固定输出协议与清洗不可覆盖 JSON 协议。
- 验证吞吐档案以角色和有效连接/模型隔离；文本角色支持批次与并发，PDF 角色仅允许并发；任务启动后冻结模型、吞吐、API Key 作用域、目标语言与页面领域快照。
- 验证只接受 `translator_model_config v3`；稀疏导入保留未提及角色/连接记忆，导入值生效，导入前验证完整有效角色图，失败时不写入 Key；成功导入后所有四个角色均重置为未测试。
- 验证默认 v3 导出不含 Key；导出 Key 需要显式参数和敏感确认。

## 实际执行

所有 Python 动态测试均使用仓库 `.venv`，并隔离 `HOME`、`USERPROFILE`、`TEMP`、`TMP` 与 `TRANSLATOR_APP_DATA_DIR`。

```text
./.venv/bin/python3 -m unittest -v tests.test_phase3_acceptance
# Ran 8 tests — OK

./.venv/bin/python3 -m unittest -v \
  tests.test_phase3_acceptance \
  tests.test_phase3_model_contracts \
  tests.test_model_roles \
  tests.test_connectivity_check \
  tests.test_model_catalog \
  tests.test_model_api_identity \
  tests.test_api_app
# Ran 67 tests — OK

powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1
# All checks passed!
```

隔离动态测试产物（不纳入版本控制）：

- `.runtime/self-tests/phase-03-model-acceptance/artifacts/unittest-output-rerun-2.txt`
- `.runtime/self-tests/phase-03-model-regression/artifacts/unittest-output.txt`

## 本线未覆盖项

- 未调用任何真实服务商或真实 API Key；本记录只证明请求构造、协议、状态失效和敏感边界。真实 Key 连通性仍需由用户主动在后续验收中授权。
- 未在 Windows 执行测试或打包；Windows 新版本支持仍按冻结范围暂缓。
- 本记录不代替 UI 线的 TypeScript 构建、隔离 Tauri 壳冒烟或最终 Phase 3 汇总门禁。
