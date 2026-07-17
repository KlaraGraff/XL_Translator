# Tauri V8 分发流程

标准发布物：

- Windows：`Translator_Windows_<version>_Setup.exe`（NSIS，WebView2 download bootstrapper）
- macOS：`Translator_macOS_<version>.dmg`

两者均包含 Tauri 主程序和无 Qt 的 PyInstaller onedir Python sidecar。sidecar 位于
Tauri 资源目录，不能拆出其中的动态库或 Python 运行时文件。

## 构建

```bash
# macOS
PYTHON_BIN=./.venv/bin/python3 bash scripts/build_macos_package.sh

# Windows PowerShell
./scripts/build_windows_package.ps1 -PythonExe .\.venv\Scripts\python.exe
```

构建前先在 `ui/` 执行 `npm ci`。macOS 需要 Rust、Node、Xcode Command Line
Tools 和 Python/PyInstaller；Windows 需要 Rust、Node、Python/PyInstaller 与 NSIS。

脚本会在安装包超过 80MB 时失败，要求先做人工裁剪评估。目标为不超过 70MB。

## 签名与公证

macOS 设置 `XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY` 后会先签名 sidecar、再签名
`Translator.app`；设置 `XL_TRANSLATOR_MACOS_NOTARY_PROFILE` 后再提交 dmg 公证。
Windows 设置 `XL_TRANSLATOR_WINDOWS_SIGN_CERT_SHA1` 后会签名 sidecar 和 NSIS 安装器。

未配置 Developer ID 时，macOS 构建仍会对完整 `.app` 执行 ad-hoc 签名并校验资源
封印，避免 Gatekeeper 将仅带链接器签名的 Tauri 应用误报为“已损坏”。ad-hoc 构建
没有 Apple 公证，首次启动仍需按 README 的说明在 Finder 中右键选择“打开”。

任何正式发布前都应在对应平台执行安装、卸载、重装和启动冒烟验证。
