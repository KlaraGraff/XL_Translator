"""页眉页脚专用通道：整行只增不改，已有译名一个字都不许动。"""

from core.header_footer_channel import (
    RESOLUTION_MODEL_APPEND,
    RESOLUTION_MODEL_REPLACE,
    RESOLUTION_TM_FRAGMENT,
    RESOLUTION_TM_LINE,
    RESOLUTION_UNRESOLVED,
    build_header_footer_payload,
    build_header_footer_prompt,
    compose_replacement,
    line_has_foreign_content,
    parse_header_footer_response,
    resolve_header_footer_lines,
    source_fragments,
    verify_insert_only,
)
from core.translation_protocol import REPLACE_TRANSLATION_PREFIX

# 用户真实文档的页眉：中文项目名 + 它的法文法定译名（挤在一起没有空格）+ 一长串
# 排版空格 + 还没翻的中文文档名。这一整套逻辑就是为这种行写的。
REAL_HEADER = (
    "贝特瑞地中海负极项目总包一标段PROJETBTRANODEMÉDITERRANÉE(Lot1)"
    "                  地面裂缝修复施工方案"
)
REAL_ADDED = "Plan de réparation des fissures du sol"


def test_only_mixed_lines_enter_the_channel():
    assert line_has_foreign_content(REAL_HEADER, source_lang="zh") is True
    assert line_has_foreign_content("某某工程抢工方案", source_lang="zh") is False
    assert line_has_foreign_content("", source_lang="zh") is False
    # 源语言不是中文时不接管：拉丁字母两边都在用，分不出哪截是「已有译文」。
    assert line_has_foreign_content(REAL_HEADER, source_lang="fr") is False


def test_source_fragments_do_not_swallow_the_existing_translation():
    assert source_fragments(REAL_HEADER, source_lang="zh") == [
        "贝特瑞地中海负极项目总包一标段",
        "地面裂缝修复施工方案",
    ]


def test_accepts_translation_appended_at_the_end():
    ok, reason = verify_insert_only(REAL_HEADER, f"{REAL_HEADER} {REAL_ADDED}")
    assert ok is True
    assert reason == ""


def test_rejects_any_rewrite_of_the_existing_legal_name():
    rewritten = (
        "贝特瑞地中海负极项目总包一标段 PROJET BTR ANODE MÉDITERRANÉE (Lot 1) "
        "地面裂缝修复施工方案 " + REAL_ADDED
    )
    ok, reason = verify_insert_only(REAL_HEADER, rewritten)
    assert ok is False
    assert "PROJETBTRANODEMÉDITERRANÉE(Lot1)" in reason


def test_rejects_dropped_content_and_newlines_and_runaway_growth():
    assert verify_insert_only(REAL_HEADER, "地面裂缝修复施工方案 " + REAL_ADDED)[0] is False
    assert verify_insert_only(REAL_HEADER, f"{REAL_HEADER}\n{REAL_ADDED}")[0] is False
    assert verify_insert_only(REAL_HEADER, "")[0] is False
    runaway = REAL_HEADER + " " + REAL_ADDED * 20
    assert verify_insert_only(REAL_HEADER, runaway)[0] is False


def test_composition_restores_the_original_layout_spacing():
    # 模型把那串排版空格压成了一个——成品必须按原行的空格重建，否则页眉会跑位。
    collapsed = (
        "贝特瑞地中海负极项目总包一标段PROJETBTRANODEMÉDITERRANÉE(Lot1) "
        f"地面裂缝修复施工方案 {REAL_ADDED}"
    )
    composed, reason = compose_replacement(REAL_HEADER, collapsed)
    assert reason == ""
    assert composed == f"{REAL_HEADER} {REAL_ADDED}"
    assert REAL_HEADER in composed


def test_composition_inserts_without_gluing_words_together():
    glued = REAL_HEADER + REAL_ADDED
    composed, reason = compose_replacement(REAL_HEADER, glued)
    assert reason == ""
    assert composed == f"{REAL_HEADER} {REAL_ADDED}"


def test_composition_keeps_a_middle_insertion_in_place():
    original = "某某工程 PROJET XX 施工方案"
    produced = "某某工程 Ouvrage XX PROJET XX 施工方案 Plan"
    composed, reason = compose_replacement(original, produced)
    assert reason == ""
    assert composed == "某某工程 Ouvrage XX PROJET XX 施工方案 Plan"


def test_parse_response_never_guesses_by_position():
    raw = '[{"id":1,"line":"B","added":"b"},{"id":0,"line":"A","added":"a"}]'
    assert parse_header_footer_response(raw, 2) == [("A", "a"), ("B", "b")]
    # 缺项、越界、脏数据一律留空，交给上层退回原样，绝不错位。
    assert parse_header_footer_response('[{"id":5,"line":"X"}]', 2) == [("", ""), ("", "")]
    assert parse_header_footer_response("not json", 2) == [("", ""), ("", "")]
    assert parse_header_footer_response('{"id":0}', 1) == [("", "")]


def test_tm_hit_on_the_whole_line_skips_the_model():
    calls: list[list[str]] = []

    outcome = resolve_header_footer_lines(
        [REAL_HEADER],
        source_lang="zh",
        target_lang="fr",
        tm_lookup=lambda probes: {REAL_HEADER: REAL_ADDED},
        ask_model=lambda batch: calls.append(batch) or [],
    )
    assert calls == []
    assert outcome.model_line_count == 0
    assert outcome.resolutions[0].resolution == RESOLUTION_TM_LINE
    # 整行命中走追加，不带整体替换标记——追加不会动原行一个字。
    assert outcome.translations == {REAL_HEADER: REAL_ADDED}


