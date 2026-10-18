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
        "系统未授权本程序控制 Office。请在「系统设置 › 隐私与安全性 › 自动化」中授权后重试。",
    ),
    (
        # AppleScript 等 Office 回话超过两分钟（-1712）。必须在下面通用的 timed out
        # 那条之前认出来，否则会被说成「模型服务响应超时」。
        (r"-1712", r"apple ?event timed out"),
        "本机 Office 长时间没有响应，可能有提示框在等待操作，或文件无法打开。",
    ),
    (
        (r"-2741", r"syntax error", r"-1708", r"doesn['’]t understand"),
        "本机 Office 不支持这项自动化操作。",
    ),
    (
        (r"-600\b", r"-609\b", r"application isn['’]t running"),
        "本机 Office 无响应。",
    ),
    (
        (r"-10827", r"klsnoexecutablerr"),
        "macOS 找不到可启动的 Microsoft Excel，可能是 Excel 安装或系统注册异常；请先手动打开一次 Excel，如仍失败，请修复或重新安装 Office。",
    ),
    (
        (r"-10814", r"can['’]t be found", r"no such application"),
        "本机未安装 Microsoft Office。",
    ),
    # --- 文件本身的问题 ---
    (
        (r"package not found at", r"not a zip file", r"badzipfile"),
        "无法打开此文件，可能已损坏，或扩展名与实际格式不符。",
    ),
    (
        (r"does not support the old \.xls", r"\bxlrd\b", r"unsupported format, or corrupt file"),
        "此文件为旧版 .xls 格式，需先转换为 .xlsx 才能翻译。",
    ),
    (
        (r"has been corrupted", r"cannot open broken document", r"cannot open document", r"failed to open"),
        "无法打开此文件，可能已损坏或设置了打开密码。",
    ),
    (
        (r"is encrypted", r"password required", r"workbook is protected"),
        "此文件受密码保护，无法读取。请先移除密码后重试。",
    ),
    (
        (r"no such file or directory", r"filenotfounderror", r"cannot find the file"),
        "文件不存在或已被移动，请重新选择来源。",
    ),
    (
        (r"permission denied", r"used by another process", r"access is denied"),
        "文件被其他程序占用，或没有读写权限。请关闭占用该文件的程序后重试。",
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
        "无法连接网络：无法解析接口地址，请检查网络、代理以及接口地址是否正确。",
    ),
    (
        (r"certificate verify failed", r"\bsslerror\b", r"ssl: ", r"self.signed certificate"),
        "安全连接校验未通过，通常是代理或安全软件拦截了加密流量。",
    ),
    (
        (
            r"connection refused",
            r"failed to establish a new connection",
            r"connection reset",
            r"connection aborted",
            # httpx 的 ConnectError：端口不通、地址写错时就是这一句。
            r"all connection attempts failed",
        ),
        "接口拒绝了连接，请检查网络，以及这条连接的地址是否可用。",
    ),
    (
        (r"timed out", r"timeout"),
        # 原文写的是「网络不稳定时可以把并发调低一些」。2026-08 对中转站的 125 次
        # 调用做过延迟统计：两个延迟峰在启动时的并发均值是 8.76 对 8.77，延迟跟并发
        # 度根本不相关。照着这句去调并发，只会让人白折腾一轮还是超时，所以那句删掉。
        #
        # 但也不能反过来断言「不是本机网络的问题」——这条规则是全局的，Excel/Word 文本
        # 翻译、文件扫描、格式转换都会走到它，挂着不稳的 VPN 跑批量时那句话正好说反。
        # 而且 502/504 网关故障的报文里也带 "timeout"，会先被这一条吃掉。
        # 所以只说观察得到的事实（重试常常就过）和一个不越界的倾向（反复超时多半在上游）。
        "模型服务响应超时，重试通常即可解决；如反复超时，多为该连接的服务端正在排队。",
    ),
    (
        (r"invalid api key", r"incorrect api key", r"\bunauthorized\b", r"\b401\b"),
        "接口拒绝了此 API 密钥，请在设置中检查该连接的密钥是否正确。",
    ),
    (
        (r"insufficient_quota", r"billing hard limit", r"余额不足", r"额度不足", r"\b402\b"),
        "该账号的接口额度或余额不足，请充值或更换连接。",
    ),
    (
        (r"\bforbidden\b", r"\b403\b"),
        "接口拒绝了本次请求，可能是该 API 密钥没有此模型的访问权限。",
    ),
    (
        (r"model_not_found", r"do not have access to", r"\b404\b"),
        "接口上找不到此模型，请在设置中确认模型名称是否正确。",
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
        "模型服务返回错误，请稍后重试。",
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
