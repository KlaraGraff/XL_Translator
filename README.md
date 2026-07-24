# Translator

Translator 是面向 Excel、Word、PDF 和图片的本地文档翻译器，支持批量翻译、翻译记忆库和双语结果输出。桌面应用使用 Tauri 2 原生窗口、系统 WebView 和 Python 文档引擎 sidecar。

## 系统与下载

新版仅支持 **macOS 12.0 Monterey 及以上版本**，并在每个正式 Release 提供两个原生安装包：

- Apple Silicon：`Translator_macOS_arm64_<版本>.dmg`
- Intel Mac：`Translator_macOS_x64_<版本>.dmg`

打开[最新版发布页](https://github.com/KlaraGraff/XL_Translator/releases/latest)，按“关于本机”显示的芯片类型选择对应 DMG；不要在 Apple Silicon 上把 Intel 包当作原生包使用。每个 DMG 都有同名的 `.sha256` 文件。下载后可在终端验证：

```bash
cd ~/Downloads
shasum -a 256 -c Translator_macOS_arm64_<版本>.dmg.sha256
```

将命令中的 `arm64` 换为 Intel Mac 使用的 `x64`。校验结果必须显示 `OK`。

新版不提供 Windows 安装包或 Windows 更新。Windows 用户请继续使用旧版。

## 安装与首次打开

1. 打开已校验的 DMG，将 `Translator.app` 拖到 `Applications`。
2. 从“应用程序”启动 Translator。
3. 正式 Release 均使用 Developer ID 签名和 Apple 公证。若 Gatekeeper 仍因本机策略阻止启动，请在“系统偏好设置/系统设置 → 隐私与安全性”中确认“仍要打开”；不要从非官方来源下载替代安装包。

首次启动只显示快速开始，不会读取、导入、迁移、修复或删除旧版本数据。当前版本的数据目录是：

`~/Library/Application Support/Translator`

## 使用边界

标准 `.xlsx`、`.docx`、PDF 和图片流程不依赖 Microsoft Office。旧 `.xls`、`.doc` 高保真转换及部分编号处理可使用本机 Microsoft Office、LibreOffice 或系统转换工具；这些是可选本地软件，不随 Translator 安装包提供。应用会在需要时说明权限、格式保真和回退风险。

翻译前需要由用户在“模型配置”中填写并主动测试可用的服务。快速开始不会自动发送 API 请求。帮助、更新检查、维护和脱敏诊断均在应用内提供；诊断不会自动上传，也不包含 API Key、原文、译文或完整 Prompt。

## 版本更新

更新只会提示并打开官方 GitHub Release 下载页，不会自动下载、替换应用或重启正在运行的任务。应用仅接受与本机架构匹配且带 SHA-256 校验文件的完整发布包。

查看 [CHANGELOG](docs/CHANGELOG.md) 了解版本变化。
