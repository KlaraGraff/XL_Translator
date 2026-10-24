"""在本机用真实图纸比一比「文字识别组件」认得准不准。只在本机运行，不上传任何文件。

    python scripts/ocr_benchmark.py OUT_DIR PDF或文件夹 [...] [--max-pages 3] [--tesseract]

* 有文字层的页：文字层就是标准答案。整页按原位替换的分辨率（200 dpi）渲染，用识别模型读
  一遍，逐行和文字层比（中文、日文行和西文行分开算）。
* 没有文字层的页（字画成线条）：没有标准答案，输出每页读到的文字和框（``*_reads.txt``、
  ``*_boxes.png``），给人看。
* ``--tesseract``：西文行也用 Tesseract 读一遍（同样装进临时目录），比一比两种引擎。
* 每一行识别结果都按原位替换的规则分一次类（译成法文时：翻译、留给人复核、保留原样），
  算出「读错了却会被翻译」的行有多少，以及把把握门槛提高后能拦下多少（``decisions``、
  ``sweep``）。没有文字层的页按分类和原因计数，单个汉字会被翻译的，列出是哪些字。

* ``--dpi`` / ``--ocr-dpi``：整页和识别模型看到的分辨率（原位替换用 200 和 300），可换着比。
* ``--det`` / ``--rec``：换检测 / 识别模型比一比。``small`` 是组件里装的那个；``medium``
  （PP-OCRv6 medium）、``server5``（PP-OCRv5 server）从 ModelScope 下载（核对 SHA256）；也可以
  给一个 .onnx 文件。
* ``--vlm images,sheet``：有文字层的页，把每一行字的小块发给设置里的「图像翻译审核模型」读
  （``images``：一块一张图；``sheet``：多块拼成一张编号的图），和识别模型、和「只把没把握的行
  交给看图模型」（hybrid）一起算准确率、请求次数、用时和 token。这会把图纸的文字小块发给那个
  模型服务，要先得到图纸主人同意；要读真实设置里的连接，用 TRANSLATOR_APP_DATA_DIR 指向
  真实数据目录的一份副本。
* 每种读法读错的行按原因分开数（``misses``）：没读到、和旁边的字读成一行、读短了、读错字。

识别模型和「PDF 处理组件」装进一个临时的应用数据目录（设了 TRANSLATOR_APP_DATA_DIR 时用它），
不碰本机已装的组件和真实设置。结果全在 OUT_DIR 里（含图纸内容，别上传）；summary.json 只有数字
（和会被翻译的单个汉字）。
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DPI = 200
_CJK = re.compile(r"[぀-ヿ㐀-鿿]")


def _norm(text: str) -> str:
    text = re.sub("[‐‑‒–—−]", "-", unicodedata.normalize("NFKC", text))
    return re.sub(r"\s+", "", text).casefold()


def _lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _inter(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def _area(b) -> float:
    return max(1.0, (b[2] - b[0]) * (b[3] - b[1]))


def _matched_text(truth: dict, rows: list[dict]) -> str:
    box = truth["display_box"]
    hits = [r for r in rows if _inter(r["display_box"], box) >= 0.5 * min(_area(r["display_box"]), _area(box))]
    rot = truth.get("display_rotation", 0)
    if rot == 90:
        hits.sort(key=lambda r: -r["display_box"][3])
    elif rot == 270:
        hits.sort(key=lambda r: r["display_box"][1])
    else:
        hits.sort(key=lambda r: (round(r["display_box"][1] / 20), r["display_box"][0]))
    return "".join(r["source_text"] for r in hits)


def _overlapping(row: dict, truth: list[dict]) -> list[dict]:
    box = row["display_box"]
    return [t for t in truth if _inter(t["display_box"], box) >= 0.5 * min(_area(t["display_box"]), _area(box))]


def _min_char(row: dict) -> float:
    words = row.get("words") or []
    probs = [w["score"] for w in words if _CJK.search(w["text"])] or [w["score"] for w in words]
    return min(probs) if probs else float(row.get("ocr_score") or 0.0)


def _decide(rows: list[dict], lang: str, target: str) -> None:
    """Each row's decision under the in-place rules (core.pdf_vector.classify), no model call."""
    from core.pdf_vector.classify import classify, harmonize
    from core.pdf_vector.common import norm_text

    harmonize(rows, lang)
    strong = {norm_text(r["source_text"]).casefold() for r in rows
              if (r.get("ocr_score") or 0) >= 0.9 and int(r.get("display_rotation", 0)) == 0}
    for r in rows:
        r["decision"], r["decision_reason"] = classify(r, 0.45, lang, strong, target)


