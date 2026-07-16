# Native Distribution Workflow

当前主线从 V5.0 起只维护 PySide6 原生桌面应用，并以 Windows 安装器和 macOS dmg 作为标准发布产物。

## 入口

- 开发/源码启动：`scripts/launch_native.py`
- macOS 源码包入口：`启动应用.command`
- Windows 源码包入口：`启动应用.bat`
- macOS 打包配置：`packaging/macos/app_macos.spec`
- Windows 打包配置：`packaging/windows/app_windows.spec`
- Windows 标准安装包：`Translator_Windows_<version>_Setup.exe`
- macOS 标准安装包：`Translator_macOS_<version>.dmg`

`启动原生应用.command` 保留为 macOS 明确命名入口；`启动应用.command`
现在也是同一条原生启动线路。

## 源码包

源码包由 `scripts/build_distribution.py` 生成，包含：

- `assets/`
- `core/`
- `engines/`
- `native_app/`
- `scripts/`
- 根目录启动脚本、配置和说明文件

源码包不再包含网页页面目录、WebView 启动器或 Streamlit 配置。

## 运行依赖

运行依赖以 `requirements.txt` 为准。主线 UI 依赖为
`PySide6-Essentials`，不再安装 Streamlit、streamlit-extras 或 pywebview。
PyInstaller 配置会显式收集 `pypdfium2_raw` 的 PDFium 动态库，并排除无运行调用的
NumPy/Pandas，避免开发环境残留的非运行依赖被误收集进安装包。

发布构建固定使用 Python 3.11，并通过
`constraints-release-py311.txt` 锁定完整依赖图。更新依赖时应在 macOS 和
Windows 各完成一次冻结包构建、`--smoke-test` 和完整测试，再更新约束文件。
构建脚本会拒绝与约束不一致的环境。

## 冻结包验证

macOS 和 Windows 构建脚本会在生成安装介质前执行冻结程序的
`--smoke-test`。该模式只验证关键模块、第三方包元数据和应用元数据，不创建
Qt 窗口、不加载或保存用户设置，也不初始化翻译记忆库。外层验证器同时设置
独立的 `TRANSLATOR_APP_DATA_DIR`，并在 60 秒后强制终止，以检查冒烟过程没有
写入应用数据或挂起。

macOS 发布最低版本由 `app_meta.MACOS_MINIMUM_SYSTEM_VERSION` 单点定义。
当前发布依赖 `PySide6-Essentials 6.11.1`，其 macOS 二进制最低版本为 15.0，
因此当前安装包不再声明不真实的 macOS 11 兼容性。打包后会扫描 `.app` 内每个
Mach-O 文件，确认实际 `minos` 不高于 `LSMinimumSystemVersion`。需要更低版本时，
必须选择确实支持目标系统的 PySide6/Qt 构建并重新生成约束，不能只修改 plist。

## 签名与公证

默认 macOS 构建只有 PyInstaller 为本地运行生成的 ad-hoc 签名，没有可信
Developer ID 发布签名，也未公证；默认 Windows 构建未签名。配置下列环境变量后，
构建脚本会执行并验证发布签名；配置了公证时，失败会中止发布：

- macOS：`XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY`，可选
  `XL_TRANSLATOR_MACOS_ENTITLEMENTS`；公证使用已在钥匙串配置的
  `XL_TRANSLATOR_MACOS_NOTARY_PROFILE`。
- Windows：`XL_TRANSLATOR_WINDOWS_SIGN_CERT_SHA1`，可选
  `XL_TRANSLATOR_WINDOWS_SIGNTOOL` 和 `XL_TRANSLATOR_WINDOWS_TIMESTAMP_URL`。

GitHub Actions 当前没有内置证书或公证凭据，所以 tag workflow 产出的安装包没有
可信发布签名。正式对外分发前，应在受控发布环境配置证书并保留签名验证记录。

## 第三方许可

PDF 功能使用 `pypdfium2`（Apache-2.0 或 BSD-3-Clause）。仓库自身采用 MIT
许可证，完整条款见根目录 `LICENSE`；构建脚本只能验证技术依赖，不能替代第三方
许可合规审查。

## 验证

每次改动后至少运行：

```powershell
powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1
```

涉及原生界面的改动，还应补充 PySide6/offscreen 动态测试，验证页面默认值、
表格、按钮状态和关键交互。

发布 workflow 在 Windows 和 macOS 上都会运行质量门和完整测试；测试使用
`QT_QPA_PLATFORM=offscreen` 与隔离的 `TRANSLATOR_APP_DATA_DIR`，不会读取或改写
开发机/runner 的真实用户设置。tag 构建还会校验 `v<APP_VERSION>` 与
`app_meta.APP_VERSION` 完全一致，避免错误 tag 生成版本名不一致的安装包。
