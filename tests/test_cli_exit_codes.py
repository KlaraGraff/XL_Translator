from __future__ import annotations

import unittest

from scripts import translate_excel_cli, translate_pdf_cli, translate_word_cli


class CliExitCodeTests(unittest.TestCase):
    def test_all_failed_results_return_nonzero(self) -> None:
        failed = [{"name": "source", "success": False}]
        for module in (translate_excel_cli, translate_word_cli, translate_pdf_cli):
            with self.subTest(module=module.__name__):
                self.assertEqual(module._result_exit_code(failed), 1)

    def test_at_least_one_success_returns_zero(self) -> None:
        partial = [
            {"name": "failed", "success": False},
            {"name": "ok", "success": True},
        ]
        for module in (translate_excel_cli, translate_word_cli, translate_pdf_cli):
            with self.subTest(module=module.__name__):
                self.assertEqual(module._result_exit_code(partial), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
