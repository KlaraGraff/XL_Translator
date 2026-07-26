# 交接：模型配置「连接方式」三段式 + 跟随图重构

## 工作目录

**`/Users/lijianwei/vibecoding/claude/XL_Translator`**（不是 BTR project plan，上一轮是从那个目录起的会话）。
先读 `AGENTS.md`：本项目要求「改完先自测再交付」，并规定了 GitHub 同步规则（remote 必须是
`https://github.com/KlaraGraff/XL_Translator`，默认分支 `main`，不要新建仓库或分支）。

## ⚠️ 并发风险（务必先确认）

有**另一个 Claude 会话在同时改这个仓库**。上一轮编辑 `api/app.py` 时收到过「文件已在磁盘上变更」
的警告，且 `core/task_logger.py`、`core/task_history.py`、`settings.py` 在 09:05 被别的进程改过。

工作区里**混着两批未提交改动**，不要整体 revert 或 `git checkout .`：

| 属于本任务 | 属于另一个会话 |
| --- | --- |
| `core/model_roles.py`、`core/model_config.py` | `core/file_scanner.py`、`core/task_history.py`、`core/task_logger.py` |
| `tests/test_followed_connection_pool.py`（新增） | `tests/test_phase7_scheduler_contracts.py`、`tests/test_settings_api_keys.py`、`tests/test_task_history.py` |
| `tests/test_connection_pool_api.py`、`tests/test_model_roles.py`、`tests/test_phase3_model_contracts.py` | |
| `api/app.py`、`settings.py`、`ui/src/main.ts`、`ui/src/tokens.css` — **这四个文件两批改动都有** | 同左 |

## 用户的原始需求（两张截图批注）

1. Word / Excel（`translation` 角色）的「连接方式」应该也支持跟随其他角色。
2. PDF 翻译（图像生成）的「连接方式」应该和其他一样有三类选项：跟随 / 本地模型 / 云端 API。
3. 跟随时，连接列表应显示**被跟随项的名称**（截图里显示 `custom_openai`，实际连的是 `DeepSeek`）。

## 已确认的两个决策（用户已选，不要再改）

- **本地模型只加给「记忆库清洗」（`cleaner`）**。PDF 图像生成 / 译文审核保持云端：能力分别是
  `image` 与 `vision_text`，`core/image_generation.py` 只有云端实现，provider 白名单只有
  `openai / custom_openai / siliconflow`。面板要在问号里**说明原因**，而不是给一个必然失败的选项。
- **允许任何角色跟随任何「当前是独立配置」的角色**（包括 translation 跟随别人）。链式跟随仍然禁止，
  自己跟随自己禁止，保存时做全图校验。

## 已完成（全部已自测通过）

### 1. 跟随时的连接列表（需求 3）—— 根因与修复

根因：`_model_role_payload` 一直返回角色**自己**的连接池，而跟随时实际拨号用的是来源角色的凭据。
自己池子里标签为空 → `display_label` 回退到 provider 名 → 显示 `custom_openai`。实测复现：

```
image own pool      : ['custom_openai']
image effective pool: ['DeepSeek']
```

- `core/model_roles.py`：新增 `pool_role()`、`list_effective_role_connections()`、`role_source_role()`；
  跟随分支把 `connection_id` 透传给来源，并回报真实拨号的 `connection_id` / `connection_label`。
- `api/app.py`：载荷改用有效连接池，新增 `connection_pool_role`；四个连接增删改排接口加
  `_own_pool_or_422` 守卫（跟随时返回明确 422，而不是「找不到要删除的连接」）。
- `ui/src/main.ts`：跟随时列表只读，带「来自 X」标签，隐藏新增/删除/设为主用，连接名称与 API 密钥置灰。

### 2. 跟随图重构（需求 1）

- `settings.py`：`EngineSettings` 新增 `source_role`（默认 `independent`，不允许为 `translation`）；
  新增模块级 `MODEL_ROLE_SOURCE_VALUES`。
- `core/model_roles.py`：`allowed_source_roles(role, settings=None)` 改为对称规则——任何角色可跟随
  任何**其他**角色；传 `settings` 时进一步过滤掉「已经在跟随别人」的角色。`normalize_source_role`
  拒绝自跟随。`resolve_effective_model_config` 重构成统一三段：先跟随 → 再本地 → 再自己的连接池，
  translation 不再单独一条分支（它只是把值存在 `engine` 上）。
- `api/app.py`：`PUT /api/models/roles/{role}` 的两条分支合并为一条，用 `model_role_owner()`。

### 3. cleaner 本地模型（需求 2，按决策收窄）

- `settings.py`：`ModelRoleSettings` 新增 `mode` / `local_provider` / `local_model` / `local_base_url`。
  三个角色共用这个类，由 `validate_model_capability` 挡住 image / pdf_review 的本地模式。
- `core/model_roles.py`：新增 `LOCAL_CAPABLE_CAPABILITIES = {"text"}` 与 `_own_model_name()`
  （跟随共享 provider/endpoint/key，**从不共享模型名称**）；文本角色现在可以跟随一个本地来源。
