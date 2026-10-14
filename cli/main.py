"""``translator-sidecar types | scan | translate | status | stop | mcp | adapter``.

Exit codes: 0 finished (some files may have failed), 1 nothing came out
(all files failed, or the task stopped or errored), 2 bad arguments or input
the engine refused, 3 no engine could be reached or started, 4 the engine
asks for a confirmation (``--yes`` gives it).  Results go to stdout,
diagnostics to stderr; ``--json`` prints one object, ``--json-lines`` one
event per line.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cli import flow
from cli.flow import CliError, NeedsConfirmation
from core.engine_client import EngineClient, EngineError, EngineUnavailable, connect

#: Subcommands; ``scripts/launch_sidecar.py`` hands these to ``main``.
COMMANDS = ("types", "scan", "translate", "status", "stop", "mcp", "adapter")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return flow.EXIT_INPUT
    out = Output(json_mode="lines" if getattr(args, "json_lines", False) else "object" if args.json else "")
    try:
        return int(args.handler(args, out))
    except NeedsConfirmation as exc:
        out.error(exc.exit_code, exc.message, exc.as_dict(), hint="确认后可加 --yes 重新运行。")
        return exc.exit_code
    except CliError as exc:
        out.error(exc.exit_code, exc.message)
        return exc.exit_code
    except EngineUnavailable as exc:
        out.error(flow.EXIT_ENGINE, str(exc))
        return flow.EXIT_ENGINE
    except EngineError as exc:
        out.error(flow.EXIT_INPUT, exc.detail, {"status": exc.status, "reason": exc.reason})
        return flow.EXIT_INPUT


# ── output ───────────────────────────────────────────────────────────────


class Output:
    """Where everything a command says goes.

    ``json_mode`` "" prints sentences, "object" one JSON object at the end,
    "lines" one JSON object per line as things happen.
    """

    def __init__(self, *, json_mode: str = "") -> None:
        self.json_mode = json_mode

    @property
    def human(self) -> bool:
        return not self.json_mode

    def line(self, text: str = "") -> None:
        if self.human:
            print(text, flush=True)

    def note(self, text: str) -> None:
        if text:
            print(text, file=sys.stderr, flush=True)

    def emit(self, payload: dict[str, Any]) -> None:
        """One JSON line (``--json-lines``)."""
        if self.json_mode == "lines":
            print(_json(payload), flush=True)

    def result(self, payload: dict[str, Any]) -> None:
        """The command's answer: the object (``--json``) or the last line (``--json-lines``)."""
        if self.json_mode == "object":
            print(_json(payload), flush=True)
        elif self.json_mode == "lines":
            print(_json({"event": "result", **payload}), flush=True)

    def error(self, code: int, message: str, extra: dict[str, Any] | None = None, *, hint: str = "") -> None:
        self.note(f"错误：{message}" + (f"\n{hint}" if hint else ""))
        if self.json_mode:
            body = {"error": {"exit_code": code, "message": message, **(extra or {})}}
            print(_json({"event": "error", **body} if self.json_mode == "lines" else body), flush=True)


