"""续译检测：判断源文件旁（或自定义输出根目录下）是否已有历史翻译产物。

纯只读模块——不创建、不修改、不删除任何文件。任何异常都必须被这里吞掉，绝不能
让一次检测失败拖垮整个扫描请求（见 detect_previous_output 的顶层 try/except）。

产物命名规则不在这里重新发明：Excel/Word 复用 core/bilingual_writer.py 的
bilingual_output_name + core/word_document.py 的 _normalize_word_output_name；
PDF 复用 core/pdf_image_translation.py 的分页归档路径与命名规则。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from core.bilingual_writer import bilingual_output_name
from core.bilingual_writer import _sanitize_filename_fragment as _sanitize_bilingual_fragment
from core.excel_coverage import build_excel_coverage_plan
from core.file_scanner import GENERATED_OUTPUT_DIR_MARKER
from core.language_registry import get_target_lang_display
from core.pdf_image_translation import (
    PDF_MANIFEST_FILENAME,
    reusable_pdf_pages,
)
from core.translation_coverage import COVERAGE_SOURCE_ONLY
from core.word_coverage import build_word_coverage_plan
from core.word_document import _normalize_word_output_name

_MAX_CANDIDATES = 5
_DEFAULT_SOURCE_LANG = "zh"

_TIMESTAMP_RE = re.compile(
    re.escape(GENERATED_OUTPUT_DIR_MARKER) + r"(\d{8}_\d{6})(?:_\d+)?$"
)


def _safe_int(value: Any) -> int:
    """manifest 是外部文件，page_count 之类的字段拿到什么都不能炸。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 目录发现与排序
# ---------------------------------------------------------------------------


def _glob_output_dirs(parent: Path, root_name: str) -> list[Path]:
    # 不用 glob：root_name 是用户的文件夹名，含 [ ] * ? 这类通配符元字符时
    # glob 会把它当模式解释（「附件[终稿]」反而匹配不到自己的产物目录）。
    # iterdir + 前缀字面量比对，任何名字都按字面处理。
    prefix = f"{root_name}{GENERATED_OUTPUT_DIR_MARKER}"
    try:
        if not parent.is_dir():
            return []
        return [
            candidate
            for candidate in parent.iterdir()
            if candidate.name.startswith(prefix) and candidate.is_dir()
        ]
    except OSError:
        return []


def _root_output_dirs(root: Path, custom_output_root: Path | None) -> list[Path]:
    found = _glob_output_dirs(root, root.name)
    if custom_output_root is not None:
        found = found + _glob_output_dirs(custom_output_root, root.name)
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in found:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _dir_timestamp(dirpath: Path) -> datetime:
    match = _TIMESTAMP_RE.search(dirpath.name)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(dirpath.stat().st_mtime)
    except OSError:
        return datetime.min


def _sort_dirs_newest_first(dirs: list[Path]) -> list[Path]:
    return sorted(dirs, key=_dir_timestamp, reverse=True)


def _format_time_label(dt: datetime) -> str:
    return dt.strftime("%m-%d %H:%M")


def _is_valid_preferred_dir(preferred_dir: str | None) -> Path | None:
    if not preferred_dir:
        return None
    path = Path(preferred_dir)
    try:
        if not path.is_dir():
            return None
    except OSError:
        return None
    if GENERATED_OUTPUT_DIR_MARKER not in path.name:
        return None
    return path


def _normalize_scan_roots(scan_roots: list[str | Path]) -> list[Path]:
    """扫描根统一成目录：直接选中的文件按它所在的文件夹算。

    任务侧就是这么定的（task_manager 里 ``source if source.is_dir() else
    source.parent``），输出目录也建在那个文件夹里、按文件夹名命名。不归一的话，
    从同一个文件夹里多选几个文件会被当成多个根，单选一个文件更是一个产物目录都
    找不到。归一后去重，同文件夹多选自然合并回单根。
    """
    normalized: list[Path] = []
    seen: set[str] = set()
    for raw in scan_roots:
        if not raw:
            continue
        root = Path(raw)
        try:
            if root.is_file():
                root = root.parent
        except OSError:
            pass
        key = str(root)
        if key not in seen:
            seen.add(key)
            normalized.append(root)
    return normalized


