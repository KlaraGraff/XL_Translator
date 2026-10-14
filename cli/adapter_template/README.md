# Translator 扩展模板

[English](README.en.md)

用这个模板给 [Translator](https://github.com/KlaraGraff/XL_Translator) 增加一种可翻译的文件类型。扩展是一个文件夹：`adapter.json` 写明类型的名称和处理哪些扩展名，`adapter.py` 里的函数从文件里取出文字、再把译文写回。调用模型、记忆库、重试、复核和进度都由 Translator 负责。

模板本身就是一个能用的扩展：它翻译「键 = 值」形式的 `.myfmt` 文件，`samples/` 里有两份样例。照着改成你的格式即可。

## 第一步：建立项目

在 GitHub 上点「Use this template」建一个自己的仓库，再克隆到本机。不用 GitHub 时，运行 `translator-sidecar adapter new <id>`，在当前目录下的 `<id>` 文件夹里生成同样的文件。

`translator-sidecar` 是 Translator 自带的命令行，位置是：

- macOS：`/Applications/Translator.app/Contents/Resources/sidecar/translator-sidecar/translator-sidecar`
- Windows：`C:\Users\<用户名>\AppData\Local\Translator\sidecar\translator-sidecar\translator-sidecar.exe`

## 第二步：改成你的格式

1. 修改 `adapter.json`：
   - `id`：只用小写字母、数字、`-` 和 `_`，最长 40 个字符；发布后不要再改。
   - `label`、`file_noun`、`description`：类型名称、文件的叫法、页面「?」里的说明，中文、英文、法文各一份。
   - `extensions`：处理的扩展名。
   - `prompt`：发给模型的内置规则，可省；用户可在「设置 → 翻译参数」里改写。
   - `capabilities`：支持「仅译文」和「原文 + 译文」中的哪几种输出。`options`：页面上的开关，可省。
   - `version`、`license`、`repo`（项目主页地址）。
2. 改写 `adapter.py` 里的 `extract`、`write`，以及可省的 `scan`。每个参数的含义写在函数说明里。
3. 在 `samples/` 里放两三份小样例，自测和扩展列表的检查都用它们。

扩展只能使用 Python 标准库和 `translator_adapter`，不能联网、启动其他程序、加载原生库或动态执行代码。扩展包里只能有 `.py`、`.json`、`.md`、`.txt` 文件和 `LICENSE`（样例不限），总大小不超过 2 MB。Translator 安装前会检查这些规则，但检查只拦明显越界的写法，不是沙箱：扩展以用户的权限运行，所以用户安装前会被提醒只安装信任的来源。

## 第三步：自测和试用

```
translator-sidecar adapter test .
```

它检查清单和代码，再在单独的进程里用 `samples/` 跑一遍取字和写回：以原文作译文写回后，再取一次字应与原文一致。全部通过时退出码为 0。

然后在 Translator 的「扩展」页点「从文件夹添加…」，选这个文件夹（或运行 `translator-sidecar adapter install . --yes`）。侧栏出现新类型后，翻译一份真实文件看看效果。改了代码后再添加一次即可更新。

## 分享

1. 把 `adapter.json` 的 `version` 改成新版本号，提交并推送。
2. 打标签并推送，标签须与 `version` 一致：`git tag v0.1.0`，`git push origin v0.1.0`。
3. 仓库的 Release 工作流打出 `<id>-<version>.zip`，附到这个标签的 Release，并在「Registry entry」一步的日志里打印扩展列表的条目。
4. 向 [XL_Translator-extensions](https://github.com/KlaraGraff/XL_Translator-extensions) 提一个 PR，新增 `extensions/<id>.json`，内容就是上一步的条目。自动检查通过、代码审核后合并，所有用户都能在「扩展」页搜到并安装。

也可以不经过扩展列表，直接把 zip 发给别人：`python scripts/pack.py`（或 `translator-sidecar adapter pack .`）在 `dist/` 下打出 zip，对方在「扩展」页点「添加扩展…」选这个 zip。

## 用编程助手开发

Claude Code、Codex 等编程助手会读 [AGENTS.md](AGENTS.md)，里面写了协议、限制和自测方法。

## 许可证

模板以 MIT 许可证提供，把 `LICENSE` 里的年份和名字换成你的。你的扩展可以使用其他许可证，请写在 `adapter.json` 的 `license` 里。