SWEEP = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95)
# 「没把握」的行：和原位替换的中日文门槛一样（core.pdf_vector.classify）
UNSURE_SCORE, UNSURE_CHAR = 0.9, 0.6


def _miss_kind(want: str, got: str) -> str:
    """一行读错的原因：没读到 / 和旁边的字读成一行 / 读短了 / 读错字。"""
    if not got:
        return "missed"
    if want in got:
        return "merged"
    if got in want:
        return "truncated"
    return "misread"


def _why_single(r: dict, rows: list[dict], V, Hs) -> tuple[str, str]:
    """一个没和邻字合起来的单个汉字：最近的同一行邻字为什么没合（对照 extract.join_spaced_cjk）。"""
    from core.pdf_vector.extract import SPACED_GAP_H, _spaced_candidate

    if not _spaced_candidate(r):
        return "self_not_candidate", f"rot={r.get('display_rotation')} col={bool(r.get('vertical_column'))}"
    a = r["display_box"]
    ha = a[3] - a[1]
    best = None
    for o in rows:
        if o is r:
            continue
        b = o["display_box"]
        hb = b[3] - b[1]
        if min(a[3], b[3]) - max(a[1], b[1]) < 0.6 * min(ha, hb):
            continue
        gap = b[0] - a[2] if b[0] >= a[0] else a[0] - b[2]
        if gap < -0.2 * ha or gap > 8 * ha:
            continue
        if best is None or gap < best[0]:
            best = (gap, o)
    if best is None:
        return "no_neighbor_on_line", ""
    gap, o = best
    b = o["display_box"]
    hb = b[3] - b[1]
    left, right = (a, b) if b[0] >= a[0] else (b, a)
    size = max(ha, hb)
    info = f"neighbor={o['source_text']!r} gap/h={gap / size:.2f} size={max(ha, hb) / max(1, min(ha, hb)):.2f}"
    if not _spaced_candidate(o):
        return "neighbor_not_candidate", info + f" rot={o.get('display_rotation')} engine={o.get('engine')}"
    if max(ha, hb) > 1.35 * min(ha, hb):
        return "size_ratio", info
    if gap > SPACED_GAP_H * size:
        return "gap_too_wide", info
    y0, y1 = max(a[1], b[1]), min(a[3], b[3])
    if any(left[2] - 1 <= x <= right[0] + 1 and s0 <= y0 + 0.2 * size and s1 >= y1 - 0.2 * size for x, s0, s1 in V):
        return "rule_between", info
    if any(q is not o and q is not r and q["display_box"][0] < right[0] - 1 and q["display_box"][2] > left[2] + 1
           and q["display_box"][1] < y1 and q["display_box"][3] > y0 for q in rows):
        return "box_between", info
    if len(o["source_text"].replace(" ", "")) > 1:
        return "neighbor_is_word_or_fork", info
    return "not_mutual_nearest", info


# ---------------------------------------------------------------- 大号识别模型