def _root_for_item(item_path: Path, scan_roots: list[Path]) -> Path | None:
    best: Path | None = None
    for root in scan_roots:
        if item_path.parent == root:
            return root
        try:
            item_path.relative_to(root)
        except ValueError:
            continue
        if best is None or len(str(root)) > len(str(best)):
            best = root
    return best


def _relative_dir_and_name(item_path: Path, root: Path) -> tuple[Path, str]:
    try:
        relative = item_path.relative_to(root)
    except ValueError:
        return Path("."), item_path.name
    return relative.parent, relative.name


def _item_path(item: Any) -> str | None:
    if isinstance(item, dict):
        return item.get("path")
    return getattr(item, "path", None)


# ---------------------------------------------------------------------------
# 单文件匹配（供 runner 复用）
# ---------------------------------------------------------------------------


def _find_highest_revision(target_dir: Path, stem: str, suffix: str) -> Path | None:
    """在 target_dir 下找 `{stem}{suffix}` 或 `{stem}_R{n}{suffix}`，取修订号最高的一个。"""
    try:
        if not target_dir.is_dir():
            return None
    except OSError:
        return None
    pattern = re.compile(rf"^{re.escape(stem)}(?:_R(\d+))?{re.escape(suffix)}$")
    best: Path | None = None
    best_revision = -1
    try:
        entries = list(target_dir.iterdir())
    except OSError:
        return None
    for candidate in entries:
        if not candidate.is_file():
            continue
        match = pattern.match(candidate.name)
        if not match:
            continue
        revision = int(match.group(1)) if match.group(1) else 0
        if revision > best_revision:
            best_revision = revision
            best = candidate
    return best


def _pdf_base_name(source_filename: str, target_lang: str, variant_label: str | None) -> str:
    """镜像 core/pdf_image_translation.py:translated_pdf_base_name 的拼接规则。

    该函数需要 AppSettings 以支持自定义目标语言，而本模块的调用方只有一个
    target_lang 字符串——这里跟 excel/word 一样退化成不带自定义语言表的
    get_target_lang_display 调用（与 bilingual_writer / word_document 的既有
    调用方式一致）。若目标语言是自定义语言，展示名会退化成语言代码本身，
    检测可能因此失配；这是已知偏离，详见任务报告。
    """
    label = _sanitize_bilingual_fragment(get_target_lang_display(target_lang, include_optional=True))
    source_path = Path(source_filename)
    source_stem = _sanitize_bilingual_fragment(source_path.stem)
    suffix = f"_{_sanitize_bilingual_fragment(variant_label)}" if variant_label else ""
    return f"{source_stem}_{label}{suffix}{source_path.suffix}"


def match_previous_output(
    resume_dir: str | Path,
    relative_path: str | Path | None,
    source_filename: str,
    target_lang: str,
    surface: str,
) -> Path | None:
    """按镜像相对路径 + 产物命名规则，在 resume_dir 下找上次翻译产物。找不到返回 None。"""
    resume_dir = Path(resume_dir)
    rel_dir = Path(relative_path) if relative_path not in (None, "") else Path(".")
    target_dir = resume_dir / rel_dir

    if surface in ("excel", "word"):
        if surface == "word":
            basename = _normalize_word_output_name(source_filename)
        else:
            basename = source_filename
            if basename.lower().endswith(".xls"):
                basename = basename[:-4] + ".xlsx"
        lang_display = _sanitize_bilingual_fragment(
            get_target_lang_display(target_lang, include_optional=True)
        )
        candidate = target_dir / bilingual_output_name(basename, lang_display)
        try:
            return candidate if candidate.is_file() else None
        except OSError:
            return None

    if surface == "pdf":
        for variant_label in ("高清", "压缩", None):
            base_name = _pdf_base_name(source_filename, target_lang, variant_label)
            stem = Path(base_name).stem
            suffix = Path(base_name).suffix
            found = _find_highest_revision(target_dir, stem, suffix)
            if found is not None:
                return found
        return None

    return None


# ---------------------------------------------------------------------------
# 底稿资格核查（检测层与 Excel/Word runner 共用）
# ---------------------------------------------------------------------------


