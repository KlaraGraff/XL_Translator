# Agent Self-Testing Kit

`agent/` 是本项目长期保留的 Agent 自测资产目录。以后无论是谁接手，只要先看这个目录，就能找到规则、运行方式和可复用的测试容器。

建议阅读顺序：
1. 根目录 `AGENTS.md`
2. `agent/SELF_TESTING_PLAYBOOK.md`
3. `agent/testing/README.md`

目录说明：
- `SELF_TESTING_PLAYBOOK.md`：完整自测规则与执行细节。
- `testing/README.md`：测试资产目录说明与复用指引。
- `testing/Run-IsolatedVenvPython.ps1`：隔离 `HOME` / `USERPROFILE` / `TEMP` / `TMP` 后再用项目 `.venv` 运行 Python 脚本。

默认交付门：
1. 先跑 `quality_gate.ps1`。
2. 再跑至少 1 个与改动直接相关的动态测试。
3. 交付时说明“实际跑了什么、覆盖了什么、还有什么没覆盖”。

复用到新项目时，优先复制以下内容：
1. 根目录 `AGENTS.md`
2. 整个 `agent/` 目录
3. 按新项目结构更新 `quality_gate.ps1`
4. 按新项目 UI 技术栈补充对应的动态测试脚本

不要把这套长期规则和一次性的验证过程文档混放；过程记录继续放 `docs/validation/...`，长期规则始终保留在根目录 `AGENTS.md` 与 `agent/` 目录。
