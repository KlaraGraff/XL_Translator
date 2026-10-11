# Translator（XL Translator）

用大模型批量翻译 **Excel、Word、PDF、图片和 CAD 图纸（DWG / DXF）** 的桌面应用，支持 macOS 和 Windows。Excel 和 Word 默认写成原文加译文的双语文件（也可以只留译文），翻过的词句存进记忆库，下次优先复用，专业术语前后一致。

A desktop app (macOS / Windows) that uses large language models to batch-translate Excel, Word, PDF, images and CAD drawings (DWG / DXF), with bilingual output and a reusable translation memory. [English overview below](#english).

## 能做什么

- **Excel 表格**：`.xlsx` 和旧版 `.xls`，可以翻译工作表名、公式单元格显示的内容和文件名；按译文调整行高，或者保持行高、缩小字号；拿不准的单元格涂色提醒复核。
- **Word 文档**：`.docx` 和旧版 `.doc`，正文、表格和页眉页脚都能翻；可以保护封面和目录不翻，自动编号翻译后不错乱。文本框和脚注暂时不翻，遇到时会在结果里提示。
- **PDF 与图片**：逐页用图像模型把页面上的文字换成目标语言，尽量保留原来的版式和插图；图片支持 PNG、JPG、WebP、BMP、TIFF；可以让审核模型逐页检查，另存一份压缩 PDF。
- **CAD 图纸**：DXF 直接翻译，DWG 先在「插件」页连接 ODA File Converter；图块里的文字可以一起翻，GBK 等中文编码的旧图纸也能正确读写。
- **记忆库**：Excel 和 Word 翻好的词句自动入库，翻译 Excel、Word 和 CAD 图纸时优先复用；固定的词条一定按你定的译法翻，支持导入、导出和清洗。
- **任务中心**：不同类型的任务可以同时跑，可以随时停止（PDF 和 CAD 还能暂停），之后只补翻没翻完的部分；每个任务都有结果统计、需要复核的位置和生成的文件。
- **语言与场景**：内置 59 种语言，源语言可以自动识别，也可以自定义目标语言；提供工程、资料管理、行政日常等场景提示词，也可以自己写。

## 支持的模型服务

在「设置 → 模型服务」里填写服务商、模型和 API 密钥，测试连接通过后就能用。文档翻译、PDF 与图片、记忆库清洗可以各用各的模型。

- 云端：OpenAI / ChatGPT、Claude（Anthropic）、DeepSeek、智谱 GLM、阿里百炼（通义）、硅基流动，以及任何 OpenAI 兼容接口
- 本机：Ollama、LM Studio，或其他 OpenAI 兼容的本地服务
- PDF 与图片需要能生成图片的模型：OpenAI、硅基流动或 OpenAI 兼容接口

文件在本机读取和写出，翻译时只把要翻译的文字（PDF 和图片是页面图像）发给你配置的模型服务；用本机模型时，文档内容不会离开你的电脑。

## V10.0.0 主要变化

- 新增 CAD 图纸翻译，页面和流程与 Excel、Word、PDF 相同
- 新配色：浅色模式改为柔和的暖米白，深色模式层次更分明
- 界面、运行日志、报错和报告里的说法统一，不再出现程序内部术语
- API 密钥被拒绝或额度用完时直接停下并说明原因；长时间限速时先保存已翻好的部分

完整说明见[发布页](https://github.com/KlaraGraff/XL_Translator/releases/latest)和 [CHANGELOG](docs/CHANGELOG.md)。

## 系统与下载

当前版本提供两个原生安装包：

- macOS（Apple Silicon，M 系列芯片）：`Translator_macOS_arm64_<版本>.dmg`，要求 **macOS 12.0 Monterey 及以上版本**
- Windows（x64）：`Translator_Windows_x64_<版本>_Setup.exe`，要求 **Windows 10 及以上版本**

打开[最新版发布页](https://github.com/KlaraGraff/XL_Translator/releases/latest)下载安装包；每个安装包都有同名的 `.sha256` 校验文件。

不提供 Intel Mac 安装包与更新，Intel Mac 用户请继续使用旧版。

### macOS 安装与首次打开

下载后可在终端验证：

```bash
cd ~/Downloads
shasum -a 256 -c Translator_macOS_arm64_<版本>.dmg.sha256
```

校验结果必须显示 `OK`。

1. 打开已校验的 DMG，将 `Translator.app` 拖到 `Applications`。
2. 从“应用程序”启动 Translator。
3. 每一版的签名状态写在该版本的发布说明里。若说明写的是 Developer ID 签名并已通过 Apple 公证，双击即可打开；若写的是 ad-hoc 签名、未公证，macOS 会提示“无法验证开发者”，需要在“系统设置 → 隐私与安全性”中确认“仍要打开”。无论哪种情况，都只安装从官方发布页下载并通过上一步校验的 DMG，不要从其他来源找替代安装包。

macOS 的数据目录是 `~/Library/Application Support/Translator`。

### Windows 安装与首次打开

下载后可在 PowerShell 验证（对比输出的哈希值与 `.sha256` 文件里的值一致）：

```powershell
Get-FileHash "$env:USERPROFILE\Downloads\Translator_Windows_x64_<版本>_Setup.exe" -Algorithm SHA256
Get-Content "$env:USERPROFILE\Downloads\Translator_Windows_x64_<版本>_Setup.exe.sha256"
```

1. 运行已校验的 `Setup.exe`。安装只写入当前用户目录，不需要管理员权限。
2. Windows 安装包暂不携带代码签名，SmartScreen 可能提示“已保护你的电脑”；请先完成上一步校验，再选择“更多信息 → 仍要运行”。只对从官方发布页下载并通过校验的安装包这样做。
3. 应用界面依赖 Microsoft Edge WebView2 运行时。若系统缺少，安装器会联网自动安装；这是首次安装唯一可能需要网络的步骤。

Windows 的数据目录是 `%LOCALAPPDATA%\Translator`。

## 首次启动

首次启动会显示快速开始，不会自动从其他目录导入旧版本数据。当前应用数据目录中已有的数据会按兼容规则读取或升级；遇到损坏数据时，会先备份原文件，再新建可用数据，并在设置中提示备份位置。如果原文件无法读取、备份或新建失败，会保留原件并说明原因，不保证能恢复其中的内容。

## 使用边界

标准 `.xlsx`、`.docx`、PDF、图片和 DXF 图纸不依赖 Microsoft Office。旧 `.xls`、`.doc` 高保真转换及部分编号处理可使用本机 Microsoft Office、LibreOffice 或系统转换工具；DWG 图纸需要 ODA File Converter。这些是可选本地软件，不随 Translator 安装包提供。应用会在需要时说明权限、格式保真和回退风险。

翻译前需要由用户在“设置 → 模型服务”中填写并主动测试可用的服务。快速开始不会自动发送 API 请求。帮助、更新检查、维护和脱敏诊断均在应用内提供；诊断不会自动上传，也不包含 API Key、原文、译文或完整 Prompt。

### 配置文件的导出与导入

“设置 → 模型服务”可以把整份模型配置导出给同事。不含密钥的导出是明文 `.json`；含密钥的导出是加密的 `.xltcfg`，密钥段用应用内置的密钥加密，文件被改动过就无法导入，导出时可以选择 7 天 / 30 天 / 90 天 / 长期有效。

安全边界：任何装有本应用的人都能解开任意一份 `.xltcfg`。它防的是文件在传输途中被不相干的人看到，防不了拿到应用的人——不要把含密钥的配置文件发到公开群或网盘。导出时只会带上你自己在本机填写过的密钥，从别人的配置里导入来的密钥不会继续外传。

## 版本更新

应用启动后会在后台检查一次更新，发现新版时点亮侧栏“设置”的红点。在“设置 → 更新与关于”可以直接完成更新：应用下载官方更新包、校验签名、就地替换，签名不匹配的包一律拒装。macOS 替换完成后由用户决定何时重启；Windows 交由安装程序完成，安装前会先确认是否打断正在运行的任务。后台提醒可以暂停，也可以忽略某个具体版本。

应用内更新不可用时（例如直接从 DMG 里运行、安装目录不可写、架构不匹配、开发构建），应用会说明具体原因并退回到手动下载安装包，不会给一个点不动的按钮。取更新信息、下载、安装三步中任何一步失败都会说明是哪一步断的，当前已安装的版本不受影响。

手动下载走官方 GitHub Release 页，应用仅接受与本机系统和架构匹配且带 SHA-256 校验文件的完整发布包。

查看 [CHANGELOG](docs/CHANGELOG.md) 了解版本变化。

## 开发

代码结构、一次任务的完整流程和「改什么去哪里」见 [架构说明](docs/ARCHITECTURE.md)；新增一个翻译板块按 [新增翻译板块清单](docs/ADDING_A_TRANSLATION_PAGE.md) 做。

## English

Translator is a desktop app for macOS (Apple Silicon, 12.0+) and Windows (x64, 10+) that translates documents with large language models while keeping their original layout.

- **Excel** (`.xlsx`, `.xls`): sheet names, formula display values and file names can be translated; row heights adjust to the translation, or font size shrinks to fit; uncertain cells are highlighted for review.
- **Word** (`.docx`, `.doc`): body text, tables, headers and footers; cover pages and tables of contents can be left untouched. Text boxes and footnotes are not translated yet and are reported when found.
- **PDF and images** (PNG, JPG, WebP, BMP, TIFF): each page is redrawn in the target language by an image model that keeps the original layout and graphics as closely as possible.
- **CAD drawings** (DXF, and DWG via ODA File Converter): text inside blocks is translated too, and drawings saved in GBK or other legacy Chinese encodings are handled correctly.
- **Bilingual output** for Excel and Word: original and translation together by default, or translation only.
- **Translation memory**: translated terms are stored and reused; pinned entries always use your preferred translation.
- **Models**: OpenAI, Claude, DeepSeek, Zhipu GLM, Alibaba Qwen (DashScope), SiliconFlow, any OpenAI-compatible API, or local models through Ollama and LM Studio.
- **59 built-in languages** plus custom target languages, with automatic source-language detection.

Download the installer for your system from the [latest release](https://github.com/KlaraGraff/XL_Translator/releases/latest). The interface is currently in Chinese. Licensed under the [MIT License](LICENSE).
