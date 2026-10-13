// 「反馈此任务」弹窗：任务中心详情和翻译结果横幅上的快捷反馈，不用离开当前页面。
//
// 默认附上系统与版本信息和这次任务的日志。任务有异常（失败、中断、需复核）时译文
// 也默认附上，日志本身就说明了问题，描述可以不写；看上去正常的任务，译文要用户自己勾，
// 还得写一句哪里不对。原文件始终要用户自己勾。要贴截图或加别的文件，点「在反馈页填写」去完整的反馈页。

import { ApiClient, ApiError } from "./api-client";
import { createButton, openModal, showToast, type ModalHandle } from "./components";
import { currentLocale, t, tx } from "./i18n";
import { navigate } from "./router";
import {
  SEND_FAILURES,
  confirmSendWithoutEmail,
  createPrivacyNote,
  createSendProgress,
  emailLooksValid,
  uploadFeedback,
} from "./feedback-shared";

export interface TaskFeedbackOptions {
  taskId: string;
  /** 「Excel · 报价单.xlsx」这样的任务名，显示在弹窗标题上方。 */
  taskLabel: string;
  /** 任务有异常（失败、中断、需复核）：描述可以不写。 */
  hasProblem: boolean;
}

interface FeedbackContext {
  endpoint_configured: boolean;
  saved_email?: string;
  ask_email?: boolean;
}

let clientPromise: Promise<ApiClient> | null = null;
function getClient(): Promise<ApiClient> {
  if (!clientPromise) {
    const instance = new ApiClient();
    clientPromise = instance.connect().then(() => instance).catch((error) => {
      clientPromise = null;
      throw error;
    });
  }
  return clientPromise;
}

function sectionTitle(text: string): HTMLElement {
  const title = document.createElement("div");
  title.className = "feedback-section-title";
  title.textContent = text;
  return title;
}

