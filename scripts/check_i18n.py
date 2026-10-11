"""Check the UI translation catalog (ui/src/i18n/catalog/*.json).

Usage::

    python scripts/check_i18n.py            # report, exit 1 on problems
    python scripts/check_i18n.py --backend  # also list backend messages with no entry

What counts as a problem:

* a ``t("…")`` key in ui/src that has no English or French entry;
* an entry whose English/French text does not carry the same ``{0}`` placeholders;
* a Chinese string literal in ui/src that is not inside ``t(…)`` and not marked
  ``// i18n-ignore`` on its line (it would stay Chinese after switching language).

Backend messages (logs, errors, reports) are translated in the UI by template
matching (``tx()`` in ui/src/i18n/index.ts).  Their coverage is reported with
``--backend`` but never fails the check: backend wording changes in other PRs
should not turn CI red; the listing says what to add to the catalog.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_SRC = ROOT / "ui" / "src"
CATALOG_DIR = UI_SRC / "i18n" / "catalog"
LOCALES = ("en", "fr")

CJK_RE = re.compile(r"[㐀-鿿]")
PLACEHOLDER_RE = re.compile(r"\{(\d+)(?:\|[^}]*)?\}")
# t("…") / t('…') / t(`…`) with a literal first argument.
T_CALL_RE = re.compile(r"""(?<![\w.$])t\(\s*(["'`])((?:\\.|(?!\1).)*?)\1""", re.S)
STRING_RE = re.compile(r"""(["'`])((?:\\.|(?!\1)[^\n])*?)\1""")

BACKEND_DIRS = ("api", "core", "engines")
BACKEND_FILES = ("settings.py", "config.py")
BACKEND_SKIP_CALLS = {
    "re.compile", "re.sub", "re.search", "re.match", "re.findall", "re.split",
    "any", "tuple", "set", "frozenset",
}


def _unescape_js(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        token = match.group(1)
        if token.startswith("u"):
            return chr(int(token[1:], 16))
        return {"n": "\n", "t": "\t", "r": "\r"}.get(token, token)

    return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", repl, value, flags=re.S)


def load_catalog() -> dict[str, dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for path in sorted(CATALOG_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, entry in data.items():
            merged.setdefault(key, {}).update({k: v for k, v in (entry or {}).items() if isinstance(v, str)})
    return merged


def _strip_comments(source: str) -> list[str]:
    """Blank out // and /* */ comments but keep line numbers and strings."""
    out: list[str] = []
    in_block = False
    for line in source.splitlines():
        result = []
        i = 0
        quote = ""
        while i < len(line):
            ch = line[i]
            if in_block:
                if line.startswith("*/", i):
                    in_block = False
                    i += 2
                    continue
                i += 1
                continue
            if quote:
                result.append(ch)
                if ch == "\\" and i + 1 < len(line):
                    result.append(line[i + 1])
                    i += 2
                    continue
                if ch == quote:
                    quote = ""
                i += 1
                continue
            if ch in "\"'`":
                quote = ch
                result.append(ch)
                i += 1
                continue
            if line.startswith("//", i):
                break
            if line.startswith("/*", i):
                in_block = True
                i += 2
                continue
            result.append(ch)
            i += 1
        out.append("".join(result))
    return out


def ui_sources() -> list[Path]:
    return sorted(p for p in UI_SRC.rglob("*.ts") if "i18n" not in p.parts)


def collect_ui_keys() -> dict[str, list[str]]:
    keys: dict[str, list[str]] = {}
    for path in ui_sources():
        text = path.read_text(encoding="utf-8")
        for match in T_CALL_RE.finditer(text):
            key = _unescape_js(match.group(2))
            if match.group(1) == "`" and "${" in key:
                continue
            line = text.count("\n", 0, match.start()) + 1
            keys.setdefault(key, []).append(f"{path.relative_to(ROOT)}:{line}")
    return keys


def find_raw_chinese() -> list[str]:
    problems: list[str] = []
    for path in ui_sources():
        raw = path.read_text(encoding="utf-8")
        raw_lines = raw.splitlines()
        code_lines = _strip_comments(raw)
        for number, line in enumerate(code_lines, start=1):
            if "i18n-ignore" in raw_lines[number - 1]:
                continue
            for match in STRING_RE.finditer(line):
                if not CJK_RE.search(match.group(2)):
                    continue
                before = line[: match.start()]
                if re.search(r"(?<![\w.$])t\(\s*$", before):
                    continue
                problems.append(f"{path.relative_to(ROOT)}:{number}: {match.group(0)[:80]}")
    return problems


def check_entries(catalog: dict[str, dict[str, str]]) -> list[str]:
    problems: list[str] = []
    for key, entry in catalog.items():
        expected = sorted(set(PLACEHOLDER_RE.findall(key)))
        for loc in LOCALES:
            value = entry.get(loc)
            if value is None:
                continue
            got = sorted(set(PLACEHOLDER_RE.findall(value)))
            if got != expected:
                problems.append(f"placeholders differ ({loc}): {key!r} -> {value!r}")
    return problems


def _template_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        index = 0
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            else:
                parts.append("{%d}" % index)
                index += 1
        return "".join(parts)
    return None


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        base = func.value
        prefix = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
        return f"{prefix}.{func.attr}"
    if isinstance(func, ast.Name):
        return func.id
    return ""


def collect_backend_messages() -> dict[str, str]:
    """User-visible-looking Chinese strings in the backend, template → location."""
    files = [p for d in BACKEND_DIRS for p in (ROOT / d).rglob("*.py")]
    files += [ROOT / name for name in BACKEND_FILES]
    found: dict[str, str] = {}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        docstrings: set[ast.AST] = set()
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
            body = getattr(node, "body", None)
            if (
                isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                docstrings.add(body[0].value)
        for node in ast.walk(tree):
            if node in docstrings or isinstance(parents.get(node), ast.JoinedStr):
                continue
            template = _template_of(node)
            if not template or not CJK_RE.search(template):
                continue
            cur: ast.AST = node
            name = ""
            while cur in parents:
                cur = parents[cur]
                if isinstance(cur, ast.Call):
                    name = _call_name(cur)
                    break
            if name.startswith("logger.") or name in BACKEND_SKIP_CALLS or name.endswith(
                (".startswith", ".endswith", ".replace", ".split", ".strip")
            ):
                continue
            found.setdefault(template, f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", action="store_true", help="list backend messages without an entry")
    args = parser.parse_args()

    catalog = load_catalog()
    keys = collect_ui_keys()
    problems: list[str] = []
    for key, where in sorted(keys.items()):
        entry = catalog.get(key, {})
        missing = [loc for loc in LOCALES if not entry.get(loc)]
        if missing:
            problems.append(f"missing {'/'.join(missing)}: {key!r} ({where[0]})")
    problems += check_entries(catalog)
    raw = find_raw_chinese()
    problems += [f"untranslated literal: {item}" for item in raw]

    for line in problems:
        print(line)
    print(f"ui keys: {len(keys)}, catalog entries: {len(catalog)}, problems: {len(problems)}")

    if args.backend:
        backend = collect_backend_messages()
        uncovered = {k: v for k, v in backend.items() if not all(catalog.get(k, {}).get(loc) for loc in LOCALES)}
        for key, where in sorted(uncovered.items(), key=lambda kv: kv[1]):
            print(f"backend without entry: {where}: {key!r}")
        print(f"backend messages: {len(backend)}, without entry: {len(uncovered)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
