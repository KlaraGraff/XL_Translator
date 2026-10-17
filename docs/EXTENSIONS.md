# 制作和分享扩展

扩展（格式适配器）让 Translator 翻译一种新的文件格式。扩展只负责两件事：从文件里取出要翻译的文字，再把译文写回去。调用模型、记忆库、重试、复核、进度和任务中心都由 Translator 负责。

本文写给扩展作者。实现细节见 [ARCHITECTURE.md](ARCHITECTURE.md)「格式适配器」「安装与子进程宿主」「扩展列表」三节。

- 扩展模板：https://github.com/KlaraGraff/XL_Translator-extension-template
- 扩展列表：https://github.com/KlaraGraff/XL_Translator-extensions

## 一个扩展由什么组成

```
<id>/
  adapter.json   清单：类型名称、处理哪些扩展名、页面上显示什么
  adapter.py     scan（可省）、extract、write 三个函数
  samples/       两三份小样例，自测和扩展列表的检查都用它们
  LICENSE、README.md（可选）
```

### adapter.json

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 小写字母、数字、`-`、`_`，最长 40 个字符；页面键为 `ext:<id>`；发布后不要再改 |
| `version` | 是 | 版本号，如 `0.1.0`；发布标签是 `v<version>` |
| `adapter_api_version` | 是 | 目前是 `1` |
| `label`、`file_noun` | 是 | 类型名称、文件的叫法，`{"zh", "en", "fr"}` 三种语言 |
| `extensions` | 是 | 处理的扩展名，不带点 |
| `description` | 建议 | 页面「?」里的说明，三种语言；提交到扩展列表时必须有 |
| `prompt` | 否 | 这种文件的内置规则（最长 4000 字），省略时用 Translator 通用的那份；用户可在「设置 → 翻译参数」里改写 |
| `capabilities` | 否 | `{"translation_only": true, "bilingual": true}`，至少一种；两种都支持时页面有「双语对照」开关 |
| `options` | 否 | 页面上的开关：`{"key", "label", "help", "default"}`；`bilingual`、`target_lang` 由应用提供，不能用作 key |
| `icon`、`license`、`repo`、`min_app_version` | 否 | 图标名、许可证、项目主页、需要的最低 Translator 版本 |
| `visible_by_default`、`sidebar_after` | 否 | 只对随 Translator 提供的类型生效：没在「扩展」页拨过时也显示在侧栏、默认顺序里紧跟哪个类型（如 `word`）。装进来的扩展写了也不起作用（装上时本来就显示在侧栏） |

清单不认识的字段一律拒绝，也不支持 `requires`（扩展不能依赖第三方库）。

### adapter.py

```python
from translator_adapter import MODE_BILINGUAL, OutputFile, ReviewItem, ScanItem, TextUnit, TranslatedUnit

def extract(path: str, options: dict | None = None) -> list[TextUnit]: ...
def write(path: str, units: list[TranslatedUnit], mode: str, out_path: str, options: dict) -> OutputFile: ...
def scan(path: str, options: dict | None = None) -> ScanItem: ...  # 可省，省略时按 extract 计数
```

