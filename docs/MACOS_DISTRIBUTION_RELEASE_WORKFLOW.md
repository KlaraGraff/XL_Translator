# Translator macOS 启动与分发入口说明（当前基线）

最后更新：2026-05-14

## 1. 文档定位

本文件说明当前仓库中的 macOS 启动入口与分发入口链路。

- 范围：macOS 启动程序与分发入口
- 目标：提供稳定的 macOS 启动体验
- 边界：**本文件不代表整个应用已完成 macOS 业务兼容**

当前已知仍存在业务层面的环境前提，例如：

- 本地 Excel 深度处理仍依赖 `xlwings` 与本机安装的 Excel

Windows 分发说明见 `docs/WINDOWS_DISTRIBUTION_RELEASE_WORKFLOW.md`。

因此，本次交付的含义是：

- macOS 已有对应启动入口
- macOS 标准发布包由 `.app` 和 `.dmg` 组成
- 启动主链路具备首启显性、后续静默、旧实例清理、健康检查后打开应用内窗口等行为

## 2. 当前 macOS 启动链路

### 2.1 标准安装包入口

- GitHub Release 产物：`Translator_macOS_<版本>.dmg`
- 用户入口：`Translator.app`
- 核心启动逻辑：`scripts/frozen_launcher.py`

`.app` 是 PyInstaller 冻结后的应用，不需要用户本机先创建 `.venv`。
启动后会在本地端口拉起 Streamlit，并自动打开应用内窗口。

### 2.2 源码包入口

- 根目录入口：`启动应用.command`
- 显性启动脚本：`scripts/start_macos.command`
- 静默辅助脚本：`scripts/launch_silent_macos.sh`
- 核心启动逻辑：`scripts/launcher.py`

### 2.3 源码包行为说明

1. 用户双击根目录 `启动应用.command`
2. 根入口先检查：
   - `.venv/.bootstrap_success`
   - `.venv/bin/python3` 或 `.venv/bin/python`
3. 如果任一条件不满足：
   - 转入 `scripts/start_macos.command`
   - 以可见方式运行 `scripts/launcher.py`
   - 首次启动会创建 `.venv`、安装依赖、校验关键包、写入 `.bootstrap_success`
4. 如果两个条件都满足：
   - 根入口调用 `scripts/launch_silent_macos.sh`
   - 该脚本会尝试将 `.venv/bin/python3 scripts/launcher.py --silent` 以后台方式拉起
   - 根入口会尽快结束，后续由 `launcher.py --silent` 继续启动应用

### 2.4 `launcher.py` 在 macOS 下的共用职责

- 首次启动时自动解析本机 `python3`
- 检查并完成首启 bootstrap
- 若检测到本项目旧实例，则先结束旧实例并等待端口释放
- 在 `8501~8510` 之间选择可用端口
- 启动 `streamlit run app.py`
- 健康检查通过后再打开应用内窗口
- 静默模式下把日志写入 `.runtime/launcher.log`

## 3. Python 运行环境约定

当前 macOS 标准安装包由 PyInstaller 内置 Python 运行时。

源码分发不携带 Windows 风格的 `runtime/python`。

- 源码包首次启动：通过本机可用的 `python3` 创建 `.venv`
- 源码包后续启动：统一使用 `.venv/bin/python3` 或 `.venv/bin/python`
- 如需手动指定首启解释器，可设置 `PRODUCT_TRANSLATE_BOOTSTRAP_PYTHON`

## 4. 分发脚本的当前支持

`scripts/build_distribution.py --platform macos` 现在会：

- 复制根目录 `启动应用.command`
- 复制 `scripts/start_macos.command`
- 复制 `scripts/launch_silent_macos.sh`
- 不复制 Windows 根入口或 `runtime/python`

这一步只保留为内部源码目录生成入口。标准安装包不调用这条链路，也不生成 zip。

## 5. GitHub Actions 分发

标准发布现在改为：

1. 在 `macos-latest` runner 安装 Python 3.11
2. 安装 `requirements-build.txt`
3. 运行 `scripts/prepare_icons.py --macos`
4. 使用 `packaging/macos/app_macos.spec` 生成 `.app`
5. 使用 `hdiutil` 生成 `Translator_macOS_<版本>.dmg`
6. 输出对应 SHA256 文件并上传到 GitHub Release

发布产物不再包含 `.zip`。

## 6. 使用与验收建议

### 6.1 推荐的分发方式

优先使用 dmg 分发并拖入或直接运行。

原因：

- dmg 是 macOS 用户更熟悉的标准分发形态
- `.app` 被封装到 dmg 后，更容易保持布局一致

### 6.2 macOS 基本验收

1. 在 macOS 上打开 dmg
2. 将 `Translator.app` 放入合适位置后双击启动
3. 验证启动后会打开应用内窗口
4. 验证重新启动时会先清理旧实例，再打开新的本地服务
5. 若需排查安装版启动链路，检查 `~/Library/Application Support/Translator/desktop_launcher.log`

## 7. 失败排查

### 7.1 安装版启动失败

- 优先检查 `~/Library/Application Support/Translator/desktop_launcher.log`
- 未签名 dmg 第一次运行可能被系统阻止，可在 Finder 右键 `Translator.app` 后选择“打开”

### 7.2 源码包首次启动失败

- 优先保留终端窗口中的实时报错
- 可直接运行 `scripts/start_macos.command` 复现

### 7.3 源码包后续静默启动失败

- 优先检查 `.runtime/launcher.log`
- 若需要切回显性排查，直接运行 `scripts/start_macos.command`

### 7.4 `.command` 无法直接打开

若是直接拷贝源码目录到 macOS，可能缺少可执行权限，可在终端执行：

```bash
chmod +x 启动应用.command scripts/start_macos.command scripts/launch_silent_macos.sh
```

然后重试。
