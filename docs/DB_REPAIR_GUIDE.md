# 数据库修复说明（与当前代码对齐）

最后更新：2026-05-25

## 当前真实情况

当前仓库中 **没有独立的 `repair_db.py` 修复脚本**。

数据库结构修复由应用启动流程自动完成：

- Python sidecar 在记忆库 API 首次调用时通过 `core.tm_manager.init_db()` 初始化数据库。
- `init_db()` 会先检查当前 TM schema 版本；如果检测到旧版数据库，会先备份旧库，再重建新 schema 并迁移旧词条
- 随后统一确保当前表结构与索引存在，并回填 `source_hash` 为空的历史数据
- 旧库备份会写到平台原生应用数据目录下的 `backups/tm/`

## 推荐处理方式

1. 正常启动应用。
2. 若这是该机器第一次运行 V5.0 原生版，按提示确认旧数据迁移。
3. 观察启动日志是否出现数据库迁移、备份或 `source_hash` 回填信息。
4. 若希望显性查看源码启动过程，请在 `src-tauri` 目录运行 Tauri dev。
5. 若仍异常，请保留报错截图和日志，反馈排查。

## 不要使用的旧指令

以下旧路径在当前仓库不存在，请勿执行：

- `python src/scripts/repair_db.py`
- `python src/scripts/launcher.py`
- `streamlit run app.py`（V5.0 起已无网页入口）

以上路径会导致“文件不存在”错误。