- `extract` 按阅读顺序返回 `TextUnit(id, text, context, kind, locked, protected, marked)`。`id` 在文件内唯一，同一个文件每次取出的 `id` 相同；`context` 是给人看的位置（如「第 12 行」），写进需复核清单。整段不该翻译的设 `locked=True`；段内必须原样保留的片段（占位符、行内代码、链接地址）放进 `protected`。
- `write` 只读 `path`，写到 `out_path`。`units` 是 `TranslatedUnit(id, text, translation, status, marked_translation)`，没译成的和锁定的 `translation` 就是原文。`mode` 是 `translation_only` 或 `bilingual`，`options` 是本次任务的开关值，另带 `target_lang`（目标语言代码，如 `fr`），写回时改语言属性、断行规则可以用它。文字以外的内容（结构、编码、换行符）尽量逐字节保留。
- `scan`、`extract` 的第二个参数 `options` 可省：写了的，拿到与 `write` 相同的开关值和 `target_lang`（例如开关关掉的内容在取字时就设 `locked=True`，不送去翻译）；只收 `path` 的照旧调用。
- 写回时发现需要人看的地方（放不下、竖排里放了西文），放进 `OutputFile.review_items`：每项 `ReviewItem(reason, id, context)`（或同样键的 dict），`reason` 是给人看的原因，`id` 是相关单元（需复核清单里显示它的原文），`context` 是位置（省略时用该单元的 `context`）。这些条目逐条进需复核清单；只给 `review_count` 的照旧只计数，两样都给时取大的。
- 段内格式（可选）：一段里有几截格式和主体不同（加粗、斜体、链接）时，把这几截用成对的 `<g1>…</g1>`、`<g2>…</g2>` 包住放进 `marked`，去掉标记必须与 `text` 一字不差，标记不能嵌套、不能跨行，一段最多 12 对；不满足时 Translator 按没有标记处理。模型会把标记放到译文对应的位置，`write` 拿到的 `marked_translation` 是带标记的译文（去掉标记就是 `translation`），按标记把每截写回原来的格式；它为空时（标记没能完整回来）把 `translation` 整段按主体格式写回。没有段内格式的段落 `marked` 留空。
- 时限：`scan` 每个文件 30 秒，`extract`、`write` 每个文件 120 秒。超时或进程崩溃时只有这个文件失败。

## 限制

Translator 安装前会检查这些规则，违反的装不上：

- 只用 Python 标准库和 `translator_adapter`（随 Translator 提供的类型在应用进程里运行，可以导入应用模块和 lxml，不受这一条限制）。
- 不能导入联网模块（`socket`、`ssl`、`http`、`urllib`、`requests`、`httpx`、`asyncio` 等）、启动其他程序的模块（`subprocess`、`multiprocessing`、`pty`）、加载原生代码的模块（`ctypes`、`cffi`）和动态加载代码的模块（`importlib`、`pickle`、`marshal`、`runpy` 等）。
- 不能调用 `exec`、`eval`、`compile`、`__import__`、`os.system`、`os.popen`、`os.fork`、`os.exec*`、`os.spawn*` 等。
- 扩展包里只能有 `.py`、`.json`、`.md`、`.txt` 和 `LICENSE`（`samples/` 下不限）；不能有原生库、可执行文件、符号链接；合计不超过 2 MB、200 个文件。
- 顶层的 `dist/`、`build/`、`scripts/`、`tests/`、`node_modules/` 和隐藏文件不进扩展包。

这些检查只拦明显越界的写法，不是沙箱。扩展在单独的子进程里运行，崩溃或卡住不会拖垮 Translator；Translator 不把数据目录的位置、设置或任何密钥交给子进程，工作目录是用完即删的临时目录。但它仍以用户的权限运行，能读写用户的文件，所以 Translator 每次安装前都会提醒用户只安装信任的来源。

## 制作

1. 在模板仓库点「Use this template」建一个自己的仓库，或者运行 `translator-sidecar adapter new <id>`，在当前目录下的 `<id>` 文件夹里生成同样的文件。模板本身就是一个能用的扩展（翻译「键 = 值」形式的 `.myfmt` 文件），照着改成你的格式即可。
2. 修改 `adapter.json`、`adapter.py`，在 `samples/` 放样例。Claude Code、Codex 等编程助手会读模板里的 `AGENTS.md`，可以让它们照着写。
3. 自测：

   ```
   translator-sidecar adapter test .
   ```

   它做与安装时相同的检查，再在子进程里用 `samples/` 跑一遍 `scan`、`extract`、`write`（两种输出都试；带 `marked` 的段落也把带标记的译文交给 `write`），核对以原文作译文写回后再取字与原文一致。全部通过时退出码为 0；加 `--json` 输出一个 JSON 对象。随 Translator 提供的类型（源码 `adapters/<id>/`，加 `--skip-id-check`）在本进程里试，与应用里的运行方式相同。
