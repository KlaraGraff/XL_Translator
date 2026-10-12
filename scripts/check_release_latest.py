"""发布或撤回之后，确认「对外的最新版」真的变成了预期的样子。

已安装的应用通过两处得知有没有新版本（见 core/update_checker.py 和
src-tauri/tauri.conf.json 的 updater.endpoints）：

- GitHub API 的 ``releases/latest``：决定「更新与关于」页提示的最新版本号；
- ``releases/latest/download/latest.json``：应用内更新真正下载的清单。

两者都由 GitHub 按「最新的非草稿正式版」自动推导，工作流改不了，只能读回核对。
GitHub 对这两个地址都有缓存，刚改完可能还是旧值，所以会重试一段时间。

用法：
  发布后：  --expect v10.0.1      两处都必须指向 10.0.1（临时签名包不带
                                  latest.json，加 --no-manifest 只核对前者）
  撤回后：  --not v10.0.1         两处都不能再指向 10.0.1，且 latest.json
                                  （若存在）必须与 releases/latest 是同一个版本
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = os.environ.get("GITHUB_REPOSITORY", "KlaraGraff/XL_Translator")
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
MANIFEST_LATEST = f"https://github.com/{REPO}/releases/latest/download/latest.json"


def _get_json(url: str, *, token: str | None = None) -> dict | None:
    """读一个 JSON 地址；404 返回 None（例如没有任何正式版、或该版没有 latest.json）。"""
    request = urllib.request.Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    if token and url.startswith("https://api.github.com/"):
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def evaluate(
    latest_tag: str | None,
    manifest_version: str | None,
    *,
    expect: str | None = None,
    not_tag: str | None = None,
    expect_manifest: bool = True,
) -> list[str]:
    """按读到的两处结果给出问题清单；空清单表示符合预期。"""
    problems: list[str] = []
    if expect:
        if latest_tag != expect:
            problems.append(f"releases/latest 是 {latest_tag or '（无）'}，预期 {expect}。")
        if expect_manifest and manifest_version != expect.removeprefix("v"):
            problems.append(f"latest.json 的版本是 {manifest_version or '（无）'}，预期 {expect.removeprefix('v')}。")
    if not_tag:
        if latest_tag == not_tag:
            problems.append(f"releases/latest 仍然是 {not_tag}。")
        if manifest_version == not_tag.removeprefix("v"):
            problems.append(f"latest.json 仍然是 {manifest_version}，已安装的应用还会收到这个版本的更新。")
        if manifest_version is not None and latest_tag and manifest_version != latest_tag.removeprefix("v"):
            problems.append(f"latest.json 的版本 {manifest_version} 与 releases/latest {latest_tag} 不一致。")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--expect", help="发布后：最新版必须是这个标签")
    group.add_argument("--not", dest="not_tag", help="撤回后：最新版不能再是这个标签")
    parser.add_argument(
        "--no-manifest",
        action="store_true",
        help="发布的是临时签名包，本来就不带 latest.json，发布后不核对它",
    )
    parser.add_argument("--attempts", type=int, default=12)
    parser.add_argument("--interval", type=float, default=15.0)
    args = parser.parse_args(argv)

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    problems: list[str] = []
    for attempt in range(1, args.attempts + 1):
        latest = _get_json(API_LATEST, token=token)
        manifest = _get_json(MANIFEST_LATEST)
        latest_tag = latest.get("tag_name") if latest else None
        manifest_version = str(manifest.get("version")) if manifest else None
        problems = evaluate(
            latest_tag,
            manifest_version,
            expect=args.expect,
            not_tag=args.not_tag,
            expect_manifest=not args.no_manifest,
        )
        if not problems:
            print(f"releases/latest = {latest_tag or '（无）'}；latest.json = {manifest_version or '（无）'}")
            return 0
        if attempt < args.attempts:
            print(f"第 {attempt} 次读取还不符合预期，{args.interval:.0f} 秒后重试：{' '.join(problems)}")
            time.sleep(args.interval)
    for problem in problems:
        print(f"- {problem}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
