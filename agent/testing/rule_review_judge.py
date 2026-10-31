"""用应用里配好的文本翻译模型，直接测「规则没过的译文」复查判得准不准（PR #258）。

用法（务必先把 TRANSLATOR_APP_DATA_DIR 指到数据副本，见 agent/testing/seed_user_config.py）::

    TRANSLATOR_APP_DATA_DIR=... ./.venv/bin/python3 agent/testing/rule_review_judge.py <结果.json>

每一对（原文, 译文）都是编的公开内容，规则都过不了：
* ``ok`` 的译文其实是对的（数字写成单词、换了写法、品牌名原样保留……）：应判 equivalent；
* ``bad`` 的译文真的有错（漏了数、数被改、少译半句、夹杂别的语言）：绝不能判 equivalent。

判错 ``bad`` 为 equivalent 就是「误放行」，打印 MISS 并以退出码 1 结束。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# (期望, 原文语言, 目标语言, 原文, 译文)
CASES: list[tuple[str, str, str, str, str]] = [
    ("ok", "zh", "fr", "共3层", "Trois niveaux au total"),
    ("ok", "zh", "en", "共3层", "Three floors in total"),
    ("ok", "zh", "fr", "预付款为合同价的百分之三十", "L'avance est de 30 % du prix du contrat"),
    ("ok", "en", "zh", "Article 12 Payment terms", "第十二条 付款方式"),
    ("ok", "en", "zh", "50% of the contract price", "合同价的百分之五十"),
    ("ok", "en", "zh", "Schneider Electric switchgear", "Schneider Electric 开关柜"),
    ("ok", "zh", "fr", "第2次会议纪要", "Procès-verbal de la deuxième réunion"),
    ("ok", "zh", "en", "两台水泵，各15kW", "Two pumps, 15 kW each"),
    ("ok", "zh", "fr", "工期一年", "Délai d'exécution : 12 mois"),
    ("ok", "zh", "ru", "坡度1:2.5", "Уклон 1:2,5"),
    ("ok", "zh", "fr", "面积100m2", "Surface : 100 m²"),
    ("bad", "zh", "fr", "25厚1:2.5水泥砂浆找平", "Ragréage au mortier de ciment 1:2,5"),
    ("bad", "zh", "en", "共3层", "Four floors in total"),
    ("bad", "zh", "fr", "合同总价1500元，30天内付清", "Prix total du contrat : 1 500 yuans"),
    ("bad", "zh", "en", "层高3.6米，共12层", "Floor height 3.6 m, 10 floors in total"),
    ("bad", "zh", "fr", "预付款为合同价的百分之三十", "L'avance est de 20 % du prix du contrat"),
    ("bad", "zh", "en", "2026年3月24日开工", "Work starts on 24 April 2026"),
    ("bad", "en", "zh", "Article 12 Payment terms", "第十三条 付款方式"),
    ("bad", "zh", "fr", "设备基坑：2830X1700mm，深1.2米", "Fosse d'équipement : 2830 × 1700 mm"),
    ("bad", "zh", "fr", "混凝土强度等级C30", "混凝土 classe de résistance C30"),
    ("bad", "zh", "en", "两台水泵，各15kW", "Two pumps"),
]


def main(out_path: str) -> int:
    from core.engine_dispatcher import build_role_engine, quality_rule_problem
    from core.translation_review import ReviewItem, review_translations
    from settings import load_settings

    settings = load_settings()
    engine = build_role_engine(settings, "translation")
    rows = []
    missed = 0
    for index, (expect, src_lang, dst_lang, source, translation) in enumerate(CASES, start=1):
        problem = quality_rule_problem(source, translation, dst_lang, source_lang=src_lang) or "（规则已放行）"
        verdict = review_translations(
            engine,
            [ReviewItem("1", source, translation, problem)],
            source_lang=src_lang,
            target_lang=dst_lang,
        )["1"]
        miss = expect == "bad" and verdict.equivalent
        missed += miss
        rows.append(
            {"no": index, "expect": expect, "source": source, "translation": translation,
             "rule": problem, "verdict": verdict.verdict, "reason": verdict.reason, "miss": miss}
        )
        flag = "MISS" if miss else ("ok" if (expect == "ok") == verdict.equivalent else "--")
        print(f"{index:2d} [{expect}] {flag:4s} {verdict.verdict:15s} {source} → {translation} | {verdict.reason}")
    # 再按语言对整批问一次（≤10 条一批，按编号对答案），看分批后结论是否一样。
    pairs = sorted({(c[1], c[2]) for c in CASES})
    for src_lang, dst_lang in pairs:
        members = [r for r, c in zip(rows, CASES) if (c[1], c[2]) == (src_lang, dst_lang)]
        batch = review_translations(
            engine,
            [ReviewItem(str(r["no"]), r["source"], r["translation"], r["rule"]) for r in members],
            source_lang=src_lang,
            target_lang=dst_lang,
        )
        for r in members:
            r["batched_verdict"] = batch[str(r["no"])].verdict
            if r["expect"] == "bad" and r["batched_verdict"] == "equivalent":
                missed += 1
                r["miss"] = True
                print(f"MISS（成批时）{r['no']} {r['source']} → {r['translation']}")
    Path(out_path).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    ok_accepted = sum(1 for r in rows if r["expect"] == "ok" and r["verdict"] == "equivalent")
    print(
        f"\n对的 {sum(r['expect'] == 'ok' for r in rows)} 条里判一致 {ok_accepted} 条；"
        f"错的 {sum(r['expect'] == 'bad' for r in rows)} 条里误放行 {missed} 条"
    )
    return 1 if missed else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1]))
