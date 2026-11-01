"""Word 翻译的质量问题收集与质量报告。

从 word_task_runner.py 拆出来。这里汇集「哪些地方要用户复核」的全部判断和写报告的
逻辑：补译后仍是原文的位置、残留中文、表格单元格行数对不上、混合语言段落的处理
结果，以及最终写到输出目录里的质量报告。运行器只负责在合适的时机调用它们。
word_task_runner 会把这里的名字原样再导出，旧的导入路径继续有效。
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from loguru import logger

from core.mixed_language import (
    MIXED_ACTION_EXISTING_BILINGUAL,
    MIXED_ACTION_FOREIGN_NOISE,
    MIXED_ACTION_TRANSLATE,
    MIXED_MARK_FOREIGN_NOISE,
    MIXED_MARK_UNRESOLVED,
    MixedLanguageResult,
)
from core.translation_coverage import COVERAGE_SOURCE_ONLY
from core.translation_filter import TranslationValidationResult
from core.word_coverage import apply_coverage_review_marks, build_word_coverage_plan
from core.word_document import count_text_bearing_header_footer_parts


# Word 输出文档里的底色只留给真正需要人手处理的两类：译文没出来（残留原文、残留中文）
# 和原文本身可疑。程序自己已经判过并放行的——严格重试恢复、语义仲裁认定等义——一律
# 不上底色，只在质量报告里留记录。满篇底色等于没有底色：用户会挨个点开发现全是"已处理"，
# 下一次就整片跳过，真正的问题跟着一起被跳过。
# 「原文本身可疑」这一类不只装混合语言那一种：补译复核判定"紧邻段落不是这一段的译文"
# 也归进来——程序只能在旁边补一条译文，原来那条可疑内容删不删得由人看着原件定，
# 属于需要人手处理，符合上面那条规矩。但只有模型真判了 not_equivalent 才上底色；
# 没问出结果而从严重翻的那批不上——接口抖一下就是一整批，涂红了就是满篇底色。
# 这张表只在一件事上用得着：同一条原文被判进两类时留哪一类（见 _set_review_mark）。
# 全集就是这两个——「语义校验接受」那一类在 9.3.1 整类删除了，它是"已经没事了"的
# 记录，不该占用文档上的底色。
_WORD_REVIEW_MARK_PRIORITY = {
    MIXED_MARK_UNRESOLVED: 1,
    MIXED_MARK_FOREIGN_NOISE: 2,
}
_POST_WRITE_COVERAGE_ISSUE_LIMIT = 50


def _set_review_mark(review_marks: dict[str, str], source: str, mark: str) -> None:
    cleaned = str(source or "").strip()
    if not cleaned:
        return
    existing = review_marks.get(cleaned)
    if existing is None or _WORD_REVIEW_MARK_PRIORITY.get(mark, 0) > _WORD_REVIEW_MARK_PRIORITY.get(existing, 0):
        review_marks[cleaned] = mark


def _add_quality_issues(
    issues: list[dict],
    segment_locations: dict[str, list[dict]],
    sources: list[str],
    *,
    problem: str,
    status: str,
    severity: str,
    validation_results: dict[str, TranslationValidationResult] | None = None,
) -> None:
    seen_keys = {
        (
            issue.get("file"),
            issue.get("location"),
            issue.get("problem"),
            issue.get("severity"),
        )
        for issue in issues
    }
    for source in sources:
        locations = segment_locations.get(source) or [
            {
                "file": "",
                "kind": "",
                "location": "",
                "location_label": "未知位置",
                "section_path": "正文",
                "snippet": _build_source_excerpt(source),
            }
        ]
        for location in locations:
            validation_result = (validation_results or {}).get(source)
            issue = {
                **location,
                "problem": problem,
                "status": status,
                "severity": severity,
            }
            if validation_result and validation_result.review_fragments:
                issue["review_fragments"] = list(validation_result.review_fragments)
            key = (
                issue.get("file"),
                issue.get("location"),
                issue.get("problem"),
                issue.get("severity"),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            issues.append(issue)


def _word_cell_line_mismatch_issue(*, file_name: str, info: dict) -> dict:
    """替换式译文行数与单元格源文段数不齐时的报告条目。

    写入器已经做了保底（保留全部原文、译文整体追加），这里负责让这件事在报告里
    被看见——以前这种单元格会被静默清空原文，内容丢了也没人知道。
    """
    location = str(info.get("location") or "output.table")
    return {
        "file": file_name,
        "kind": "table_cell",
        "location": location,
        "location_label": _format_output_location_label(location),
        "section_path": "表格",
        "snippet": _build_source_excerpt(str(info.get("source") or "")),
        "problem": "替换译文与原文行数不一致",
        "status": "已保留原文并在单元格末尾追加整段译文，请人工核对分段。",
        "severity": "needs_review",
    }


def _word_textbox_overflow_issue(*, file_name: str, info: dict) -> dict:
    """文本框的字缩到下限仍放不下译文。文本框的大小是作者定的，不替他改。"""
    location = str(info.get("location") or "textbox")
    return {
        "file": file_name,
        "kind": "textbox",
        "location": location,
        "location_label": _format_location_label(location),
        "section_path": str(info.get("label") or "文本框"),
        "snippet": _build_source_excerpt(str(info.get("source") or "")),
        "problem": "文本框放不下译文",
        "status": "已把文本框里的字缩到原来的 60%，仍可能有文字被遮住，请在 Word 里调大文本框。",
        "severity": "needs_review",
    }


def _word_formula_position_issue(*, file_name: str, info: dict) -> dict:
    """补标记试满次数仍放不回原位的公式：放在所在那一句的句末，译文里标黄加了批注。"""
    location = str(info.get("location") or "")
    return {
        "file": file_name,
        "kind": "formula_position",
        "location": location,
        "location_label": _format_location_label(location) if location else "",
        "section_path": "",
        "snippet": _build_source_excerpt(str(info.get("source") or "")),
        "problem": "公式位置待核对",
        "status": "该句中有公式未能放回原位，已放在句末，并在译文中标黄、添加批注，请对照原文调整。",
        "severity": "needs_review",
    }


def _word_write_issue(*, file_name: str, info: dict) -> dict:
    """写入器回报的问题，按种类分给对应的报告条目。"""
    if info.get("kind") == "textbox_overflow":
        return _word_textbox_overflow_issue(file_name=file_name, info=info)
    if info.get("kind") == "formula_position":
        return _word_formula_position_issue(file_name=file_name, info=info)
    return _word_cell_line_mismatch_issue(file_name=file_name, info=info)


def _append_post_write_coverage_issues(
    *,
    issues: list[dict],
    file_name: str,
    output_path: Path,
    target_lang: str,
    source_lang: str,
    protect_front_matter: bool = False,
    review_mark_colors: dict[str, str] | None = None,
    existing_highlight_policy: str | None = None,
    mark_log_callback=None,
    pre_reported_residual_sources: set[str] | None = None,
    already_target_sources: set[str] | None = None,
    report_untranslated: bool = True,
) -> int:
    """成品文档体检：先按体检结果往文档上涂复核标记，再把同一批位置写进报告。

    ``report_untranslated=False`` 用在用户按了停止之后：那些还是原文的段落是「停止时
    尚未翻译」，留给续译补，不是译文质量问题——不涂复核标记、不进需复核清单，只按
    返回值计数。夹带零星原文的译文（residual）照常报。

    顺序不能倒过来。以前这里只写报告，报告里几十条「需人工复核」在文件里一个标记
    都没有——用户勾了「标记需复核内容」，拿到的却是一张要自己去几百页里对照的清单。
    体检本来就只能在成品文档上做（要看的正是写完之后的结果），所以标记也放在这一趟。
    传 existing_highlight_policy=None 表示用户没开标记，只写报告。
    """
    plan = build_word_coverage_plan(
        output_path,
        target_lang=target_lang,
        source_lang=source_lang,
        protect_front_matter=protect_front_matter,
    )
    if already_target_sources:
        # 自动识别时模型报过「本来就是目标语言」的段落原样保留是对的，不是漏翻。
        plan = replace(
            plan,
            units=[
                unit
                for unit in plan.units
                if not (
                    unit.status == COVERAGE_SOURCE_ONLY
                    and unit.source_text.strip() in already_target_sources
                )
            ],
        )
    mark_plan = plan
    if not report_untranslated:
        mark_plan = replace(
            plan,
            units=[unit for unit in plan.units if unit.status != COVERAGE_SOURCE_ONLY],
        )
    if existing_highlight_policy is not None:
        marked = apply_coverage_review_marks(
            output_path,
            plan=mark_plan,
            review_mark_colors=review_mark_colors,
            existing_highlight_policy=existing_highlight_policy,
        )
        if marked and mark_log_callback:
            mark_log_callback(marked)
    source_units = plan.source_units
    residual_units = plan.residual_units
    if not source_units and not residual_units:
        return 0
    reported_source_units = source_units if report_untranslated else []

    existing_keys = {
        (
            issue.get("file"),
            issue.get("location"),
            issue.get("problem"),
            issue.get("severity"),
        )
        for issue in issues
    }
    for unit in reported_source_units[:_POST_WRITE_COVERAGE_ISSUE_LIMIT]:
        issue = {
            "file": file_name,
            "kind": unit.kind,
            "location": unit.location,
            "location_label": _format_output_location_label(unit.location),
            "section_path": unit.section_path or "正文",
            "snippet": _build_source_excerpt(unit.source_text),
            "problem": "输出文档仍存在未译源文",
            "status": "该位置未识别到目标语言译文。",
            "severity": "needs_review",
        }
        key = (
            issue.get("file"),
            issue.get("location"),
            issue.get("problem"),
            issue.get("severity"),
        )
        if key in existing_keys:
            continue
        existing_keys.add(key)
        issues.append(issue)

    if len(reported_source_units) > _POST_WRITE_COVERAGE_ISSUE_LIMIT:
        issues.append(
            {
                "file": file_name,
                "kind": "document",
                "location": "output.coverage",
                "location_label": "输出文档",
                "section_path": "正文",
                "snippet": f"共发现 {len(source_units)} 处未译源文，已仅列出前 {_POST_WRITE_COVERAGE_ISSUE_LIMIT} 处。",
                "problem": "输出文档仍存在未译源文",
                "status": "其余未译位置过多，已截断展示。",
                "severity": "needs_review",
            }
        )

    _append_residual_cjk_issues(
        issues=issues,
        existing_keys=existing_keys,
        file_name=file_name,
        residual_units=residual_units,
        pre_reported_residual_sources=pre_reported_residual_sources,
    )

    return len(source_units)


def _append_residual_cjk_issues(
    *,
    issues: list[dict],
    existing_keys: set,
    file_name: str,
    residual_units: list,
    pre_reported_residual_sources: set[str] | None = None,
) -> None:
    """译文整体已翻好、只夹带零星中文时，单独给一条更轻的提示。

    实稿里这些残留常常是章节序号（一、二、三）或单个汉字，不是日期编号——别替用户
    先入为主地断定是哪一种，提示里照实列出残留了什么就够了。

    写盘前的残留巡检已经逐位置报过的段落这里跳过：那条带修复拒收理由、信息更全，
    再报一条坐标和措辞都不同的，位置合并键对不上，同一处残留就会数成两条待办。
    """
    skip_sources = pre_reported_residual_sources or set()
    for unit in residual_units[:_POST_WRITE_COVERAGE_ISSUE_LIMIT]:
        if unit.source_text.strip() in skip_sources:
            continue
        fragments = [str(item) for item in unit.data.get("residual_cjk") or []]
        if not fragments:
            continue
        location = str(unit.data.get("residual_location") or unit.location)
        issue = {
            "file": file_name,
            "kind": unit.kind,
            "location": location,
            "location_label": _format_output_location_label(location),
            "section_path": unit.section_path or "正文",
            "snippet": _build_source_excerpt(
                str(unit.data.get("residual_text") or unit.target_text)
            ),
            "problem": "译文中仍有少量未翻译的原文",
            # 只报实际残留了什么。早先这里固定跟一句"多为日期或编号"，可残留的常常
            # 是章节序号（一、二、三）或单个汉字，那句话等于替用户断言了一个没查过的
            # 原因，会把人往错的方向引。
            "status": f"该位置译文已完成，仅以下内容未翻译：{'、'.join(fragments)}，请确认是否需要改写。",
            "severity": "needs_review",
        }
        key = (
            issue.get("file"),
            issue.get("location"),
            issue.get("problem"),
            issue.get("severity"),
        )
        if key in existing_keys:
            continue
        existing_keys.add(key)
        issues.append(issue)

    if len(residual_units) > _POST_WRITE_COVERAGE_ISSUE_LIMIT:
        issues.append(
            {
                "file": file_name,
                "kind": "document",
                "location": "output.residual",
                "location_label": "输出文档",
                "section_path": "正文",
                "snippet": (
                    f"共发现 {len(residual_units)} 处译文中仍有未翻译的原文，仅列出前 {_POST_WRITE_COVERAGE_ISSUE_LIMIT} 处。"
                ),
                "problem": "译文中仍有少量未翻译的原文",
                "status": "其余位置过多，已截断展示。",
                "severity": "needs_review",
            }
        )


def _apply_mixed_language_word_results(
    *,
    mixed_results: dict[str, MixedLanguageResult],
    translations: dict[str, str],
    quality_issues: list[dict],
    segment_locations: dict[str, list[dict]],
    review_marks: dict[str, str],
) -> None:
    existing_bilingual: list[str] = []
    foreign_noise: list[str] = []
    uncertain: list[str] = []
    semantic_translate: list[str] = []
    leftover: list[str] = []

    for source, result in mixed_results.items():
        if result.action == MIXED_ACTION_EXISTING_BILINGUAL:
            existing_bilingual.append(source)
            continue
        if result.action == MIXED_ACTION_FOREIGN_NOISE:
            if result.translation.strip():
                translations[source] = result.translation.strip()
            foreign_noise.append(source)
            _set_review_mark(review_marks, source, MIXED_MARK_FOREIGN_NOISE)
            continue
        if result.action == MIXED_ACTION_TRANSLATE:
            if result.translation.strip():
                translations[source] = result.translation.strip()
            if result.leftover:
                # 仅输出译文时重试过仍夹着没翻的外文：译文照写，标需复核。
                leftover.append(source)
                _set_review_mark(review_marks, source, MIXED_MARK_UNRESOLVED)
                continue
            if result.accepted_by == "semantic":
                # 语义仲裁放行的不上底色，只在报告里留一条记录。
                semantic_translate.append(source)
            continue
        uncertain.append(source)
        translations[source] = source
        _set_review_mark(review_marks, source, MIXED_MARK_UNRESOLVED)

    if existing_bilingual:
        _add_quality_issues(
            quality_issues,
            segment_locations,
            existing_bilingual,
            problem="原文疑似已包含目标语言译文",
            status="已保留原内容，未追加译文，未存入记忆库。",
            severity="resolved",
        )
    if foreign_noise:
        _add_quality_issues(
            quality_issues,
            segment_locations,
            foreign_noise,
            problem="原文疑似夹杂错误外文",
            status=(
                "已输出译文，但原文中夹杂疑似有误的外文，未存入记忆库；"
                "请对照源文件确认该段原文是否需要更正。"
            ),
            # 程序能做的只是把这段照常翻出来，它没法判断原文里那串外文是不是写错了——
            # 那要对着原件看。这是"原文可能有问题"，不是"程序已经处理好了"。
            severity="needs_review",
        )
    if leftover:
        _add_quality_issues(
            quality_issues,
            segment_locations,
            leftover,
            problem="译文中仍夹带未翻译的外文",
            status="已输出译文并标记需复核。",
            severity="needs_review",
        )
    if uncertain:
        _add_quality_issues(
            quality_issues,
            segment_locations,
            uncertain,
            problem="夹杂多种语言的内容未能确认",
            status="已保留原文，需人工复核。",
            severity="needs_review",
        )
    if semantic_translate:
        _add_quality_issues(
            quality_issues,
            segment_locations,
            semantic_translate,
            problem="夹杂多种语言的译文复查后采用",
            status=(
                "该段夹杂多种语言的译文未通过自动检查，经复查确认含义正确，"
                "已采用（不存入记忆库）。"
            ),
            severity="resolved",
        )


def _build_source_excerpt(text: str, *, head: int = 18, tail: int = 16) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= head + tail + 3:
        return normalized
    return f"{normalized[:head]}……{normalized[-tail:]}"


def _merge_sentences(current: str, addition: str) -> str:
    """Join two judgments about the same position without repeating either."""
    nxt = " ".join(str(addition or "").split())
    cur = " ".join(str(current or "").split())
    if not nxt:
        return cur
    if not cur:
        return nxt
    if nxt in cur:
        return cur
    return f"{cur}；{nxt}"


def _format_location_label(location: str) -> str:
    paragraph_match = re.match(r"body\.paragraph\[(\d+)\]", str(location or ""))
    if paragraph_match:
        return f"正文段落 {int(paragraph_match.group(1)) + 1}"

    cell_match = re.match(r"table\[(\d+)\]\.cell\[(\d+)\]", str(location or ""))
    if cell_match:
        return f"表格 {int(cell_match.group(1)) + 1} / 单元格 {int(cell_match.group(2)) + 1}"

    control_match = re.match(
        r"content_control\[(\d+)\]\.paragraph\[(\d+)\]", str(location or "")
    )
    if control_match:
        control, paragraph = control_match.groups()
        return f"内容控件 {int(control) + 1} / 段落 {int(paragraph) + 1}"

    story_match = re.match(r"(textbox|footnote|endnote)\[(-?\d+)\]\.paragraph\[(\d+)\]", str(location or ""))
    if story_match:
        kind, number, paragraph = story_match.groups()
        if kind == "textbox":
            return f"文本框 {int(number) + 1} / 段落 {int(paragraph) + 1}"
        if kind == "footnote":
            return f"脚注 {number} / 段落 {int(paragraph) + 1}"
        return f"尾注 {number} / 段落 {int(paragraph) + 1}"

    return location or "未知位置"


def _format_output_location_label(location: str) -> str:
    """成品体检条目的位置标签。输出文档的段落号与源文档不是同一套坐标
    （双语写入后段落数已经变了），加「输出」前缀，免得用户拿着报告去
    源文档里数段落。"""
    label = _format_location_label(location)
    return label if label == "未知位置" else f"输出{label}"


def _build_translation_scope_lines(
    file_results: list[dict],
    *,
    translate_headers_footers: bool = False,
    translation_only: bool = False,
) -> list[str]:
    """报告里明写翻译范围，用户不该翻到那一页才发现某处没翻。"""
    header_footer_files: list[str] = []
    for item in file_results:
        if not item.get("success"):
            continue
        target = item.get("output") or item.get("source_path")
        if not target:
            continue
        if count_text_bearing_header_footer_parts(target) > 0:
            header_footer_files.append(str(item.get("name") or "未知文件"))

    if not header_footer_files:
        return []

    listed = "、".join(header_footer_files[:5])
    if len(header_footer_files) > 5:
        listed += f" 等 {len(header_footer_files)} 个文件"
    if translate_headers_footers and translation_only:
        header_footer_line = f"- 页眉、页脚已翻译，译文已替换原文：{listed}。"
    elif translate_headers_footers:
        header_footer_line = (
            f"- 页眉、页脚已翻译，译文紧接在同一行原文之后（不另起一行，以免增加页眉页脚高度）：{listed}。"
        )
    else:
        header_footer_line = (
            f"- 页眉、页脚不参与翻译，输出文档中保持原文：{listed}。如需翻译，"
            "请在 Word 选项中开启「翻译页眉页脚」后重新运行。"
        )
    return [
        "## 翻译范围说明",
        "",
        header_footer_line,
        "- 自动生成的目录、索引和页码域同样不翻译：域更新后会覆盖译文。",
        "",
    ]


_WORD_ISSUE_SEVERITY_LABELS = {
    "resolved": "已自动处理",
    "info": "提示",
    "needs_review": "需人工复核",
}


def _write_word_quality_report(
    *,
    output_dir: Path,
    file_results: list[dict],
    issues: list[dict],
    elapsed_sec: float,
    tm_hit_count: int,
    api_call_count: int,
    translate_headers_footers: bool = False,
    translation_only: bool = False,
) -> Path | None:
    try:
        report_path = output_dir / "word_translation_report.md"
        successful = sum(1 for item in file_results if item.get("success"))
        failed = len(file_results) - successful
        resolved_count = sum(1 for issue in issues if issue.get("severity") == "resolved")
        info_count = sum(1 for issue in issues if issue.get("severity") == "info")
        review_count = len(issues) - resolved_count - info_count

        lines = [
            "# Word 翻译质量报告",
            "",
            "## 任务概览",
            "",
            f"- 文件数：{len(file_results)}",
            f"- 成功文件：{successful}",
            f"- 失败文件：{failed}",
            f"- 耗时：{elapsed_sec:.2f} 秒",
            f"- 来自记忆库：{tm_hit_count}",
            f"- 模型翻译：{api_call_count}",
            f"- 已自动处理事项：{resolved_count}",
            f"- 需人工复核事项：{review_count}",
            f"- 提示事项：{info_count}",
            "",
        ]

        preprocess_lines = []
        for item in file_results:
            preprocess = item.get("preprocess") or {}
            method = preprocess.get("method")
            if not method:
                continue
            preprocess_lines.append(
                (
                    f"- {item.get('name') or '未知文件'}：{method}"
                    f"（自动编号 {int(preprocess.get('labels_seen') or 0)} 段，"
                    f"其中 {int(preprocess.get('labels_prepended') or 0)} 段的编号已转成文字）"
                )
            )
        if preprocess_lines:
            lines.extend(["## 文档准备", "", *preprocess_lines, ""])

        lines.extend(
            _build_translation_scope_lines(
                file_results,
                translate_headers_footers=translate_headers_footers,
                translation_only=translation_only,
            )
        )

        if not issues:
            lines.extend(["## 质量提示", "", "未发现需要提示的问题。", ""])
        else:
            # 标题要跟里面的条目对得上：一份只有目录提示的报告顶着「需复核内容」，
            # 而概览里刚写完「需人工复核事项：0」，两句话自己打自己。
            lines.extend(["## 需复核内容" if review_count else "## 质量提示", ""])
            for idx, issue in enumerate(issues, 1):
                label = _WORD_ISSUE_SEVERITY_LABELS.get(
                    str(issue.get("severity") or ""), "需人工复核"
                )
                lines.extend(
                    [
                        f"### {idx}. {label}",
                        "",
                        f"- 文件：{issue.get('file') or '未知文件'}",
                        f"- 章节路径：{issue.get('section_path') or '正文'}",
                        f"- 位置：{issue.get('location_label') or '未知位置'}",
                        f"- 段落：{issue.get('snippet') or ''}",
                        f"- 问题：{issue.get('problem') or ''}",
                        f"- 处理结果：{issue.get('status') or ''}",
                    ]
                )
                fragments = issue.get("review_fragments") or []
                if fragments:
                    # 这些片段取自**原文**，是规则校验拿去比对、没在译文里按字面对上的部分。
                    # 早先叫「问题片段」，列出来的又都是中文日期编号，读起来像"译文里的
                    # 日期错了"，而实际情况往往是译文把日期正常译成了外文写法。
                    lines.append(
                        f"- 自动检查时未在译文中匹配到的原文片段：{'、'.join(str(item) for item in fragments)}"
                    )
                lines.append("")

        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path
    except Exception as exc:  # noqa: BLE001 - the caller records a non-fatal warning.
        # Do not swallow the exception here.  ``WordTaskRunner._run`` owns the
        # task-level result contract and converts this into ``report_warning``
        # while preserving usable Word output.  Returning ``None`` here would
        # make a real filesystem failure indistinguishable from no report and
        # hide its reason from the UI.
        logger.warning(f"Word 翻译质量报告写入失败：{exc}")
        raise OSError(f"Word 翻译质量报告写入失败：{exc}") from exc
