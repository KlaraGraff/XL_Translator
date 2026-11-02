/**
 * 工作台：点「开始翻译」被拦下（缺模型、缺组件等）时的说明弹窗。
 */
import { apiErrorReason } from "../api-client";
import { openModal } from "../components";
import { t } from "../i18n";
import { navigate } from "../router";
import { redactedText, type JsonObject } from "./workspace-util";
import { MODEL_MISSING_ROLE, type Surface, type SurfaceState, preflightAndSubmit } from "./workspace";

/** 前置校验把任务拦下来时给一个弹窗，不给两秒就消失的提示：任务根本没开始这件事得留在
 *  屏幕上。更要紧的是有些拦截本身是有出路的——审核模型上次测试失败那条就写着「或明确
 *  确认继续」，而 9.2.6 只在提示文字里说了这句话，界面上没有任何按钮能确认，看起来就
 *  像开始按钮坏了。 */
export function showStartBlockedModal(surface: Surface, st: SurfaceState, payload: JsonObject, error: unknown): void {
  // 开始前给审核模型发了一张写着字的测试图（core/image_input_check.py）。不能识别图片的
  // 模型审核每一页都会出错，出路是「本次不审核」或换模型；回了话却没读出字的，图片可能
  // 没送到模型，另给一个「仍用它审核」。两种都只影响这一次任务，设置里的开关不动。
  const imageInputReason = apiErrorReason(error);
  if (imageInputReason === "pdf_review_no_image_input" || imageInputReason === "pdf_review_image_unseen") {
    const unseen = imageInputReason === "pdf_review_image_unseen";
    openModal({
      // 三四个按钮、英法文又长：用宽一些的弹窗，按钮尽量排在一行。
      wide: true,
      tone: "warn",
      icon: "warn",
      title: unseen ? t("审核模型可能没看到图片") : t("审核模型不能识别图片"),
      body: unseen
        ? [
          t("测试时审核模型有回复，但没有读出测试图中的文字。图片可能未送达模型（例如所用的中转服务没有转发图片），这样的审核结果不可信。"),
          t("建议本次不审核直接开始，或在「设置 → 模型服务 → 图像翻译审核」中更换模型或连接。"),
          t("如确认该模型能识别图片，也可以仍用它审核。"),
        ]
        : [
          t("逐页审核已开启，但测试发现当前的图像翻译审核模型不能识别图片，用它审核每一页都会出错。"),
          t("可以本次不审核直接开始，译文照常生成；或在「设置 → 模型服务 → 图像翻译审核」中换一个能识别图片的模型。"),
          t("本次不审核只影响这一次任务，设置不会被修改。"),
        ],
      actions: [
        { label: t("取消") },
        { label: t("前往设置更换模型"), onClick: () => navigate("settings", { page: "models", role: "pdf_review" }) },
        ...(unseen
          ? [{
            label: t("仍用它审核"),
            onClick: () => void preflightAndSubmit(surface, st, { ...payload, allow_known_review_failure: true }),
          }]
          : []),
        {
          label: t("本次不审核，直接开始"),
          variant: "primary" as const,
          onClick: () => void preflightAndSubmit(surface, st, { ...payload, allow_skip_pdf_review: true }),
        },
      ],
    });
    return;
  }
  if (apiErrorReason(error) === "pdf_review_model_unavailable") {
    openModal({
      tone: "warn",
      icon: "warn",
      title: t("审核模型上次测试未通过"),
      body: [
        t("逐页审核已开启，但本机最近一次测试「图像翻译审核」模型时失败。如现在开始，页面将正常翻译，但审核步骤很可能在每一页都出错。"),
        t("建议先在「设置 → 模型服务 → 图像翻译审核」中重新测试，或关闭逐页审核后再开始。"),
        t("也可忽略测试结果直接开始：仅影响本次任务，设置不会被修改。"),
      ],
      actions: [
        { label: t("取消") },
        { label: t("前往设置重新测试"), onClick: () => navigate("settings", { page: "models" }) },
        {
          label: t("忽略并开始"),
          variant: "primary",
          onClick: () => void preflightAndSubmit(surface, st, { ...payload, allow_known_review_failure: true }),
        },
      ],
    });
    return;
  }
  const missingRole = MODEL_MISSING_ROLE[apiErrorReason(error)];
  if (missingRole) {
    // 以前这里和其他拦截一样只有「知道了」：用户读完「尚未填写模型名称」还得自己去找
    // 设置在哪。直接给一个去「设置 › 模型服务」对应角色的主按钮。
    openModal({
      tone: "warn",
      icon: "warn",
      title: missingRole === "pdf_review" ? t("尚未配置审核模型") : t("尚未配置翻译模型"),
      body: [
        redactedText((error as Error)?.message, t("尚未填写翻译所用的模型。")),
        t("填写模型名称和 API 密钥后，返回此处点击「开始翻译」。"),
      ],
      actions: [
        { label: t("取消") },
        { label: t("前往设置"), variant: "primary", onClick: () => navigate("settings", { page: "models", role: missingRole }) },
      ],
    });
    return;
  }
  openModal({
    tone: "warn",
    icon: "warn",
    title: t("本次任务未能开始"),
    body: [redactedText((error as Error)?.message, t("任务准备失败，无更多信息。"))],
    actions: [{ label: t("知道了"), variant: "primary" }],
  });
}
