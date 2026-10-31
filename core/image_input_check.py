"""模型能不能识别图片：发图之前先确认，确认不了就不发用户的页面。

做法学的是「发图前先查、不行就不发」，但「能不能看图」不靠写死的模型表，也不让用户
自己勾：模型换得太快，同一个模型经过不同中转站结果也不一样。这里由软件自己发一张
写着随机码的小图，让模型读出来：

* 读对了 → ``yes``：这条连接上的这个模型能识别图片。
* 接口报错，而且报的是「不收图」 → ``no``：换模型才行。
* 回了话却没读出码 → ``unseen``：图片多半没送到模型（中转站把图丢了，或者模型没看图
  就作答）。这种审核结果不可信，比报错更糟，所以单列出来让用户决定。

结论按「服务商 + 地址 + 协议 + 模型 + 密钥」记在设置的 ``image_input_checks`` 里，
不分用途：审核跟随文本翻译模型时，测的就是同一条路。改了其中任何一项，就查不到旧结论，
回到「未确认」。

任务里还有一道保底（``ImageInputGuard``）：这个模型在本次任务里还一次都没成功过，
又连续两页报「不收图」一类的错，就不再发图。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx
from loguru import logger
from PIL import Image, ImageDraw

IMAGE_INPUT_YES = "yes"
IMAGE_INPUT_NO = "no"
IMAGE_INPUT_UNSEEN = "unseen"
IMAGE_INPUT_RESULTS = frozenset({IMAGE_INPUT_YES, IMAGE_INPUT_NO, IMAGE_INPUT_UNSEEN})
# 探测本身没有结论（网络、密钥、额度、服务商报错）：不记结论，按原来的方式提示。
PROBE_ERROR = "error"

# 设置里最多留这么多条结论，按检查时间丢掉最旧的。
MAX_STORED_CHECKS = 40

# 开始任务前的自动检查只试一次、最多等这么久；超时不拦，交给任务中的保底。
PREFLIGHT_PROBE_TIMEOUT_SECONDS = 30.0

# 给用户看的结论说明。
IMAGE_INPUT_NO_MESSAGE = "该模型不能识别图片。图像翻译审核需要能识别图片的模型，请换一个支持图片输入的模型。"
IMAGE_INPUT_UNSEEN_MESSAGE = (
    "模型有回复，但没有读出测试图中的文字，图片可能未送达模型。请换一个能识别图片的模型，或更换连接。"
)
IMAGE_INPUT_YES_MESSAGE = "模型读出了测试图中的文字，能识别图片。"


# ── 测试图 ──────────────────────────────────────────────────
# 每次随机 4 个字符，模型猜不到；不用 0/O、1/I、2/Z、5/S、8/B、6/G 这类容易看错的字。
PROBE_CODE_ALPHABET = "ACDEFHKMNPRTUWXY3479"
PROBE_CODE_LENGTH = 4

# 自带 5×7 点阵字形：不依赖系统字体，各平台画出来一模一样（测试也能据此读回）。
PROBE_GLYPHS: dict[str, tuple[str, ...]] = {
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "C": (".####", "#....", "#....", "#....", "#....", "#....", ".####"),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "3": ("####.", "....#", "....#", ".###.", "....#", "....#", "####."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "....#", ".###."),
}
PROBE_CELL_PX = 12      # 一个点画成 12×12 像素
PROBE_GLYPH_GAP = 2     # 字与字之间空 2 个点
PROBE_MARGIN = 3        # 四周留 3 个点

PROBE_INSTRUCTIONS = (
    "You are checking whether an attached image reaches you. "
    "Read the characters printed in the image and reply with them only."
)
PROBE_USER_TEXT = "Which characters are printed in this image? Reply with those characters only."


def new_probe_code() -> str:
    return "".join(secrets.choice(PROBE_CODE_ALPHABET) for _ in range(PROBE_CODE_LENGTH))


def draw_probe_image(code: str, path: Path) -> Path:
    """白底黑字画出 ``code``，存成 PNG（4 个字约 384×156 像素，几百 token）。"""
    cell = PROBE_CELL_PX
    columns = PROBE_MARGIN * 2 + len(code) * 5 + max(0, len(code) - 1) * PROBE_GLYPH_GAP
    rows = PROBE_MARGIN * 2 + 7
    image = Image.new("RGB", (columns * cell, rows * cell), "white")
    draw = ImageDraw.Draw(image)
    for index, char in enumerate(code):
        left = PROBE_MARGIN + index * (5 + PROBE_GLYPH_GAP)
        for row, line in enumerate(PROBE_GLYPHS[char]):
            for col, mark in enumerate(line):
                if mark == "#":
                    x0 = (left + col) * cell
                    y0 = (PROBE_MARGIN + row) * cell
                    draw.rectangle((x0, y0, x0 + cell - 1, y0 + cell - 1), fill="black")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")
    return path


def reply_has_code(reply: str, code: str) -> bool:
    """模型的回复里有没有这串码（不分大小写，忽略空格、横线这类分隔）。"""
    normalized = re.sub(r"[^0-9A-Z]", "", str(reply or "").upper())
    return bool(code) and code.upper() in normalized


# ── 报错是不是「不收图」 ─────────────────────────────────────
_IMAGE_WORDS = (
    "image", "vision", "visual", "multimodal", "multi-modal", "modalit", "vlm",
    "图片", "图像", "视觉", "多模态",
)
_REFUSAL_PHRASES = (
    "not support", "n't support", "unsupported", "only supported", "not supported",
    "unknown variant", "not a multimodal", "not multimodal", "not a vlm",
    "text-only", "text only", "not accept", "cannot accept", "no endpoints found",
    "不支持", "仅支持文本", "只支持文本", "不能识别",
)
# 这些说明模型其实收图，只是这一张出了问题（太大、格式、内容审核），或者是连接本身的问题。
_NOT_A_REFUSAL = (
    "too large", "too big", "exceed", "maximum", "max size", "size limit", "pixels",
    "dimension", "resolution", "format", "decode", "corrupt", "download", "fetch",
    "content_policy", "content policy", "moderation", "safety", "sensitive", "inappropriate",
    "invalid api key", "incorrect api key", "unauthorized", "authentication", "forbidden",
    "insufficient_quota", "quota", "billing", "balance", "rate limit", "too many requests",
    "model not found", "model does not exist", "no such model", "does not exist",
    "过大", "超过", "超出", "格式", "违规", "敏感", "余额", "额度", "欠费", "未授权", "模型不存在",
)
# 硅基流动对非视觉模型发图的固定错误码（2026-10-21 真实账号实测：400 + code 20041）。
_SILICONFLOW_NOT_VLM_CODE = "20041"


def _error_chain(exc: BaseException) -> list[BaseException]:
    """包装过的报错（``raise … from exc``）一层层往里看；不跟 ``__context__``：那是
    处理别的报错时顺带碰上的，比如自动模式下先试过的另一种接口。"""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < 6:
        chain.append(current)
        current = current.__cause__
    return chain


def _readable_json(body: str) -> str:
    """JSON 报错体里的中文常被转义成 ``\\uXXXX``，还原了才认得出「不支持」这类词。"""
    try:
        return json.dumps(json.loads(body), ensure_ascii=False)
    except (TypeError, ValueError):
        return body


def _status_and_text(exc: BaseException) -> tuple[int | None, str]:
    """链上最里层的 HTTP 报错：状态码 + 服务商回的原文（不含请求地址，地址里的词不算数）。"""
    for item in reversed(_error_chain(exc)):
        response = getattr(item, "response", None)
        if isinstance(item, httpx.HTTPStatusError) and response is not None:
            try:
                body = response.text
            except Exception:  # noqa: BLE001 - a stream that cannot be read again
                body = ""
            return int(response.status_code), _readable_json(body)
    return None, ""


def looks_like_no_image_input(exc: BaseException) -> bool:
    """这次报错是不是在说「这个模型不收图片」。

    只认得出来的才算：网络、密钥、额度、模型名写错、图片太大、内容审核都不算——那时换
    模型没用，提示会把人带偏。认不出的报错照原来的方式处理。
    """
    if any(isinstance(item, (httpx.TransportError, TimeoutError)) for item in _error_chain(exc)):
        return False
    status, raw = _status_and_text(exc)
    if status is None or status in {401, 402, 403, 408, 413, 429} or status >= 500:
        return False
    text = raw.lower()
    if status == 400 and _SILICONFLOW_NOT_VLM_CODE in text:
        return True
    if any(marker in text for marker in _NOT_A_REFUSAL):
        return False
    return any(word in text for word in _IMAGE_WORDS) and any(
        phrase in text for phrase in _REFUSAL_PHRASES
    )


# ── 结论存放 ────────────────────────────────────────────────
def image_input_key(config: Any) -> str:
    """不分用途的一条路：服务商 + 地址 + 协议 + 模型 + 密钥（只取摘要）。"""
    api_key = str(getattr(config, "api_key", "") or "").strip()
    parts = [
        str(getattr(config, "provider", "") or "").strip(),
        str(getattr(config, "base_url", "") or "").strip().rstrip("/"),
        str(getattr(config, "api_mode", "") or "").strip(),
        str(getattr(config, "model", "") or "").strip(),
        hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12] if api_key else "",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]


def image_input_verdict(settings: Any, config: Any) -> str:
    """设置里记下的结论：yes / no / unseen；没测过（或配置变了）是 ""。"""
    checks = getattr(settings, "image_input_checks", None) or {}
    entry = checks.get(image_input_key(config))
    result = str(getattr(entry, "result", "") or "")
    return result if result in IMAGE_INPUT_RESULTS else ""


def image_input_message(settings: Any, config: Any) -> str:
    checks = getattr(settings, "image_input_checks", None) or {}
    entry = checks.get(image_input_key(config))
    return str(getattr(entry, "message", "") or "")


def set_image_input_verdict(settings: Any, config: Any, result: str, message: str, checked_at: str) -> None:
    """把结论写进这份设置（只改内存里的这一份；存盘见 ``save_image_input_verdict``）。"""
    from settings import ImageInputCheck

    if result not in IMAGE_INPUT_RESULTS:
        return
    checks = dict(getattr(settings, "image_input_checks", None) or {})
    checks[image_input_key(config)] = ImageInputCheck(
        result=result,
        model=str(getattr(config, "model", "") or ""),
        message=message,
        checked_at=checked_at,
    )
    if len(checks) > MAX_STORED_CHECKS:
        newest = sorted(checks.items(), key=lambda item: item[1].checked_at, reverse=True)
        checks = dict(newest[:MAX_STORED_CHECKS])
    settings.image_input_checks = checks


def save_image_input_verdict(
    config: Any,
    result: str,
    message: str,
    *,
    settings: Any = None,
    role: str = "",
    availability_message: str = "",
) -> None:
    """记下结论并存盘。

    ``settings`` 是调用方手里那一份（任务的设置副本），同步改上，免得这次任务里还查不到。
    存盘走 ``update_settings_atomically``：任务跑的时候用户可能正在改设置，只改这一条，
    不拿任务开始时的旧设置整份盖回去。给了 ``role`` 时，这个用途的主用连接如果还是这条
    配置，顺手把它的「测试通过 / 测试失败」也记上，设置页和下次开始任务都看得到。
    """
    from core.model_roles import (
        model_config_signature,
        record_model_role_availability,
        resolve_effective_model_config,
    )
    from settings import update_settings_atomically

    if result not in IMAGE_INPUT_RESULTS:
        return
    checked_at = datetime.now().isoformat(timespec="seconds")
    if settings is not None:
        set_image_input_verdict(settings, config, result, message, checked_at)
    signature = model_config_signature(config)

    def update(current: Any) -> bool:
        set_image_input_verdict(current, config, result, message, checked_at)
        if role:
            try:
                primary = resolve_effective_model_config(current, role)
            except Exception:  # noqa: BLE001 - the role is broken now; the verdict still stands
                primary = None
            if primary is not None and model_config_signature(primary) == signature:
                record_model_role_availability(
                    current,
                    role,
                    ok=result == IMAGE_INPUT_YES,
                    message=availability_message or message,
                    signature=signature,
                    checked_at=checked_at,
                )
        return True

    try:
        update_settings_atomically(update)
    except Exception as exc:  # noqa: BLE001 - a verdict that could not be saved is re-checked next time
        logger.warning(f"[识图检查] 结论未能存入设置：{exc}")


# ── 实测 ────────────────────────────────────────────────────
@dataclass(frozen=True)
class ImageInputProbe:
    result: str           # yes / no / unseen / error
    message: str          # 给用户看的一句（error 时是空的，由调用方按原来的方式说）
    reply: str = ""       # 模型的原始回复（只进日志）
    error: BaseException | None = None


AskImages = Callable[[str, str, list[Path]], str]


def probe_image_input(ask: AskImages, *, attempts: int = 1) -> ImageInputProbe:
    """发一张带随机码的小图让模型读出来。

    ``ask(instructions, user_text, images)`` 发出请求、返回模型回复的文字，失败就抛错
    （审核用的 ``OpenAICompatiblePdfReviewClient.read_images`` 就是这样）。只有网络、
    服务商这类没有结论的失败才重试；读对、拒收、读不出都是结论，不再重试。
    """
    last_exc: BaseException | None = None
    with tempfile.TemporaryDirectory() as tmp:
        for _attempt in range(max(1, int(attempts))):
            code = new_probe_code()
            image = draw_probe_image(code, Path(tmp) / "image_input_probe.png")
            try:
                reply = ask(PROBE_INSTRUCTIONS, PROBE_USER_TEXT, [image])
            except Exception as exc:  # noqa: BLE001 - classified below
                last_exc = exc
                if looks_like_no_image_input(exc):
                    logger.debug(f"[识图检查] 模型拒收图片：{exc!r}")
                    return ImageInputProbe(IMAGE_INPUT_NO, IMAGE_INPUT_NO_MESSAGE, error=exc)
                logger.debug(f"[识图检查] 请求失败：{exc!r}")
                continue
            if reply_has_code(reply, code):
                return ImageInputProbe(IMAGE_INPUT_YES, IMAGE_INPUT_YES_MESSAGE, reply=reply)
            logger.debug(f"[识图检查] 没读出测试码 {code}，模型回复：{str(reply)[:200]!r}")
            return ImageInputProbe(IMAGE_INPUT_UNSEEN, IMAGE_INPUT_UNSEEN_MESSAGE, reply=str(reply or ""))
    return ImageInputProbe(PROBE_ERROR, "", error=last_exc)


# ── 任务中的保底 ────────────────────────────────────────────
class ImageInputGuard:
    """这个模型在本次任务里还没成功过，又连续 ``threshold`` 页报同一类错，就该停发了。

    成功过一次就再也不会触发：之后某页报错只算那一页的问题（比如那页内容被拒）。
    ``classify`` 决定哪些报错算数，认不出的报错会把「连续」打断。同一页重试几次只算
    一页（``key`` 传页的标识）。
    """

    def __init__(
        self,
        classify: Callable[[BaseException], bool] = looks_like_no_image_input,
        *,
        threshold: int = 2,
    ) -> None:
        self._classify = classify
        self._threshold = max(1, int(threshold))
        self._lock = threading.Lock()
        self._succeeded = False
        self._streak: set[Any] = set()
        self._tripped = False

    @property
    def tripped(self) -> bool:
        with self._lock:
            return self._tripped

    def succeeded(self) -> None:
        with self._lock:
            self._succeeded = True
            self._streak.clear()

    def failed(self, exc: BaseException, *, key: Any = None) -> bool:
        """记一次失败；正好在这一次触发时返回 True（之后再失败返回 False）。"""
        matches = self._classify(exc)
        with self._lock:
            if self._tripped or self._succeeded:
                return False
            if not matches:
                self._streak.clear()
                return False
            self._streak.add(object() if key is None else key)
            if len(self._streak) >= self._threshold:
                self._tripped = True
                return True
            return False


# ── 图像翻译模型（出图）：这一页的报错是不是「这个模型生成不了图」 ─────────
# 图片太大、内容审核这类只跟这一页有关，换模型也没用，不算。
_PAGE_SPECIFIC = (
    "too large", "too big", "exceed", "maximum", "size limit", "pixels",
    "content_policy", "content policy", "moderation", "safety", "sensitive", "inappropriate",
    "过大", "超过", "超出", "违规", "敏感",
)


def image_model_refused_page(exc: BaseException) -> bool:
    """图像翻译模型这一页的报错像不像「填了生成不了图的模型」（如文字模型）。"""
    from core.image_generation import looks_like_wrong_image_model

    if not looks_like_wrong_image_model(exc):
        return False
    _status, body = _status_and_text(exc)
    text = (body or " ".join(str(item) for item in _error_chain(exc))).lower()
    return not any(marker in text for marker in _PAGE_SPECIFIC)


def save_role_availability(config: Any, role: str, *, ok: bool, message: str) -> None:
    """把某个用途的「测试通过 / 测试失败」存回设置（任务里发现的，下次开始前就知道）。

    只在这个用途的主用连接还是这条配置时才写：任务跑的时候用户可能已经换了模型。
    """
    from core.model_roles import (
        model_config_signature,
        record_model_role_availability,
        resolve_effective_model_config,
    )
    from settings import update_settings_atomically

    checked_at = datetime.now().isoformat(timespec="seconds")
    signature = model_config_signature(config)

    def update(current: Any) -> bool:
        try:
            primary = resolve_effective_model_config(current, role)
        except Exception:  # noqa: BLE001 - the role is broken now; nothing to record
            return False
        if model_config_signature(primary) != signature:
            return False
        record_model_role_availability(
            current, role, ok=ok, message=message, signature=signature, checked_at=checked_at,
        )
        return True

    try:
        update_settings_atomically(update)
    except Exception as exc:  # noqa: BLE001 - only a hint for next time
        logger.warning(f"[识图检查] 测试结论未能存入设置：{exc}")