def _json(payload: Any) -> str:
    # A pipe on a non-UTF-8 console (cp1252) cannot carry Chinese text;
    # escaped JSON parses the same everywhere.
    utf8 = str(getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") == "utf8"
    return json.dumps(payload, ensure_ascii=not utf8, default=str)


# ── commands ─────────────────────────────────────────────────────────────


def _types(args: argparse.Namespace, out: Output) -> int:
    with _connect() as client:
        types = flow.file_types(client.surfaces())
    out.result({"types": [flow.type_payload(item) for item in types]})
    for item in types:
        extra = "" if item.get("builtin", True) else "（扩展）"
        extensions = " ".join(f".{suffix}" for suffix in item.get("extensions") or ())
        out.line(f"{item['key']:<14}{flow.label_of(item)}{extra}  {extensions}")
        for option in item.get("options") or ():
            out.line(f"{'':<16}--option {option.get('key')}=…  {flow.label_of({'label': option.get('label')})}")
    return flow.EXIT_OK


def _scan(args: argparse.Namespace, out: Output) -> int:
    flow.resolve_paths(args.paths)
    with _connect() as client:
        detection = flow.detect(client.surfaces(), args.paths, type_key=args.type)
        out.note(detection.note)
        surface, paths = flow.scan_request(detection)
        scanned = client.scan(surface, paths, apply_saved_defaults=True)
    items = [item for item in scanned.get("items") or () if isinstance(item, dict)]
    skipped = [item for item in scanned.get("skipped") or () if isinstance(item, dict)]
    out.result(
        {
            "type": detection.key,
            "note": detection.note,
            "items": items,
            "skipped": skipped,
            "summary": scanned.get("summary") or {},
        }
    )
    out.line(f"类型：{flow.label_of(detection.descriptor)}（{detection.key}），可翻译 {len(items)} 个文件")
    for item in items:
        out.line(f"  {item.get('relative_path') or item.get('name') or item.get('path')}")
    if skipped:
        out.line(f"跳过 {len(skipped)} 个：")
        for item in skipped:
            name = item.get("relative_path") or item.get("name") or item.get("path") or ""
            out.line(f"  {name}：{item.get('reason') or item.get('message') or ''}".rstrip("："))
    return flow.EXIT_OK if items else flow.EXIT_INPUT


def _translate(args: argparse.Namespace, out: Output) -> int:
    flow.resolve_paths(args.paths)
    option_values = flow.parse_option_pairs(args.option or [])
    client = _connect()
    try:
        detection = flow.detect(client.surfaces(), args.paths, type_key=args.type)
        out.note(detection.note)
        request = flow.build_request(
            detection,
            target_lang=args.to,
            source_lang=args.source,
            options=option_values,
        )
        if args.dry_run:
            return _dry_run(client, detection, request, out)
        started = flow.start_task(client, request, accept_all=args.yes)
        wait = args.wait
        if client.started_here and not wait:
            # The engine started for this command ends with it, and the task with it.
            out.note("Translator 未打开：翻译引擎随本命令运行，已改为等待任务完成。")
            wait = True
        head = {
            "task_id": started.task_id,
            "type": detection.key,
            "note": detection.note,
            "request": started.request,
            "options": started.preflight.get("options") or {},
            "accepted": started.accepted,
        }
        out.emit({"event": "started", **head})
        out.line(f"已开始「{flow.label_of(detection.descriptor)}」任务 {started.task_id}。")
        if not client.started_here:
            out.line("可在应用的「任务中心」查看进度、停止或复核。")
        if not wait:
            out.result({**head, "state": started.task.get("state")})
            out.line(f"查看进度：status {started.task_id}")
            return flow.EXIT_OK
        try:
            _follow(client, started.task_id, out)
        except KeyboardInterrupt:
            if client.started_here:
                out.note("已中断：任务随翻译引擎停止。")
            else:
                out.note(f"已停止等待；任务仍在应用中运行，可用 stop {started.task_id} 停止。")
            return 130
        summary = flow.summarize(client.results(started.task_id))
        out.result({**head, "result": summary})
        _print_summary(out, summary)
        return flow.exit_code_for(summary)
    finally:
        client.close()


def _dry_run(client: EngineClient, detection: flow.Detection, request: dict[str, Any], out: Output) -> int:
    report = flow.dry_run(client, request)
    report = {"type": detection.key, "note": detection.note, **report}
    # A dry run is read by tools first: JSON unless --json-lines was asked for.
    if out.json_mode == "lines":
        out.result(report)
    else:
        print(_json(report), flush=True)
    if report.get("problem"):
        out.note(f"错误：{report['problem']['message']}")
        return flow.EXIT_INPUT
    if report.get("confirmation"):
        out.note(f"需要确认：{report['confirmation']['message']}")
        return flow.EXIT_CONFIRM
    return flow.EXIT_OK


def _follow(client: EngineClient, task_id: str, out: Output) -> None:
    printed: dict[str, Any] = {}
    for event in client.events(task_id):
        out.emit({"event": event.type, "id": event.id, "data": event.data})
        if out.human:
            text = flow.progress_line(event, printed)
            if text:
                out.line(text)


def _print_summary(out: Output, summary: dict[str, Any]) -> None:
    words = {
        "done": "已完成",
        "completed_with_issues": "已完成",
        "error": "出错",
        "stopped": "已停止",
        "interrupted": "已中断",
    }
    parts = [words.get(summary["state"], summary["state"])]
    if summary["files"]:
        parts.append(f"生成 {summary['generated_count']} / {len(summary['files'])} 个文件")
    if summary.get("review_count"):
        parts.append(f"需复核 {summary['review_count']} 处")
    out.line("，".join(parts) + "。")
    if summary.get("message") and summary["state"] not in {"done", "completed_with_issues"}:
        out.line(summary["message"])
    if summary["output_dir"]:
        out.line(f"输出目录：{summary['output_dir']}")
    for item in summary["files"]:
        if item["output"]:
            out.line(f"  {item['output']}")
    if summary.get("report_path"):
        out.line(f"报告：{summary['report_path']}")


def _status(args: argparse.Namespace, out: Output) -> int:
    with _connect() as client:
        if args.task_id:
            summary = flow.task_status(client.task(args.task_id), logs=args.logs)
            progress, logs = summary["progress"], summary["logs"]
            out.result(summary)
            percent = progress.get("overall_percent")
            phase = progress.get("phase_name") or ""
            out.line(
                f"{summary['task_id']}  {summary['surface']}  {summary['state']}"
                + (f"  {percent}%" if percent is not None and not summary["terminal"] else "")
                + (f"  {phase}" if phase and not summary["terminal"] else "")
            )
            for message in logs:
                out.line(f"  {message}")
            if summary["terminal"]:
                _print_summary(out, summary)
            return flow.EXIT_OK
        listing = client.tasks()
    active = [_brief(record) for record in listing.get("active") or () if isinstance(record, dict)]
    recent = [
        _brief(record) for record in listing.get("recent") or () if isinstance(record, dict) and record.get("terminal")
    ][: args.limit]
    out.result({"active": active, "recent": recent})
    out.line("进行中：" if active else "没有进行中的任务。")
    for item in active:
        percent = item["percent"]
        out.line(f"  {item['task_id']}  {item['surface']}  {item['state']}" + (f"  {percent}%" if percent is not None else "") + f"  {item['label']}")
    if recent:
        out.line("最近完成：")
        for item in recent:
            out.line(f"  {item['task_id']}  {item['surface']}  {item['state']}  {item['label']}")
    return flow.EXIT_OK


def _brief(record: dict[str, Any]) -> dict[str, Any]:
    progress = record.get("progress") if isinstance(record.get("progress"), dict) else {}
    return {
        "task_id": str(record.get("task_id") or ""),
        "surface": str(record.get("surface") or ""),
        "state": str(record.get("state") or ""),
        "terminal": bool(record.get("terminal")),
        "percent": progress.get("overall_percent"),
        "label": str(record.get("source_label") or ""),
        "updated_at": record.get("updated_at"),
    }


def _stop(args: argparse.Namespace, out: Output) -> int:
    try:
        client = _connect(spawn=False)
    except EngineUnavailable:
        raise EngineUnavailable("翻译引擎未在运行，没有可停止的任务。") from None
    with client:
        status = client.stop(args.task_id)
    out.result({"task_id": args.task_id, "state": status.get("state")})
    out.line(f"已请求停止任务 {args.task_id}（当前状态：{status.get('state')}）。")
    return flow.EXIT_OK


def _mcp(args: argparse.Namespace, out: Output) -> int:
    from cli.mcp_server import serve

    return serve()


# ── adapter: make, test, install and pack extensions ─────────────────────


def _adapter_new(args: argparse.Namespace, out: Output) -> int:
    from cli.adapter_commands import AdapterCommandError, new_adapter

    target = Path(args.dir).expanduser().resolve() if args.dir else Path.cwd() / args.id
    try:
        files = new_adapter(args.id, target)
    except AdapterCommandError as exc:
        raise CliError(str(exc)) from None
    out.result({"id": args.id, "directory": str(target), "files": files})
    out.line(f"已在 {target} 生成扩展「{args.id}」（{len(files)} 个文件）。")
    out.line("下一步：按 README.md 修改 adapter.json 和 adapter.py，再运行 adapter test 自测。")
    return flow.EXIT_OK


def _adapter_test(args: argparse.Namespace, out: Output) -> int:
    from cli.adapter_commands import check_adapter
    from core.adapter_install import AdapterInstallError, unpacked

    source = Path(args.dir).expanduser().resolve()
    if not source.exists():
        raise CliError(f"找不到：{source}")
    try:
        with unpacked(source) as directory:
            report = check_adapter(directory, allow_reserved_id=args.skip_id_check)
    except AdapterInstallError as exc:
        raise CliError(str(exc)) from None
    report.directory = str(source)
    payload = report.to_dict()
    out.result(payload)
    if report.manifest is not None:
        manifest = report.manifest
        suffixes = " ".join(f".{suffix}" for suffix in manifest.extensions)
        out.line(f"扩展：{manifest.label.get('zh') or manifest.id}（{manifest.id}）{manifest.version}，处理 {suffixes}")
        out.line(f"文件：{len(report.files)} 个，样例 {len(report.samples)} 个，合计 {_size(report.size)}")
    for run in report.runs:
        modes = "、".join(
            f"{'仅译文' if mode == 'translation_only' else '原文 + 译文'}{'通过' if state == 'ok' else '未通过'}"
            for mode, state in run.modes.items()
        )
        locked = f"（其中 {run.locked} 段不翻译）" if run.locked else ""
        out.line(f"  {run.file}：取出 {run.units} 段{locked}，{modes or '未能写回'}，用时 {run.seconds:.1f} 秒")
    for problem in report.problems:
        out.line(f"问题：{problem}")
    for warning in report.warnings:
        out.line(f"提醒：{warning}")
    out.line("结果：通过。" if report.ok else f"结果：未通过（{len(report.problems)} 个问题）。")
    return flow.EXIT_OK if report.ok else flow.EXIT_FAILED


def _adapter_install(args: argparse.Namespace, out: Output) -> int:
    from core.adapter_install import CONFIRM_NOTICE

    source = Path(args.source).expanduser().resolve()
    if not source.exists():
        raise CliError(f"找不到：{source}")
    with _connect() as client:
        try:
            summary = client.extension_preview(str(source))
        except EngineError as exc:
            problems = (exc.payload or {}).get("problems") if isinstance(exc.payload, dict) else None
            for problem in problems or [exc.detail]:
                out.note(f"问题：{problem}")
            raise CliError("检查未通过，未安装。") from None
        actions = {"install": "添加", "upgrade": "升级", "downgrade": "降级", "reinstall": "重新安装"}
        label = (summary.get("label") or {}).get("zh") or summary.get("id")
        version = summary.get("version")
        installed = summary.get("installed_version")
        out.line(
            f"将{actions.get(summary.get('action'), '添加')}扩展「{label}」（{summary.get('key')}）{version}"
            + (f"，已安装 {installed}" if installed and summary.get("action") != "install" else "")
        )
        suffixes = " ".join(f".{suffix}" for suffix in summary.get("extensions") or ())
        out.line(f"处理 {suffixes}；许可证：{summary.get('license') or '未注明'}；{summary.get('file_count')} 个文件，{_size(int(summary.get('size') or 0))}")
        for warning in summary.get("warnings") or ():
            out.line(f"提醒：{warning}")
        out.line(CONFIRM_NOTICE)
        if not args.yes:
            raise NeedsConfirmation("安装扩展前需要确认来源可信。", reason="extension_unconfirmed", accept={"yes": True})
        client.extension_install(str(source), str(summary.get("sha256") or ""))
        progress = _wait_extension_install(client)
        if progress.get("phase") != "done":
            raise CliError(str(progress.get("error") or "扩展安装失败。"))
        record = progress.get("result") or {}
        out.result({"preview": summary, "installed": True, "record": record})
        out.line(f"已安装「{label}」{version}。")
        out.line("Translator 下次启动时会显示这个类型。" if client.started_here else "已出现在 Translator 的侧栏，可在「扩展」页调整。")
    return flow.EXIT_OK


def _wait_extension_install(client: EngineClient, timeout: float = 300.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        progress = client.extension_install_progress()
        if not progress.get("running") or time.monotonic() > deadline:
            return progress
        time.sleep(0.1)


def _adapter_uninstall(args: argparse.Namespace, out: Output) -> int:
    adapter_id = str(args.id or "").strip().lower()
    with _connect() as client:
        result = client.extension_uninstall(adapter_id)
    record = result.get("uninstalled") or {}
    label = (record.get("label") or {}).get("zh") or adapter_id
    out.result({"uninstalled": record})
    out.line(f"已卸载「{label}」（ext:{adapter_id}），它的设置保留。")
    return flow.EXIT_OK


def _adapter_pack(args: argparse.Namespace, out: Output) -> int:
    from cli.adapter_commands import AdapterCommandError, pack_adapter

    directory = Path(args.dir).expanduser().resolve()
    try:
        result = pack_adapter(directory, out=args.out or "", tag=args.tag, repo=args.repo, author=args.author)
    except AdapterCommandError as exc:
        raise CliError(str(exc)) from None
    out.result(result)
    out.line(f"已打包：{result['zip']}（{len(result['files'])} 个文件）")
    out.line(f"SHA-256：{result['entry']['sha256']}")
    out.line(f"扩展列表的条目：{result['entry_file']}")
    if not result["entry"]["download"]:
        out.line("提醒：adapter.json 的 repo 不是 GitHub 地址，条目里的 download 需要手动填写。")
    if not result["entry"]["summary"]:
        out.line("提醒：adapter.json 没有 description，条目里的 summary 需要手动填写（中、英、法三种语言）。")
    return flow.EXIT_OK


def _size(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB" if size >= 1024 * 1024 else f"{max(1, round(size / 1024))} KB"


def _connect(*, spawn: bool = True) -> EngineClient:
    return connect(spawn_if_needed=spawn)


# ── arguments ────────────────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="translator-sidecar",
        description="用 Translator 翻译文件。Translator 开着时任务进入它的任务中心；没开时在后台启动翻译引擎，命令结束后退出。",
    )
    sub = parser.add_subparsers(dest="command", metavar="命令")

    def command(name: str, handler: Callable[..., int], help_text: str) -> argparse.ArgumentParser:
        item = sub.add_parser(name, help=help_text, description=help_text)
        item.set_defaults(handler=handler)
        item.add_argument("--json", action="store_true", help="输出一个 JSON 对象")
        return item

    command("types", _types, "列出可翻译的文件类型")

    scan = command("scan", _scan, "列出给定路径里会被翻译的文件")
    scan.add_argument("paths", nargs="+", metavar="路径", help="文件或文件夹；多个文件须在同一文件夹")
    scan.add_argument("--type", help="指定类型（见 types）；默认按扩展名判断")

    translate = command("translate", _translate, "翻译文件或文件夹")
    translate.add_argument("paths", nargs="+", metavar="路径", help="文件或文件夹；多个文件须在同一文件夹")
    translate.add_argument("--type", help="指定类型（见 types）；默认按扩展名判断")
    translate.add_argument("--to", metavar="语言", help="目标语言代码，如 fr、en、zh；默认用应用里保存的")
    translate.add_argument("--from", dest="source", metavar="语言", help="源语言代码或 auto；默认用应用里保存的")
    translate.add_argument(
        "--option",
        action="append",
        metavar="名称=值",
        help="任务选项，可重复；未给的选项与应用里的设置一致",
    )
    translate.add_argument("--wait", action="store_true", help="等任务结束，期间打印进度")
    translate.add_argument("--yes", action="store_true", help="遇到需要确认的提示时一律继续")
    translate.add_argument(
        "--dry-run", action="store_true", help="只检查不翻译：列出会翻译的文件、实际选项和需要确认的事项（JSON）"
    )
    translate.add_argument("--json-lines", action="store_true", help="每行输出一个 JSON 事件（配合 --wait）")

    status = command("status", _status, "查看任务；不给任务号时列出进行中和最近的任务")
    status.add_argument("task_id", nargs="?", metavar="任务号")
    status.add_argument("--limit", type=int, default=10, help="最近任务最多列几条（默认 10）")
    status.add_argument("--logs", type=int, default=5, help="显示最近几条运行日志（默认 5）")

    stop = command("stop", _stop, "停止一个正在进行的任务")
    stop.add_argument("task_id", metavar="任务号")

    mcp_help = "作为 MCP 服务器运行（stdio），供智能体调用；工具与以上命令一一对应"
    sub.add_parser("mcp", help=mcp_help, description=mcp_help).set_defaults(handler=_mcp, json=False)

    adapter_help = "制作、自测、安装和打包扩展（新的文件类型），见模板仓库 KlaraGraff/XL_Translator-extension-template"
    adapter = sub.add_parser("adapter", help=adapter_help, description=adapter_help)
    adapter.set_defaults(handler=lambda args, out: _usage(adapter), json=False)
    actions = adapter.add_subparsers(dest="adapter_command", metavar="操作")

    def action(name: str, handler: Callable[..., int], help_text: str) -> argparse.ArgumentParser:
        item = actions.add_parser(name, help=help_text, description=help_text)
        item.set_defaults(handler=handler)
        item.add_argument("--json", action="store_true", help="输出一个 JSON 对象")
        return item

    new = action("new", _adapter_new, "从模板生成一个能用的扩展")
    new.add_argument("id", help="扩展的 id：小写字母、数字、下划线和连字符")
    new.add_argument("--dir", help="生成到哪个文件夹（默认当前目录下的 <id>）")

    test = action("test", _adapter_test, "检查扩展（文件夹或 zip）并用 samples/ 里的样例试运行；全部通过时退出码为 0")
    test.add_argument("dir", nargs="?", default=".", metavar="文件夹或zip", help="扩展文件夹或 zip，默认当前目录")
    test.add_argument("--skip-id-check", action="store_true", help="不检查 id 是否与应用自带的类型相同（检查自带类型时用）")

    install = action("install", _adapter_install, "把扩展（文件夹或 zip）装进 Translator")
    install.add_argument("source", metavar="文件夹或zip")
    install.add_argument("--yes", action="store_true", help="确认来源可信，直接安装")

    uninstall = action("uninstall", _adapter_uninstall, "卸载一个已安装的扩展（它的设置保留）")
    uninstall.add_argument("id", help="扩展的 id")

    pack = action("pack", _adapter_pack, "打出 zip、SHA-256 和扩展列表的条目（在 <文件夹>/dist 下）")
    pack.add_argument("dir", nargs="?", default=".", metavar="文件夹", help="扩展文件夹，默认当前目录")
    pack.add_argument("--out", help="输出目录，默认 <文件夹>/dist")
    pack.add_argument("--tag", default="", help="发布标签，如 v0.1.0；须与 adapter.json 的 version 一致")
    pack.add_argument("--repo", default="", help="项目主页，默认取 adapter.json 的 repo")
    pack.add_argument("--author", default="", help="作者，默认取 GitHub 账号名")
    return parser


def _usage(parser: argparse.ArgumentParser) -> int:
    parser.print_help(sys.stderr)
    return flow.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