def _plan_text_occurrences(plan: Any, *, keyed_by_sheet: bool) -> dict[tuple[str | None, str], int]:
    """一份覆盖率计划里「见过的文本」多重集：键是 (分表名, 文本)，值是出现次数。

    必须按出现次数（而不是文本集合）计数：源文件新增的行/段落若恰好复用了底稿里
    已有的文字，集合比对会当它「见过」而放行，续译换底稿后这些新增内容既不翻译也
    不进产物——集合比对正是这个静默丢失的入口。Excel 还要带上分表名做键：整张新增
    的工作表哪怕逐格文字都在旧表里出现过，也照样是底稿里没有的内容。Word 没有分表
    概念，键的分表名恒为 None。

    每个 unit 的源文半边和整格原文（data["cell_text"]，两者不同时）各计一次。
    整格原文也算，是为了容住拆分失败的情况——底稿里某格「原文\\n译文」没被
    split_existing_bilingual_text 拆开时，unit.source_text 是整格文字，单靠它
    对不上源文件里的那半句原文。
    """
    occurrences: dict[tuple[str | None, str], int] = {}
    for unit in plan.units:
        data = getattr(unit, "data", None)
        sheet: str | None = None
        if keyed_by_sheet and isinstance(data, dict):
            sheet = str(data.get("sheet") or "") or None
        keys: set[str] = set()
        source = str(unit.source_text or "").strip()
        if source:
            keys.add(source)
        if isinstance(data, dict):
            cell_text = str(data.get("cell_text") or "").strip()
            if cell_text:
                keys.add(cell_text)
        for key in keys:
            occurrences[(sheet, key)] = occurrences.get((sheet, key), 0) + 1
    return occurrences


def baseline_missing_source_texts(
    source_path: str | Path,
    baseline_path: str | Path,
    *,
    surface: str,
    target_lang: str,
    source_lang: str,
    baseline_plan: Any | None = None,
    formula_display_value_backfill: bool = True,
) -> list[str] | None:
    """核查上次产物有没有资格当续译底稿：源文件里有、底稿里没有的待译文本。

    续译的补译计划只看底稿自身——源文件在上次翻译之后新增的内容根本不在底稿里，
    照常续译会把它静默丢掉（既不翻译也不出现在产物里）。所以换底稿之前必须核对：
    源文件覆盖率计划里每一条 source_only 文本，都要能在底稿计划的文本全集里找到。

    返回值三态：
      - None：核查跑不起来（.doc/.xls 打不开、计划构建失败……），调用方按「无法
        核查」处理——维持既有行为照常用底稿，不因核查工具失灵而拒用底稿；
      - []：底稿包含源文件全部待译文本，放心用；
      - 非空列表：源文件比上次翻译时多了这些内容（按出现次数逐条计，重复文本会
        重复出现），这份底稿不能再当底稿。

    比对按多重集做「消耗式」匹配：源文件每一处待译文本消耗底稿里一次同文出现，
    消耗完还有剩就是新增。Excel 额外按分表名分桶——整张新增的工作表即使逐格文字
    都在旧表里出现过，也判为新增。Word 的预处理会给段落物化编号前缀（「1.1 概述」
    对「概述」），允许消耗一条「以源文本结尾」的底稿文本（前缀容差），但同一条
    底稿文本只能被消耗一次——源文件新增的短段落不能靠别的段落带前缀的旧文本蒙混
    过关；Excel 无此机制，严格相等。
    """
    try:
        if surface == "excel":
            # 回填开关要和真正跑任务的补译计划一致：开关关着时公式格根本不进
            # 待译集合，硬要求底稿里有它们的显示值文本只会平白拒掉一份好底稿。
            source_plan = build_excel_coverage_plan(
                Path(source_path),
                target_lang=target_lang,
                source_lang=source_lang,
                formula_display_value_backfill=formula_display_value_backfill,
            )
            if baseline_plan is None:
                baseline_plan = build_excel_coverage_plan(
                    Path(baseline_path),
                    target_lang=target_lang,
                    source_lang=source_lang,
                    formula_display_value_backfill=formula_display_value_backfill,
                )
        elif surface == "word":
            source_plan = build_word_coverage_plan(
                Path(source_path), target_lang=target_lang, source_lang=source_lang
            )
            if baseline_plan is None:
                baseline_plan = build_word_coverage_plan(
                    Path(baseline_path), target_lang=target_lang, source_lang=source_lang
                )
        else:
            return None
        keyed_by_sheet = surface == "excel"
        known = _plan_text_occurrences(baseline_plan, keyed_by_sheet=keyed_by_sheet)
    except Exception:  # noqa: BLE001 - 核查失败不等于底稿有问题，交还调用方维持原行为。
        logger.debug(
            f"[续译核查] 底稿资格核查跑不起来，按无法核查处理：{source_path}",
            exc_info=True,
        )
        return None

    prefix_tolerant = surface == "word"
    # 每个物化了编号前缀的底稿段落（「1.1 概述」对「概述」）都要走一次前缀容差
    # 匹配——这是编号文档的常态而不是分歧信号，不能按次数设限。按底稿文本的尾部
    # 1~4 个字符分桶建索引：查询时长文本取末 4 字定位桶，短文本整串定位桶，桶内
    # 再核对 endswith，语义与逐条全集扫描完全一致但复杂度降到桶内小范围。
    rescue_index: dict[str, list[tuple[str | None, str]]] = {}
    if prefix_tolerant:
        for candidate_key in known:
            text = candidate_key[1]
            for length in (1, 2, 3, 4):
                if len(text) >= length:
                    rescue_index.setdefault(text[-length:], []).append(candidate_key)
    missing: list[str] = []
    for unit in source_plan.units:
        if unit.status != COVERAGE_SOURCE_ONLY:
            continue
        source = str(unit.source_text or "").strip()
        if not source:
            continue
        data = getattr(unit, "data", None)
        sheet: str | None = None
        if keyed_by_sheet and isinstance(data, dict):
            sheet = str(data.get("sheet") or "") or None
        key = (sheet, source)
        if known.get(key, 0) > 0:
            known[key] -= 1
            continue
        if prefix_tolerant:
            lookup = source[-4:] if len(source) >= 4 else source
            rescued_key: tuple[str | None, str] | None = None
            for candidate_key in rescue_index.get(lookup, ()):
                if known.get(candidate_key, 0) > 0 and candidate_key[1].endswith(source):
                    rescued_key = candidate_key
                    break
            if rescued_key is not None:
                known[rescued_key] -= 1
                continue
        missing.append(source)
    return missing


