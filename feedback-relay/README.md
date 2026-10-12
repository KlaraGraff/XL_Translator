# 反馈中转站

应用内「反馈」页点击「发送」后，请求先到这里（Cloudflare Workers），再转成私有仓库
`KlaraGraff/XL_Translator-feedback` 里的一条 issue。附件包存在 Workers KV，90 天后自动删除，
issue 里只放一个带签名、90 天内有效的下载链接。

## 一次性设置

1. Cloudflare：打开「Workers & Pages」，记下 Account ID；在 My Profile → API Tokens 用
   「Edit Cloudflare Workers」模板生成令牌。
2. GitHub：新建私有仓库 `XL_Translator-feedback`；生成细粒度令牌，只选这个仓库，
   Issues 权限设为 Read and write。
3. 主仓库 Settings → Secrets and variables → Actions 添加 `CLOUDFLARE_API_TOKEN`、
   `CLOUDFLARE_ACCOUNT_ID`、`FEEDBACK_GITHUB_TOKEN`。
4. 手动运行 Actions 里的「Deploy feedback relay」。运行摘要里给出中转站地址，把它写进
   `app_meta.py` 的 `FEEDBACK_ENDPOINT`，之后打包的版本才能在线发送。

之后改动 `feedback-relay/` 并合进 main 会自动重新部署。GitHub 令牌过期后发送会失败（应用
会提示保存为 ZIP），换一个新令牌填进 `FEEDBACK_GITHUB_TOKEN` 再运行一次部署即可。

## 测试

`node tests/feedback_relay.test.mjs` 用内存 KV 和假的 GitHub 跑一遍提交、下载、限流和失败回滚。
本地联调可以用 `wrangler dev`，在 `.dev.vars` 里设 `GITHUB_TOKEN`、`LINK_SECRET`，并用
`GITHUB_API` 指向一个假的 GitHub 服务。

线上链路：手动运行「Deploy feedback relay」时勾选 `smoke_test`，部署完会真实发一条测试反馈，
用反馈仓库的令牌找到对应 issue、下载附件比对，然后留言并关闭这条 issue。

## 通知用户

反馈页写着「问题解决或功能上线时，我们会发邮件通知您」，由 `.github/workflows/feedback-notify.yml` 在新版本正式发布后自动发出（`release.yml` 的 `step=publish` 成功后调用；也可手动运行，默认只试运行）。

1. 修好问题或做完功能时，在对应的反馈 issue 里加标签 `待通知`，并留一条通知评论，按用户的界面语言（issue 里「界面语言」）写；邮件里软件一律叫「Translator」（XL Translator 只是项目名）：

   ```
   <!-- xl-notify version=10.0.1 -->
   主题：Translator 10.0.1 已修复您反馈的问题

   您好，……
   ```

   第一行写修复所在的版本，第二行是邮件主题（`主题：`、`Subject:` 或 `Objet :`），空一行后是正文。正文不用署名：发信时末尾统一加「—— Translator」（邮件同时带纯文本和 HTML 两版，HTML 版落款靠右），评论里自动带上的 Claude Code 署名也会去掉。
2. 该版本发布后，工作流把正文发到 issue 里「联系方式」的邮箱，评论一句已通知（邮箱打码），把标签换成 `已通知` 并关闭 issue。没留可用邮箱的标 `无邮箱` 并关闭；发送失败的保留 `待通知`，下次发布再发。

发信用软件专用邮箱的 SMTP（默认 Gmail，`smtp.gmail.com:465`），密钥 `FEEDBACK_SMTP_USER`、`FEEDBACK_SMTP_PASSWORD`（应用专用密码）放在本仓库的 Actions 密钥里；还没配置时只试运行。本仓库公开，日志里只出现打码后的邮箱，不打印邮件正文。

