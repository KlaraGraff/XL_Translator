// 反馈页和「反馈此任务」弹窗共用的部分：发送与进度、邮箱检查、只问一次的邮箱确认、
// 勾选原文件 / 译文时的隐私说明。两处说法和行为必须一致，所以放在一起。

import type { ApiClient } from "./api-client";
import { createProgressBar, openModal } from "./components";
import { icon } from "./icons";
import { t } from "./i18n";
import "./views/feedback.css";

export interface FeedbackSendStatus {
  job_id?: string;
  state: "uploading" | "done" | "error";
  sent: number;
  total: number;
  id: string;
  message: string;
  reason: string;
}

/** 这些失败显示成「未发出」横幅（可改用 ZIP）；其余是输入问题，只提示。 */
export const SEND_FAILURES = ["network", "server", "rate_limited", "not_configured", "job_missing"];

/** 空着可以；填了就得像个邮箱，不然没法通知到。 */
export function emailLooksValid(value: string): boolean {
  const text = value.trim();
  return !text || /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(text);
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1).replace(/\.0$/, "")} MB`;
}

/**
 * 发出一条反馈：后端打好包就返回，上传在后台进行；这里按真实已发出的字节回调进度，
 * 直到成功或失败。打包阶段的输入错误（内容为空、附件过大……）照常抛 ApiError。
 */
export async function uploadFeedback(
  client: ApiClient,
  body: Record<string, unknown>,
  onProgress: (progress: { sent: number; total: number }) => void,
): Promise<FeedbackSendStatus> {
  const started = await client.request<FeedbackSendStatus>("/api/feedback/send", {
    method: "POST",
    body: JSON.stringify(body),
  });
  let status = started;
  while (status.state === "uploading") {
    onProgress({ sent: status.sent, total: status.total });
    await new Promise((resolve) => window.setTimeout(resolve, 300));
    status = await client.request<FeedbackSendStatus>(`/api/feedback/send/${encodeURIComponent(started.job_id ?? "")}`);
  }
  return status;
}

/** 上传进度：一行字加一根进度条。null 表示还在打包，拿不到字节数。 */
export function createSendProgress(): { root: HTMLElement; update: (progress: { sent: number; total: number } | null) => void } {
  const root = document.createElement("div");
  root.className = "feedback-progress";
  const label = document.createElement("div");
  label.className = "feedback-progress-label";
  const bar = createProgressBar({ percent: 0 });
  root.append(label, bar.root);
  const update = (progress: { sent: number; total: number } | null): void => {
    // 打包和等服务器确认这两段拿不到字节数，用来回滑动的短条，不报百分比。
    if (!progress) {
      label.textContent = t("正在打包…");
      bar.root.classList.add("indet");
      return;
    }
    bar.root.classList.remove("indet");
    if (progress.total > 0 && progress.sent >= progress.total) {
      label.textContent = t("已上传 {0}，正在等待服务器确认…", formatBytes(progress.total));
      bar.setPercent(100);
      return;
    }
    const percent = progress.total > 0 ? Math.floor((progress.sent / progress.total) * 100) : 0;
    label.textContent = t("正在上传 {0} / {1}（{2}%）", formatBytes(progress.sent), formatBytes(progress.total), String(percent));
    bar.setPercent(percent);
  };
  update(null);
  return { root, update };
}

/**
 * 没填邮箱就发送时问一次。只问到用户第一次不留邮箱发出为止（后端记下这个选择，
 * 反馈上下文的 ask_email 随之变成 false），之后不再打扰。
 */
export function confirmSendWithoutEmail(): Promise<"fill" | "send"> {
  return new Promise((resolve) => {
    openModal({
      tone: "tint",
      icon: "message",
      title: t("是否留下邮箱？"),
      body: [t("留下邮箱后，问题处理完成或功能上线时，我们会发邮件通知您。不留邮箱也可以发送，此提示只出现这一次。")],
      actions: [
        { label: t("直接发送"), onClick: () => resolve("send") },
        { label: t("填写邮箱"), variant: "primary", onClick: () => resolve("fill") },
      ],
    });
  });
}

/** 勾了原文件或译文才出现：用户最担心的是自己的文件，就在勾选的地方说清楚怎么处理。 */
export function createPrivacyNote(): HTMLElement {
  const privacy = document.createElement("p");
  privacy.className = "feedback-privacy";
  privacy.append(icon("lock", { size: "sm" }), document.createTextNode(
    t("原文件和译文仅用于定位和修复问题，绝不会公开或泄露；问题处理完毕后立即删除，不再保存。"),
  ));
  return privacy;
}