# ---------------------------------------------------------------------------
# PDF manifest 读取
# ---------------------------------------------------------------------------


def _load_pdf_manifest(resume_dir: Path) -> dict[str, Any] | None:
    manifest_path = resume_dir / PDF_MANIFEST_FILENAME
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _normalize_relative(value: str) -> str:
    return str(value).replace("\\", "/").strip("/")


def _find_pdf_file_record(manifest: dict[str, Any], relative_path: str) -> dict[str, Any] | None:
    files = manifest.get("files")
    if not isinstance(files, list):
        return None
    target = _normalize_relative(relative_path)
    for record in files:
        if not isinstance(record, dict):
            continue
        if _normalize_relative(str(record.get("relative_path", ""))) == target:
            return record
    return None


def _reusable_page_counts(
    resume_dir: Path,
    manifest: dict[str, Any] | None,
    reusable_cache: dict[str, dict[str, int]],
) -> dict[str, int]:
    """每个文件「续译时真正能复用」的页数——判定标准与 PDF runner 完全同源。

    以前这里自带一套「哪些页算翻完」的标准（含 emergency_normalized、只查
    文件名存在），比 runner 实际复用的标准（仅 success、排除占位页/超限页/
    审校失败页/质量旗标页）宽——弹窗报的剩余页数会比续译实际要跑的少，
    用户看到「还差 2 页」结果跑了 5 页。改为直接调 runner 的
    reusable_pdf_pages 数数（verify_images=False 跳过整图解码，扫描期只要
    计数），两边永远一个口径。
    """
    key = str(resume_dir)
    if key not in reusable_cache:
        counts: dict[str, int] = {}
        try:
            for relative, pages in reusable_pdf_pages(
                manifest, resume_dir, verify_images=False
            ).items():
                counts[relative] = len(pages)
        except Exception:  # noqa: BLE001 - 复用计数失败退回 0，不拖垮扫描。
            logger.debug(f"[续译检测] reusable_pdf_pages 失败：{resume_dir}", exc_info=True)
            counts = {}
        reusable_cache[key] = counts
    return reusable_cache[key]


