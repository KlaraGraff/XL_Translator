# Tauri 双平台分发流程

正式 Release 同时提供两个原生资产（缺一即整体失败，不允许只发一个平台）：

- `Translator_macOS_arm64_<version>.dmg` 及同名 `.sha256`，面向 macOS 12.0 Monterey 及以上
- `Translator_Windows_x64_<version>_Setup.exe` 及同名 `.sha256`，面向 Windows 10 及以上

不得交叉编译或以 Rosetta 替代原生构建。`arm64` 必须在 Apple Silicon 原生构建机上生成；Windows 安装器必须在原生 x64 Windows 构建机上生成（CI 用 `PROCESSOR_ARCHITECTURE = AMD64` 硬门校验）。不再发布 Intel（x86_64）macOS 安装包。

## 本地测试构建

手动执行只生成明确标记为 `UNSIGNED_TEST` 的 GitHub artifact，不得作为正式下载资产。

macOS（构建机必须是 macOS，且 `MACOS_ARCH` 等于 `uname -m`）：

```bash
MACOS_ARCH="$(uname -m)" \
MACOSX_DEPLOYMENT_TARGET=12.0 \
PYTHON_BIN=./.venv/bin/python3 \
bash scripts/build_macos_package.sh
```

脚本固定使用 Python 3.11 和 `MACOSX_DEPLOYMENT_TARGET=12.0`，在签名与 DMG 生成前扫描整个 `.app`：`LSMinimumSystemVersion` 必须为 `12.0`，所有 Mach-O 的 `minos` 必须不高于 12.0，并且都必须包含当前原生架构。扫描报告保存在 `.runtime/package/macos-reports/`。

Windows（构建机必须是原生 x64 Windows，命令在 Git Bash 中执行）：

```bash
PYTHON_BIN=./.venv/Scripts/python.exe \
./.venv/Scripts/python.exe scripts/build_tauri_package.py --platform windows
```

Windows 构建产出 NSIS 安装器（`installMode: currentUser`，不需要管理员权限；WebView2 采用在线引导安装）。安装器重命名进 `dist/` 并生成 `.sha256`。

两个平台构建前都需要在 `ui/` 执行 `npm ci`，并具备 Rust、Node 与受控 Python 3.11（macOS 另需 Xcode Command Line Tools）。标准发布依赖不允许通过“不受支持 Python”开关绕过。

## 正式发布与临时标签构建

GitHub Actions 只接受稳定标签 `vX.Y.Z`。标签、`app_meta.py`、`src-tauri/tauri.conf.json`、`src-tauri/Cargo.toml` 和 `ui/package.json` 的版本必须完全一致；任一不一致即失败。

发布 job 会下载两个平台的产物，逐一执行 `shasum -a 256 -c` 校验，两平台资产与校验文件齐全后才创建正式 stable GitHub Release。

### macOS 签名与公证

正式 Release 路径必须同时提供以下 GitHub Actions Secrets（名字与 lantern 仓库一致，同一套值可直接复用；注意 secrets 按仓库隔离，需要在每个仓库分别配置）。全部存在时，稳定 tag 才会进入签名、公证和 Release 发布路径：

- `APPLE_CERTIFICATE` — Developer ID Application 证书 .p12 的 base64
- `APPLE_CERTIFICATE_PASSWORD` — 导出 .p12 时设置的密码
- `APPLE_ID` — Apple 开发者账号邮箱
- `APPLE_PASSWORD` — 该账号的 App 专用密码（appleid.apple.com 生成）
- `APPLE_TEAM_ID` — 开发者团队 ID

CI 将证书导入临时 keychain（钥匙串密码 run 内随机生成），签名 sidecar、应用和 DMG，用 `notarytool`（Apple ID + App 专用密码 + Team ID）提交 Apple 公证，staple 后用 Gatekeeper 评估。

如果稳定 tag 缺少任一 Apple Secret，工作流仍然照常发布正式 Release（不再标注 Pre-release），只是 macOS 产物降级为原生 `TEMP_SIGNED_TEST`：应用和 sidecar 使用 ad-hoc 临时签名、DMG 不公证，首次打开需要右键“打开”。Release 说明里会写明这一点，构建日志也会给出 warning。`workflow_dispatch` 仍生成 `UNSIGNED_TEST` artifact，不创建或修改 Release。

### Windows 签名现状

Windows 安装器暂不做代码签名，正式与测试通道产物同名（名字不随渠道变化）。用户侧以 `.sha256` 校验为准，SmartScreen 提示属预期行为，README 已写明操作路径。引入 Windows 代码签名证书后再升级此节。

## 应用内更新（latest.json）

应用内更新只在**正式发布**（formal-release，即 Apple 密钥齐全的稳定 tag）上产出。临时通道（`temporary-test`）的 macOS 产物是 ad-hoc 签名、未公证的，如果也生成更新包，一旦有人把那个 Release 取消草稿，所有已安装的机器会在没有任何下载动作的情况下被换成未公证版本。因此工作流对更新产物额外加了一道闸：不是正式发布就不把 minisign 私钥交给构建步骤，`build_macos_package.sh` 里还有第二道同样的判断，防止有人在本机带着密钥手动跑。

各平台的更新载荷不一样，是 Tauri v2 更新器决定的，不是可选项：

- macOS：已签名、已公证、已 staple 的 `.app` 打包成 `Translator_macOS_arm64_<version>.app.tar.gz`。DMG 需要人手拖拽，更新器用不了，所以另发一份 tarball。
- Windows：**就是那个 NSIS 安装器 `.exe` 本身**（静默运行）。Tauri v2 对 NSIS 不再另出压缩包 —— 加了 `createUpdaterArtifacts` 也只会给 `.exe` 旁边多一个 `.sig`。

两者都由 `TAURI_SIGNING_PRIVATE_KEY` 签名，签名以同名 `.sig` 文件随包发布。发布 job 用 `scripts/build_updater_manifest.py` 生成 `latest.json`；该脚本会比对 `.sig` 与 `tauri.conf.json` 里公钥的 key id，因为 Tauri CLI 在密钥不匹配时只打印 warning，不会让构建失败 —— 那种 Release 全绿，却在每个用户的机器上装不上。

### 发布说明的写法约定

Release 正文里第一条 `---` 分隔线**以上**的内容会显示在应用内的「更新与关于」页；分隔线以下的内容只出现在 GitHub 下载页。签名与 SmartScreen 提示、安装包清单、`.sha256` 校验说明一律写在分隔线以下：这些话是讲给还没装上软件的人听的，而看应用内那一页的人早就装好了，更新器也会自己下载、自己验签。

## 发布验收

macOS：正式发布仍需在 macOS 12 arm64 实机完成安装、Gatekeeper、首次启动、sidecar、标准 `.xlsx`/`.docx`/PDF/图片 Mock 流程和卸载重装验收。该实机门不能由 CI 或本地 Mock 替代。

Windows：当前没有 Windows 实机，验收标准为 CI 原生构建通过（含 `PROCESSOR_ARCHITECTURE` 硬门、NSIS 产物唯一性校验、sha256 齐全）加全量自动化测试通过；实机安装观感待有条件时补验，此边界在交付报告中如实标注。
