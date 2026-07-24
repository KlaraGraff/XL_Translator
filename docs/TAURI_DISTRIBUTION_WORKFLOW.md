# Tauri macOS 分发流程

新版只发布 macOS 12.0 Monterey 及以上版本，正式 Release 仅有两个原生资产：

- `Translator_macOS_arm64_<version>.dmg` 及同名 `.sha256`
- `Translator_macOS_x64_<version>.dmg` 及同名 `.sha256`

不得交叉编译、以 Rosetta 替代原生构建，或上传 Windows/NSIS 安装器。`arm64` 必须在 Apple Silicon 原生构建机上生成；`x86_64` 必须在 Intel 原生构建机上生成，面向用户的文件名采用 `x64`。

## 本地测试构建

手动执行只生成明确标记为 `UNSIGNED_TEST` 的 GitHub artifact，不得作为正式下载资产：

```bash
MACOS_ARCH="$(uname -m)" \
MACOSX_DEPLOYMENT_TARGET=12.0 \
PYTHON_BIN=./.venv/bin/python3 \
bash scripts/build_macos_package.sh
```

构建机必须是 macOS，并且 `MACOS_ARCH` 必须等于 `uname -m`。脚本固定使用 Python 3.11 和 `MACOSX_DEPLOYMENT_TARGET=12.0`，在签名与 DMG 生成前扫描整个 `.app`：`LSMinimumSystemVersion` 必须为 `12.0`，所有 Mach-O 的 `minos` 必须不高于 12.0，并且都必须包含当前原生架构。扫描报告保存在 `.runtime/package/macos-reports/`。

构建前需要在 `ui/` 执行 `npm ci`，并具备 Rust、Node、Xcode Command Line Tools 和受控 Python 3.11。标准发布依赖不允许通过“不受支持 Python”开关绕过。

## 正式发布与临时标签构建

GitHub Actions 只接受稳定标签 `vX.Y.Z`。标签、`app_meta.py`、`src-tauri/tauri.conf.json`、`src-tauri/Cargo.toml` 和 `ui/package.json` 的版本必须完全一致；任一不一致即失败。

正式 Release 路径必须同时提供以下 GitHub Actions Secrets。全部存在时，稳定 tag 才会进入签名、公证和 Release 发布路径：

- `APPLE_DEVELOPER_ID_CERTIFICATE_BASE64`
- `APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD`
- `APPLE_KEYCHAIN_PASSWORD`
- `APPLE_NOTARY_KEY_ID`
- `APPLE_NOTARY_ISSUER_ID`
- `APPLE_NOTARY_PRIVATE_KEY_BASE64`

CI 将证书导入临时 keychain，签名 sidecar、应用和 DMG，提交 Apple 公证，staple 后用 Gatekeeper 评估。两个原生构建均成功、各自校验和通过且资产完整后，才会创建 GitHub Release。

如果稳定 tag 缺少任一 Apple Secret，工作流不会伪装成正式发布，而会自动降级为两个原生 `TEMP_SIGNED_TEST` artifact：应用和 sidecar 使用 ad-hoc 临时签名，DMG 不公证、不创建 GitHub Release，且不能作为正式下载资产。`workflow_dispatch` 仍生成 `UNSIGNED_TEST` artifact，不创建或修改 Release。

正式发布仍需分别在 macOS 12 arm64 与 macOS 12 x86_64 实机完成安装、Gatekeeper、首次启动、sidecar、标准 `.xlsx`/`.docx`/PDF/图片 Mock 流程和卸载重装验收。该实机门不能由 CI 或本地 Mock 替代。
