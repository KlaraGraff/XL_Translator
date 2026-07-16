# Translator

Translator 是一个面向 Excel、Word 和 PDF 文档的本地翻译器，支持批量翻译、术语库管理和双语结果输出。

## 版本说明

当前主线使用 Tauri 2 原生窗口、系统 WebView 和 Python 文档引擎 sidecar。旧 PySide6 和 Streamlit 壳层均已退役。

## 下载最新版

打开 [最新版发布页](https://github.com/KlaraGraff/XL_Translator/releases/latest)，在 `Assets` 中下载适合你系统的安装包。发布页当前沿用既有 GitHub 仓库地址。

- Windows: `Translator_Windows_<版本>_Setup.exe`
- macOS: `Translator_macOS_<版本>.dmg`

## 安装

### Windows

下载并运行 Windows 安装包，安装完成后从开始菜单或桌面启动 `Translator`。

### macOS

下载并打开 macOS 安装包，将 `Translator.app` 拖入 `Applications`。如果系统首次阻止打开，请在 Finder 中右键 `Translator.app`，然后选择“打开”。

## 旧版数据迁移

首次启动时，如果检测到旧目录 `~/.xl_translator` 中有翻译记忆库、设置或 API Key，Translator 会先弹窗确认是否迁移到平台原生应用数据目录。日志、桌面启动状态和诊断归档可作为一个可选项单独保留。

新的本地数据目录：

- macOS: `~/Library/Application Support/Translator`
- Windows: `%LOCALAPPDATA%\Translator`
- Linux: `~/.local/share/Translator`

## 版本更新

查看 [CHANGELOG](docs/CHANGELOG.md) 了解最近版本变化。
