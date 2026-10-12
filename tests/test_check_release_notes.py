from __future__ import annotations

from pathlib import Path

from scripts.check_release_notes import (
    extract_channel,
    main,
    notes_errors,
)

ROOT = Path(__file__).resolve().parents[1]

DRAFT_BODY = (
    "⚠️ 发布说明整理中，稍后补上。Release notes are being written.\n\n"
    "构建渠道 / Channel: `{channel}`\n"
)


def _notes(version: str = "10.0.1", mac_note: str = "（Developer ID 签名并通过 Apple 公证）", temp: bool = False) -> str:
    suffix = "_TEMP_SIGNED_TEST" if temp else ""
    return (
        "## 中文\n\n**修复**\n原来会卡住，现在不会。\n\n**下载**\n"
        f"- macOS：Translator_macOS_arm64_{version}{suffix}.dmg{mac_note}\n"
        f"- Windows：Translator_Windows_x64_{version}_Setup.exe\n\n"
        "## English\n\n**Fix**\nIt no longer hangs.\n"
    )


def test_channel_is_read_from_the_draft_written_by_the_build():
    assert extract_channel(DRAFT_BODY.format(channel="formal-release")) == "formal-release"
    assert extract_channel("手动改过的正文") is None


def test_well_formed_formal_notes_pass():
    assert notes_errors(_notes(), "10.0.1", "formal-release") == []


def test_published_v10_notes_pass_the_same_checks():
    notes = (ROOT / "docs" / "release-notes" / "v10.0.0.md").read_text(encoding="utf-8")
    assert notes_errors(notes, "10.0.0", "formal-release") == []


def test_placeholder_and_empty_notes_are_rejected():
    assert notes_errors("  \n", "10.0.1", "formal-release") == ["发布说明是空的。"]
    errors = notes_errors(DRAFT_BODY.format(channel="formal-release"), "10.0.1", "formal-release")
    assert any("占位" in error for error in errors)


def test_english_before_chinese_is_rejected():
    text = _notes().replace("## 中文", "## TMP").replace("## English", "## 中文").replace("## TMP", "## English")
    assert any("中文一节" in error for error in notes_errors(text, "10.0.1", "formal-release"))


def test_download_names_must_match_this_version():
    errors = notes_errors(_notes(version="10.0.0"), "10.0.1", "formal-release")
    assert any("Translator_macOS_arm64_10.0.1.dmg" in error for error in errors)
    assert any("Translator_Windows_x64_10.0.1_Setup.exe" in error for error in errors)


def test_formal_build_must_not_claim_unnotarized():
    errors = notes_errors(_notes(mac_note="（未签名、未公证）"), "10.0.1", "formal-release")
    assert any("未公证" in error for error in errors)


def test_temporary_build_must_say_unnotarized_and_use_its_file_name():
    claimed = _notes(temp=True)
    assert any("必须写明" in error for error in notes_errors(claimed, "10.0.1", "temporary-test"))
    honest = _notes(temp=True, mac_note="（临时签名、未公证，首次打开需右键「打开」）")
    assert notes_errors(honest, "10.0.1", "temporary-test") == []
    assert any(
        "TEMP_SIGNED_TEST" in error
        for error in notes_errors(_notes(mac_note="（未公证）"), "10.0.1", "temporary-test")
    )


def test_unknown_channel_is_rejected():
    assert any("无法识别" in error for error in notes_errors(_notes(), "10.0.1", "unsigned-test"))


def test_cli_check_and_compare(tmp_path, capsys):
    notes = tmp_path / "v10.0.1.md"
    notes.write_text(_notes(), encoding="utf-8")
    draft = tmp_path / "draft.md"
    draft.write_text(DRAFT_BODY.format(channel="formal-release"), encoding="utf-8")
    assert main(["--notes", str(notes), "--version", "10.0.1", "--draft-body", str(draft)]) == 0

    draft.write_text("没有渠道行", encoding="utf-8")
    assert main(["--notes", str(notes), "--version", "10.0.1", "--draft-body", str(draft)]) == 1

    readback = tmp_path / "readback.md"
    readback.write_text(_notes().replace("\n", "\r\n").rstrip(), encoding="utf-8")
    assert main(["--notes", str(notes), "--compare", str(readback)]) == 0
    readback.write_text(_notes() + "多出来一行\n", encoding="utf-8")
    assert main(["--notes", str(notes), "--compare", str(readback)]) == 1


def test_release_workflow_replaces_body_before_publishing():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    replace = workflow.index('gh release edit "$tag" --notes-file')
    readback = workflow.index("--compare")
    publish = workflow.index('gh release edit "$tag" --draft=false')
    assert replace < readback < publish
    # 用 GITHUB_TOKEN 建的 tag 不会触发 push，必须显式 dispatch 正式构建。
    assert 'gh workflow run build-distributions.yml --ref "v${VERSION}" -f release=true' in workflow
    assert "refs/heads/main" in workflow


def test_build_workflow_accepts_dispatched_tag_releases_only_when_asked():
    workflow = (ROOT / ".github" / "workflows" / "build-distributions.yml").read_text(encoding="utf-8")
    assert "github.event_name == 'workflow_dispatch' && inputs.release" in workflow
    assert "default: false" in workflow
    assert "tag_name: ${{ github.ref_name }}" in workflow
