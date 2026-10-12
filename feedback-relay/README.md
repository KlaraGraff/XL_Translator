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
