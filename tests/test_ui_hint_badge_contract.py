"""``.hint`` / ``.swrow-hint`` are the 15px round「?」badge, not a text style.

Output-location notes once reused ``el("div", "hint")`` for whole sentences;
the badge's fixed width squeezed them into one character per line. Badges
must come from ``createHintBadge``; small explanatory text uses ``.rp-note``.
"""

import re
from pathlib import Path

UI = Path(__file__).parents[1] / "ui" / "src"
BADGE = r"(?:hint|swrow-hint)"
CLASS_LIST = rf'"(?:[^"]*\s)?{BADGE}(?:\s[^"]*)?"'
PATTERNS = [
    re.compile(rf"\bel\(\s*\"\w+\"\s*,\s*{CLASS_LIST}"),
    re.compile(rf"className\s*[:=]\s*{CLASS_LIST}"),
    re.compile(rf"classList\.(?:add|toggle)\(\s*\"{BADGE}\""),
    re.compile(rf"class={CLASS_LIST}"),
]
# Places that build the「?」itself: the factory and the prompt-anatomy「?」button.
ALLOWED = {
    "components.ts": "export function createHintBadge(",
    "views/prompt-anatomy.ts": 'button.textContent = "?";',
}


def test_badge_class_only_comes_from_create_hint_badge():
    offenders = []
    for path in sorted(UI.rglob("*.ts")):
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(UI).as_posix()
        if rel in ALLOWED:
            assert ALLOWED[rel] in source, f"{rel}: allowlisted badge builder moved"
            continue
        for number, line in enumerate(source.splitlines(), 1):
            if any(p.search(line) for p in PATTERNS):
                offenders.append(f"{rel}:{number}: {line.strip()}")
    assert not offenders, "use createHintBadge() for「?」and .rp-note for text:\n" + "\n".join(offenders)