def test_tm_hit_on_the_single_residual_fragment_skips_the_model():
    outcome = resolve_header_footer_lines(
        ["PROJET BTR ANODE MÉDITERRANÉE 地面裂缝修复施工方案"],
        source_lang="zh",
        target_lang="fr",
        tm_lookup=lambda probes: {"地面裂缝修复施工方案": REAL_ADDED},
        ask_model=None,
    )
    assert outcome.resolutions[0].resolution == RESOLUTION_TM_FRAGMENT
    assert outcome.resolutions[0].translation == REAL_ADDED


def test_two_residual_fragments_go_to_the_model_instead_of_guessing():
    # 两处中文残片时程序无从判断译名该接在哪一处，硬拼会接错位置。
    outcome = resolve_header_footer_lines(
        [REAL_HEADER],
        source_lang="zh",
        target_lang="fr",
        tm_lookup=lambda probes: {"地面裂缝修复施工方案": REAL_ADDED},
        ask_model=lambda batch: [(f"{batch[0]} {REAL_ADDED}", REAL_ADDED)],
    )
    assert outcome.model_line_count == 1
    assert outcome.resolutions[0].resolution == RESOLUTION_MODEL_REPLACE


def test_model_answer_becomes_a_replace_marked_translation():
    outcome = resolve_header_footer_lines(
        [REAL_HEADER],
        source_lang="zh",
        target_lang="fr",
        tm_lookup=None,
        ask_model=lambda batch: [(f"{batch[0]} {REAL_ADDED}", REAL_ADDED)],
    )
    value = outcome.translations[REAL_HEADER]
    assert value.startswith(REPLACE_TRANSLATION_PREFIX)
    assert value.endswith(f"{REAL_HEADER} {REAL_ADDED}")


def test_failed_verification_falls_back_to_appending_the_new_text():
    rewritten = (
        "贝特瑞地中海负极项目总包一标段 PROJET BTR ANODE MÉDITERRANÉE (Lot 1) "
        f"地面裂缝修复施工方案 {REAL_ADDED}"
    )
    outcome = resolve_header_footer_lines(
        [REAL_HEADER],
        source_lang="zh",
        target_lang="fr",
        tm_lookup=None,
        ask_model=lambda batch: [(rewritten, REAL_ADDED)],
    )
    resolution = outcome.resolutions[0]
    assert resolution.resolution == RESOLUTION_MODEL_APPEND
    assert resolution.replace_line is False
    assert resolution.translation == REAL_ADDED
    assert resolution.reject_reason
    assert outcome.rejected == [resolution]


def test_unusable_answer_leaves_the_line_untouched():
    outcome = resolve_header_footer_lines(
        [REAL_HEADER],
        source_lang="zh",
        target_lang="fr",
        tm_lookup=None,
        ask_model=lambda batch: [("完全另一行内容", "")],
    )
    resolution = outcome.resolutions[0]
    assert resolution.resolution == RESOLUTION_UNRESOLVED
    assert resolution.translation == ""
    assert outcome.translations == {}


def test_model_failure_does_not_lose_lines():
    def boom(batch):
        raise RuntimeError("接口抖了")

    outcome = resolve_header_footer_lines(
        [REAL_HEADER],
        source_lang="zh",
        target_lang="fr",
        tm_lookup=None,
        ask_model=boom,
    )
    assert len(outcome.resolutions) == 1
    assert outcome.translations == {}


def test_broken_tm_lookup_does_not_block_the_channel():
    def boom(probes):
        raise RuntimeError("记忆库坏了")

    outcome = resolve_header_footer_lines(
        [REAL_HEADER],
        source_lang="zh",
        target_lang="fr",
        tm_lookup=boom,
        ask_model=lambda batch: [(f"{batch[0]} {REAL_ADDED}", REAL_ADDED)],
    )
    assert outcome.resolutions[0].resolution == RESOLUTION_MODEL_REPLACE


def test_line_already_fully_translated_writes_nothing():
    outcome = resolve_header_footer_lines(
        [REAL_HEADER],
        source_lang="zh",
        target_lang="fr",
        tm_lookup=None,
        ask_model=lambda batch: [(batch[0], "")],
    )
    assert outcome.translations == {}
    assert outcome.resolutions[0].resolution == RESOLUTION_UNRESOLVED


def test_batching_splits_long_input():
    seen: list[int] = []

    def ask(batch):
        seen.append(len(batch))
        return [(f"{line} {REAL_ADDED}", REAL_ADDED) for line in batch]

    lines = [f"第{index}节页眉 PROJET {index}" for index in range(5)]
    resolve_header_footer_lines(
        lines,
        source_lang="zh",
        target_lang="fr",
        tm_lookup=None,
        ask_model=ask,
        batch_size=2,
    )
    assert seen == [2, 2, 1]


def test_prompt_and_payload_carry_the_hard_rules():
    prompt = build_header_footer_prompt(
        source_lang="zh",
        target_lang="fr",
        extra_instructions="建筑工程领域用词。",
    )
    assert "一个字符都不许改" in prompt
    assert "不属于源语言" in prompt
    assert "建筑工程领域用词。" in prompt
    payload = build_header_footer_payload([REAL_HEADER])
    assert '"id": 0' in payload
    assert REAL_HEADER in payload
