"""导出配置里密钥段的封装与解封。

「导出含 Key」以前把密钥明文写进 JSON，文件一旦在微信、邮箱、U 盘里被第三个人
拿到，记事本一开就是全部密钥。这里把密钥单独加密，两边全自动：导出时用内置公钥
封上，导入时用内置私钥解开，用户不输任何东西、不联网。

**安全边界（产品上已明确接受，别在实现里假装它不存在）**：内置密钥在每一份安装
包里都一样，所以任何持有本软件的人都能解开任何一份导出文件。这道防线挡的是「文件
在传输途中被不持有本软件的人捡到」，挡不住持有本软件的人。界面上必须照实说，见
docs/mockups/2026-08-11_encrypted_config_export.html 的界面 2。

设计上留了三个以后补不上的口子：

1. **密钥是整段抽走，不是替换成占位符。** 封装后文档里根本没有 ``api_key`` 字段。
   老版本读到这样一份文件，会当成「不含 Key 的导出」正常导入配置，而不是把
   ``"sealed-key:0"`` 之类的占位符当成真密钥写进本机密钥库——后者会让老版本用一
   把垃圾密钥去调 API，报出来的错还和密钥毫无关系。
2. **文件头带 ``key_id``。** 内置密钥万一外泄，下个版本换一把就能补救；旧文件靠
   这个字段能被认出「不是我这把能解的」，从而报「请更新软件」而不是含糊的解密失败。
3. **配置正文的哈希进认证范围（AAD）。** 不这么做的话，拿到文件的人虽然读不到
   密钥，却能改掉某条连接的 base_url——对方导入后一用，密钥就自己发到改包人的
   服务器上了。密钥保密而路由不保真，等于没保密。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from loguru import logger

from core._embedded_key import EMBEDDED_KEY_ID, EMBEDDED_PRIVATE_KEY_B64

# 封装格式版本。和导出文档的 ``version`` 是两回事：文档 schema 没变（还是 v3），
# 变的只是密钥怎么放。分开编号，才能在不动 schema 的前提下改加密方式。
SEAL_FORMAT = 1
SEALED_FIELD = "sealed_keys"
DEV_KEY_ID = "dev"

# HKDF 的 info 串。写死成带版本的常量：将来换密钥派生方式时，旧文件用旧 info、
# 新文件用新 info，不会互相解出一个「能解开但是错的」结果。
_HKDF_INFO = b"xl-translator/config-seal/v1"

DEFAULT_VALID_DAYS = 30

# ── 解封结果 ──────────────────────────────────────────────
# 这五种状态直接对应界面上的五种说法，见样张界面 4~8。少一种，界面就只能含糊报错。
UNSEAL_OK = "unsealed"            # 正常解开
UNSEAL_PLAINTEXT = "plaintext"    # 旧版明文文件，没有加密段
UNSEAL_EXPIRED = "expired"        # 解得开，但过了有效期
UNSEAL_UNSUPPORTED = "unsupported"  # 格式或 key_id 不认识 —— 本机软件太旧
UNSEAL_CORRUPT = "corrupt"        # 认证失败 —— 传输损坏或被人改过


@dataclass(frozen=True)
class UnsealResult:
    """解封的产物。``document`` 永远是可以交给解析器的那一份。

    除 ``UNSEAL_OK`` 外的所有状态，``document`` 里都不含任何 ``api_key``：解不开
    就当没有密钥，绝不能把半解开的、或者过期的密钥塞回文档——调用方只要照常往下
    走就是安全的，不需要每处都记得判状态。``UNSEAL_CORRUPT`` 是唯一例外的用法：
    正文本身已经不可信，调用方必须整份拒绝，见 ``api/app.py`` 的导入端点。
    """

    document: dict[str, Any]
    status: str
    key_count: int
    expires_at: str | None
    sealed: bool

    @property
    def ok(self) -> bool:
        return self.status == UNSEAL_OK


def embedded_key_id() -> str:
    """本机这份安装包内置的密钥编号。``"dev"`` 表示是开发兜底密钥，不该出厂。"""
    return EMBEDDED_KEY_ID


def _embedded_private_key() -> X25519PrivateKey:
    raw = base64.b64decode(EMBEDDED_PRIVATE_KEY_B64)
    return X25519PrivateKey.from_private_bytes(raw)


def _derive_aes_key(shared: bytes, epk: bytes, key_id: str) -> bytes:
    """把 ECDH 的共享秘密拉伸成 AES-256 的密钥。

    ``salt`` 用临时公钥、``info`` 里拌进 key_id：同一把内置密钥下每份文件的临时
    公钥都不同，因此每份文件的 AES 密钥都不同，一份文件的密钥被算出来也推不出
    另一份。
    """
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=epk,
        info=_HKDF_INFO + b"/" + key_id.encode("utf-8"),
    ).derive(shared)


def _canonical(payload: Any) -> bytes:
    """稳定的 JSON 字节表示。排序 + 去空格，保证两端算出同一个哈希。"""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _body_digest(document: dict[str, Any]) -> str:
    """去掉加密段之后，配置正文的哈希。进 AAD，用来锁住正文不被篡改。"""
    body = {key: value for key, value in document.items() if key != SEALED_FIELD}
    return hashlib.sha256(_canonical(body)).hexdigest()


def _aad(document: dict[str, Any], key_id: str, expires_at: str | None) -> bytes:
    """认证范围：文件类型、schema 版本、密钥编号、有效期，外加正文哈希。

    有效期在这里面，所以改文件里的日期、改文件名、改系统时间都无效——改了就解不开，
    而不是解出一份「看起来还没过期」的东西。
    """
    return _canonical({
        "type": document.get("type"),
        "version": document.get("version"),
        "format": SEAL_FORMAT,
        "key_id": key_id,
        "expires_at": expires_at,
        "body": _body_digest(document),
    })


# ── 密钥在文档里的位置 ────────────────────────────────────
# 导出文档里的密钥只有 ``api_key`` 这一个字段名，出现在三处：连接列表、cloud 块、
# 以及「换服务商时记住的配置」。与其在三处各写一遍抽取逻辑（漏一处就是明文泄露），
# 不如整份文档走一遍，凡是 ``api_key`` 一律带路径抽走。将来新增第四处也自动覆盖。
_SECRET_FIELD = "api_key"


def _extract_secrets(node: Any, path: list[Any], out: list[dict[str, Any]]) -> None:
    if isinstance(node, dict):
        for key in list(node.keys()):
            value = node[key]
            if key == _SECRET_FIELD and isinstance(value, str) and value:
                out.append({"path": [*path, key], "value": value})
                del node[key]
                continue
            _extract_secrets(value, [*path, key], out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _extract_secrets(value, [*path, index], out)


def _restore_secrets(document: dict[str, Any], entries: list[Any]) -> int:
    """把密钥按路径放回文档。路径对不上的条目跳过，不让一条坏数据毁掉整次导入。"""
    restored = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        value = entry.get("value")
        if not isinstance(path, list) or not path or not isinstance(value, str):
            continue
        node: Any = document
        try:
            for step in path[:-1]:
                node = node[step]
            node[path[-1]] = value
        except (KeyError, IndexError, TypeError):
            logger.warning("解封时有一条密钥的路径在文档里找不到，已跳过：{}", path)
            continue
        restored += 1
    return restored


def _expiry_iso(valid_days: int | None) -> str | None:
    if valid_days is None:
        return None
    expires = datetime.now(timezone.utc) + timedelta(days=int(valid_days))
    # 秒级精度就够，且去掉微秒能让 AAD 里的字符串短而稳定。
    return expires.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_expiry(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def seal_model_config_document(
    document: dict[str, Any],
    *,
    valid_days: int | None = DEFAULT_VALID_DAYS,
) -> dict[str, Any]:
    """把文档里的密钥抽出来加密，返回可以直接写盘的那一份。

    ``valid_days`` 传 ``None`` 表示长期有效。文档里一把密钥都没有时原样返回：
    「导出不含 Key」不该平白多出一个空的加密段，那只会让对方以为里面有东西。
    """
    sealed = json.loads(json.dumps(document))  # 深拷贝，绝不改调用方手里那一份
    secrets: list[dict[str, Any]] = []
    _extract_secrets(sealed, [], secrets)
    if not secrets:
        return sealed

    expires_at = _expiry_iso(valid_days)
    ephemeral = X25519PrivateKey.generate()
    epk = ephemeral.public_key().public_bytes_raw()
    shared = ephemeral.exchange(_embedded_private_key().public_key())
    aes_key = _derive_aes_key(shared, epk, EMBEDDED_KEY_ID)

    # 随机 nonce 而非计数器：这里每次都是新生成的 AES 密钥（临时公钥每份都不同），
    # 12 字节随机 nonce 在同一把密钥下只用一次，不存在重用风险。
    nonce = os.urandom(12)
    plaintext = _canonical(secrets)
    ciphertext = AESGCM(aes_key).encrypt(
        nonce, plaintext, _aad(sealed, EMBEDDED_KEY_ID, expires_at)
    )

    sealed[SEALED_FIELD] = {
        "format": SEAL_FORMAT,
        "key_id": EMBEDDED_KEY_ID,
        "epk": base64.b64encode(epk).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "expires_at": expires_at,
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return sealed


def unseal_model_config_document(document: Any) -> UnsealResult:
    """解开文档里的密钥段。任何失败都不抛异常，一律走状态返回。

    做成状态而不是异常，是因为这五种情况里有四种都还有出路（照常导入配置、只是
    没有密钥），只有 ``UNSEAL_CORRUPT`` 该整份拒绝。用异常的话，调用方很容易把
    「过期」也当成致命错误，对方就白跑一趟。
    """
    if not isinstance(document, dict):
        return UnsealResult({}, UNSEAL_CORRUPT, 0, None, False)

    raw_seal = document.get(SEALED_FIELD)
    if raw_seal is None:
        # 9.2.x 及更早导出的文件：密钥就明文躺在 ``api_key`` 里。必须照常读得进来，
        # 升级不能让用户手上的旧文件变成废纸（见界面 7）。
        return UnsealResult(document, UNSEAL_PLAINTEXT, _count_plaintext_keys(document), None, False)

    if not isinstance(raw_seal, dict):
        return UnsealResult(_stripped(document), UNSEAL_CORRUPT, 0, None, True)

    expires_raw = raw_seal.get("expires_at")
    expires_at = expires_raw if isinstance(expires_raw, str) else None

    if raw_seal.get("format") != SEAL_FORMAT or raw_seal.get("key_id") != EMBEDDED_KEY_ID:
        # 不是我这把密钥能解的：要么对方软件比我新（换过密钥），要么格式变了。
        # 两种都是「请更新软件」，配置部分仍然可以照常导入。
        return UnsealResult(_stripped(document), UNSEAL_UNSUPPORTED, 0, expires_at, True)

    try:
        epk = base64.b64decode(str(raw_seal.get("epk") or ""), validate=True)
        nonce = base64.b64decode(str(raw_seal.get("nonce") or ""), validate=True)
        ciphertext = base64.b64decode(str(raw_seal.get("ciphertext") or ""), validate=True)
        shared = _embedded_private_key().exchange(X25519PublicKey.from_public_bytes(epk))
        aes_key = _derive_aes_key(shared, epk, EMBEDDED_KEY_ID)
        plaintext = AESGCM(aes_key).decrypt(
            nonce, ciphertext, _aad(document, EMBEDDED_KEY_ID, expires_at)
        )
        entries = json.loads(plaintext.decode("utf-8"))
    except (InvalidTag, ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        # 认证失败。可能是传输损坏，也可能是有人改过正文（AAD 锁着正文哈希）。
        # 分不清是哪一种，也不该分——两种都不可信。
        return UnsealResult(_stripped(document), UNSEAL_CORRUPT, 0, expires_at, True)

    if not isinstance(entries, list):
        return UnsealResult(_stripped(document), UNSEAL_CORRUPT, 0, expires_at, True)

    # 有效期在解密**之后**才判：先判的话，一份被人动过手脚的文件会被报成「已过期」，
    # 而它真正的问题是不可信。先证明文件是真的，再谈它是不是过期了。
    deadline = _parse_expiry(expires_at)
    if deadline is not None and datetime.now(timezone.utc) > deadline:
        return UnsealResult(_stripped(document), UNSEAL_EXPIRED, len(entries), expires_at, True)

    restored_document = _stripped(document)
    restored = _restore_secrets(restored_document, entries)
    return UnsealResult(restored_document, UNSEAL_OK, restored, expires_at, True)


def _stripped(document: dict[str, Any]) -> dict[str, Any]:
    """去掉加密段的文档副本。解析器不认识这个字段，留着只会变成一次无谓的校验。"""
    clone = json.loads(json.dumps(document))
    clone.pop(SEALED_FIELD, None)
    return clone


def _count_plaintext_keys(document: dict[str, Any]) -> int:
    """数一数旧版明文文件里有几把密钥，只为界面上能说个数，不改动文档。"""
    found: list[dict[str, Any]] = []
    _extract_secrets(json.loads(json.dumps(document)), [], found)
    return len(found)
