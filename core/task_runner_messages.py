"""Messages a task runner puts on its queue, and the stop signal it raises.

Every runner (Excel, Word, PDF, CAD, TM cleaning) speaks this vocabulary;
``api.task_manager`` turns each message into a task event.  They used to live
in ``core.task_runner`` and are still re-exported from there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ProgressMsg:
    """全局进度消息。"""
    phase_index:  int      # 当前阶段序号（1/2/3/4）
    phase_total:  int      # 总阶段数（3）
    phase_name:   str      # 阶段名称（如"全局扫描"、"云端翻译"、"生成文件"）
    step_done:    int      # 当前阶段已完成步数
    step_total:   int      # 当前阶段总步数


@dataclass
class StatusMsg:
    phase_desc: str


@dataclass
class WordRecoveryStatusMsg:
    """Word recovery summary for the non-scrolling execution monitor."""
    retry_round: int = 0
    retry_total: int = 0
    retry_processing_count: int = 0
    retry_recovered_count: int = 0
    retry_unresolved_count: int = 0
    semantic_processing_count: int = 0
    semantic_checked_count: int = 0
    semantic_accepted_count: int = 0
    semantic_uncertain_count: int = 0


@dataclass
class PdfReviewStatusMsg:
    """PDF page-review summary for the non-scrolling execution monitor."""
    enabled: bool = False
    review_round: int = 0
    review_total: int = 0
    review_processing_count: int = 0
    review_passed_count: int = 0
    review_failed_count: int = 0


@dataclass
class PdfPageRecoveryStatusMsg:
    """PDF page retry/recovery summary for the non-scrolling execution monitor."""
    total_pages: int = 0
    completed_pages: int = 0
    submitted_page_count: int = 0
    pending_submitted_page_count: int = 0
    retrying_page_count: int = 0
    retried_page_count: int = 0
    recovered_page_count: int = 0
    placeholder_page_count: int = 0
    # 按「跳过 A3 及更大的页面」原样保留的页。它们算在 completed_pages 里（跑完了），
    # 但没有生成译文，界面的「已生成 N 页」要把它们扣掉。
    skipped_oversize_page_count: int = 0


@dataclass
class LogMsg:
    level:   str           # INFO / OK / WARN / ERROR
    message: str
    ts:      str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    visible: bool = True
    # 这个任务嵌在别的任务里跑（PDF「转成 Word」）时，外层也要转给用户看的说明。
    relay:   bool = False


@dataclass
class DoneMsg:
    output_dir:   str
    file_results: list[dict]
    elapsed_sec:  float
    tm_hit_count: int
    api_call_count: int
    issues: list[dict] = field(default_factory=list)
    report_path: str = ""
    manifest_path: str = ""
    report_warning: str = ""
    files: list[dict] = field(default_factory=list)
    kpi: dict[str, object] = field(default_factory=dict)
    recovery: dict[str, object] = field(default_factory=dict)
    review: dict[str, object] = field(default_factory=dict)
    # Runtime failover history: who took over mid-run, and why.
    connections: dict[str, object] = field(default_factory=dict)
    language: dict[str, object] = field(default_factory=dict)
    error: dict[str, object] = field(default_factory=dict)
    # 记忆库词条在本次任务里的采用情况（core.term_session.TaskTermSession.finish）。
    terms: dict[str, object] = field(default_factory=dict)
    # 用户按了停止、但任务还是正常跑完时留下的痕迹（requested / truncated）。没有它，
    # 小结会写「已完成 · 全部通过」，用户看不出自己那一下停止到底起没起作用。
    stop: dict[str, object] = field(default_factory=dict)
    translated_dir: str = ""
    review_dir: str = ""
    summary_path: str = ""
    checkpoint_path: str = ""


@dataclass
class ErrorMsg:
    message: str
    output_dir: str = ""
    report_path: str = ""
    report_warning: str = ""
    manifest_path: str = ""
    files: list[dict] = field(default_factory=list)
    kpi: dict[str, object] = field(default_factory=dict)
    recovery: dict[str, object] = field(default_factory=dict)
    review: dict[str, object] = field(default_factory=dict)
    # Runtime failover history: who took over mid-run, and why.
    connections: dict[str, object] = field(default_factory=dict)
    language: dict[str, object] = field(default_factory=dict)
    error: dict[str, object] = field(default_factory=dict)
    # 记忆库词条在本次任务里的采用情况（core.term_session.TaskTermSession.finish）。
    terms: dict[str, object] = field(default_factory=dict)
    translated_dir: str = ""
    review_dir: str = ""
    summary_path: str = ""
    checkpoint_path: str = ""


@dataclass
class StoppedMsg:
    message: str
    output_dir: str = ""
    report_path: str = ""
    report_warning: str = ""
    manifest_path: str = ""
    files: list[dict] = field(default_factory=list)
    kpi: dict[str, object] = field(default_factory=dict)
    recovery: dict[str, object] = field(default_factory=dict)
    review: dict[str, object] = field(default_factory=dict)
    # Runtime failover history: who took over mid-run, and why.
    connections: dict[str, object] = field(default_factory=dict)
    language: dict[str, object] = field(default_factory=dict)
    error: dict[str, object] = field(default_factory=dict)
    # 记忆库词条在本次任务里的采用情况（core.term_session.TaskTermSession.finish）。
    terms: dict[str, object] = field(default_factory=dict)
    translated_dir: str = ""
    review_dir: str = ""
    summary_path: str = ""
    checkpoint_path: str = ""
    # DoneMsg 一直带着 issues（残留中文待复核等 quality_issues），StoppedMsg 之前没有——
    # 停止收尾那条分支只是没把这个字段接上，不是设计上认为"停止就不该带 issues"。
    # 结果是任务真被停止时，正在处理的这份文件如果恰好检出了残留中文/待复核项，
    # 这条信息会在收尾时直接被丢掉，前端和任务中心都看不到（详见下面 StoppedMsg(...)
    # 构造处的说明）。补上这个字段，前端 workspace.ts 的 openIssues 统计（读
    # result.issues，不挑 task.state）才吃得到停止路径下的 quality_issues。
    # 新增字段带默认值，旧任务记录/旧消息反序列化时缺这个键按空列表处理，不影响
    # 旧数据可读。
    issues: list[dict] = field(default_factory=list)


class TaskStopped(Exception):
    """后台任务收到停止信号时抛出，用于统一收尾。"""
