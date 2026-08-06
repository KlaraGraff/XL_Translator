# V9.0.0 交付报告

日期：2026-08-06 ｜ 分支：main ｜ 版本：9.0.0（已 bump，未推 tag）

## 一、交付范围完成情况

| 工作线 | 状态 | 关键提交 |
| --- | --- | --- |
| UI 全面重写（七个视图 + 快速开始向导，API 路由不动） | ✅ 完成并通过浏览器实测 | 8fe772c |
| Excel 补丁式写入器（嵌入图/悬浮图老大难） | ✅ 完成，真实 API 跑批 + 字节级机械核查通过 | （前序提交） |
| PDF 逐页审核（已通过页常驻一键复核） | ✅ 完成，前端接线过代码评审 | 8fe772c |
| Windows 打包 / CI / 更新检查器 | ✅ 完成，CI 手动通道两次全绿 | （前序提交） |
| README 与分发文档双端更新 + 发布契约测试 | ✅ 完成 | 9a357ff |
| 跑批自测（真实 deepseek API） | ✅ Excel 全量/补译、Word 通过 | — |
| `--output-dir` 对 Excel/Word 不生效（跑批发现） | ✅ 已修复 + 回归测试 | 7990a82 |
| 版本统一 bump 9.0.0 + CHANGELOG 条目 | ✅ 完成 | 18c038e |
| CI 最终演练（当前 main，workflow_dispatch，无对外发布） | ✅ 全绿 | run 31093245669 |

最终验收基线：pytest **543 passed**、ruff 全绿、tsc 无错误、vite 生产构建通过、生产 bundle 中 dev 垫片 0 引用。

## 二、CI 产物（最终演练 run）

运行链接：<https://github.com/KlaraGraff/XL_Translator/actions/runs/31093245669>（全部 job 成功，Publish 按设计跳过）

| 产物 | 大小 |
| --- | --- |
| Translator_Windows_x64_unsigned-test | 27.6 MB |
| Translator_macOS_arm64_unsigned-test | 40.1 MB |

产物为未签名测试构件（UNSIGNED_TEST 命名），仅供验收，不是对外发布物。正式发布物由推 v9.0.0 tag 的同一 workflow 生成。

## 三、需要你过目的产品文案与形态偏差

以下各项在推 tag 前需要你确认（都可以快速改）：

**产品文案**
1. **README**：双平台下载/校验/安装/首启说明已重写（含 Windows SmartScreen、WebView2、Get-FileHash 指引）。
2. **CHANGELOG V9.0.0 条目**（docs/CHANGELOG.md 顶部）：Windows 首发、UI 重设计、补丁式写入器、PDF 逐页复核、`--output-dir` 修复、独立数据基线。

**实现与样张的已接受偏差**（评审时判定合理，最终裁量在你）
3. 任务完成不弹成功 toast——完成状态由任务卡与日志面板呈现。
4. Word/PDF 工作区的统计卡是从 Excel 样张外推的（样张只画了 Excel）。
5. 任务风险确认模态比样张简化（去掉了次要说明文字）。
6. 模态按钮没有中性灰 tone，统一用主色/危险色两档。
7. 部分图标与样张不完全一致（用现有 sprite 内最接近的替代）。
8. 记忆库分页的每页条数选择器是样张没有的新增。
9. PDF 任务到达终态后审核卡不再渲染（快照仅保留给已打开的对比弹窗）；样张未定义终态行为。

## 四、边界与未覆盖项（如实声明）

- **Windows 无实机**：交付标准按拍板执行——CI 构建 + 自动化测试通过。NSIS 安装包在真实 Windows 上的安装/首启/更新流程未经人工验证。
- **PDF/图片翻译跑批未真实调用**：当前配置无 image-generation 模型凭据，preflight 正确拒绝启动（未造假数据）。PDF 逐页审核链路的后端有 pytest 覆盖、前端过了代码评审与浏览器走查，但没有跑过真实图像翻译批次。
- settings_version 24→26 静默回退、TM schema 阻断两项，按「分发前不写旧版兼容」铁律不处理。

## 五、发布过程记录

- 首次推 v9.0.0 tag 的发布 run 在 Publish 的校验步骤失败：Windows 上 Python 文本模式把 sha256 文件的换行写成 CRLF，macOS 端 `shasum -c` 找不到带 `\r` 的文件名。两次演练没暴露它，因为该校验只在 tag 通道运行。
- 已修复（写文件固定 LF）并新增字节级回归测试；tag 重新指向修复后的提交再次发布。

## 六、发布结果（已撤回，待重发）

发布经历了三个阶段，当前对外最新版本回到 **v8.1.2**：

1. **临时签名版曾短暂发布**：用户确认文案与偏差后推 tag，v9.0.0 以 temporary-test 通道发布（当时仓库没有 Apple 签名 secrets，macOS 资产为 ad-hoc 临时签名的 `TEMP_SIGNED_TEST.dmg`，42.6 MB；Windows 安装包 27.6 MB，双 sha256 交叉校验通过）。
2. **签名凭据重建完成**：用户对临时签名提出疑问后，重建了整套 Apple 签名凭据——新 Developer ID Application 证书（Team ID J622D6994N，2031-08-07 到期）、legacy 格式 .p12、五个 GitHub secrets（与 lantern 仓库同名同值）。CI 实测验证：证书导入、签名身份识别、notarytool 凭据校验全部通过；凭据已双重备份（本机 `~/Documents/AppleSigning-Translator/` + iCloud 云盘，.p12 密码存于「密码」App）。
3. **正式签名重发两次都撞上 GitHub Actions 官方事故**（2026-08-06 晚）：第一次 runner 在等待苹果公证结果约 50 分钟后断网（签名与公证上传均已成功，故障纯属平台侧）；第二次重跑在 job 启动阶段就遇到 Service Unavailable，GitHub 状态页确认 Actions partial outage。用户随即决定暂缓发布，等待下一批改动完成后一并发布。

当前状态：v9.0.0 tag 与临时签名 Release 均已撤回删除（对外从未露出正式签名版）；苹果侧的公证提交记录无法撤回但对外不可见、无影响。**重发路径已完全就绪**：改动完成后重打 v9.0.0 tag 推送即可，CI 会自动走正式签名+公证通道，macOS 产物名为 `Translator_macOS_arm64_9.0.0.dmg`（无 TEMP 后缀）。
