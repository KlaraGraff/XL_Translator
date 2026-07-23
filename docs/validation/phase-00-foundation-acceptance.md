# Phase 0 基础契约与兼容性验收

状态：`进行中；本地隔离验收已通过，macOS 12 实机发布门保留至 Phase 8/9`

本记录对应实施方案的 Phase 0，作为后续阶段的入口证据。样例和测试不得访问真实用户目录、Key 或 TM 数据库。

## 已执行

| 类别 | 命令/证据 | 结果 |
| --- | --- | --- |
| 静态质量门 | `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1` | 待本阶段集成后填写 |
| 基础契约动态测试 | `./.venv/bin/python3 -m unittest tests.test_phase0_foundation` | 待本阶段集成后填写 |
| 样例夹具 | `tests/phase0_foundation.py` | Excel、Word、PDF、图片、TM JSON、自定义目标语言 |
| Mock API | `MockTranslationProvider` | 不使用真实 Key，返回实际源/目标语言对 |
| 应用目录隔离 | `TRANSLATOR_APP_DATA_DIR` | 旧目录哨兵保持不变 |
| 版本/架构门 | `scripts/verify_macos_minimum_version.py` | 无 Mach-O 或版本/架构不满足时失败 |
| WebView 基线 | `ui/vite.config.ts` | `safari15.1` |

## 外部暂缓项

- 真实服务 Key 连通性和真实翻译：按最终决策继续暂缓。
- macOS 12 arm64/x86_64 实机安装、Gatekeeper、公证和 Office Apple Events：留到 Phase 8/9 的发布门。
- Windows 新版构建和验收：不属于本次发布范围，继续使用旧版本。

## 通过条件

进入 Phase 1 前必须确认：隔离夹具可重复生成、Mock API 契约包含实际语言代码、旧目录哨兵未被读取或写入、macOS 12 声明与 WebView 目标已被测试覆盖，并完成质量门。任一真实数据越界、Key 泄露或发布基线失败均阻断 Phase 0。