# RapidOCR 3.10.0 模型清单里的 ONNX（ModelScope），SHA256 也取自那份清单
_MODELSCOPE = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/"
CANDIDATES = {
    ("det", "medium"): ("PP-OCRv6/det/PP-OCRv6_det_medium.onnx",
                        "92078b7355007ccfffcd4c8cd441a3afd4538904d06881b29a155e1e679907c2"),
    ("det", "server5"): ("PP-OCRv5/det/ch_PP-OCRv5_det_server.onnx",
                         "0f8846b1d4bba223a2a2f9d9b44022fbc22cc019051a602b41a7fda9667e4cad"),
    ("rec", "medium"): ("PP-OCRv6/rec/PP-OCRv6_rec_medium.onnx",
                        "eef444829dbbe18d7fea59a3f6eb75647518d2b3a9568d27c92e42940204894b"),
    ("rec", "server5"): ("PP-OCRv5/rec/ch_PP-OCRv5_rec_server.onnx",
                         "e09385400eaaaef34ceff54aeb7c4f0f1fe014c27fa8b9905d4709b65746562a"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_model(kind: str, name: str, cache: Path) -> Path | None:
    """small：组件里装的那个（None）；medium / server5：下载并核对；其他当 .onnx 文件路径。"""
    if name == "small":
        return None
    if (kind, name) not in CANDIDATES:
        path = Path(name).expanduser()
        if not path.is_file():
            raise SystemExit(f"--{kind} {name}: not small / medium / server5 and not a file")
        return path
    relative, sha = CANDIDATES[(kind, name)]
    dest = cache / Path(relative).name
    if dest.is_file() and _sha256(dest) == sha:
        return dest
    import httpx

    cache.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    print(f"downloading {relative} …", flush=True)
    with httpx.stream("GET", _MODELSCOPE + relative, follow_redirects=True, timeout=120) as resp:
        resp.raise_for_status()
        with part.open("wb") as fh:
            for chunk in resp.iter_bytes(1 << 20):
                fh.write(chunk)
    if _sha256(part) != sha:
        part.unlink()
        raise SystemExit(f"{relative}: SHA256 does not match")
    part.replace(dest)
    return dest


def _use_models(det: Path | None, rec: Path | None, out: Path) -> dict[str, float]:
    """这次运行的识别进程改用给定的模型；返回用到的两个模型各多少 MB。"""
    from core import ppocr_component
    from core.ppocr_component_install import DET_MODEL, REC_MODEL

    paths = ppocr_component.reader_paths()
    if paths is None:
        return {}
    site, models, numpy_site = paths
    chosen = {DET_MODEL: det or models / DET_MODEL, REC_MODEL: rec or models / REC_MODEL}
    sizes = {"det_mb": round(chosen[DET_MODEL].stat().st_size / 1e6, 1),
             "rec_mb": round(chosen[REC_MODEL].stat().st_size / 1e6, 1)}
    if det is None and rec is None:
        return sizes
    folder = out / "_models"
    folder.mkdir(parents=True, exist_ok=True)
    for name, source in chosen.items():
        shutil.copyfile(source, folder / name)
    ppocr_component.reader_paths = lambda: (site, folder, numpy_site)
    return sizes


# ---------------------------------------------------------------- 看图模型读字

VLM_PROMPT = (
    "You transcribe text from small crops of engineering drawings. Each crop holds one label "
    "or one line of text, usually Chinese, Japanese, English or French, often with digits, units "
    "and symbols. Transcribe exactly what is printed: the same characters in the same order, "
    "keeping digits, units, punctuation and symbols such as ±, Φ, °, ×, ≥. Do not translate, "
    "correct, complete or explain. A tall crop may hold Chinese characters written top to "
    "bottom: give them in reading order. Neighbouring labels are blanked out of each crop: "
    "transcribe only the line in the crop, and copy every digit exactly as printed, never "
    "guessing one. If a crop has no readable text, give an empty string. "
    "Answer with strict JSON only: an object mapping every crop number (as a string) to its text."
)
VLM_MIN_HEIGHT = 48      # 小字放大到至少这么高（像素）
VLM_MAX_SIDE = 1600
SHEET_MAX_HEIGHT = 2400


def _row_crop(gray, row: dict, others: list[dict] = (), k: float = 1.0):
    """一行字的小块，转正（竖排的中文列保持竖着），小字放大。``gray`` 是识别模型看的那张图
    （``k`` 倍于行框的像素）；挤进小块的别的行涂白，看图模型只看到这一行。"""
    from PIL import Image, ImageDraw

    x0, y0, x1, y1 = (float(v) * k for v in row["display_box"])
    rot = int(row.get("display_rotation", 0))
    if rot == 0:
        # lines packed tighter than their boxes: each box gives up half of the overlap
        oy0, oy1 = y0, y1
        for o in others:
            if o is row or int(o.get("display_rotation", 0)) != 0:
                continue
            bx0, by0, bx1, by1 = (float(v) * k for v in o["display_box"])
            if min(x1, bx1) - max(x0, bx0) < 0.3 * min(x1 - x0, bx1 - bx0):
                continue
            if by0 < oy0 < by1 < oy1:
                y0 = max(y0, (oy0 + by1) / 2)
            elif oy0 < by0 < oy1 < by1:
                y1 = min(y1, (by0 + oy1) / 2)
    thick = (y1 - y0) if rot == 0 else (x1 - x0)
    pad = max(3.0, 0.15 * thick)
    cx0, cy0 = int(max(0, x0 - pad)), int(max(0, y0 - pad))
    cx1, cy1 = int(min(gray.width, x1 + pad)), int(min(gray.height, y1 + pad))
    crop = gray.crop((cx0, cy0, cx1, cy1))
    mask = Image.new("L", crop.size, 0)
    draw = ImageDraw.Draw(mask)
    for o in others:
        if o is row:
            continue
        ox0, oy0, ox1, oy1 = (float(v) * k for v in o["display_box"])
        if ox0 < cx1 and ox1 > cx0 and oy0 < cy1 and oy1 > cy0:
            m = 0.2 * min(ox1 - ox0, oy1 - oy0)   # tight boxes stop just inside the glyphs
            draw.rectangle((ox0 - cx0 - m, oy0 - cy0 - m, ox1 - cx0 + m, oy1 - cy0 + m), fill=255)
    if mask.getbbox():
        draw.rectangle((x0 - cx0, y0 - cy0, x1 - cx0, y1 - cy0), fill=0)
        crop.paste(255, mask=mask)
    if not row.get("vertical_column"):
        if rot == 90:
            crop = crop.rotate(-90, expand=True)
        elif rot == 270:
            crop = crop.rotate(90, expand=True)
    short = min(crop.size) if row.get("vertical_column") else crop.height
    scale = max(1.0, VLM_MIN_HEIGHT / max(1, short))
    scale = min(scale, VLM_MAX_SIDE / max(crop.size))
    if abs(scale - 1.0) > 0.01:
        crop = crop.resize((max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
                           Image.Resampling.LANCZOS)
    return crop


def _png_url(image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _sheet(crops: list) -> object:
    """多块拼成一张：每块左边写编号，块与块之间留白。"""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.load_default(size=30)
    margin, gap = 90, 20
    width = min(VLM_MAX_SIDE + margin, margin + max(c.width for c in crops))
    fitted = []
    for c in crops:
        if c.width > width - margin:
            k = (width - margin) / c.width
            c = c.resize((width - margin, max(1, round(c.height * k))))
        fitted.append(c)
    height = sum(c.height for c in fitted) + gap * (len(fitted) + 1)
    sheet = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(sheet)
    y = gap
    for n, c in enumerate(fitted, 1):
        draw.text((8, y + max(0, c.height // 2 - 16)), f"{n}.", fill=0, font=font)
        sheet.paste(c, (margin, y))
        y += c.height + gap
    return sheet


class VisionReader:
    """用设置里的「图像翻译审核模型」读字（和审核同一个连接、同一种接口）。"""

    def __init__(self, connection_id: str = "", model: str = "", workers: int = 4) -> None:
        from config import normalize_cloud_base_url
        from core.model_roles import ROLE_PDF_REVIEW, resolve_effective_model_config
        from core.pdf_review import review_routes
        from settings import load_settings

        self.cfg = resolve_effective_model_config(load_settings(), ROLE_PDF_REVIEW, connection_id=connection_id)
        if not self.cfg.api_key or not (model or self.cfg.model):
            raise SystemExit("the 图像翻译审核模型 connection has no key or no model")
        self.model = model or self.cfg.model
        self.base = normalize_cloud_base_url(self.cfg.provider, self.cfg.base_url)
        self.routes = review_routes(self.cfg)
        self.workers = workers
        self.stats: Counter = Counter()
        self._lock = threading.Lock()

    def describe(self) -> dict:
        return {"provider": self.cfg.provider, "model": self.model}

    def _payload(self, route: str, text: str, parts: list) -> dict:
        if route == "chat":
            content = [{"type": "text", "text": text}]
            for label, image in parts:
                if label:
                    content.append({"type": "text", "text": label})
                content.append({"type": "image_url", "image_url": {"url": _png_url(image)}})
            return {"model": self.model, "stream": False,
                    "messages": [{"role": "system", "content": VLM_PROMPT}, {"role": "user", "content": content}]}
        content = [{"type": "input_text", "text": text}]
        for label, image in parts:
            if label:
                content.append({"type": "input_text", "text": label})
            content.append({"type": "input_image", "image_url": _png_url(image)})
        return {"model": self.model, "instructions": VLM_PROMPT, "store": False, "stream": False,
                "input": [{"role": "user", "content": content}]}

    def _ask(self, text: str, parts: list) -> dict:
        import httpx

        from core.pdf_review import _extract_json_object, _extract_response_text
        from core.text_transport import extract_chat_text

        headers = {"Authorization": f"Bearer {self.cfg.api_key}", "Content-Type": "application/json"}
        for attempt in range(3):
            route = self.routes[0]
            started = time.monotonic()
            try:
                with httpx.Client(timeout=240) as client:
                    resp = client.post(f"{self.base}/{'chat/completions' if route == 'chat' else 'responses'}",
                                       headers=headers, json=self._payload(route, text, parts))
                if resp.status_code in (404, 405) and len(self.routes) > 1:
                    with self._lock:
                        if self.routes[0] == route:
                            self.routes = self.routes[1:]
                    continue
                if resp.status_code == 429 or resp.status_code >= 500:
                    time.sleep(5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                payload = resp.json()
                answer = extract_chat_text(payload) if route == "chat" else _extract_response_text(payload)
                usage = payload.get("usage") or {}
                with self._lock:
                    self.stats["requests"] += 1
                    self.stats["request_seconds"] += time.monotonic() - started
                    self.stats["tokens_in"] += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                    self.stats["tokens_out"] += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                return _extract_json_object(answer) or {}
            except Exception as exc:  # noqa: BLE001 - count it and try again
                with self._lock:
                    self.stats["errors"] += 1
                    self.stats[f"error {type(exc).__name__}"] += 1
                time.sleep(3)
        with self._lock:
            self.stats["failed_requests"] += 1
        return {}

    def read(self, crops: list, layout: str, batch: int) -> list[str | None]:
        """每块读出的字；请求失败或没答的块是 None。"""
        groups: list[list[int]] = []
        current: list[int] = []
        height = 0
        for i, c in enumerate(crops):
            if current and (len(current) >= batch or (layout == "sheet" and height + c.height > SHEET_MAX_HEIGHT)):
                groups.append(current)
                current, height = [], 0
            current.append(i)
            height += c.height + 20
        if current:
            groups.append(current)

        def one(group: list[int]) -> tuple[list[int], dict]:
            if layout == "sheet":
                text = (f"The image stacks {len(group)} numbered crops, one under another; "
                        "the number is at the left of each crop.")
                return group, self._ask(text, [("", _sheet([crops[i] for i in group]))])
            text = f"{len(group)} crops follow, each after its number."
            return group, self._ask(text, [(f"Crop {n}:", crops[i]) for n, i in enumerate(group, 1)])

        out: list[str | None] = [None] * len(crops)
        with ThreadPoolExecutor(self.workers) as pool:
            for group, answer in pool.map(one, groups):
                for n, i in enumerate(group, 1):
                    value = answer.get(str(n))
                    out[i] = value if isinstance(value, str) else None
        with self._lock:
            self.stats["crops"] += len(crops)
            self.stats["unanswered"] += sum(1 for v in out if v is None)
        return out


def _install(tesseract: bool) -> None:
    from core import ocr_component_install, pdf_component_install, ppocr_component_install

    if pdf_component_install.installed_site_dir() is None:
        print("installing the PDF component …", flush=True)
        pdf_component_install.install()
    if ppocr_component_install.installed_dirs() is None:
        print("installing the recognition models …", flush=True)
        ppocr_component_install.install()
    if tesseract and ocr_component_install.installed_executable() is None and ocr_component_install.manifest():
        print("installing Tesseract …", flush=True)
        ocr_component_install.install()


def _pdfs(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for item in paths:
        path = Path(item).expanduser()
        out += sorted(path.rglob("*.pdf")) if path.is_dir() else [path]
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out")
    parser.add_argument("pdfs", nargs="+")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--tesseract", action="store_true")
    parser.add_argument("--dpi", type=float, default=DPI, help="page resolution (原位替换 uses 200)")
    parser.add_argument("--ocr-dpi", type=float, default=None,
                        help="resolution the recognition models read at (default: 原位替换's, 300)")
    parser.add_argument("--det", default="small", help="small / medium / server5 / an .onnx file")
    parser.add_argument("--rec", default="small", help="small / medium / server5 / an .onnx file")
    parser.add_argument("--vlm", default="", help="images, sheet or images,sheet")
    parser.add_argument("--vlm-batch", type=int, default=20)
    parser.add_argument("--vlm-workers", type=int, default=4)
    parser.add_argument("--vlm-connection", default="", help="a 图像翻译审核模型 connection id (default: the first)")
    parser.add_argument("--vlm-model", default="", help="another model name on that connection")
    args = parser.parse_args(argv)
    dpi = args.dpi
    from core.pdf_vector.extract import OCR_DPI

    args.ocr_dpi = args.ocr_dpi or OCR_DPI
    layouts = [x for x in args.vlm.replace(" ", "").split(",") if x]
    if any(x not in ("images", "sheet") for x in layouts):
        parser.error("--vlm takes images, sheet or images,sheet")
    if not os.environ.get("TRANSLATOR_APP_DATA_DIR"):
        os.environ["TRANSLATOR_APP_DATA_DIR"] = tempfile.mkdtemp(prefix="ocr-benchmark-")
    _install(args.tesseract)
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(os.environ["TRANSLATOR_APP_DATA_DIR"]) / "ocr-benchmark-models"
    model_sizes = _use_models(_fetch_model("det", args.det, cache), _fetch_model("rec", args.rec, cache), out)
    vision = {layout: VisionReader(args.vlm_connection, args.vlm_model, args.vlm_workers) for layout in layouts}

    from PIL import Image, ImageDraw

    from core.pdf_component import require_fitz
    from core.pdf_vector import extract
    from core.ppocr_component import make_reader, shutdown_reader

    fitz = require_fitz()
    reader = make_reader()
    if reader is None:
        print("the recognition models are not usable here")
        return 1
    tesseract = None
    if args.tesseract:
        from core.ocr_engine import resolve_tesseract

        exe = resolve_tesseract()
        tesseract = str(exe) if exe else None
        print(f"tesseract: {tesseract or 'not found'}")
    totals: dict[str, dict[str, float]] = {}
    # 每种读法（ppocr、vlm_<layout>、hybrid_<layout>）：逐行准确率和读错的原因
    variant_totals: dict[str, dict[str, dict[str, float]]] = {}
    variant_misses: dict[str, dict[str, Counter]] = {}
    row_agreement: dict[str, Counter] = {}
    unsure_rows = all_rows = 0
    vlm_rows_log = []
    pages_summary = []
    mismatches = []
    judged: list[dict] = []          # rows of text-layer pages with their decision and whether they read right
    blind: dict[str, int] = {}       # rows of pages without a text layer: decision / reason counts
    single: dict[str, int] = {}      # lone Chinese / Japanese characters that would be translated
    single_why: Counter = Counter()  # ... and why each was not joined with its neighbour
    single_log: list[str] = []

    from core.pdf_vector.runner import guess_language

    def add(kind: str, exact: bool, acc: float) -> None:
        total = totals.setdefault(kind, {"lines": 0, "exact": 0, "char_acc": 0.0})
        total["lines"] += 1
        total["exact"] += int(exact)
        total["char_acc"] += acc

    for pdf in _pdfs(args.pdfs):
        try:
            doc = fitz.open(pdf)
        except Exception as exc:  # noqa: BLE001 - skip unreadable files
            print(f"skip {pdf}: {exc}")
            continue
        for pno in range(min(len(doc), args.max_pages)):
            page = doc[pno]
            tag = f"{len(pages_summary) + 1:03d}"
            truth = [r for r in extract.native_rows(page, dpi)
                     if sum(c.isalnum() for c in r["source_text"]) >= 2 and not r.get("oblique")]
            pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
            gray = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
            started = time.monotonic()
            rows, _dropped = extract.dedup(extract.read_outlines(page, dpi, reader, ocr_dpi=args.ocr_dpi))
            seconds = time.monotonic() - started
            # the same steps as 原位替换 after reading: join letter-spaced labels, split at
            # table rules, join paragraphs
            V, Hs = extract.rules(page, dpi)
            done = extract.join_spaced_cjk(copy.deepcopy(rows), [], V, Hs)
            done, _n = extract.split_at_rules(done, V, Hs)
            done = extract.group_paragraphs(done, V, Hs)
            lang = guess_language([r["source_text"] for r in truth] or [r["source_text"] for r in done],
                                  fallback="zh")
            _decide(done, lang, "fr")
            for r in done:
                kind = "cjk" if _CJK.search(r["source_text"]) else "latin"
                letters = [c for c in r["source_text"] if c.isalpha()]
                if truth:
                    hits = _overlapping(r, truth)
                    want = "".join(_norm(t["source_text"]) for t in sorted(
                        hits, key=lambda t: (round(t["display_box"][1] / 20), t["display_box"][0])))
                    judged.append({"kind": kind, "decision": r["decision"], "reason": r["decision_reason"],
                                   "right": (_norm(r["source_text"]) == want) if hits else None,
                                   "score": float(r.get("ocr_score") or 0.0), "min_char": _min_char(r),
                                   "single": len(letters) == 1})
                else:
                    blind[f"{kind} {r['decision']} {r['decision_reason']}"] = \
                        blind.get(f"{kind} {r['decision']} {r['decision_reason']}", 0) + 1
                    if r["decision"] == "translate" and len(letters) == 1 and _CJK.search(letters[0]):
                        single[letters[0]] = single.get(letters[0], 0) + 1
                        why, detail = _why_single(r, done, V, Hs)
                        single_why[why] += 1
                        single_log.append(f"{tag} {r['source_text']!r} {r.get('ocr_score', 0):.2f} {why} {detail}")
            if truth:
                texts = {"ppocr": [r["source_text"] for r in rows]}
                unsure = [(r.get("ocr_score") or 0.0) < UNSURE_SCORE or _min_char(r) < UNSURE_CHAR for r in rows]
                unsure_rows += sum(unsure)
                all_rows += len(rows)
                if vision:
                    # 小块从识别模型看的那张（更清楚的）图上裁
                    res = extract.ocr_resolution(page, dpi, args.ocr_dpi)
                    sharp = page.get_pixmap(matrix=fitz.Matrix(res / 72, res / 72), colorspace=fitz.csGRAY,
                                            alpha=False)
                    sharp = Image.frombytes("L", (sharp.width, sharp.height), sharp.samples)
                    crops = [_row_crop(sharp, r, rows, res / dpi) for r in rows]
                    del sharp
                    for layout, vr in vision.items():
                        began = time.monotonic()
                        read = vr.read(crops, layout, args.vlm_batch)
                        vr.stats["wall_seconds"] += time.monotonic() - began
                        texts[f"vlm_{layout}"] = [v if v is not None else "" for v in read]
                        texts[f"hybrid_{layout}"] = [
                            (v if (u and v is not None) else r["source_text"]) for r, u, v in zip(rows, unsure, read)]
                        for r, u, v in zip(rows, unsure, read):
                            hits = _overlapping(r, truth)
                            if not hits:
                                continue
                            want = "".join(_norm(t["source_text"]) for t in sorted(
                                hits, key=lambda t: (round(t["display_box"][1] / 20), t["display_box"][0])))
                            p_ok, v_ok = _norm(r["source_text"]) == want, _norm(v or "") == want
                            agree = _norm(r["source_text"]) == _norm(v or "")
                            row_agreement.setdefault(layout, Counter())[
                                f"{'agree' if agree else 'differ'}_ppocr_{'right' if p_ok else 'wrong'}"
                                f"_vlm_{'right' if v_ok else 'wrong'}{'_unsure' if u else ''}"] += 1
                            vlm_rows_log.append({"page": tag, "layout": layout, "unsure": u,
                                                 "truth": want, "ppocr": r["source_text"], "vlm": v})
                for name, values in texts.items():
                    variant_rows = [dict(r, source_text=t) for r, t in zip(rows, values)]
                    for line in truth:
                        kind = "cjk" if _CJK.search(line["source_text"]) else "latin"
                        want = _norm(line["source_text"])
                        got = _norm(_matched_text(line, variant_rows))
                        total = variant_totals.setdefault(name, {}).setdefault(
                            kind, {"lines": 0, "exact": 0, "char_acc": 0.0})
                        total["lines"] += 1
                        total["exact"] += int(got == want)
                        total["char_acc"] += max(0.0, 1 - _lev(want, got) / max(1, len(want)))
                        if got != want:
                            variant_misses.setdefault(name, {}).setdefault(kind, Counter())[_miss_kind(want, got)] += 1
            tess_rows = []
            if tesseract and truth:
                raw = extract.ocr_rows(gray, "fra+eng", (0, 90, 270), max(2, (os.cpu_count() or 4) - 2), tesseract)
                tess_rows, _dropped = extract.dedup(raw)
            info = {"page": tag, "size_px": list(gray.size), "seconds": round(seconds, 1),
                    "text_layer_lines": len(truth), "ocr_rows": len(rows)}
            for line in truth:
                kind = "cjk" if _CJK.search(line["source_text"]) else "latin"
                want = _norm(line["source_text"])
                got = _matched_text(line, rows)
                acc = max(0.0, 1 - _lev(want, _norm(got)) / max(1, len(want)))
                add(kind, _norm(got) == want, acc)
                if _norm(got) != want:
                    mismatches.append(f"{tag} {kind} {acc:.2f}  {line['source_text']!r} -> {got!r}")
                if tess_rows and kind == "latin":
                    tess = _matched_text(line, tess_rows)
                    tacc = max(0.0, 1 - _lev(want, _norm(tess)) / max(1, len(want)))
                    add("latin_tesseract", _norm(tess) == want, tacc)
            if not truth:
                # 没有文字层：给人看读到了什么、在哪里
                (out / f"{tag}_reads.txt").write_text(
                    "\n".join(f"{r['display_rotation']:>3} {r.get('ocr_score', 0):.2f}  {r['source_text']}" for r in rows),
                    encoding="utf-8")
                overlay = gray.convert("RGB")
                draw = ImageDraw.Draw(overlay)
                for r in rows:
                    draw.rectangle(r["display_box"], outline=(220, 0, 0), width=3)
                overlay.save(out / f"{tag}_boxes.png")
            pages_summary.append(info)
            print(f"{tag}: {info}", flush=True)
        doc.close()
    shutdown_reader()
    summary = {kind: {"lines": int(t["lines"]), "exact": round(t["exact"] / t["lines"], 3),
                      "char_acc": round(t["char_acc"] / t["lines"], 3)}
               for kind, t in totals.items() if t["lines"]}
    # 读对 / 读错 × 翻译 / 复核 / 保留：「读错了却会被翻译」是要压下去的那一格
    decisions: dict[str, dict[str, int]] = {}
    for j in judged:
        right = {True: "right", False: "wrong", None: "no_text_layer_line"}[j["right"]]
        cell = decisions.setdefault(j["kind"], {})
        cell[f"{j['decision']}_{right}"] = cell.get(f"{j['decision']}_{right}", 0) + 1
    summary["decisions"] = decisions
    # 只对会被翻译的行：把把握门槛提到 t 以后，还会翻译的读对 / 读错各多少
    sweep: dict[str, dict[str, list[int]]] = {}
    for kind in ("cjk", "latin"):
        rows_k = [j for j in judged if j["kind"] == kind and j["decision"] == "translate" and j["right"] is not None]
        for field in ("min_char", "score"):
            sweep[f"{kind}_{field}"] = {
                str(t): [sum(1 for j in rows_k if j[field] >= t and j["right"]),
                         sum(1 for j in rows_k if j[field] >= t and not j["right"])] for t in SWEEP}
    summary["sweep_right_wrong"] = sweep
    summary["single_char_translated_on_text_layer_pages"] = dict(Counter(
        "right" if j["right"] else "wrong" if j["right"] is False else "no_line"
        for j in judged if j["single"] and j["kind"] == "cjk" and j["decision"] == "translate"))
    summary["no_text_layer_pages"] = dict(sorted(blind.items(), key=lambda kv: -kv[1]))
    summary["no_text_layer_single_chars_translated"] = dict(sorted(single.items(), key=lambda kv: -kv[1]))
    summary["variants"] = {
        name: {**{kind: {"lines": int(t["lines"]), "exact": round(t["exact"] / t["lines"], 3),
                         "char_acc": round(t["char_acc"] / t["lines"], 3)}
                  for kind, t in kinds.items() if t["lines"]},
               "misses": {kind: dict(c) for kind, c in variant_misses.get(name, {}).items()}}
        for name, kinds in variant_totals.items()}
    summary["models"] = {"det": args.det, "rec": args.rec, "dpi": dpi, "ocr_dpi": args.ocr_dpi, **model_sizes}
    summary["unsure_rows"] = [unsure_rows, all_rows]
    if vision:
        summary["vlm"] = {layout: {**vr.describe(), **{k: (round(v, 1) if isinstance(v, float) else v)
                                                       for k, v in vr.stats.items()}}
                          for layout, vr in vision.items()}
        summary["vlm_row_agreement"] = {layout: dict(c) for layout, c in row_agreement.items()}
        with (out / "vlm_rows.jsonl").open("w", encoding="utf-8") as fh:
            for item in vlm_rows_log:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    summary["no_text_layer_single_chars_why"] = dict(single_why.most_common())
    (out / "single_chars.txt").write_text("\n".join(single_log), encoding="utf-8")
    summary["pages"] = len(pages_summary)
    summary["seconds_per_page"] = round(sum(p["seconds"] for p in pages_summary) / max(1, len(pages_summary)), 1)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "pages.json").write_text(json.dumps(pages_summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "mismatches.txt").write_text("\n".join(mismatches), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
