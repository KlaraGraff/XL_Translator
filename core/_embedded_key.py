"""内置的模型配置加密私钥。

模型配置导出加密用的是这里给出的 X25519 私钥。仓库是公开的，所以下面这把
密钥是**开发用兜底密钥**：故意生成、故意公开、不承担任何保密责任，任何人
都能从这份源码里读出来并解密用这把密钥加密的配置。它只用来让本地开发和
CI 里没有正式密钥时的流程能跑通（单测、手动构建），绝不能被当作真实的加
密保护。

正式发布时，`scripts/inject_embedded_key.py` 会在打包前用 GitHub Actions
的加密 secret 覆盖这个文件的两个常量，`scripts/verify_release_metadata.py`
会校验发布产物里 `EMBEDDED_KEY_ID` 不再是 `"dev"`——如果 CI 忘了注入，发布
会直接失败，而不是静默地把开发密钥发给用户。

`EMBEDDED_KEY_ID == "dev"` 就是「这不是正式密钥」的标记，任何读到这个值
的代码都应当认为当前构建不具备真实的加密保护。
"""

from __future__ import annotations

EMBEDDED_KEY_ID = "dev"
EMBEDDED_PRIVATE_KEY_B64 = "SCGPmGszB7tgsk34ZM401pdVBYtICWLzGNpgDXfWzlA="