# ---------------------------------------------------------------------------
# 主检测函数
# ---------------------------------------------------------------------------


def _classify_excel_or_word(
    surface: str,
    matched: Path,
    *,
    item_path: Path,
    target_lang: str,
    source_lang: str,
    formula_display_value_backfill: bool = True,
) -> tuple[str, int | None]:
    try:
        if surface == "excel":
            # 与 runner 的补译计划同一套开关：不然回填关闭的用户会看到弹窗报
            # 「还剩 N 处」、续译跑完却一格没动——正是下面注释里那个死循环观感。
            plan = build_excel_coverage_plan(
                matched,
                target_lang=target_lang,
                source_lang=source_lang,
                formula_display_value_backfill=formula_display_value_backfill,
            )
        else:
            plan = build_word_coverage_plan(matched, target_lang=target_lang, source_lang=source_lang)
        # 只数 source_only：补译写回只处理 source_units，AMBIGUOUS（多行歧义格）
        # 补译碰都不碰。把它算进未译数，会出现「剩 3 处 → 续译 → 还是剩 3 处」
        # 的死循环观感——数字必须对应续译真正会消掉的量。
        untranslated = plan.summary.get(COVERAGE_SOURCE_ONLY, 0)
    except Exception:  # noqa: BLE001 - 覆盖率计划失败一律降级为 found，绝不拖垮扫描。
        logger.debug(f"[续译检测] 覆盖率计划失败，降级为 found：{matched}", exc_info=True)
        return "found", None
    # 底稿自身翻没翻完只是一半真相：源文件在上次翻译之后新增的内容不在底稿里，
    # 光看底稿会把「源文件已经变了」误报成「全部翻完」。
    missing = baseline_missing_source_texts(
        item_path,
        matched,
        surface=surface,
        target_lang=target_lang,
        source_lang=source_lang,
        baseline_plan=plan,
        formula_display_value_backfill=formula_display_value_backfill,
    )
    if missing:
        # missing 非空意味着 runner 一定会拒用这份底稿、整份按源文件重翻
        # （已有译文由翻译记忆尽量复用）。必须原样上报「diverged」，不能把
        # 「要整份重翻」折算成一个更小的补译数字——那会让弹窗承诺一次并不
        # 存在的省钱。
        return "diverged", None
    return ("full" if untranslated == 0 else "partial"), untranslated


def _pdf_manifest_lang_matches(manifest: dict[str, Any] | None, target_lang: str) -> bool:
    """manifest 顶层记着上次的目标语言；与本次不一致时整份清单对本次毫无用处。

    PDF runner 的续译入口就是这么判的（pdf_image_translation 的语言闸：语言不一致
    直接整批拒绝复用）。检测不做同样的核对，换语言重扫会照旧按旧语言的清单报
    「上次已翻完 N/N 页」，弹窗随即承诺一次 runner 根本不会兑现的复用。
    旧清单没这个字段时按匹配处理——跟 runner 的「两边都非空才比对」一致。
    """
    if manifest is None:
        return False
    previous_lang = str(manifest.get("target_lang") or "")
    return not (previous_lang and target_lang and previous_lang != target_lang)