4. 试用：在 Translator 的「扩展」页点「从文件夹添加…」选这个文件夹，或运行 `translator-sidecar adapter install . --yes`。侧栏出现新类型后，翻译一份真实文件看看效果。改了代码后再添加一次即可更新。

`translator-sidecar` 是 Translator 自带的命令行：macOS 在 `/Applications/Translator.app/Contents/Resources/sidecar/translator-sidecar/translator-sidecar`，Windows 在 `%LOCALAPPDATA%\Translator\sidecar\translator-sidecar\translator-sidecar.exe`。有 Translator 源码时，也可以在源码目录运行 `python -m cli adapter …`。

## 分享

### 提交到扩展列表

1. 把 `adapter.json` 的 `version` 改成新版本号，提交并推送。
2. 打标签并推送，标签须与 `version` 一致：`git tag v0.1.0`，`git push origin v0.1.0`。
3. 模板自带的 Release 工作流打出 `<id>-<version>.zip` 和 `.sha256`，附到这个标签的 Release，并在日志的「Registry entry」一步打印扩展列表的条目。
4. 向扩展列表仓库提一个 PR，新增 `extensions/<id>.json`，内容就是上一步的条目。

扩展列表的自动检查会下载 zip、核对大小和 SHA-256，并运行同一个 `adapter test`；通过后维护者阅读源码，合并时填写 `status`（`reviewed`）和审核信息。合并后，所有用户都能在「扩展」页的「浏览扩展」里搜到并安装。审核和下架规则见扩展列表仓库的 README。

### 直接发给别人

`translator-sidecar adapter pack .`（或模板里的 `python scripts/pack.py`）在 `dist/` 下打出 zip。对方在「扩展」页点「添加扩展…」选这个 zip。

## 用户那边发生什么

- **安装前**：Translator 先检查扩展包，再弹出确认框，列出名称、版本、文件类型、许可证、来源和告警，并写明扩展以用户权限运行。检查没通过的会逐条列出原因，不能安装。从扩展列表安装时，下载的 zip 须与条目的大小和 SHA-256 一致。
- **安装后**：扩展复制到应用数据目录的 `plugins/adapters/<id>/` 下（`samples/` 不复制），登记在 `plugins/adapters/installed.json`，新类型立即出现在侧栏（用户关过它的不动）。翻译参数里多一个这个类型的页签，`prompt` 是它的默认内置规则。
- **翻译页**：和 Word 页同一套：目标语言、源语言、补充要求、输出位置（「高级」里，可选写在来源文件夹旁或自定义文件夹），存在 `settings.extensions[<id>]`。扫描后文件表列出每个文件 `scan` 数出的段数和 `warnings`（整句，如「3 张图片中的文字未翻译」，界面按整句翻译成界面语言，认不出的原样显示）。
- **仅译文时的混合内容**：原文是中文、目标是别的语言、选了「仅译文」时，已经夹着外文的内容（「土石方工程合计 Total of Earthworks」）与 Excel、Word 同一套规则整体翻成目标语言；翻不出的保留原文、列入需复核清单，译文里仍夹着没翻的外文的保留译文、列入需复核清单。双语对照不受影响。
- **更新和卸载**：「浏览扩展」里有新版本时显示「更新」；翻译类型列表里装进来的类型旁边有「卸载」。类型还有任务在进行或已暂停时，不能更新或卸载。卸载后这个类型的翻译参数保留，重新安装后恢复。

## 命令行速查

| 命令 | 作用 |
| --- | --- |
| `adapter new <id> [--dir 目录]` | 生成扩展骨架（与模板仓库的文件相同） |
| `adapter test <目录或 zip> [--json]` | 安装前检查加样例试运行；退出码 0 通过、1 有问题 |
| `adapter install <目录或 zip> [--yes]` | 打印确认文字；加 `--yes` 才安装 |
| `adapter uninstall <id>` | 卸载 |
| `adapter pack <目录> [--out 目录] [--tag v0.1.0] [--repo 地址] [--author 名字]` | 先检查再打包，写出 zip、`.sha256` 和扩展列表条目 |
