"""English and French counts must agree with the number: "1 page", not "1 pages".

A translation that puts a plural noun right after a count placeholder
("{0} pages", "{0} fichiers") has to choose the form with ``{0|page|pages}``
(see ui/src/i18n/GLOSSARY.md).  Counts that can never be 1 (limits, timeouts,
lists only shown for two or more) are listed in ``ALLOWED`` with the reason.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG_DIR = ROOT / "ui" / "src" / "i18n" / "catalog"

PLURAL_NOUNS = {
    "en": set(
        "pages files items entries edits drawings lines paragraphs characters duplicates addresses "
        "locations translations texts tiles rounds outputs conflicts requests keys models tasks "
        "cells boxes images comments documents presentations slides terms rows columns shapes charts "
        "sheets segments batches words attempts retries components connections roles records errors "
        "warnings pictures formulas tables notes objects labels bytes changes places blocks".split()
    ),
    "fr": set(
        "pages fichiers éléments entrées modifications plans lignes paragraphes caractères doublons "
        "adresses emplacements traductions textes zones passes sorties conflits requêtes clés "
        "modèles tâches cellules images commentaires documents présentations diapositives termes colonnes "
        "formes graphiques feuilles segments lots mots tentatives composants connexions réglages "
        "enregistrements erreurs formules tableaux notes objets étiquettes octets blocs autres endroits".split()
    ),
}

# Chinese key prefix → why the count is never 1 (or the noun is not counted).
ALLOWED = {
    "Excel 连续 {0} 秒没有进展": "timeout setting",
    "LibreOffice 转换超时": "timeout setting",
    "图纸转换工具在 {0} 秒内": "timeout setting",
    "图纸转换超时": "timeout setting",
    "处理超时（超过 {0} 秒": "timeout setting",
    "模型服务在 {0} 秒内": "timeout setting",
    "[WARN] {0}!{1} 原文加译文超过 Excel 单元格上限": "the {2} limit is 32767; {3} cut characters is the remainder of a long text",
    "[WARN] 共 {0} 个单元格超过 Excel 的 {1} 字上限": "{1} is Excel's 32767 limit",
    "自定义语言名称不能超过": "length limit",
    "{0} 等 {1} 个文件": "only shown for two or more files",
    "{0}未生成译文，这 {1} 个文件均未生成": "only shown for two or more files",
    "仅显示前 30 个文件": "only shown above 30",
    "仅显示前 50 处": "only shown above 50",
    "仅显示前 {0} 条预览": "preview limit",
    "已选择前 {0} 条": "selection limit",
    "并发上限": "concurrency limit",
    "扫描到的 {0} 项均未能加入清单": "batch scan of a folder",
    "译图太小": "image size in pixels",
    "译图尺寸过小": "image size in pixels",
    "遗留原文的译文有 {0} 条，超出单次自动补译上限": "{0} exceeds the limit, so it is above 1",
    "（共 {0} 个地址": "only shown for two or more addresses",
}

_COUNT_RE = re.compile(r"\{(\d+)\}(\s*(?:/|of|sur)\s*\{(\d+)\})?((?:\s+[A-Za-zÀ-ÿ'\-]+){1,2})")


def _problems() -> list[str]:
    problems: list[str] = []
    for path in sorted(CATALOG_DIR.glob("*.json")):
        for key, entry in json.loads(path.read_text(encoding="utf-8")).items():
            if not isinstance(entry, dict) or any(key.startswith(prefix) for prefix in ALLOWED):
                continue
            for locale, nouns in PLURAL_NOUNS.items():
                text = entry.get(locale) or ""
                for match in _COUNT_RE.finditer(text):
                    index = match.group(3) or match.group(1)
                    if "{" + index + "|" in text:
                        continue
                    if any(word.lower() in nouns for word in match.group(4).split()):
                        problems.append(f"{path.name} [{locale}] {key!r}: {text!r}")
                        break
    return problems


class UiPluralTests(unittest.TestCase):
    def test_counts_choose_singular_or_plural(self) -> None:
        problems = _problems()
        self.assertEqual(
            problems,
            [],
            "Use {N|singular|plural} after the count (see ui/src/i18n/GLOSSARY.md), "
            "or add the key to ALLOWED with the reason it is never 1:\n" + "\n".join(problems),
        )

    def test_bundled_adapters_name_one_file(self) -> None:
        for manifest in sorted((ROOT / "adapters").glob("*/adapter.json")):
            data = json.loads(manifest.read_text(encoding="utf-8"))
            one = data.get("file_noun_one") or {}
            self.assertTrue(one.get("en") and one.get("fr"), f"{manifest}: file_noun_one needs en and fr")


if __name__ == "__main__":
    unittest.main(verbosity=2)
