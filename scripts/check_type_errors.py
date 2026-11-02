"""PR 检查：改过的文件里，类型检查（pyright）报的错不许变多。

全项目现在约 700 处类型错误，一次改完不现实，所以和 check_silent_excepts.py 一样只看增量：
PR 改过的业务代码文件（不含 tests/、scripts/）加在一起和基线比，总数没变多就通过
（按总数比，是为了把代码搬到新文件时不被误拦）。
老错误碰到时顺手修，不强制。

基线的做法：在临时目录里 `git worktree add` 出 PR 的基线提交，对同一批文件跑一次 pyright。

用法（仓库根目录）：
    python scripts/check_type_errors.py <基线提交>
退出码：0 通过，1 有文件的错误变多，2 检查本身出错。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections import Counter

SKIP_PREFIXES = ("tests/", "scripts/", "ui/", "src-tauri/", "docs/", "packaging/", "agent/")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def _changed_python_files(base: str) -> list[str]:
    out = _git("diff", "--name-only", "--diff-filter=AMR", base, "--", "*.py")
    return [path for path in out.splitlines() if path and not path.startswith(SKIP_PREFIXES)]


def _errors(root: str, files: list[str]) -> dict[str, list[dict]]:
    present = [path for path in files if os.path.exists(os.path.join(root, path))]
    if not present:
        return {}
    result = subprocess.run(
        [sys.executable, "-m", "pyright", "--pythonpath", sys.executable, "--outputjson", *present],
        cwd=root,
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"pyright 没有输出结果：\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
    by_file: dict[str, list[dict]] = {path: [] for path in present}
    prefix = os.path.realpath(root) + os.sep
    for item in payload.get("generalDiagnostics", []):
        if item.get("severity") != "error":
            continue
        path = os.path.realpath(item["file"])
        rel = path[len(prefix):] if path.startswith(prefix) else path
        if rel in by_file:
            by_file[rel].append(item)
    return by_file


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    base = _git("merge-base", argv[1], "HEAD").strip()
    files = _changed_python_files(base)
    if not files:
        print("通过：没有改到业务代码文件。")
        return 0
    now = _errors(os.getcwd(), files)
    with tempfile.TemporaryDirectory() as tmp:
        worktree = os.path.join(tmp, "base")
        _git("worktree", "add", "--detach", worktree, base)
        try:
            before = _errors(worktree, files)
        finally:
            _git("worktree", "remove", "--force", worktree)
    # 比的是这批文件的总数：把代码从一个文件搬到新文件时，新文件的错误是从老文件带过来的，不算新增。
    total_now = sum(len(now.get(path, [])) for path in files)
    total_before = sum(len(before.get(path, [])) for path in files)
    if total_now > total_before:
        print(f"改过的 {len(files)} 个文件里，类型错误从 {total_before} 处变成 {total_now} 处：")
        for path in files:
            old_keys = Counter((e.get("rule"), e.get("message")) for e in before.get(path, []))
            for item in now.get(path, []):
                key = (item.get("rule"), item.get("message"))
                if old_keys[key] > 0:
                    old_keys[key] -= 1
                    continue
                line = item["range"]["start"]["line"] + 1
                print(f"  {path} 第 {line} 行 {item.get('rule', '')}：{item['message'].splitlines()[0]}")
        print(
            "\n上面是和基线相比多出来的（搬家带过来的也会列出，以总数为准）。修好再推；"
            "确实是误报的，在那一行写 `# pyright: ignore[<规则名>]`。"
        )
        return 1
    print(f"通过：改过的 {len(files)} 个文件里类型错误 {total_before} → {total_now} 处，没有变多。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
