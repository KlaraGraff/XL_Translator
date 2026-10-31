# 开发说明（写给编程助手）

这个仓库是 Translator 的一个扩展（格式适配器），让 Translator 能翻译一种新的文件格式。要改的只有 `adapter.json`、`adapter.py` 和 `samples/`。完整的作者说明见 [README.md](README.md)。

## 协议（adapter_api_version 1）

`adapter.json` 字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 小写字母、数字、`-`、`_`，最长 40 个字符；发布后不改 |
| `version` | 是 | 版本号，如 `0.1.0`；发布标签是 `v<version>` |
| `adapter_api_version` | 是 | 写 `1` |
| `label` | 是 | 类型名称，`{"zh", "en", "fr"}` 三种语言都要有 |
| `file_noun` | 是 | 文件的叫法，三种语言 |
| `file_noun_one` | 否 | 一个文件时的英文、法文单数叫法 |
| `extensions` | 是 | 扩展名列表，不带点，如 `["myfmt"]` |
| `description` | 建议 | 页面「?」里的说明，三种语言；提交到扩展列表时必须有 |
| `prompt` | 否 | 发给模型的内置规则（中文，最长 4000 字）；省略时用 Translator 通用的那份 |
| `capabilities` | 否 | `{"translation_only": true, "bilingual": true}`，至少一种 |
| `options` | 否 | 页面开关：`{"key", "label", "help", "default"}`，`label`、`help` 三种语言 |
| `icon` | 否 | 图标名，如 `doc-file` |
| `license`、`repo`、`min_app_version` | 否 | 许可证、项目主页、需要的最低 Translator 版本 |

清单不认识的字段一律拒绝；不支持 `requires`（不能依赖第三方库）。

`adapter.py` 从 `translator_adapter` 导入数据类型，提供三个函数：

```python
from translator_adapter import MODE_BILINGUAL, OutputFile, ScanItem, TextUnit, TranslatedUnit

def extract(path: str) -> list[TextUnit]: ...
def write(path: str, units: list[TranslatedUnit], mode: str, out_path: str, options: dict) -> OutputFile: ...
def scan(path: str) -> ScanItem: ...  # 可省，省略时按 extract 计数
```

- `TextUnit(id, text, context="", kind="paragraph", locked=False, protected=[])`
- `TranslatedUnit(id, text, translation, status)`，`unit.changed` 表示译文与原文不同
- `ScanItem(unit_count, warnings=[])`，`OutputFile(path, review_count=0)`
- `mode` 是 `"translation_only"` 或 `"bilingual"`（常量 `MODE_TRANSLATION_ONLY`、`MODE_BILINGUAL`）

规则：

- `extract` 按阅读顺序返回；`id` 在本文件内唯一，同一个文件每次取出的 `id` 相同（`write` 靠它找回位置）；`text` 不为空。
- 不该翻译、但要留在原位的片段（占位符、行内代码、链接地址）放进 `protected`，必须是 `text` 的子串；整段不该翻译的设 `locked=True`。
- `write` 只读 `path`，写到 `out_path`；每段写 `unit.translation`（没译成的、`locked` 的就是原文）；文字以外的内容（结构、格式、编码、换行符）尽量逐字节保留。`bilingual` 时同时写原文和译文，写法由格式决定。
- 时限：`scan` 每个文件 30 秒，`extract`、`write` 每个文件 120 秒，超时 Translator 会结束进程，这个文件记为失败。
- 扩展在单独的进程里运行，工作目录是临时目录，Translator 不会把数据目录或任何密钥交给它。`print` 的内容进 Translator 的日志，可以用来调试。

## 限制（安装前检查，违反就装不上）

- 只用 Python 标准库和 `translator_adapter`。
- 不能导入：`socket`、`ssl`、`http`、`urllib`、`requests`、`httpx`、`asyncio`、`ftplib`、`smtplib`、`xmlrpc`、`webbrowser` 等联网模块；`subprocess`、`multiprocessing`、`pty`；`ctypes`、`cffi`；`importlib`、`pickle`、`marshal`、`runpy`、`code`、`zipimport`。
- 不能调用：`exec`、`eval`、`compile`、`__import__`；`os.system`、`os.popen`、`os.startfile`、`os.fork`、`os.exec*`、`os.spawn*`、`os.posix_spawn`。
- 扩展包里只能有 `.py`、`.json`、`.md`、`.txt` 和 `LICENSE`；`samples/` 下可以是任何样例。不能有原生库、可执行文件、符号链接。合计不超过 2 MB、200 个文件。
- 顶层的 `dist/`、`build/`、`scripts/`、`tests/`、`node_modules/` 和隐藏文件不进扩展包，可以放开发用的东西。
- 不联网、不调用模型、不读写 `path` 和 `out_path` 以外的用户文件。

## 自测

```
translator-sidecar adapter test .          # 人看的结果
translator-sidecar adapter test . --json   # 一个 JSON 对象
```

它做的检查与安装时相同，再在单独的进程里对 `samples/` 中每个扩展名匹配的样例依次运行 `scan`、`extract`、`write`（两种输出都试），并核对：以原文作译文写回后再取字，应与原文一致；带标记的译文写回后再取字，每段都应带上标记。退出码 0 表示全部通过，1 表示有问题，问题逐条列出。

`translator-sidecar` 在 Translator 安装目录里（macOS：`/Applications/Translator.app/Contents/Resources/sidecar/translator-sidecar/translator-sidecar`；Windows：`%LOCALAPPDATA%\Translator\sidecar\translator-sidecar\translator-sidecar.exe`）。有 Translator 源码时，也可以在源码目录运行 `python -m cli adapter test <本目录>`。

改完代码后，先跑自测并修好所有问题，再交给用户。样例应覆盖真实文件里会出现的情况，包括不该翻译的内容。

## 打包与发布

- `python scripts/pack.py` 在 `dist/` 下打出 `<id>-<version>.zip`、`.sha256` 和扩展列表的条目 `<id>-<version>.entry.json`。不要提交 `dist/`。
- 发布：改 `adapter.json` 的 `version` → 提交 → 推送标签 `v<version>`；`.github/workflows/release.yml` 打包并附到 Release。
- 进扩展列表：向 https://github.com/KlaraGraff/XL_Translator-extensions 提 PR，新增 `extensions/<id>.json`（即条目文件的内容）。
