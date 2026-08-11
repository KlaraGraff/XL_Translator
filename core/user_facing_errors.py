"""Turn raw exception text into one sentence a non-technical user can act on.

The app used to hand users the exception string verbatim.  Real examples from a
UI walkthrough: ``407:413: syntax error ... (-2741)`` for an AppleScript that
Word for Mac no longer accepts, ``[Errno 8] nodename nor servname provided`` for
a DNS failure, ``Package not found at '...'`` for a corrupted .docx.  None of
those tell the user what happened or what to do next.

Rules this module follows:

* One sentence, cause first, then the one action worth taking.  No error codes,
  no library names, no URLs.
* Only rewrite what we recognize.  An unmatched message is returned as-is
  (minus obvious noise) — hiding an unknown failure behind a vague sentence is
  worse than showing the raw text, because it destroys the only clue we have.
* Never invent a cause.  Every pattern below was observed in a real run or is a
  documented error string of a library we call.
* The raw text stays available to whoever logs at debug level; this function is
  for the sentence that reaches the user's eyes.
"""

from __future__ import annotations

import re

__all__ = ["humanize_error", "strip_error_noise"]


# Lines that carry nothing for the reader.  httpx appends the MDN link to every
# HTTP status error; tracebacks leak internals.
_NOISE_PATTERNS = (
    re.compile(r"^For more information check:.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Traceback \(most recent call last\):.*$", re.IGNORECASE | re.MULTILINE),
)


def strip_error_noise(value: object) -> str:
    """Collapse an exception (or message) to a single trimmed line."""
    raw = value if isinstance(value, str) else str(value or "")
    for pattern in _NOISE_PATTERNS:
        raw = pattern.sub("", raw)
    return " ".join(raw.split()).strip()


# (markers, sentence).  Markers are regexes matched against the whole message
# case-insensitively; the first rule with any match wins, so specific rules go
# above general ones.  Bare status codes are wrapped in \b so a count or a byte
# size never trips them.
_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    # --- 本机 Office 自动化 ---
    # 这四句只说本机 Office 那边发生了什么。是否有内置方式接手要由调用方补充：
    # Word 编号预处理确实会接手，.xls 转换不会（不选兼容转换就是失败），
    # 在共用句子里替所有调用方承诺「已改用内置方式处理」会当场说谎。
    (
        (
            r"-1743",
            r"not authorized to send apple events",
            r"assistive access",
            # AppleScript 的授权拒绝也可能只报一句 permission denied / declined
            # permission，必须在下面那条「文件被占用」之前认出来，否则会把人指去
            # 关闭无关程序。
            r"declined permission",
            r"apple ?events?.{0,40}(not permitted|permission denied)",
            r"(not permitted|permission denied).{0,40}apple ?events?",
        ),
        "系统没有授权本程序控制 Office。可在「系统设置 › 隐私与安全性 › 自动化」里授权后重试。",
    ),
    (
        (r"-2741", r"syntax error", r"-1708", r"doesn['’]t understand"),
        "本机 Office 不支持这项自动化操作。",
    ),
    (
        (r"-600\b", r"-609\b", r"application isn['’]t running"),
        "本机 Office 没有响应。",
    ),
    (
        (r"-10814", r"can['’]t be found", r"no such application"),
        "本机没有安装 Microsoft Office。",
    ),
    # --- 文件本身的问题 ---
    (
        (r"package not found at", r"not a zip file", r"badzipfile"),
        "这个文件打不开，可能已损坏，或者后缀名与真实格式不符。",
    ),
    (
        (r"does not support the old \.xls", r"\bxlrd\b", r"unsupported format, or corrupt file"),
        "这是旧版 .xls 格式，需要先转换成 .xlsx 才能翻译。",
    ),
    (
        (r"has been corrupted", r"cannot open broken document", r"cannot open document", r"failed to open"),
        "这个文件打不开，可能已损坏或设置了打开密码。",
    ),
    (
        (r"is encrypted", r"password required", r"workbook is protected"),
        "这个文件有密码保护，程序无法读取。请先去掉密码再试。",
    ),
    (
        (r"no such file or directory", r"filenotfounderror", r"cannot find the file"),
        "文件不存在或已被移动，请重新选择来源。",
    ),
    (
        (r"permission denied", r"used by another process", r"access is denied"),
        "文件被其他程序占用，或者没有读写权限，请关闭占用它的程序后重试。",
    ),
    (
        (r"no space left on device", r"disk full", r"not enough space"),
        "磁盘空间不足，无法写出结果文件。",
    ),
    # --- 网络与接口 ---
    (
        (
            r"nodename nor servname",
            r"name or service not known",
            r"temporary failure in name resolution",
            r"getaddrinfo failed",
            r"failed to resolve",
        ),
        "连不上网络：解析不到接口地址，请检查网络、代理，以及接口地址是否写对。",
    ),
    (
        (r"certificate verify failed", r"\bsslerror\b", r"ssl: ", r"self.signed certificate"),
        "安全连接校验没通过，通常是代理或安全软件拦截了加密流量。",
    ),
    (
        (r"connection refused", r"failed to establish a new connection", r"connection reset", r"connection aborted"),
        "接口拒绝了连接，请检查网络，以及这条连接的地址是否可用。",
    ),
    (
        (r"timed out", r"timeout"),
        "等接口响应超时，请稍后重试；网络不稳定时可以把并发调低一些。",
    ),
    (
        (r"invalid api key", r"incorrect api key", r"\bunauthorized\b", r"\b401\b"),
        "接口拒绝了这个 API Key，请在设置里检查这条连接的 Key 是否正确。",
    ),
    (
        (r"insufficient_quota", r"billing hard limit", r"余额不足", r"额度不足", r"\b402\b"),
        "这个账号的接口额度或余额不够了，请充值或换一条连接。",
    ),
    (
        (r"\bforbidden\b", r"\b403\b"),
        "接口拒绝了这次请求，可能是这个 Key 没有该模型的权限。",
    ),
    (
        (r"model_not_found", r"do not have access to", r"\b404\b"),
        "接口上找不到这个模型，请在设置里确认模型名是否写对。",
    ),
    (
        (r"rate limit", r"too many requests", r"\b429\b", r"并发达到上限"),
        "接口反馈请求过于频繁，程序已自动放慢并重试。",
    ),
    (
        (r"service unavailable", r"service temporarily unavailable", r"\b503\b"),
        "接口所在的服务暂时不可用，请稍后重试，或在设置里换一条连接。",
    ),
    (
        (r"bad gateway", r"gateway time", r"\b502\b", r"\b504\b"),
        "接口网关暂时异常，请稍后重试。",
    ),
    (
        (r"internal server error", r"\b500\b"),
        "接口服务端出错了，请稍后重试。",
    ),
)

_COMPILED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile("|".join(markers), re.IGNORECASE), sentence) for markers, sentence in _RULES
)


def humanize_error(value: object, fallback: str = "") -> str:
    """Return one user-readable sentence for ``value``.

    ``value`` may be an exception or an already-stringified message.  Unknown
    messages come back as-is (noise stripped) unless ``fallback`` is given, in
    which case ``fallback`` is used — pass it only where the raw text is known
    to be useless to the reader.
    """
    message = strip_error_noise(value)
    if not message:
        return fallback
    for pattern, sentence in _COMPILED:
        if pattern.search(message):
            return sentence
    return fallback or message
