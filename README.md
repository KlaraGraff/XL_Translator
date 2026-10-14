# Translator（XL Translator）

**简体中文** | [English](README.en.md)

Translator 是一款基于大语言模型的桌面文档翻译应用，支持批量翻译 **Excel、Word、PDF、图片及 CAD 图纸（DWG / DXF）**，适用于 macOS 与 Windows。Excel 与 Word 默认输出原文与译文对照的双语文件，也可仅保留译文。已翻译的词句存入翻译记忆库，供后续任务复用，以保持术语一致。

[下载最新版本](https://github.com/KlaraGraff/XL_Translator/releases/latest)

![Translator 主界面：Excel 翻译完成](docs/images/readme/app-excel.png)

> 本页的界面截图与各功能下的翻译前后对照图对应同一批工程文档：Excel 与 PDF 为法译中，Word 与 CAD 图纸为中译法。截图中的文件名均为中性的示例名称，界面语言为中文，配色为默认的素雅灰。界面语言可在「设置 → 外观与语言」中切换为英语或法语；输出文件夹名称、报告文件及写入文档的复核标记目前仍为中文。

## 功能

### Excel

支持 `.xlsx` 及旧版 `.xls` 格式。可翻译工作表名称、公式单元格的显示值及文件名；行高可随译文自动调整，也可保持行高并缩小字号；需要复核的单元格以底色标出。型号、标准编号等内容（如 `GB/T 8163-2018`）保持原样。

![Excel 翻译进行中](docs/images/readme/app-excel-running.png)

<p align="center">
  <img src="docs/images/readme/result-excel-original.png" width="49%" alt="Excel 原文（法语）">
  <img src="docs/images/readme/result-excel-bilingual.png" width="49%" alt="Excel 双语输出（法语原文下附中文译文）">
</p>
<p align="center"><sub>左：原文（法语）；右：双语输出，原文在上，中文译文在下</sub></p>

### Word

支持 `.docx` 及旧版 `.doc` 格式，可翻译正文、表格及页眉页脚。可选择不翻译封面和目录；自动编号在翻译后保持正确。文本框和脚注暂不翻译，检测到时将在结果中提示。

![Word 翻译完成](docs/images/readme/app-word.png)

<p align="center">
  <img src="docs/images/readme/result-word-original.png" width="49%" alt="Word 原文（中文）">
  <img src="docs/images/readme/result-word-bilingual.png" width="49%" alt="Word 双语输出（每段原文后附法语译文）">
</p>
<p align="center"><sub>左：原文（中文）；右：双语输出，每段原文后附法语译文</sub></p>

### PDF 与图片

由图像模型逐页将文字替换为目标语言，并尽量保留原有版式和插图。图片支持 PNG、JPG、WebP、BMP 和 TIFF 格式。可启用审核模型逐页检查，翻译完成后可单独重新生成指定页面，并可另存压缩版 PDF。

![PDF 翻译完成及逐页结果](docs/images/readme/app-pdf.png)

<p align="center">
  <img src="docs/images/readme/result-pdf-original.png" width="49%" alt="PDF 原文页面（法语）">
  <img src="docs/images/readme/result-pdf-translated.png" width="49%" alt="PDF 译文页面（中文）">
</p>
<p align="center"><sub>左：原文页面（法语）；右：译文页面（中文）</sub></p>

### CAD 图纸

DXF 文件可直接翻译；DWG 文件需先在「插件」页面配置 ODA File Converter，格式转换在任务中进行。图块内的文字可一并翻译。GBK 等中文编码的旧图纸可正确读写，旧版 CAD 以特殊编码保存的中文也能正确识别。

译文写回方式可在页面右侧「译文写回」中设置：

- 显示方式：仅译文、原文在上译文在下，或译文在上原文在下；双语时译文与原文位于同一图层。
- 译文字体：默认使用 AutoCAD 自带字体写入译文，在其他电脑上打开也能正常显示；也可沿用原文字体。
- 超长译文自动缩小：译文较长、会压到相邻文字时，先收窄字宽，再缩小字号（默认先缩至 70%，最小 40%）；仍放不下的列入报告供复核。
- 锁定不翻译：指定图框、标题栏等图块和图层，其中的文字保持原样；名称可直接输入，也可从图纸中挑选。

![CAD 翻译完成，右侧为译文写回选项](docs/images/readme/app-cad.png)

![CAD 原图（中文）](docs/images/readme/result-cad-original.png)

![CAD 译图（法语）](docs/images/readme/result-cad-translated.png)

<p align="center"><sub>上：原图（中文）；下：译图（法语）</sub></p>

### 记忆库

Excel 与 Word 的翻译结果自动存入记忆库，在翻译 Excel、Word 和 CAD 图纸时优先复用。已固定的词条始终采用指定译法。记忆库支持导入、导出和清洗。

![记忆库（带图钉标记的为已固定词条）](docs/images/readme/app-library.png)

### 任务中心

不同类型的任务可同时运行，任务可随时停止（PDF 和 CAD 任务还可暂停），之后可仅翻译剩余部分。每个任务均保留结果统计、待复核位置及输出文件。

![任务中心](docs/images/readme/app-tasks.png)

![任务中心（深色模式）](docs/images/readme/app-tasks-dark.png)

### 反馈

侧栏的「反馈」页用于报告问题或提出建议。可填写描述并直接粘贴截图，按需附带系统与版本信息、所选任务的日志、原文件和译文（原文件和译文默认不附带）；失败的任务也可在任务中心通过「反馈此问题」进入。发送前可查看将发送的全部内容，发送时显示上传进度，也可改为保存为 ZIP 文件。日志中的 API 密钥和文件路径已去除，随反馈上传的文件保存 90 天后自动删除。填写邮箱后，问题解决或相关功能上线时会收到邮件通知。

![反馈页](docs/images/readme/app-feedback.png)

### 外观

界面提供浅色与深色模式，可跟随系统自动切换。在「设置 → 外观与语言」中可选择五套配色方案：素雅灰（默认）、暖调纸感、冷调柔光、青瓷和琥珀护眼；浅色和深色模式可分别选用不同的方案，例如白天使用素雅灰、夜间使用琥珀护眼。

### 语言与提示词

内置 59 种语言，支持自动识别源语言及自定义目标语言。

各翻译页面提供「自动识别场景」开关，默认开启：翻译前先通读一遍文档，判断文档类型和所属专业，再据此理解单独出现的短词，例如图纸门号中的「甲」不会被译成合同中的「甲方」。开启后每份文件多调用一次模型。

提示词由固定的内置规则和可选的「补充要求」组成：内置规则每次都会发送，补充要求只追加在其后，不会替换内置规则。补充要求可在「设置 → 翻译参数」中按 Excel、Word、CAD 分别设置，并可导出和导入。

![提示词设置：内置规则、自动识别场景与补充要求](docs/images/readme/app-prompts.png)

### 扩展

除 Excel、Word、PDF 和 CAD 外，其他文件类型可以通过扩展翻译，例如随应用提供的「文本」（.txt、.md），在「扩展」页打开后即出现在侧栏。在「扩展」页可以浏览并安装扩展列表里审核过的扩展，也可以添加别人发来的扩展包；扩展由第三方作者编写，以你的用户权限运行，请只安装信任的来源。自己制作扩展可从[扩展模板](https://github.com/KlaraGraff/XL_Translator-extension-template)开始，做好后提交到[扩展列表](https://github.com/KlaraGraff/XL_Translator-extensions)分享给其他人，步骤见 [docs/EXTENSIONS.md](docs/EXTENSIONS.md)。

## 模型服务

在「设置 → 模型服务」中填写服务商、模型名称和 API 密钥，测试连接成功后即可使用。文档翻译、PDF 与图片翻译、记忆库清洗可分别配置不同的模型。

服务暂时出错或超时时，该批内容会自动重新发送，仍失败时改用备用连接。Excel 和 CAD 的重发次数可在「设置 → 翻译参数」中调整（0 至 5 次，默认 2 次）。API 密钥无效或额度用尽时，任务立即停止并说明原因。

| 类型 | 支持的服务 |
|---|---|
| 云端 | OpenAI / ChatGPT、Claude（Anthropic）、DeepSeek、智谱 GLM、阿里百炼（通义）、硅基流动，以及其他 OpenAI 兼容接口 |
| 本地 | Ollama、LM Studio 及其他 OpenAI 兼容的本地服务 |
| PDF 与图片 | 需使用支持图像生成的模型：OpenAI、硅基流动或 OpenAI 兼容接口 |

文件的读取和写入均在本机完成，翻译时仅将待翻译文字（PDF 与图片为页面图像）发送至所配置的模型服务。使用本地模型时，文档内容不会离开本机。

![模型服务设置](docs/images/readme/app-settings.png)

## 系统要求与下载

当前版本提供两个原生安装包：

- macOS（Apple Silicon，M 系列芯片）：`Translator_macOS_arm64_<版本>.dmg`，要求 **macOS 12.0 Monterey 及以上版本**
- Windows（x64）：`Translator_Windows_x64_<版本>_Setup.exe`，要求 **Windows 10 及以上版本**

安装包可从[最新版本发布页](https://github.com/KlaraGraff/XL_Translator/releases/latest)下载，每个安装包均附有同名的 `.sha256` 校验文件。不提供 Intel Mac 版本的安装包及更新，Intel Mac 用户请继续使用旧版本。

### macOS 安装与首次打开

下载后可在终端中校验：

```bash
cd ~/Downloads
shasum -a 256 -c Translator_macOS_arm64_<版本>.dmg.sha256
```

校验结果应显示 `OK`。

1. 打开已校验的 DMG，将 `Translator.app` 拖入「应用程序」文件夹。
2. 从「应用程序」启动 Translator。
3. 各版本的签名状态以该版本的发布说明为准。若为 Developer ID 签名且已通过 Apple 公证，可直接打开；若为 ad-hoc 签名且未公证，macOS 会提示「无法验证开发者」，需在「系统设置 → 隐私与安全性」中选择「仍要打开」。请仅安装从官方发布页下载并通过校验的 DMG，不要使用其他来源的安装包。

macOS 的数据目录为 `~/Library/Application Support/Translator`。

### Windows 安装与首次打开

下载后可在 PowerShell 中校验，输出的哈希值应与 `.sha256` 文件中的值一致：

```powershell
Get-FileHash "$env:USERPROFILE\Downloads\Translator_Windows_x64_<版本>_Setup.exe" -Algorithm SHA256
Get-Content "$env:USERPROFILE\Downloads\Translator_Windows_x64_<版本>_Setup.exe.sha256"
```

1. 运行已校验的 `Setup.exe`。安装程序仅写入当前用户目录，无需管理员权限。
2. Windows 安装包目前未进行代码签名，SmartScreen 可能提示「Windows 已保护你的电脑」。完成校验后，选择「更多信息 → 仍要运行」。此操作仅适用于从官方发布页下载并通过校验的安装包。
3. 应用界面依赖 Microsoft Edge WebView2 运行时。如系统中未安装，安装程序将联网自动安装；这是首次安装过程中唯一可能需要联网的步骤。

Windows 的数据目录为 `%LOCALAPPDATA%\Translator`。

## 首次启动

首次启动时显示快速开始向导，不会自动从其他目录导入旧版本数据。应用数据目录中的现有数据将按兼容规则读取或升级。如检测到数据损坏，应用会先备份原文件，再创建新的数据文件，并在设置中提示备份位置。如原文件无法读取、备份或重建，应用将保留原文件并说明原因，但不保证其内容可以恢复。

## 使用说明

标准 `.xlsx`、`.docx`、PDF、图片及 DXF 图纸无需安装 Microsoft Office。旧版 `.xls`、`.doc` 的高保真转换及部分编号处理可调用本机的 Microsoft Office、LibreOffice 或系统转换工具；DWG 图纸需要 ODA File Converter。上述软件均为可选，不随 Translator 安装包提供。需要使用时，应用会说明所需权限、格式保真程度及备用方案的影响。

翻译前，需在「设置 → 模型服务」中配置模型服务并完成连接测试。快速开始向导不会自动发送 API 请求。帮助、更新检查、维护及脱敏诊断功能均内置于应用中；诊断信息不会自动上传，且不包含 API 密钥、原文、译文或完整提示词。

### 配置的导出与导入

「设置 → 模型服务」支持导出完整的模型配置，便于分享给他人使用。不含密钥的导出文件为明文 `.json`；含密钥的导出文件为加密的 `.xltcfg`，密钥部分使用应用内置密钥加密，文件被修改后将无法导入。导出时可选择 7 天、30 天、90 天或长期有效。

安全提示：任何安装了本应用的用户均可解密 `.xltcfg` 文件。该加密仅用于防止文件在传输过程中被无关人员查看，无法防范持有本应用的用户。请勿将含密钥的配置文件发布到公开群组或网盘。导出文件仅包含在本机手动填写的密钥，从他人配置中导入的密钥不会被再次导出。

## 接入智能体

Translator 可作为 MCP 服务器，供 Claude Desktop、Claude Code、Cursor 等智能体调用：智能体可查询支持的文件类型、扫描文件、发起翻译、查看进度并取得结果文件的路径。同一程序也可在终端以命令行使用。Translator 打开时，任务显示在任务中心；未打开时，翻译引擎在后台运行，结束后自动退出。模型、语言和翻译参数沿用应用中的设置，使用前需先在应用中完成模型配置。译文写入原文件旁的新文件夹，原文件不会被修改。

最简单的接入方式：打开应用侧栏的「MCP」页面，点击「复制接入说明」，粘贴并发送给智能体，由它完成配置；按提示重新启动智能体后即可使用。以下为手动配置方法。

翻译引擎的位置：

- macOS：`/Applications/Translator.app/Contents/Resources/sidecar/translator-sidecar/translator-sidecar`
- Windows：`C:\Users\<用户名>\AppData\Local\Translator\sidecar\translator-sidecar\translator-sidecar.exe`

「MCP」页面的「高级」中显示本机的实际路径，配置和命令可直接复制。以下示例使用 macOS 路径；Windows 路径写入 JSON 时，需将每个 `\` 写作 `\\`。配置文件中已有其他服务时，将 `translator` 一项加入现有的 `mcpServers` 即可。

**Claude Desktop**：在「Settings → Developer → Edit Config」中打开 `claude_desktop_config.json`（macOS 位于 `~/Library/Application Support/Claude/`，Windows 位于 `%APPDATA%\Claude\`），加入以下内容后重新启动 Claude Desktop。

```json
{
  "mcpServers": {
    "translator": {
      "command": "/Applications/Translator.app/Contents/Resources/sidecar/translator-sidecar/translator-sidecar",
      "args": ["mcp"]
    }
  }
}
```

**Claude Code**：在终端中执行以下命令，对所有项目生效；如仅用于某个项目，可将上面的 JSON 保存为该项目根目录下的 `.mcp.json`。

```bash
claude mcp add --scope user translator -- /Applications/Translator.app/Contents/Resources/sidecar/translator-sidecar/translator-sidecar mcp
```

**Cursor**：将上面的 JSON 保存为 `~/.cursor/mcp.json`（对所有项目生效；Windows 为 `%USERPROFILE%\.cursor\mcp.json`），或项目中的 `.cursor/mcp.json`。

**命令行**：带子命令运行同一程序即可，例如将一个文件译为法语并等待完成：

```bash
"/Applications/Translator.app/Contents/Resources/sidecar/translator-sidecar/translator-sidecar" translate ~/Documents/检查表.xlsx --to fr --wait
```

`--help` 列出全部子命令（`types`、`scan`、`translate`、`status`、`stop`、`mcp`）；`translate --dry-run` 仅列出将翻译的文件和需要确认的事项，不发起翻译。在 Windows PowerShell 中，带引号的路径前需加 `&`。

## 版本更新

应用启动后会在后台检查一次更新，发现新版本时在侧栏「设置」上显示提示标记。在「设置 → 更新与关于」中可直接完成更新：应用下载官方更新包、校验签名并就地替换，签名不符的更新包一律拒绝安装。macOS 上替换完成后由用户决定重启时间；Windows 上由安装程序完成更新，安装前会确认是否中断正在运行的任务。后台更新提醒可以暂停，也可以忽略指定版本。

应用内更新不可用时（例如直接从 DMG 运行、安装目录不可写、架构不匹配或开发版本），应用会说明原因并引导手动下载安装包。获取更新信息、下载或安装任一步骤失败时，应用会指明失败的步骤，当前已安装的版本不受影响。

手动下载请访问官方 GitHub Release 页面。应用仅接受与本机系统及架构匹配、且附有 SHA-256 校验文件的完整发布包。

## 开发

代码结构、任务的完整执行流程及各类修改的对应位置，参见[架构说明](docs/ARCHITECTURE.md)；新增翻译模块的步骤，参见[新增翻译模块清单](docs/ADDING_A_TRANSLATION_PAGE.md)。桌面外壳使用 Tauri 2（Rust），界面使用 TypeScript，文档处理引擎使用 Python。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