export function openTaskFeedback(options: TaskFeedbackOptions): void {
  const state = {
    description: "",
    contact: "",
    contactEdited: false,
    includeSystem: true,
    includeLogs: true,
    includeSources: false,
    includeOutputs: options.hasProblem,
    busy: false,
  };
  let context: FeedbackContext | null = null;

  const form = document.createElement("div");
  form.className = "task-feedback";

  form.append(sectionTitle(options.hasProblem ? t("问题描述（选填）") : t("问题描述")));
  const area = document.createElement("textarea");
  area.className = "feedback-text";
  area.rows = 5;
  area.maxLength = 5000;
  area.placeholder = options.hasProblem
    ? t("已附上这次任务的日志。可以补充您看到的情况，不写也能发送。")
    : t("请描述哪里不对，例如哪段译文有误、哪一步出错。");
  area.addEventListener("input", () => {
    state.description = area.value;
    sync();
  });
  form.append(area);

  form.append(sectionTitle(t("附带内容")));
  const kinds = document.createElement("div");
  kinds.className = "feedback-kinds";
  form.append(kinds);
  const renderKinds = (): void => {
    const option = (title: string, key: "includeSystem" | "includeLogs" | "includeSources" | "includeOutputs"): HTMLElement => {
      const row = document.createElement("label");
      row.className = state[key] ? "feedback-kind on" : "feedback-kind";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.className = "ck";
      box.checked = state[key];
      box.disabled = state.busy;
      box.addEventListener("change", () => {
        state[key] = box.checked;
        renderKinds();
      });
      const label = document.createElement("span");
      label.className = "feedback-kind-title";
      label.textContent = title;
      row.append(box, label);
      return row;
    };
    kinds.replaceChildren(
      option(t("系统与版本信息"), "includeSystem"),
      option(t("任务日志"), "includeLogs"),
      option(t("原文件"), "includeSources"),
      option(t("译文"), "includeOutputs"),
    );
    if (state.includeSources || state.includeOutputs) kinds.append(createPrivacyNote());
  };
  renderKinds();

  form.append(sectionTitle(t("邮箱（建议填写）")));
  const contact = document.createElement("input");
  contact.type = "email";
  contact.className = "feedback-input";
  contact.maxLength = 200;
  contact.autocomplete = "email";
  contact.spellcheck = false;
  contact.placeholder = "name@example.com";
  const contactNote = document.createElement("p");
  contactNote.className = "feedback-note";
  contact.addEventListener("input", () => {
    state.contact = contact.value;
    state.contactEdited = true;
    sync();
  });
  form.append(contact, contactNote);

  // 发送失败的原因、上传进度都显示在表单最下面，弹窗不关，改完可以再发。
  const status = document.createElement("div");
  status.className = "task-feedback-status";
  form.append(status);

  const handle: ModalHandle = openModal({
    tone: options.hasProblem ? "warn" : "tint",
    icon: "message",
    sourceLabel: options.taskLabel,
    title: t("反馈此任务"),
    body: [form],
    wide: true,
    actions: [
      // 已经写的描述、邮箱和勾选的附带内容一起带过去，不用重来。
      {
        label: t("在反馈页填写"),
        onClick: () => navigate("feedback", {
          taskId: options.taskId,
          description: state.description,
          contact: state.contactEdited ? state.contact : "",
          include: {
            system: state.includeSystem,
            logs: state.includeLogs,
            sources: state.includeSources,
            outputs: state.includeOutputs,
          },
        }),
      },
      { label: t("取消") },
      { label: t("发送"), variant: "primary", keepOpen: true, onClick: () => send() },
    ],
  });
  const buttons = Array.from(handle.element.querySelectorAll<HTMLButtonElement>(".macts button"));
  const sendButton = buttons[buttons.length - 1];
  // 「在反馈页填写」可以加截图和别的文件，跟发送按钮分开放在左边。
  buttons[0]?.classList.add("task-feedback-full");

  function sync(): void {
    const emailOk = emailLooksValid(state.contact);
    contact.classList.toggle("invalid", !emailOk);
    contactNote.classList.toggle("feedback-error", !emailOk);
    contactNote.textContent = emailOk
      ? t("留下邮箱后，问题解决或功能上线时，我们会发邮件通知您。")
      : t("邮箱格式不正确，请检查后再发送。");
    const configured = context ? context.endpoint_configured : true;
    const needsText = !options.hasProblem && !state.description.trim();
    sendButton.disabled = state.busy || !configured || !emailOk || needsText;
    sendButton.textContent = state.busy ? t("正在发送…") : t("发送");
    area.disabled = state.busy;
    contact.disabled = state.busy;
    if (!configured && !state.busy) showStatus(t("此版本尚未开通在线发送，请点「在反馈页填写」保存为 ZIP。"), true);
  }

  function showStatus(message: string, error = false): void {
    const line = document.createElement("p");
    line.className = error ? "feedback-note feedback-error" : "feedback-note";
    line.textContent = message;
    status.replaceChildren(line);
  }

  async function send(): Promise<void> {
    if (!state.contact.trim() && context?.ask_email) {
      if ((await confirmSendWithoutEmail()) === "fill") {
        contact.focus();
        return;
      }
    }
    state.busy = true;
    renderKinds();
    sync();
    const progress = createSendProgress();
    status.replaceChildren(progress.root);
    try {
      const client = await getClient();
      const result = await uploadFeedback(client, {
        category: "problem",
        description: state.description,
        contact: state.contact,
        include_system: state.includeSystem,
        include_logs: state.includeLogs,
        include_source_files: state.includeSources,
        include_output_files: state.includeOutputs,
        ui_language: currentLocale(),
        task_ids: [options.taskId],
        file_paths: [],
        images: [],
      }, progress.update);
      if (result.state === "error") {
        showStatus(SEND_FAILURES.includes(result.reason) ? t("反馈未发出：{0}", tx(result.message)) : tx(result.message), true);
      } else {
        handle.close();
        showSent(result.id, state.contact.trim());
        return;
      }
    } catch (error) {
      const message = error instanceof ApiError ? error.message : error instanceof Error ? error.message : String(error);
      showStatus(t("反馈未发出：{0}", message), true);
    } finally {
      state.busy = false;
      if (handle.element.isConnected) {
        renderKinds();
        sync();
      }
    }
  }

  void (async () => {
    try {
      const client = await getClient();
      context = await client.request<FeedbackContext>("/api/feedback/context");
      if (!state.contactEdited && context.saved_email) {
        state.contact = context.saved_email;
        contact.value = state.contact;
      }
    } catch (error) {
      showToast({ message: error instanceof Error ? error.message : String(error), error: true });
    }
    if (handle.element.isConnected) sync();
  })();
  sync();
  area.focus();
}

function showSent(reference: string, email: string): void {
  const thanks = email
    ? t("感谢您的反馈。问题解决或功能上线时，我们会发邮件到 {0}。", email)
    : t("感谢您的反馈。");
  openModal({
    tone: "ok",
    icon: "check",
    title: t("反馈已发送"),
    body: [t("反馈编号：{0}", reference), thanks],
    actions: [{ label: t("关闭"), variant: "primary" }],
  });
}

/** 结果横幅、任务详情上的「反馈此任务」按钮。 */
export function createTaskFeedbackButton(options: TaskFeedbackOptions & { size?: "mini" }): HTMLButtonElement {
  return createButton({
    label: t("反馈此任务"),
    icon: "message",
    size: options.size,
    onClick: () => openTaskFeedback(options),
  });
}
