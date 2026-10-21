"""Independent QA for one source/output pair produced by writeback.py.

Checks: qpdf structure, page size/rotation, embedded images, vector paths
(every removed path must lie inside a written text rectangle), numbers kept,
pixels changed outside written regions, new text landing on drawing ink,
written-text collisions, and a per-row ledger (why written/kept/reviewed).
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
from collections import Counter
from pathlib import Path

from PIL import Image, ImageChops

from .common import display_pt_to_page, fitz, numbers, px_to_display_pt, read_json, sha256, write_json  # noqa: F401
from .writeback import _item_geom, _kept_native, apply_removal, ink_count, redaction_rect, render

Image.MAX_IMAGE_PIXELS = None


def _ikey(it):
    pts = []
    for v in it[1:]:
        if isinstance(v, fitz.Point):
            pts += [round(v.x, 1), round(v.y, 1)]
        elif isinstance(v, fitz.Rect):
            pts += [round(c, 1) for c in v]
        elif isinstance(v, fitz.Quad):
            pts += [round(c, 1) for c in v.rect]
    return (it[0], tuple(pts))


def _imid(it):
    return _item_geom(it)[0]


def path_sigs(page: fitz.Page, keep=None):
    sigs = Counter()
    rects = {}
    for d in page.get_drawings():
        r = d["rect"]
        key = (d.get("type"), tuple(round(v, 1) for v in (r.x0, r.y0, r.x1, r.y1)),
               tuple(round(c, 2) for c in (d.get("color") or ())), tuple(round(c, 2) for c in (d.get("fill") or ())),
               round(float(d.get("width") or 0), 2), len(d["items"]))
        sigs[key] += 1
        rects[key] = r
        if keep is not None:
            keep.setdefault(key, d)
    return sigs, rects


def uncovered_text(page: fitz.Page, rows: list[dict], dpi: float, overlay: Path | None = None) -> dict:
    """Estimate vector lettering that no extracted row covers.

    Glyph-sized paths (no longer than ~2.5 text heights) outside every row box
    are clustered into lines; a cluster with enough strokes and a text-like
    aspect is a candidate for text OCR missed.  Symbols and hatching can also
    qualify, so this is an upper-bound estimate, not a count of missed words.
    """
    import statistics
    k = dpi / 72.0
    th = [((r["display_box"][3] - r["display_box"][1]) if r.get("display_rotation", 0) % 180 == 0
           else (r["display_box"][2] - r["display_box"][0])) for r in rows if r.get("origin") == "ocr"]
    if not th:
        return {"candidates": 0, "covered_paths": 0, "uncovered_paths": 0}
    t = statistics.median(th)
    boxes = [fitz.Rect(r["display_box"]) + (-2, -2, 2, 2) for r in rows]
    small_in, small_out = 0, []
    for d in page.get_drawings():
        r = d["rect"] * page.rotation_matrix
        r = fitz.Rect(r.x0 * k, r.y0 * k, r.x1 * k, r.y1 * k)
        if max(r.width, r.height) > 2.5 * t or (r.width < 0.5 and r.height < 0.5):
            continue
        if any(b.contains(r) for b in boxes):
            small_in += 1
        else:
            small_out.append(r)
    # cluster by proximity (grid union-find)
    pad = 0.6 * t
    parent = list(range(len(small_out)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    grid: dict = {}
    G = max(8, int(2 * t))
    for i, r in enumerate(small_out):
        for gx in range(int((r.x0 - pad) // G), int((r.x1 + pad) // G) + 1):
            for gy in range(int((r.y0 - pad) // G), int((r.y1 + pad) // G) + 1):
                for j in grid.get((gx, gy), []):
                    q = small_out[j]
                    if (r + (-pad, -pad, pad, pad)).intersects(q):
                        parent[find(i)] = find(j)
                grid.setdefault((gx, gy), []).append(i)
    groups: dict = {}
    for i in range(len(small_out)):
        groups.setdefault(find(i), []).append(i)
    cands = []
    for g in groups.values():
        if len(g) < 6:
            continue
        bb = fitz.Rect(small_out[g[0]])
        for i in g[1:]:
            bb |= small_out[i]
        short, long_ = sorted((bb.width, bb.height))
        if short <= 2.2 * t and long_ >= 1.5 * short:
            cands.append([round(v) for v in bb])
    if overlay is not None:
        img = render(page, dpi)
        from PIL import ImageDraw
        dr = ImageDraw.Draw(img)
        for b in cands:
            dr.rectangle(b, outline=(255, 0, 160), width=4)
        img.save(overlay)
    return {"candidates": len(cands), "covered_paths": small_in, "uncovered_paths_in_candidates": sum(
        1 for _ in cands), "examples": cands[:30], "median_text_height_px": round(t, 1),
        "note": "upper-bound estimate: symbols and hatching can also form candidates"}


def run(source: Path, output: Path, manifest: Path, report: Path, out: Path, ledger: Path, dpi: float = 200):
    man = read_json(manifest)
    rep = read_json(report)
    rows = {r["id"]: r for r in man["rows"]}
    written = rep["written"]
    src, dst = fitz.open(source), fitz.open(output)
    sp, op = src[0], dst[0]
    res = {"source": str(source), "source_sha256": sha256(source), "output": str(output), "output_sha256": sha256(output)}
    try:
        q = subprocess.run(["qpdf", "--check", str(output)], capture_output=True, text=True, timeout=120,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        res["qpdf"] = {"returncode": q.returncode, "ok": q.returncode == 0, "tail": q.stdout.strip().splitlines()[-1:]}
    except (OSError, subprocess.SubprocessError):
        # qpdf is a development check; users' machines usually do not have it
        res["qpdf"] = {"returncode": None, "ok": True, "skipped": True}
    res["page"] = {"source_rect": list(sp.rect), "output_rect": list(op.rect), "source_rotation": sp.rotation,
                   "output_rotation": op.rotation, "pages": [len(src), len(dst)],
                   "same": tuple(sp.rect) == tuple(op.rect) and sp.rotation == op.rotation and len(src) == len(dst) == 1}
    # compare what is actually placed on the page (content hash + position);
    # unused image resources may be dropped by garbage collection on save
    def placed(pg):
        return sorted((i["digest"].hex(), tuple(round(v, 1) for v in i["bbox"])) for i in pg.get_image_info(hashes=True))
    res["images"] = {"source": len(placed(sp)), "output": len(placed(op)), "identical": placed(sp) == placed(op),
                     "xref_source": len(sp.get_images(full=True)), "xref_output": len(op.get_images(full=True))}

    # vector paths: which disappeared, and were they all inside a written row?
    sdraw: dict = {}
    ss, srect = path_sigs(sp, sdraw)
    os_, _ = path_sigs(op)
    out_items = Counter(_ikey(it) for d in op.get_drawings() for it in d["items"])
    removed = ss - os_
    allowed = []
    for w in written:
        r = rows[w["id"]]
        for lb in r.get("line_boxes") or [r["display_box"]]:
            a = redaction_rect(dict(r, display_box=lb), sp, dpi)[1]
            allowed.append(fitz.Rect(a.x0 - 0.5, a.y0 - 0.5, a.x1 + 0.5, a.y1 + 0.5))  # float edge tolerance
    unexpected = []
    replayed = {tuple(x["rect"]) for x in rep.get("replayed_paths", [])}
    n_replayed = 0
    n_letters_from_compound = 0
    for key, n in removed.items():
        rr = srect[key]
        if tuple(round(v, 1) for v in rr) in replayed:
            n_replayed += n  # letters removed, leader/underline parts redrawn
            continue
        # a path that kept its other pieces and only lost the pieces lying in
        # written lines (letters stored together with a leader or frame)
        missing = [it for it in sdraw[key]["items"] if out_items[_ikey(it)] == 0]
        if missing and len(missing) < len(sdraw[key]["items"]) and all(
                any(a.contains(_imid(it)) for a in allowed) for it in missing):
            n_letters_from_compound += n
            continue
        inside = any(a.contains(rr) for a in allowed)
        # straddling glyph pieces: at least half inside a written rectangle
        half = any(rr.intersects(a) and (rr & a).width * (rr & a).height >= 0.5 * max(rr.width * rr.height, 1e-6)
                   for a in allowed)
        if not inside and not half:
            unexpected.append({"rect": [round(v, 1) for v in rr], "count": n, "type": key[0]})
    res["vector_paths"] = {"source": sum(ss.values()), "output": sum(os_.values()),
                           "removed": sum(removed.values()), "added": sum((os_ - ss).values()),
                           "removed_outside_written_rects": len(unexpected), "replayed_non_letter_parts": n_replayed,
                           "letters_removed_from_compound_paths": n_letters_from_compound,
                           "examples": unexpected[:20]}

    # numbers
    num_fail = [w["id"] for w in written
                if any(n not in numbers(w["translation"], dates=True) for n in numbers(w["source_text"], dates=True))]
    res["numbers"] = {"checked": len(written), "failures": num_fail}

    # pixels
    sg = render(sp, dpi).convert("L")
    og = render(op, dpi).convert("L")
    diff = ImageChops.difference(sg, og).point(lambda v: 255 if v > 24 else 0)
    mask = Image.new("L", sg.size, 0)
    from PIL import ImageDraw
    dr = ImageDraw.Draw(mask)
    for w in written:
        r = rows[w["id"]]
        for lb in r.get("line_boxes") or [r["display_box"]]:
            b = redaction_rect(dict(r, display_box=lb), sp, dpi)[0]
            dr.rectangle([b[0] - 2, b[1] - 2, b[2] + 2, b[3] + 2], fill=255)
        for b in w["written_rects_px"]:
            dr.rectangle([b[0] - 2, b[1] - 2, b[2] + 2, b[3] + 2], fill=255)
    outside = ImageChops.subtract(diff, mask)
    ch = outside.histogram()[255]
    total = sg.size[0] * sg.size[1]
    hot = {}
    if ch:
        opx = outside.load()
        for y in range(0, outside.height, 2):
            for x in range(0, outside.width, 2):
                if opx[x, y]:
                    key = (x // 200 * 200, y // 200 * 200)
                    hot[key] = hot.get(key, 0) + 1
    res["pixels"] = {"render_dpi": dpi, "size": list(sg.size), "changed_outside_written_px": ch,
                     "changed_outside_fraction": ch / total,
                     "hotspots_200px": [[k[0], k[1], v] for k, v in sorted(hot.items(), key=lambda kv: -kv[1])[:8]]}
    # new text over remaining drawing ink (lines crossing the new text)
    tmp = fitz.open(source)
    wid = {w["id"] for w in written}
    apply_removal(tmp[0], [rows[i] for i in wid], dpi,
                  [r["display_box"] for r in rows.values() if r["id"] not in wid and r.get("origin") == "ocr"],
                  kept_text=_kept_native(rows.values(), wid))
    rg = render(tmp[0], dpi).convert("L")
    tmp.close()
    hits = []
    for w in written:
        ink, area = 0, 0
        for b in w["written_rects_px"]:
            i, a = ink_count(rg, b)
            ink, area = ink + i, area + a
        frac = ink / area if area else 0
        if frac > 0.02:
            hits.append({"id": w["id"], "translation": w["translation"][:30], "ink_under_new_text": round(frac, 3)})
    res["text_on_drawing_ink"] = {"rows_over_2pct": len(hits), "examples": sorted(hits, key=lambda h: -h["ink_under_new_text"])[:15]}
    # collisions between written lines
    rects = [(w["id"], b) for w in written for b in w["written_rects_px"]]
    coll = []
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            a, b = rects[i][1], rects[j][1]
            if rects[i][0] != rects[j][0] and min(a[2], b[2]) - max(a[0], b[0]) > 1 and min(a[3], b[3]) - max(a[1], b[1]) > 1:
                coll.append([rects[i][0], rects[j][0]])
    res["written_collisions"] = coll
    res["unrecognized_text"] = uncovered_text(sp, man["rows"], dpi, Path(out).with_name("unrecognized_overlay.png"))
    res["chinese_chars_output"] = len(re.findall(r"[一-鿿]", op.get_text()))
    res["fonts"] = sorted({f[3] for f in op.get_fonts(full=True)})
    res["rows"] = {"total": len(rows), "decision": Counter(r["decision"] for r in rows.values()),
                   "decision_reason": Counter(r["decision_reason"] for r in rows.values()),
                   "translation_source": Counter(r["translation_source"] for r in rows.values()),
                   "written": len(written), "write_decided_not_written": rep["metrics"]["candidates"] - len(written),
                   "skipped_writeback_reasons": Counter(s["reason"] for s in rep["skipped"]
                                                        if rows.get(s["id"], {}).get("decision") == "write")}
    res["fit_modes"] = rep["metrics"]["fit_modes"]
    res["status"] = "pass" if (res["qpdf"]["ok"] and res["page"]["same"] and res["images"]["identical"]
                               and not unexpected and not num_fail and not coll) else "review"
    write_json(out, res)
    wset = {w["id"]: w for w in written}
    with open(ledger, "w", newline="", encoding="utf-8-sig") as f:
        cw = csv.writer(f)
        cw.writerow(["id", "origin", "rotation", "ocr_score", "source_text", "decision", "reason", "translation_source",
                     "translation", "written", "fontsize", "target_fontsize", "fit"])
        for r in man["rows"]:
            w = wset.get(r["id"])
            cw.writerow([r["id"], r.get("origin"), r.get("display_rotation"),
                         "" if r.get("ocr_score") is None else round(r["ocr_score"], 2), r["source_text"],
                         r["decision"], r["decision_reason"], r["translation_source"],
                         r["translation"] if r["decision"] == "write" else "", bool(w),
                         w and w["fontsize"], w and w["target_fontsize"], w and w["fit"]])
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--ledger", type=Path, required=True)
    ap.add_argument("--dpi", type=float, default=200)
    a = ap.parse_args()
    r = run(a.source, a.output, a.manifest, a.report, a.out, a.ledger, a.dpi)
    print({k: r[k] for k in ("status", "qpdf", "page", "images", "numbers", "pixels", "written_collisions")})
    print("vector", {k: v for k, v in r["vector_paths"].items() if k != "examples"})
    print("ink_under_text", r["text_on_drawing_ink"]["rows_over_2pct"], "zh", r["chinese_chars_output"])


if __name__ == "__main__":
    main()
