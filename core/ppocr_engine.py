"""Chinese / Japanese text recognition with the PP-OCR models (ONNX Runtime).

Tesseract's Chinese data reads drawing lettering badly (wrong characters, junk
around table lines); the PP-OCR models (PaddleOCR, Apache-2.0, as shipped by
RapidOCR) read Chinese, Japanese and Latin script in one pass.  This module is
only the two networks and their pre/post-processing -- text detection (DB) and
line recognition (CTC) -- following RapidOCR / PaddleOCR; how a drawing is cut
into tiles and how the lines become rows lives in ``core.pdf_vector.extract``.

Needs ``onnxruntime``, ``numpy`` and ``cv2`` (the last two come with the PDF
component).  This module lives under ``core`` and must never import from ``api``.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# DB post-processing (RapidOCR defaults for PP-OCRv6)
DET_THRESH = 0.3
DET_BOX_THRESH = 0.5
DET_UNCLIP = 1.6
DET_MAX_CANDIDATES = 3000
DET_MIN_SIZE = 3
REC_HEIGHT = 48
REC_MIN_WIDTH = 320
REC_BATCH = 8


@dataclass
class LineRead:
    text: str
    score: float
    # per character: probability and position (x from the left edge of the crop, px)
    chars: list[tuple[str, float, float, float]] = field(default_factory=list)


def _cv2():
    import cv2  # noqa: PLC0415 - loaded with the PDF component

    return cv2


def _ort():
    import onnxruntime  # noqa: PLC0415 - part of the optional Chinese recognition pack

    return onnxruntime


class PPOCR:
    """One detector and one recogniser; ``detect`` / ``recognize`` are thread-safe."""

    def __init__(self, det_model: Path | str, rec_model: Path | str, threads: int | None = None):
        ort = _ort()
        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = int(threads)
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        # no memory arena: tiles and line widths vary, and an arena keeps its largest
        # allocation for the life of the process (RapidOCR's setting too)
        opts.enable_cpu_mem_arena = False
        providers = ["CPUExecutionProvider"]
        self.det = ort.InferenceSession(str(det_model), sess_options=opts, providers=providers)
        self.rec = ort.InferenceSession(str(rec_model), sess_options=opts, providers=providers)
        meta = self.rec.get_modelmeta().custom_metadata_map
        keys = (meta.get("character") or "").splitlines()
        if not keys:
            raise ValueError("recognition model has no character list")
        # CTC: index 0 is the blank, the space comes last (PaddleOCR's dictionary convention)
        self.chars = ["", *keys, " "]
        self._det_in = self.det.get_inputs()[0].name
        self._rec_in = self.rec.get_inputs()[0].name
        self._lock = threading.Lock()

    # ------------------------------------------------------------ detection

    def detect(self, gray: np.ndarray) -> list[tuple[np.ndarray, float]]:
        """Text boxes in ``gray`` (uint8, H x W) as (4 x 2 float corners: tl, tr, br, bl; score).

        The image is read at its own size (rounded to a multiple of 32): drawings
        keep small lettering only at full resolution, so callers cut big pages
        into tiles rather than shrinking them.
        """
        cv2 = _cv2()
        h, w = gray.shape[:2]
        rh, rw = max(32, int(round(h / 32) * 32)), max(32, int(round(w / 32) * 32))
        img = cv2.resize(gray, (rw, rh)) if (rh, rw) != (h, w) else gray
        x = (img.astype(np.float32) / 255.0 - 0.5) / 0.5
        x = np.repeat(x[None, None, :, :], 3, axis=1)
        prob = self.det.run(None, {self._det_in: x})[0][0, 0]
        mask = cv2.dilate((prob > DET_THRESH).astype(np.uint8), np.ones((2, 2), np.uint8))
        contours, _ = cv2.findContours(mask * 255, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        out = []
        for contour in contours[:DET_MAX_CANDIDATES]:
            rect = cv2.minAreaRect(contour)
            if min(rect[1]) < DET_MIN_SIZE:
                continue
            pts = cv2.boxPoints(rect)
            score = _box_score(prob, pts)
            if score < DET_BOX_THRESH:
                continue
            (cx, cy), (bw, bh), angle = rect
            # unclip: grow every side by area * ratio / perimeter (PaddleOCR's DB rule;
            # for a rectangle the rounded offset polygon has this bounding rectangle)
            d = bw * bh * DET_UNCLIP / (2 * (bw + bh))
            bw, bh = bw + 2 * d, bh + 2 * d
            if min(bw, bh) < DET_MIN_SIZE + 2:
                continue
            pts = cv2.boxPoints(((cx, cy), (bw, bh), angle))
            pts[:, 0] = np.clip(np.round(pts[:, 0] / rw * w), 0, w - 1)
            pts[:, 1] = np.clip(np.round(pts[:, 1] / rh * h), 0, h - 1)
            pts = _order(pts)
            if np.linalg.norm(pts[0] - pts[1]) <= 3 or np.linalg.norm(pts[0] - pts[3]) <= 3:
                continue
            out.append((pts, float(score)))
        return out

    # ---------------------------------------------------------- recognition

    def recognize(self, crops: list[np.ndarray]) -> list[LineRead]:
        """Read text lines; every crop is one horizontal line (uint8 gray, any size)."""
        cv2 = _cv2()
        results: list[LineRead | None] = [None] * len(crops)
        order = sorted(range(len(crops)), key=lambda i: crops[i].shape[1] / max(1, crops[i].shape[0]))
        for start in range(0, len(order), REC_BATCH):
            idx = order[start:start + REC_BATCH]
            ratios = [crops[i].shape[1] / max(1, crops[i].shape[0]) for i in idx]
            width = int(REC_HEIGHT * max(REC_MIN_WIDTH / REC_HEIGHT, *ratios))
            batch = np.zeros((len(idx), 3, REC_HEIGHT, width), np.float32)
            used = []
            for n, i in enumerate(idx):
                c = crops[i]
                rw = min(width, int(math.ceil(REC_HEIGHT * c.shape[1] / max(1, c.shape[0]))))
                rw = max(1, rw)
                img = cv2.resize(c, (rw, REC_HEIGHT)).astype(np.float32)
                batch[n, :, :, :rw] = ((img / 255.0) - 0.5) / 0.5
                used.append(rw)
            preds = self.rec.run(None, {self._rec_in: batch})[0]
            frames = preds.shape[1]
            for n, i in enumerate(idx):
                results[i] = self._decode(preds[n], width / frames, crops[i].shape[1] / used[n])
        return [r or LineRead("", 0.0) for r in results]

    def _decode(self, pred: np.ndarray, frame_px: float, scale: float) -> LineRead:
        ids = pred.argmax(axis=1)
        probs = pred.max(axis=1)
        chars = []
        prev = -1
        for t, k in enumerate(ids):
            k = int(k)
            if k != prev and k != 0:
                chars.append([self.chars[k] if k < len(self.chars) else "", float(probs[t]), t, t])
            elif k == prev and k != 0 and chars:
                chars[-1][3] = t
            prev = k
        out = []
        for ch, p, t0, t1 in chars:
            if not ch:
                continue
            out.append((ch, p, t0 * frame_px * scale, (t1 + 1) * frame_px * scale))
        text = "".join(c[0] for c in out)
        score = float(np.mean([c[1] for c in out])) if out else 0.0
        return LineRead(text, score, out)


def _order(pts: np.ndarray) -> np.ndarray:
    """Corners as top-left, top-right, bottom-right, bottom-left."""
    xs = pts[np.argsort(pts[:, 0]), :]
    left = xs[:2][np.argsort(xs[:2, 1])]
    right = xs[2:][np.argsort(xs[2:, 1])]
    return np.array([left[0], right[0], right[1], left[1]], dtype=np.float32)


def _box_score(prob: np.ndarray, pts: np.ndarray) -> float:
    cv2 = _cv2()
    h, w = prob.shape
    x0 = int(np.clip(np.floor(pts[:, 0].min()), 0, w - 1))
    x1 = int(np.clip(np.ceil(pts[:, 0].max()), 0, w - 1))
    y0 = int(np.clip(np.floor(pts[:, 1].min()), 0, h - 1))
    y1 = int(np.clip(np.ceil(pts[:, 1].max()), 0, h - 1))
    mask = np.zeros((y1 - y0 + 1, x1 - x0 + 1), np.uint8)
    shifted = pts.copy()
    shifted[:, 0] -= x0
    shifted[:, 1] -= y0
    cv2.fillPoly(mask, shifted.reshape(1, -1, 2).astype(np.int32), 1)
    return float(cv2.mean(prob[y0:y1 + 1, x0:x1 + 1], mask)[0])


def crop_quad(gray: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """The quad straightened into an upright crop (PaddleOCR's get_rotate_crop_image)."""
    cv2 = _cv2()
    w = int(max(np.linalg.norm(quad[0] - quad[1]), np.linalg.norm(quad[2] - quad[3])))
    h = int(max(np.linalg.norm(quad[0] - quad[3]), np.linalg.norm(quad[1] - quad[2])))
    dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32)
    m = cv2.getPerspectiveTransform(quad.astype(np.float32), dst)
    return cv2.warpPerspective(gray, m, (max(1, w), max(1, h)), borderMode=cv2.BORDER_REPLICATE,
                               flags=cv2.INTER_CUBIC)
