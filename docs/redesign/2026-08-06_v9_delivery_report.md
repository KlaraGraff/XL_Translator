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

## 五、发布步骤（待你确认后执行）

1. 你过目第三节各项 → 有要改的我改完重新全绿。
2. 推 `v9.0.0` tag → CI 自动出双平台正式发布物（macOS 为 ad-hoc 临时签名，发布说明已写明右键打开）。

**推 tag 是唯一等你确认的动作。**
