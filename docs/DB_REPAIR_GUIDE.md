# 数据库修复说明（与当前代码对齐）

最后更新：2026-04-06

## 当前真实情况

当前仓库中 **没有独立的 `repair_db.py` 修复脚本**。

数据库结构修复由应用启动流程自动完成：

- `app.py` 启动时调用 `core.tm_manager.init_db()`
- `init_db()` 会先检查当前 TM schema 版本；如果检测到旧版数据库，会先备份旧库，再重建新 schema 并迁移旧词条
- 随后统一确保当前表结构与索引存在，并回填 `source_hash` 为空的历史数据
- 旧库备份会写到 `~/.xl_translator/backups/tm/`

## 推荐处理方式

1. 正常启动应用。
2. 若这是该机器的第一次启动，通常会先看到可见命令行窗口；后续启动通常为静默模式。
3. 观察启动日志是否出现数据库迁移、备份或 `source_hash` 回填信息。
4. 若希望显性查看完整启动过程，也可直接运行 `scripts/start_macos.command`。
5. 若仍异常，请保留报错截图和日志，反馈排查。

## 不要使用的旧指令

以下旧路径在当前仓库不存在，请勿执行：

- `python src/scripts/repair_db.py`
- `python src/scripts/launcher.py`

以上路径会导致“文件不存在”错误。