- `api/app.py`：载荷新增 `source_role_options`、`supports_local`。

### 4. UI 统一「连接方式」控件

`ui/src/main.ts`：两个旧控件（`#engineMode`、`#roleSource`）合并为单个 `#accessMode`，值编码为
`"cloud"` / `"local"` / `"follow:<role>"`；新增 `state.modelAccessDraft`（切到本地或跟随会换掉整块
表单，必须先落状态再重绘，不能只改 DOM 的 disabled）；同步改了 `saveModel`、`modelFormDirty`、
`ensureSavedModelForm` 和 change 事件处理。**跟随列表为空时显示一句说明**——截图里正是这一点让用户
以为功能缺失。

### 验证结果（暂停时的状态）

```bash
cd /Users/lijianwei/vibecoding/claude/XL_Translator && ./.venv/bin/python3 -m pytest tests/ -q
```
`476 passed, 25 subtests passed` · `ruff check core/ api/ settings.py tests/` 全通过 ·
`cd ui && npx tsc --noEmit -p tsconfig.json` 通过。

因设计变更而**故意改动**的既有测试（不是修坏了）：
- `test_phase3_model_contracts.py::test_four_roles_have_explicit_capabilities_and_allowed_sources` — 旧的静态白名单换成对称规则。
- `test_model_roles.py` / `test_phase3_model_contracts.py` 的 `cloud_only_roles...` — cleaner 从「云端专用」名单里移出（它是 text 角色，可本地）。
- `test_connection_pool_api.py::test_pools_are_per_role` — 原来用默认跟随状态的 cleaner 来验证「每角色独立池」，现在先把它切成独立配置。

## 立刻要接着做的第一件事（改到一半）

`core/model_config.py` 的**导出已改成对称**（四个角色都写 `mode` / `source_role` / `local`），
但**导入端还没跟上**：`parse_model_config_import` 里仍然只对 translation 读 `mode` / `local`，
只对非 translation 读 `source_role`（约在 478–502 行的 `if role == ROLE_TRANSLATION: ... else: ...`）。

后果：一次导出再导入会丢掉 translation 的 `source_role`，以及 cleaner 的 `mode` / `local_*`。
请把导入端改成同样对称，并补一个**导出→导入 round-trip 测试**覆盖：
translation 跟随 cleaner、cleaner 本地模型这两种新状态。
（注意 `MODEL_CONFIG_ROLE_CLOUD_FIELDS` 和 `pdf_translation` ↔ `image` 的键名映射。）

## 其余待办

1. **`core/model_api_identity.py` 的连接分配**（上一轮**故意没动**，需要你和用户确认）：
   `task_api_context_for_page` 仍从 `list_role_connections`（角色自己的池）分配，而跟随角色实际
   用的是来源的池。改成 `list_effective_role_connections` 更一致，但会改变
   `spread_tasks_across_connections` 的并发/占用统计口径，属于运行时行为变更，可能影响限流与费用。
2. **界面自测**（AGENTS.md 规则 6 + 收尾动作，尚未做）：`ui/` 构建 + `src-tauri/` 检查，用隔离的
   `TRANSLATOR_APP_DATA_DIR` 起开发壳，逐个角色截图「连接方式」下拉，确认三段式、只读连接列表、
   「来自 X」标签、跟随列表为空时的说明。macOS 命令：`cd src-tauri && ../ui/node_modules/.bin/tauri dev`。
   注意 `/Applications/Translator.app` 有个跑了 2 小时的旧进程（PID 14753/14759）——**因为有另一个
   会话在用这个仓库，上一轮没有去关它**，动手前先跟用户确认。
3. **`quality_gate.ps1` 未执行**（PowerShell 脚本，本机是 macOS）。请与用户确认替代做法。
4. **文档**：`docs/upgrade-functional-migration-decisions.md`、
   `docs/upgrade-functional-migration-implementation-plan.md`、`CONTEXT.md` 里关于「只能跟随翻译模型 /
   不能链式复用 / 非翻译角色只有云端」的描述已经过时，需要核对更新。
5. **提交**：未提交任何内容。提交前必须把本任务的改动和另一个会话的改动区分开（见上表）。

## 复现用的小脚本（验证跟随池）

```bash
cd /Users/lijianwei/vibecoding/claude/XL_Translator && ./.venv/bin/python3 - <<'PY'
import tempfile, pathlib
from unittest.mock import patch
import settings as sm
root = pathlib.Path(tempfile.mkdtemp())
with patch.object(sm,"APP_DATA_DIR",root), patch.object(sm,"KEYS_PATH",root/"keys.json"), patch.object(sm,"SETTINGS_PATH",root/"settings.json"):
    from core.model_roles import list_role_connections, list_effective_role_connections, pool_role
    s = sm.load_settings()
    s.engine.connections[0].label = "DeepSeek"
    s.image_model_role.source_role = "translation"
    print("own      :", [c.display_label for c in list_role_connections(s,"image")])
    print("effective:", [c.display_label for c in list_effective_role_connections(s,"image")])
    print("pool_role:", pool_role(s,"image"))
PY
```
