# 反馈中转站

应用内「反馈」页点击「发送」后，请求先到这里（Cloudflare Workers），再转成私有仓库
`KlaraGraff/XL_Translator-feedback` 里的一条 issue。附件包存在 Workers KV，issue 里只放一个
带签名、90 天内有效的下载链接。

问题处理完毕就删附件：中转站每天 03:00（UTC）检查一次，issue 已关闭、或带「待通知」标签（已修好、
等发版通知）的，删掉它的附件包，正文里的下载链接换成「反馈包 … 已在问题处理完毕后删除（日期）」。
其余的最长保存 90 天，到期自动删除。应用里对勾选原文件和译文的用户就是这样承诺的，所以复现时下载到
本地的反馈包和用户文件，用完也要删掉，不能提交进任何仓库。

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

## 收到反馈即叫醒 Claude

Claude 的「每日反馈分拣」定时任务（每天摩洛哥时间 4:29，叫醒专门的「反馈分拣」对话；它给新反馈打标签，再请总协调为每条问题单开修复对话）另开了 API 触发地址（`wrangler.toml` 的
`ROUTINE_FIRE_URL`）。中转站建好 issue 后请求一次这个地址，Claude 马上开始分拣，不用等到第二天早上。
一次处理会看完所有未处理的反馈，所以 30 分钟内最多叫一次；被挡下或没叫醒的，由每 30 分钟一次的定时检查
补叫。请求里只有一句固定提醒，不带用户写的内容。

触发令牌在 claude.ai/code/routines 里这个定时任务的编辑页生成（添加 API 触发 → Generate token），只显示一次。
填进主仓库 Actions 密钥 `FEEDBACK_ROUTINE_TOKEN`，再运行一次部署即可；没配就只靠每天的定时检查。

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

   第一行写修复所在的版本，第二行是邮件主题（`主题：`、`Subject:` 或 `Objet :`），空一行后是正文。正文不用署名：发信时末尾统一加「—— Translator」（邮件同时带纯文本和 HTML 两版，HTML 版落款靠右），评论里自动带上的 Claude Code 署名也会去掉。主题后会自动加上反馈编号（如「您反馈的问题已修复（FB-20261012-2VW8XM）」），便于检索，不用自己写。中文通知一律称「您」。正文末尾会按界面语言自动补一段更新方法（应用内「设置 → 更新与关于」一键更新，以及发布页 `releases/latest` 的下载链接），通知里不用再写；正文已带发布页链接时不重复加。
2. 该版本发布后，工作流把正文发到 issue 里「联系方式」的邮箱，评论一句已通知（邮箱打码），把标签换成 `已通知` 并关闭 issue。没留可用邮箱的标 `无邮箱` 并关闭；发送失败的保留 `待通知`，下次发布再发。
3. 预发布（只请个别用户先试用的版本，`release.yml` 的 `step=prerelease`）不会自动发邮件。要通知某几位试用的用户时，先把他们 issue 里的通知评论写成请他试用的内容，再手动运行 `feedback-notify.yml`：勾 `prerelease`，`issues` 填编号（空格分隔），`version` 填预发布的版本号。脚本先确认这一版已发布为 Pre-release，只发这几个 issue，正文末尾附的是这一版自己的发布页链接（不写「应用内更新」，因为预发布在应用里看不到），发完同样标「已通知」并关闭，转为正式版时不会再发第二封。

发信用软件专用邮箱的 SMTP（默认 Gmail，`smtp.gmail.com:465`），密钥 `FEEDBACK_SMTP_USER`、`FEEDBACK_SMTP_PASSWORD`（应用专用密码）放在本仓库的 Actions 密钥里；还没配置时只试运行。本仓库公开，日志里只出现打码后的邮箱，不打印邮件正文。

