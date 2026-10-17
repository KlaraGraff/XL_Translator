// 帮助视图（流光原型 renderHelp）：欢迎区 + 「三步开始」三张卡 + 常见问题折叠列表。
// 卡片和问题上的入口按钮用 router.navigate 深链到对应视图 / 设置子页，不复刻旧版的
// 模态 / onboarding 流程。旧版六张说明卡的正文都还在：前三张是三步，后三张进常见问题。

import type { ViewParams } from "../router";
import { navigate } from "../router";
import { setTopbar } from "../shell";
import { createButton, createFold } from "../components";
import { icon, type IconName } from "../icons";
import { showQuickStart } from "../quickstart";
import { t, localized } from "../i18n";
import { isMac } from "../platform";
import "./help.css";


interface StepSpec {
  iconName: IconName;
  title: string;
  body: string;
  foot: (host: HTMLElement) => void;
}

interface FaqSpec {
  title: string;
  body: string;
  foot?: (host: HTMLElement) => void;
}

const STEPS: StepSpec[] = localized(() => ([
  {
    iconName: "gear",
    title: t("配置翻译模型"),
    body: t("填写服务商、模型和 API 密钥后，点击「测试连接」确认配置可用。文档翻译、PDF 与图片、记忆库清洗可分别使用不同的模型。"),
    foot: (host) => {
      host.append(createButton({ label: t("打开模型配置"), size: "mini", onClick: () => navigate("settings", { page: "models" }) }));
    },
  },
  {
    iconName: "doc-file",
    title: t("把文件交给 Translator"),
    body: t("拖入 Excel、Word、PDF、图片或 CAD 图纸（DWG / DXF），自动进入对应工作区并读取内容；也可以在路径栏选择文件或文件夹。源语言默认自动识别，PDF 和图片仅需选择目标语言。"),
    foot: (host) => {
      host.append(
        createButton({ label: t("前往翻译"), size: "mini", variant: "primary", onClick: () => navigate("home") }),
        createButton({ label: "Excel", size: "mini", variant: "ghost", onClick: () => navigate("excel") }),
        createButton({ label: "Word", size: "mini", variant: "ghost", onClick: () => navigate("word") }),
        createButton({ label: t("PDF / 图片"), size: "mini", variant: "ghost", onClick: () => navigate("pdf") }),
        createButton({ label: t("CAD 图纸"), size: "mini", variant: "ghost", onClick: () => navigate("cad") }),
      );
    },
  },
  {
    iconName: "book",
    title: t("记忆库"),
    body: t("Excel 和 Word 中已翻译的词句将保存到记忆库，之后翻译 Excel、Word 和 CAD 图纸时优先复用。固定词条始终按您设定的译法翻译；语言混杂或识别不准确的内容照常翻译，但不会保存到记忆库。"),
    foot: (host) => {
      host.append(createButton({ label: t("打开记忆库"), size: "mini", onClick: () => navigate("library") }));
    },
  },
]));

function faqs(): FaqSpec[] {
  const list: FaqSpec[] = [
    {
      title: t("任务、停止与并行"),
      // 429 是 HTTP 状态码，界面上不给用户看内部代号，说清后果就够了。
      body: t("不同类型的任务可同时进行。两个任务使用同一条模型连接时，将先提示可能出现变慢、排队或超时，且两项任务均会计费；确认后才会开始第二个任务。"),
      foot: (host) => {
        host.append(createButton({ label: t("打开任务"), size: "mini", onClick: () => navigate("tasks") }));
      },
    },
    {
      title: t("输出与兼容格式"),
      body: t("标准 .xlsx、.docx、PDF、图片和 DXF 图纸无需安装 Office。旧版 .xls / .doc 可通过本机 Office 高质量转换；如未安装或未授权，将先询问是否改用兼容转换。DWG 图纸需先在「扩展 → 组件」中连接图纸转换工具。"),
    },
  ];
  // 用本机 Office 转旧版文件时，macOS 会弹窗问是否允许 Translator 控制 Excel / Word；
  // 当时点了「不允许」，之后转换就一直失败。这里写的是事后怎么改回来。
  // 标题直接写用户碰到的现象，不写「自动化权限」这种系统术语；Windows 没有这项设置，不显示。
  if (isMac()) {
    list.push({
      title: t("转换旧版文件时提示没有权限？"),
      body: t("用本机 Office 转换旧版 .xls / .doc 时，macOS 会询问是否允许 Translator 控制 Excel 或 Word。如果当时选了「不允许」，转换会失败。可在「系统设置 → 隐私与安全性 → 自动化」（macOS 12 为「系统偏好设置 → 安全性与隐私 → 隐私 → 自动化」）中为 Translator 勾选 Microsoft Excel 或 Word，然后重试。"),
    });
  }
  list.push({
    title: t("更新、支持与隐私"),
    // 应用内更新会下载、验签、就地替换；Windows 上没有 DMG。帮助页说错更新怎么工作，
    // 用户就会在真的替换应用时以为出了别的事。
    body: t("可在设置中检查更新：下载后先验证签名再安装，重启时间由您决定（Windows 将打开安装程序，安装完成后自动重新打开应用）。诊断信息仅在您主动导出时生成，不包含 API 密钥、原文、译文、提示词或文件路径。反馈仅在您点击「发送」后上传，附带哪些日志和文件由您逐项选择。"),
    foot: (host) => {
      host.append(
        createButton({ label: t("维护与诊断"), size: "mini", onClick: () => navigate("settings", { page: "data" }) }),
        createButton({ label: t("检查更新"), size: "mini", onClick: () => navigate("settings", { page: "about" }) }),
        // 以前跳到公开的 GitHub Issues：用户得有账号，贴出来的日志谁都看得到。
        createButton({ label: t("反馈问题"), icon: "message", size: "mini", onClick: () => navigate("feedback") }),
      );
    },
  });
  return list;
}

