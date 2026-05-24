# Agent Self-Testing Playbook

## 1. 默认工作顺序

任何代码改动完成后，交付前都按这个顺序走：

1. 用项目虚拟环境跑静态检查：

```powershell
powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1
```

2. 再补至少 1 个与改动直接相关的动态测试。
3. 动态测试如果会碰到用户目录、TM 数据库、`settings.json`、`keys.json`、日志或临时文件，优先使用 `agent/testing/Run-IsolatedVenvPython.ps1`。
4. 所有动态测试产物统一写到 `.runtime/self-tests/<task-slug>/`。
5. 交付时必须明确说明实际执行过哪些测试；如果没跑成，也要说明原因和风险。

只跑 `ruff check` 不算完成。

## 2. Python 与环境规则

- 优先使用 `./.venv/bin/python3`。
- 不要混用系统 Python。
- 不要把测试数据写回真实用户目录。
- 需要隔离时，在 Python 进程启动前就设置好 `HOME`、`USERPROFILE`、`TEMP`、`TMP`，这样 `config.py` 里的 `Path.home()` 才会指向测试目录。

## 3. 隔离运行器

优先使用：

```powershell
powershell -ExecutionPolicy Bypass -File ./agent/testing/Run-IsolatedVenvPython.ps1 `
  -TaskSlug translate-page-smoke `
  -ScriptPath .runtime/self-tests/translate-page-smoke/check_translate_page.py
```

这个脚本会自动：

- 使用项目 `.venv`
- 创建 `.runtime/self-tests/<task-slug>/home`
- 创建 `.runtime/self-tests/<task-slug>/temp`
- 创建 `.runtime/self-tests/<task-slug>/artifacts`
- 设置 `HOME` / `USERPROFILE` / `TEMP` / `TMP`
- 把项目根目录注入 `PYTHONPATH`
- 暴露两个辅助环境变量：
  - `PRODUCT_TRANSLATE_SELF_TEST_ROOT`
  - `PRODUCT_TRANSLATE_SELF_TEST_ARTIFACTS`

如果测试脚本需要写文件，优先写到 `PRODUCT_TRANSLATE_SELF_TEST_ARTIFACTS`。

## 4. 原生界面动态测试

当前主线只保留 PySide6 原生界面。界面改动优先用隔离脚本直接实例化页面或主窗口，不再保留网页页面包装器。

典型做法是：

1. 在 `.runtime/self-tests/<task-slug>/` 下写一个一次性验证脚本。
2. 在脚本启动前设置 `QT_QPA_PLATFORM=offscreen`。
3. 创建 `QApplication`，再实例化目标页面或 `NativeMainWindow`。
4. 对控件文本、默认值、按钮状态、表格内容和关键交互结果做断言。
5. 用 `Run-IsolatedVenvPython.ps1` 或项目 `.venv` 运行脚本。

示例：

```python
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from native_app.pages.word_translate import WordTranslatePage

app = QApplication.instance() or QApplication(sys.argv)
page = WordTranslatePage()

assert page.title_label.text() == "Word 翻译"
```

如果要验证截图或复杂布局，优先把产物写到 `PRODUCT_TRANSLATE_SELF_TEST_ARTIFACTS`。

## 5. TM / settings / 数据文件相关测试

这类测试默认按“有副作用”处理：

- `settings.py` 会落盘 `settings.json`
- `config.py` 会把应用数据目录放到平台原生位置，测试中可用 `TRANSLATOR_APP_DATA_DIR` 隔离
- `core/tm_manager.py` 会连接 TM 数据库

因此：

1. 优先通过隔离运行器在新 Python 进程里启动测试。
2. 尽量在隔离环境已经就位后，再导入项目模块。
3. 如果必须在模块导入后再切换数据库路径，需要同时修补引用了路径常量的模块，而不是只改一处。

## 6. 常见坑

### macOS 说明

- 当前项目主线保留 PySide6 原生界面，自测默认按原生应用路径和命令组织。
- 项目文件统一按 UTF-8 编辑和保存。

## 7. 最低交付标准

每次交付前至少说明：

1. 跑了哪个静态检查。
2. 跑了哪个动态测试。
3. 动态测试覆盖了哪个改动点。
4. 如果没跑成，具体卡在哪里。

推荐表述：

- `已运行 powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1`
- `已运行 native page offscreen smoke`
- `已验证自定义输出目录分支`
- `未覆盖真实 Excel COM 调用，原因是当前环境缺少 ...`
