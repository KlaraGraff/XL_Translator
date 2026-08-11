# Translator

Translator 是面向 Excel、Word、PDF 和图片的本地文档翻译器，支持批量翻译、翻译记忆库和双语结果输出。桌面应用使用 Tauri 2 原生窗口、系统 WebView 和 Python 文档引擎 sidecar。

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

首次启动只显示快速开始，不会读取、导入、迁移、修复或删除旧版本数据。

## 使用边界

标准 `.xlsx`、`.docx`、PDF 和图片流程不依赖 Microsoft Office。旧 `.xls`、`.doc` 高保真转换及部分编号处理可使用本机 Microsoft Office、LibreOffice 或系统转换工具；这些是可选本地软件，不随 Translator 安装包提供。应用会在需要时说明权限、格式保真和回退风险。

翻译前需要由用户在“设置 → 模型服务”中填写并主动测试可用的服务。快速开始不会自动发送 API 请求。帮助、更新检查、维护和脱敏诊断均在应用内提供；诊断不会自动上传，也不包含 API Key、原文、译文或完整 Prompt。

### 配置文件的导出与导入

“设置 → 模型服务”可以把整份模型配置导出给同事。不含密钥的导出是明文 `.json`；含密钥的导出是加密的 `.xltcfg`，密钥段用应用内置的密钥加密，文件被改动过就无法导入，导出时可以选择 7 天 / 30 天 / 90 天 / 长期有效。

安全边界：任何装有本应用的人都能解开任意一份 `.xltcfg`。它防的是文件在传输途中被不相干的人看到，防不了拿到应用的人——不要把含密钥的配置文件发到公开群或网盘。导出时只会带上你自己在本机填写过的密钥，从别人的配置里导入来的密钥不会继续外传。

## 版本更新

应用启动后会在后台检查一次更新，发现新版时点亮侧栏“设置”的红点。在“设置 → 更新与关于”可以直接完成更新：应用下载官方更新包、校验签名、就地替换，签名不匹配的包一律拒装。macOS 替换完成后由用户决定何时重启；Windows 交由安装程序完成，安装前会先确认是否打断正在运行的任务。后台提醒可以暂停，也可以忽略某个具体版本。

应用内更新不可用时（例如直接从 DMG 里运行、安装目录不可写、架构不匹配、开发构建），应用会说明具体原因并退回到手动下载安装包，不会给一个点不动的按钮。取更新信息、下载、安装三步中任何一步失败都会说明是哪一步断的，当前已安装的版本不受影响。

手动下载走官方 GitHub Release 页，应用仅接受与本机系统和架构匹配且带 SHA-256 校验文件的完整发布包。

查看 [CHANGELOG](docs/CHANGELOG.md) 了解版本变化。