function buildHero(container: HTMLElement): void {
  const hero = document.createElement("div");
  hero.className = "hhero";
  const copy = document.createElement("div");
  copy.className = "hhero-copy";
  const heading = document.createElement("h2");
  heading.textContent = t("欢迎使用 Translator");
  const description = document.createElement("p");
  description.textContent = t("把 Excel、Word、PDF、图片和 CAD 图纸交给它，保留版式，中外文双向互译。帮助内容随应用提供，无需联网。");
  copy.append(heading, description);
  const actions = document.createElement("div");
  actions.className = "acts";
  actions.append(
    createButton({ label: t("快速开始"), icon: "play", onClick: () => void showQuickStart() }),
    createButton({ label: t("反馈"), icon: "message", variant: "ghost", onClick: () => navigate("feedback") }),
  );
  hero.append(copy, actions);
  container.append(hero);
}

function buildSteps(container: HTMLElement): void {
  const title = document.createElement("h3");
  title.className = "help-h";
  title.textContent = t("三步开始");
  const steps = document.createElement("div");
  steps.className = "steps stag";
  for (const [index, spec] of STEPS.entries()) {
    const card = document.createElement("div");
    card.className = "xcard";
    card.style.setProperty("--i", String(index));
    const top = document.createElement("div");
    top.className = "top";
    const badge = document.createElement("span");
    badge.className = "xi";
    badge.append(icon(spec.iconName, { size: "sm" }));
    const name = document.createElement("b");
    name.textContent = `${index + 1}. ${spec.title}`;
    top.append(badge, name);
    const body = document.createElement("p");
    body.textContent = spec.body;
    const foot = document.createElement("div");
    foot.className = "foot";
    spec.foot(foot);
    card.append(top, body, foot);
    steps.append(card);
  }
  container.append(title, steps);
}

function buildFaq(container: HTMLElement): void {
  const title = document.createElement("h3");
  title.className = "help-h";
  title.textContent = t("常见问题");
  const list = document.createElement("div");
  list.className = "help-faq";
  for (const [index, spec] of faqs().entries()) {
    const content = document.createElement("div");
    content.className = "help-faq-b";
    const body = document.createElement("p");
    body.textContent = spec.body;
    content.append(body);
    if (spec.foot) {
      const foot = document.createElement("div");
      foot.className = "foot";
      spec.foot(foot);
      content.append(foot);
    }
    list.append(createFold({ title: spec.title, content, open: index === 0 }).root);
  }
  container.append(title, list);
}

export function mount(container: HTMLElement, _params: ViewParams): void {
  setTopbar({
    // 这里不放状态胶囊：别的页面的胶囊显示的是真实状态（任务数、扫描进度），
    // 帮助页原来挂一个固定的「本地离线」，看着像应用断网了。
    title: t("帮助"),
    subtitle: t("三步开始，常见问题在下方"),
  });

  container.classList.add("help");
  container.style.flexDirection = "column";
  container.style.overflow = "auto";

  const page = document.createElement("div");
  page.className = "help-page";
  buildHero(page);
  buildSteps(page);
  buildFaq(page);
  container.append(page);
}

export function unmount(): void {
  // 帮助页没有订阅、定时器或 SSE 连接需要清理。
}
