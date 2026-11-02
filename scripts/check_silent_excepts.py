"""PR 检查：改过的文件里，「出错被兜住后什么都不做」的地方不许变多。

指的是 ruff 的两条规则：
  S110  try … except …: pass
  S112  try … except …: continue
兜住错误时至少记一行日志；确实可以忽略的，在那一行写
`# noqa: S110 <为什么可以忽略>`。

只比较这次 PR 改过的业务代码文件（不含 tests/、scripts/），加在一起和基线比：
总数没变多就通过，所以老代码不用一次改完，碰到时顺手补；把代码搬到新文件也不会被误拦。

用法（仓库根目录）：
    python scripts/check_silent_excepts.py <基线提交>
退出码：0 通过，1 有文件变多。
"""

from __future__ import annotations

import json
import subprocess
import sys

RULES = "S110,S112"
SKIP_PREFIXES = ("tests/", "scripts/", "ui/", "src-tauri/", "docs/")


def _merge_base(base: str) -> str:
    return subprocess.run(
        ["git", "merge-base", base, "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _changed_python_files(base: str) -> list[str]:
    # 和工作区比，本地没提交的改动也算进去。
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AMR", base, "--", "*.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [path for path in out.splitlines() if path and not path.startswith(SKIP_PREFIXES)]


def _findings(path: str, source: str) -> list[dict]:
    result = subprocess.run(
        [
            sys.executable, "-m", "ruff", "check", "--select", RULES,
            "--output-format", "json", "--stdin-filename", path, "-",
        ],
        input=source,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise SystemExit(f"ruff 运行失败：{path}\n{result.stderr}")
    return json.loads(result.stdout or "[]")


def _base_source(base: str, path: str) -> str:
    result = subprocess.run(["git", "show", f"{base}:{path}"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    base = _merge_base(argv[1])
    # 比的是这批文件的总数：把代码从一个文件搬到新文件时，带过去的老写法不算新增。
    total_now = total_before = 0
    report: list[str] = []
    for path in _changed_python_files(base):
        with open(path, encoding="utf-8") as handle:
            now = _findings(path, handle.read())
        before = _findings(path, _base_source(base, path))
        total_now += len(now)
        total_before += len(before)
        if len(now) > len(before):
            report.append(f"{path}：{len(before)} 处 → {len(now)} 处")
            report.extend(f"  第 {item['location']['row']} 行 {item['code']}" for item in now)
    if total_now > total_before:
        print(f"改过的文件里，出错后什么都不做的地方从 {total_before} 处变成 {total_now} 处：")
        print("\n".join(report))
        print(
            "\n兜住错误时至少记一行日志（logger.debug 也行）；"
            "确实可以忽略的，在那一行写 `# noqa: S110 <原因>`。"
        )
        return 1
    print(f"通过：改过的文件里「出错后什么都不做」{total_before} → {total_now} 处，没有变多。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
