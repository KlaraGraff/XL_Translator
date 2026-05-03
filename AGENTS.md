# Agent Instructions

本项目默认启用“改完先自测再交付”。

先读入口：
- `agent/README.md`
- `agent/SELF_TESTING_PLAYBOOK.md`
- `agent/testing/README.md`

硬性规则：
1. 任何代码改动交付前，必须先运行 `powershell -ExecutionPolicy Bypass -File .\quality_gate.ps1`。
2. 静态检查通过后，必须再执行至少 1 个与本次改动直接相关的动态测试。
3. 一律优先使用 `./.venv/bin/python3`；不要混用系统 Python。首次 bootstrap 仅允许用于创建 `.venv`。
4. 涉及 `settings.json`、`keys.json`、TM 数据库、临时目录或用户目录的测试，必须先做隔离。
5. 动态测试产物统一放到 `.runtime\self-tests\<task-slug>\`。
6. Streamlit 页面优先使用 `agent/testing/wrappers/` 下的页面包装器配合 `streamlit.testing.v1.AppTest`。
7. 如果无法完成测试，交付前必须明确说明未执行项、阻塞原因和风险范围。

复用这套规则到新项目时，优先复制根目录 `AGENTS.md` 和整个 `agent/` 目录，再按新项目实际结构调整 `quality_gate.ps1` 与页面包装器。