def _classify_pdf(
    resume_dir: Path,
    relative_path_str: str,
    source_filename: str,
    source_path: Path,
    target_lang: str,
    manifest_cache: dict[str, dict[str, Any] | None],
    reusable_cache: dict[str, dict[str, int]],
) -> dict[str, Any]:
    if str(resume_dir) not in manifest_cache:
        manifest_cache[str(resume_dir)] = _load_pdf_manifest(resume_dir)
    manifest = manifest_cache[str(resume_dir)]
    if not _pdf_manifest_lang_matches(manifest, target_lang):
        # 语言不一致的清单当不存在处理：往下落到按「本次语言」的产物命名规则找
        # 文件——找不到就是 none，跟第一次翻这个语言一个待遇。
        manifest = None

    record = _find_pdf_file_record(manifest, relative_path_str) if manifest is not None else None
    if record is not None:
        matched_output: Path | None = None
        recorded_pdf_path = record.get("translated_pdf_path")
        if recorded_pdf_path:
            try:
                candidate = Path(str(recorded_pdf_path))
                if candidate.is_file():
                    matched_output = candidate
            except OSError:
                matched_output = None
        if matched_output is None:
            matched_output = match_previous_output(
                resume_dir, Path(relative_path_str).parent, source_filename, target_lang, "pdf"
            )
        # 源文件在上次翻译之后改动过（页数记录对应的是旧版本）：runner 的体积闸
        # 会整份重新生成（pdf_image_translation 的 source_pdf_size_bytes 比对），
        # 检测按旧记录报页进度就是在替一份过期产物背书。跟 runner 一样，
        # 两边体积都拿得到且不相等才判分歧；旧清单没存体积则维持原判。
        previous_size = _safe_int(record.get("source_pdf_size_bytes"))
        try:
            current_size = source_path.stat().st_size
        except OSError:
            current_size = 0
        if previous_size and current_size and previous_size != current_size:
            return {
                "status": "diverged",
                "matched_output": str(matched_output) if matched_output else None,
                "untranslated_count": None,
                "page_done": None,
                "page_total": None,
            }
        page_total = _safe_int(record.get("page_count"))
        counts = _reusable_page_counts(resume_dir, manifest, reusable_cache)
        page_done = counts.get(Path(_normalize_relative(relative_path_str)).as_posix(), 0)
        if page_total <= 0:
            status = "found"
        elif page_done >= page_total:
            status = "full"
        else:
            status = "partial"
        return {
            "status": status,
            "matched_output": str(matched_output) if matched_output else None,
            "untranslated_count": None,
            "page_done": page_done,
            "page_total": page_total,
        }

    matched_output = match_previous_output(
        resume_dir, Path(relative_path_str).parent, source_filename, target_lang, "pdf"
    )
    if matched_output is None:
        return {
            "status": "none",
            "matched_output": None,
            "untranslated_count": None,
            "page_done": None,
            "page_total": None,
        }
    return {
        "status": "found",
        "matched_output": str(matched_output),
        "untranslated_count": None,
        "page_done": None,
        "page_total": None,
    }


def detect_previous_output(
    surface: str,
    items: list[Any],
    scan_roots: list[str | Path],
    custom_output_root: str | Path | None,
    target_lang: str,
    source_lang: str | None = None,
    preferred_dir: str | None = None,
    formula_display_value_backfill: bool = True,
) -> dict[str, Any] | None:
    """检测扫描输入是否已有历史翻译产物；检测失败一律返回 None（绝不抛给调用方）。"""
    try:
        return _detect_previous_output_impl(
            surface,
            items,
            scan_roots,
            custom_output_root,
            target_lang,
            source_lang,
            preferred_dir,
            formula_display_value_backfill,
        )
    except Exception:  # noqa: BLE001 - 检测是可选增强，绝不能拖垮扫描主流程。
        logger.warning("[续译检测] detect_previous_output 内部异常，按未检测到处理。", exc_info=True)
        return None


