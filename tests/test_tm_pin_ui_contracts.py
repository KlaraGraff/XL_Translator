"""记忆库页的固定（图钉）交互契约。

页面本身没有 DOM 测试环境，这里按仓库里既有的做法，对 views/library.ts 做源码级断言，
盯住这次改动的几个要害：

* 表格里有单独的「固定」列，每行一颗图钉，点一下就走 pin 接口；
* 编辑 / 删除固定词条必须先过确认框，only then 带 allow_pinned=true；
* 未固定的词条带 allow_pinned=false（这就是原来的 bug：页面无条件带 true）；
* 批量删除里有固定词条时，勾选框默认不勾，未勾时不删固定的。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

LIBRARY = Path(__file__).resolve().parents[1] / "ui" / "src" / "views" / "library.ts"


def _source() -> str:
    return LIBRARY.read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    match = re.search(rf"(?:async )?function {re.escape(name)}\(.*?\n\}}", _source(), re.S)
    assert match, f"在 library.ts 里找不到函数 {name}"
    return match.group(0)


class PinColumnTests(unittest.TestCase):
    def test_table_has_a_pin_column_right_before_the_row_actions(self) -> None:
        body = _function_body("renderTable")
        # 表头：固定列紧挨在操作列之前。
        self.assertLess(body.index('pinTh.dataset.col = "pin"'), body.index('actionsTh.dataset.col = "actions"'))
        self.assertIn('pinTh.textContent = t("固定")', body)
        # 每一行都有图钉，点击走同一个切换函数。
        self.assertIn('pinBtn.className = pinned ? "pin-toggle on" : "pin-toggle"', body)
        self.assertIn("togglePin(entry)", body)
        # 已固定 / 未固定的悬停说明按样张原文。
        self.assertIn('t("已固定，点击取消固定")', body)
        self.assertIn('t("点击固定")', body)

    def test_pin_toggle_uses_the_existing_pin_endpoint_and_offers_undo(self) -> None:
        body = _function_body("togglePin")
        self.assertIn("/api/tm/entries/${entry.id}/pin", body)
        self.assertIn('t("已固定「{0}」", entry.source_text)', body)
        self.assertIn('t("已取消固定「{0}」", entry.source_text)', body)
        # 撤销：再切一次，回到原来的状态。
        self.assertIn('label: t("撤销")', body)
        self.assertIn("void togglePin(current)", body)


class PinnedOnlyFilterTests(unittest.TestCase):
    def test_filter_chip_lives_in_the_filter_row_with_the_pinned_count(self) -> None:
        body = _function_body("renderFilterRow")
        self.assertIn('t("只看固定")', body)
        self.assertIn("icon(\"pin\"", body)
        self.assertIn("fmtNum(pinCount)", body)
        self.assertIn("togglePinnedOnly", body)

    def test_list_request_carries_the_optional_pinned_parameter(self) -> None:
        body = _function_body("refreshTm")
        self.assertIn('${query.pinnedOnly ? "&pinned=true" : ""}', body)
        self.assertIn("payload.pin_count?.pinned", body)
        # 跨页「选择全部」也要按同一个固定筛选取 id。
        self.assertIn("query.pinnedOnly ? \"&pinned=true\" : \"\"", _function_body("fetchAllMatchingTmIds"))


class PinnedBulkActionTests(unittest.TestCase):
    def test_bulk_bar_offers_pin_and_unpin(self) -> None:
        body = _function_body("buildFoot")
        self.assertIn('t("固定")', body)
        self.assertIn('t("取消固定")', body)
        self.assertIn('"bulk-pin"', body)
        self.assertIn('"bulk-unpin"', body)
        self.assertIn('"/api/tm/entries/bulk/pin"', _function_body("bulkSetPinned"))


class PinnedEditAndDeleteTests(unittest.TestCase):
    def test_editing_a_pinned_entry_asks_first(self) -> None:
        body = _function_body("openPinnedEditConfirm")
        self.assertIn('t("这是一个固定的词条")', body)
        self.assertIn('t("继续编辑")', body)
        self.assertIn("openAddEditModal(entry)", body)
        # 表格里的编辑按钮：固定的先弹确认框。
        self.assertIn(
            "isPinned(entry) ? openPinnedEditConfirm(entry) : openAddEditModal(entry)",
            _function_body("renderTable"),
        )

    def test_update_sends_allow_pinned_only_for_a_pinned_entry(self) -> None:
        body = _function_body("openAddEditModal")
        self.assertNotIn("allow_pinned: true", body)
        self.assertIn("allow_pinned: editing ? isPinned(", body)

    def test_deleting_a_pinned_entry_uses_the_danger_confirm_then_allows_it(self) -> None:
        body = _function_body("openDeleteModal")
        self.assertIn('t("删除固定的词条？")', body)
        self.assertIn('label: t("删除"), variant: "danger-solid"', body)
        self.assertIn('const allowPinned = pinned ? "true" : "false";', body)
        # 未固定的流程照旧带 false：原来无条件 true 的 bug 不能再回来。
        self.assertNotIn("allow_pinned=true`", body)


class BulkDeletePinnedTests(unittest.TestCase):
    def test_selection_with_pinned_entries_gets_the_checkbox_dialog(self) -> None:
        body = _function_body("confirmBulkDelete")
        self.assertIn('t("删除选中的 {0} 条词条？"', body)
        self.assertIn('t("固定的词条也一起删除")', body)
        # 默认不勾。
        self.assertIn("box.checked = false;", body)
        # 按钮上的数字跟着勾选变。
        self.assertIn('deleteBtn.textContent = t("删除 {0} 条", fmtNum(deleting));', body)
        # 未勾 → allow_pinned=false（固定的保留）；勾上才 true。
        self.assertIn("JSON.stringify({ ids, allow_pinned: deletePinned })", body)
        # 没有固定词条时保留原来的弹窗。
        self.assertIn('t("批量删除记忆条目？")', body)

    def test_selection_counts_pinned_rows_on_other_pages_too(self) -> None:
        body = _function_body("selectedPinnedEntries")
        self.assertIn("/api/tm/entries/by-ids?ids=", body)


if __name__ == "__main__":
    unittest.main()
