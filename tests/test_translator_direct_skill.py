from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from openpyxl import Workbook


REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTER_PATH = REPO_ROOT / ".agents" / "skills" / "translator-direct" / "scripts" / "translator_router.py"


def _load_router() -> ModuleType:
    spec = importlib.util.spec_from_file_location("translator_router_under_test", ROUTER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load router from {ROUTER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TranslatorDirectSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.router = _load_router()

    def test_route_groups_split_mixed_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "table.xlsx").write_bytes(b"placeholder")
            (root / "doc.docx").write_bytes(b"placeholder")
            (root / "scan.pdf").write_bytes(b"%PDF-1.4\n")
            (root / "photo.png").write_bytes(b"placeholder")
            generated = root / "old_翻译输出_20260708_120000"
            generated.mkdir()
            (generated / "ignored.xlsx").write_bytes(b"placeholder")

            groups = self.router._scan_route_groups(root, include_images=True)

        self.assertEqual([path.name for path in groups["excel"]], ["table.xlsx"])
        self.assertEqual([path.name for path in groups["word"]], ["doc.docx"])
        self.assertEqual([path.name for path in groups["pdf"]], ["photo.png", "scan.pdf"])

    def test_auto_source_language_samples_xlsx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workbook_path = Path(tmp) / "sample.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet["A1"] = "施工方案需要翻译，材料进场计划需要同步。"
            workbook.save(workbook_path)
            args = self.router._parse_args([str(workbook_path), "--dry-run"])
            groups = {"excel": [workbook_path], "word": [], "pdf": []}

            source_lang = self.router._resolve_source_lang(args, groups)

        self.assertEqual(source_lang, "zh")

    def test_default_router_plan_uses_requested_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sample.xlsx"
            source.write_bytes(b"placeholder")
            args = self.router._parse_args([str(source), "--dry-run"])

            plans = self.router._build_plans(
                repo_root=REPO_ROOT,
                source_path=source,
                groups={"excel": [source], "word": [], "pdf": []},
                args=args,
                source_lang="zh",
                current_config={
                    "text": {"provider": "openai", "model": "gpt-test", "base_url": ""},
                    "image": {},
                    "review": {},
                },
            )

        self.assertEqual(args.text_concurrency, 10)
        self.assertIn("--concurrency", plans[0].command)
        self.assertEqual(plans[0].command[plans[0].command.index("--concurrency") + 1], "10")

    def test_current_agent_openai_config_sets_pdf_image_default(self) -> None:
        with patch.dict("os.environ", {"OPENAI_API_KEY": "secret"}, clear=True):
            config = self.router._discover_current_agent_config()

        self.assertEqual(config["image"]["provider"], "openai")
        self.assertEqual(config["image"]["model"], "gpt-image-2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
