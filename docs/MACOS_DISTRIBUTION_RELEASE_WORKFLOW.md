# XL Translator macOS 启动与分发入口说明（当前基线）

最后更新：2026-04-08

## 1. 文档定位

本文件说明当前仓库已经收敛为 macOS 单平台后的启动入口与分发入口链路。

- 范围：macOS 启动程序与分发入口
- 目标：提供稳定的 macOS 单平台启动体验
- 边界：**本文件不代表整个应用已完成 macOS 业务兼容**

当前已知仍存在业务层面的环境前提，例如：

- 本地 Excel 深度处理仍依赖 `xlwings` 与本机安装的 Excel

因此，本次交付的含义是：

- macOS 已有对应启动入口
- macOS 分发包已能携带 `.command` 入口
- 启动主链路具备首启显性、后续静默、旧实例清理、健康检查后打开浏览器等行为

## 2. 当前 macOS 启动链路

### 2.1 用户入口

- 根目录入口：`启动应用.command`
- 显性启动脚本：`scripts/start_macos.command`
- 静默辅助脚本：`scripts/launch_silent_macos.sh`
- 核心启动逻辑：`scripts/launcher.py`

### 2.2 行为说明

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

### 2.3 `launcher.py` 在 macOS 下的共用职责

- 首次启动时自动解析本机 `python3`
- 检查并完成首启 bootstrap
- 若检测到本项目旧实例，则先结束旧实例并等待端口释放
- 在 `8501~8510` 之间选择可用端口
- 启动 `streamlit run app.py`
- 健康检查通过后再打开浏览器
- 静默模式下把日志写入 `.runtime/launcher.log`

## 3. Python 运行环境约定

当前仓库不再携带 Windows 风格的 `runtime/python`。

- 首次启动：通过本机可用的 `python3` 创建 `.venv`
- 后续启动：统一使用 `.venv/bin/python3` 或 `.venv/bin/python`
- 如需手动指定首启解释器，可设置 `PRODUCT_TRANSLATE_BOOTSTRAP_PYTHON`

## 4. 分发脚本的当前支持

`scripts/build_distribution.py` 现在会：

- 复制根目录 `启动应用.command`
- 复制 `scripts/start_macos.command`
- 复制 `scripts/launch_silent_macos.sh`
- 不再复制 Windows 启动脚本或 `runtime/python`
- 在生成 zip 时，为 `.command` / `.sh` 写入可执行权限位

这一步是为了降低 macOS 用户解压后还要手动 `chmod +x` 的概率。

## 5. 使用与验收建议

### 5.1 推荐的分发方式

优先使用 zip 分发并在 macOS 上解压后运行。

原因：

- 当前 zip 生成逻辑会主动写入 `.command` / `.sh` 的可执行权限位
- 如果只是把 Windows 上生成的目录直接拷贝到 macOS，权限位可能丢失

### 5.2 macOS 基本验收

1. 在 macOS 上解压分发 zip
2. 双击 `启动应用.command`
3. 验证首次启动会显示可见终端过程
4. 验证首次启动完成后会打开浏览器
5. 保持 `.venv` 与 `.bootstrap_success` 不动，再次双击 `启动应用.command`
6. 验证第二次启动会优先走静默链路，终端通常只会短暂出现
7. 若需排查静默链路，检查 `.runtime/launcher.log`

## 6. 失败排查

### 6.1 首次启动失败

- 优先保留终端窗口中的实时报错
- 可直接运行 `scripts/start_macos.command` 复现

### 6.2 后续静默启动失败

- 优先检查 `.runtime/launcher.log`
- 若需要切回显性排查，直接运行 `scripts/start_macos.command`

### 6.3 `.command` 无法直接打开

若是从非 zip 场景拷贝到 macOS，可能缺少可执行权限，可在终端执行：

```bash
chmod +x 启动应用.command scripts/start_macos.command scripts/launch_silent_macos.sh
```

然后重试。