def _detect_previous_output_impl(
    surface: str,
    items: list[Any],
    scan_roots: list[str | Path],
    custom_output_root: str | Path | None,
    target_lang: str,
    source_lang: str | None,
    preferred_dir: str | None,
    formula_display_value_backfill: bool = True,
) -> dict[str, Any] | None:
    resolved_source_lang = source_lang or _DEFAULT_SOURCE_LANG
    roots = _normalize_scan_roots(scan_roots)
    if not roots:
        return None

    custom_root = Path(custom_output_root).expanduser() if custom_output_root else None

    per_root_dirs: dict[str, list[Path]] = {}
    for root in roots:
        per_root_dirs[str(root)] = _sort_dirs_newest_first(_root_output_dirs(root, custom_root))

    valid_preferred = _is_valid_preferred_dir(preferred_dir)

    has_any_candidate = valid_preferred is not None or any(per_root_dirs.values())
    if not has_any_candidate:
        return None

    single_root = len(roots) == 1

    candidates: list[dict[str, str]] = []
    selected_dir: Path | None = None

    if single_root:
        dirs = per_root_dirs[str(roots[0])]
        candidates = [
            {
                "dir": str(d),
                "label": _format_time_label(_dir_timestamp(d)),
                "timestamp": _dir_timestamp(d).strftime("%Y-%m-%d %H:%M:%S"),
            }
            for d in dirs[:_MAX_CANDIDATES]
        ]
        if valid_preferred is not None:
            selected_dir = valid_preferred
        elif dirs:
            selected_dir = dirs[0]
    else:
        candidates = []
        if valid_preferred is not None:
            selected_dir = valid_preferred

    def resume_dir_for_root(root: Path) -> Path | None:
        if valid_preferred is not None:
            return valid_preferred
        dirs = per_root_dirs[str(root)]
        return dirs[0] if dirs else None

    manifest_cache: dict[str, dict[str, Any] | None] = {}
    reusable_cache: dict[str, dict[str, int]] = {}
    files: list[dict[str, Any]] = []
    counts = {"full": 0, "partial": 0, "none": 0, "found": 0, "diverged": 0}
    pdf_page_done_total = 0
    pdf_page_total_total = 0
    pdf_has_page_data = False

    for item in items:
        raw_path = _item_path(item)
        if not raw_path:
            continue
        item_path = Path(raw_path)
        root = _root_for_item(item_path, roots)
        entry: dict[str, Any] = {
            "path": str(item_path),
            "status": "none",
            "matched_output": None,
            "untranslated_count": None,
            "page_done": None,
            "page_total": None,
        }

        resume_dir = resume_dir_for_root(root) if root is not None else None
        # 逐文件兜底：一个文件的分类炸了（坏 manifest 字段、坏产物文件），只降级
        # 这一个条目为 none，不能把整批文件的检测结果一起拖成 None。
        try:
            if resume_dir is not None and root is not None:
                rel_dir, source_filename = _relative_dir_and_name(item_path, root)

                if surface == "pdf":
                    relative_path_str = str(rel_dir / source_filename) if str(rel_dir) != "." else source_filename
                    result = _classify_pdf(
                        resume_dir,
                        relative_path_str,
                        source_filename,
                        item_path,
                        target_lang,
                        manifest_cache,
                        reusable_cache,
                    )
                    entry.update(result)
                    if result["page_total"] is not None:
                        pdf_has_page_data = True
                        pdf_page_done_total += result["page_done"] or 0
                        pdf_page_total_total += result["page_total"] or 0
                else:
                    matched = match_previous_output(
                        resume_dir, rel_dir, source_filename, target_lang, surface
                    )
                    if matched is None:
                        entry["status"] = "none"
                    else:
                        entry["matched_output"] = str(matched)
                        status, untranslated = _classify_excel_or_word(
                            surface,
                            matched,
                            item_path=item_path,
                            target_lang=target_lang,
                            source_lang=resolved_source_lang,
                            formula_display_value_backfill=formula_display_value_backfill,
                        )
                        entry["status"] = status
                        entry["untranslated_count"] = untranslated
        except Exception:  # noqa: BLE001
            logger.debug(f"[续译检测] 单文件分类失败，按 none 处理：{item_path}", exc_info=True)

        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        files.append(entry)

    last_task_stopped: bool | None = None
    last_time_label: str | None = None
    if selected_dir is not None:
        last_time_label = _format_time_label(_dir_timestamp(selected_dir))
        if surface == "pdf":
            selected_key = str(selected_dir)
            manifest = (
                manifest_cache[selected_key]
                if selected_key in manifest_cache
                else _load_pdf_manifest(selected_dir)
            )
            # 语言不一致的清单说的是另一个语言那次任务的中断状态，别拿来吓唬本次。
            if (
                isinstance(manifest, dict)
                and "stopped" in manifest
                and _pdf_manifest_lang_matches(manifest, target_lang)
            ):
                stopped_value = manifest.get("stopped")
                if isinstance(stopped_value, bool):
                    last_task_stopped = stopped_value

    summary: dict[str, Any] = {
        "full": counts["full"],
        "partial": counts["partial"],
        "none": counts["none"],
        "found": counts["found"],
        # 源文件在上次翻译后有改动：底稿/页存档当不了续译起点，续译时整份重翻。
        "diverged": counts["diverged"],
        "page_done": pdf_page_done_total if pdf_has_page_data else None,
        "page_total": pdf_page_total_total if pdf_has_page_data else None,
        "last_task_stopped": last_task_stopped,
        "last_time_label": last_time_label,
    }

    return {
        "target_lang": target_lang,
        "candidates": candidates,
        "selected_dir": str(selected_dir) if selected_dir is not None else None,
        "files": files,
        "summary": summary,
    }
