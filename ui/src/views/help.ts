// 帮助视图 —— 端口自 main.ts 的 renderHelpView()（英雄区 + 6 张说明卡）。
// 样张没有单独画这一页，版式按 V9 组件与设计令牌自建（见 ./help.css），
// 卡片划分沿用旧版，文案已改成面向普通用户的说法并补上 CAD 图纸；卡片上的入口按钮改为 router.navigate 深链到
// 对应新视图 / 设置子页，而不是复刻旧版的模态/onboarding 流程。

import type { ViewParams } from "../router";
import { navigate } from "../router";
import { setTopbar } from "../shell";
import { createCard, createButton } from "../components";
import { icon, type IconName } from "../icons";
import { invoke } from "@tauri-apps/api/core";
import { showQuickStart } from "../quickstart";
import { t, localized } from "../i18n";
import "./help.css";

async function openExternalUrl(url: string): Promise<void> {
  await invoke("open_external_url", { url });
}

const IS_MAC = /Mac/i.test(navigator.userAgent);

interface HelpCardSpec {
  iconName: IconName;
  title: string;
  body: string;
  footer?: (host: HTMLElement) => void;
}

const CARDS: HelpCardSpec[] = localized(() => ([
  {
    iconName: "gear",
    title: t("1. 配置翻译模型"),
    body: t("填写服务商、模型和 API 密钥后，点击「测试连接」确认配置可用。文档翻译、PDF 与图片、记忆库清洗可分别使用不同的模型。"),
    footer: (host) => {
      host.append(
        createButton({ label: t("打开模型配置"), size: "mini", onClick: () => navigate("settings", { page: "models" }) }),
      );
    },
  },
  {
    iconName: "doc-file",
    title: t("2. 选择文件与语言"),
    body: t("支持 Excel、Word、PDF / 图片和 CAD 图纸（DWG / DXF）。通过「浏览」选择文件或文件夹后将自动扫描。源语言默认自动识别，PDF 和图片仅需选择目标语言。"),
    footer: (host) => {
      const row = document.createElement("div");
      row.className = "help-links";
      row.append(
        createButton({ label: "Excel", size: "mini", onClick: () => navigate("excel") }),
        createButton({ label: "Word", size: "mini", onClick: () => navigate("word") }),
        createButton({ label: t("PDF / 图片"), size: "mini", onClick: () => navigate("pdf") }),
        createButton({ label: t("CAD 图纸"), size: "mini", onClick: () => navigate("cad") }),
      );
      host.append(row);
    },
  },
  {
    iconName: "book",
    title: t("3. 记忆库"),
    body: t("Excel 和 Word 中已翻译的词句将保存到记忆库，之后翻译 Excel、Word 和 CAD 图纸时优先复用。固定词条始终按您设定的译法翻译；语言混杂或识别不准确的内容照常翻译，但不会保存到记忆库。"),
    footer: (host) => {
      host.append(createButton({ label: t("打开记忆库"), size: "mini", onClick: () => navigate("library") }));
    },
  },
  {
    iconName: "tasks",
    title: t("任务、停止与并行"),
    // 429 是 HTTP 状态码，界面上不给用户看内部代号，说清后果就够了。
    body: t("不同类型的任务可同时进行。两个任务使用同一条模型连接时，将先提示可能出现变慢、排队或超时，且两项任务均会计费；确认后才会开始第二个任务。"),
    footer: (host) => {
      host.append(createButton({ label: t("打开任务中心"), size: "mini", onClick: () => navigate("tasks") }));
    },
  },
  {
    iconName: "folder",
    title: t("输出与兼容格式"),
    body: t("标准 .xlsx、.docx、PDF、图片和 DXF 图纸无需安装 Office。旧版 .xls / .doc 可通过本机 Office 高质量转换；如未安装或未授权，将先询问是否改用兼容转换。DWG 图纸需先在「插件」中连接图纸转换工具。"),
    // 用本机 Office 转旧版文件时，macOS 会弹窗问是否允许 Translator 控制 Excel / Word；
    // 当时点了「不允许」，之后转换就一直失败。这里写的是事后怎么改回来。
    // 标题直接写用户碰到的现象，不写「自动化权限」这种系统术语；Windows 没有这项设置，不显示。
    footer: IS_MAC
      ? (host) => {
          const details = document.createElement("details");
          details.className = "help-details";
          const summary = document.createElement("summary");
          summary.textContent = t("转换旧版文件时提示没有权限？");
          const note = document.createElement("p");
          note.textContent =
            t("用本机 Office 转换旧版 .xls / .doc 时，macOS 会询问是否允许 Translator 控制 Excel 或 Word。如果当时选了「不允许」，转换会失败。可在「系统设置 → 隐私与安全性 → 自动化」（macOS 12 为「系统偏好设置 → 安全性与隐私 → 隐私 → 自动化」）中为 Translator 勾选 Microsoft Excel 或 Word，然后重试。");
          details.append(summary, note);
          host.append(details);
        }
      : undefined,
  },
  {
    iconName: "ext",
    title: t("更新、支持与隐私"),
    // 这句话过去写的是「更新只会打开 GitHub 的适配 DMG，不会替换应用或自动重启」——
    // 应用内更新上线之后它就不成立了（会下载、验签、就地替换），而且 Windows 上根本没有
    // DMG。帮助页说错更新怎么工作，用户就会在真的替换应用时以为出了别的事。
    body: t("可在设置中检查更新：下载后先验证签名再安装，重启时间由您决定（Windows 将打开安装程序，安装完成后自动重新打开应用）。诊断信息仅在您主动导出时生成，不包含 API 密钥、原文、译文、提示词或文件路径。"),
    footer: (host) => {
      const row = document.createElement("div");
      row.className = "help-links";
      row.append(
        createButton({ label: t("维护与诊断"), size: "mini", onClick: () => navigate("settings", { page: "data" }) }),
      );
      const feedbackBtn = createButton({
        label: t("反馈问题"),
        icon: "ext",
        size: "mini",
        onClick: () => void openExternalUrl("https://github.com/KlaraGraff/XL_Translator/issues"),
      });
      row.append(feedbackBtn);
      host.append(row);
    },
  },
]));

function buildHero(container: HTMLElement): void {
  const hero = document.createElement("div");
  hero.className = "card help-hero";

  const label = document.createElement("span");
  label.className = "section-label";
  label.textContent = t("使用指南");
  hero.append(label);

  const heading = document.createElement("h2");
  heading.textContent = t("从模型配置到结果文件");
  hero.append(heading);

  const description = document.createElement("p");
  description.textContent = t("帮助内容随应用提供，无需联网。开始翻译前，模型配置与文件扫描均由您主动执行。");
  hero.append(description);

  const actions = document.createElement("div");
  actions.className = "help-hero-actions";
  actions.append(
    createButton({
      label: t("重新查看快速开始"),
      variant: "primary",
      onClick: () => void showQuickStart(),
    }),
    createButton({
      label: t("检查更新"),
      onClick: () => navigate("settings", { page: "about" }),
    }),
  );
  hero.append(actions);

  container.append(hero);
}

function buildCard(spec: HelpCardSpec): HTMLElement {
  const card = createCard([], "help-card");

  const iconHost = document.createElement("div");
  iconHost.className = "help-card-icon";
  iconHost.append(icon(spec.iconName, { size: "md" }));
  card.append(iconHost);

  const heading = document.createElement("h3");
  heading.textContent = spec.title;
  card.append(heading);

  const body = document.createElement("p");
  body.textContent = spec.body;
  card.append(body);

  if (spec.footer) spec.footer(card);

  return card;
}

export function mount(container: HTMLElement, _params: ViewParams): void {
  setTopbar({
    // 这里不放状态胶囊：别的页面的胶囊显示的是真实状态（任务数、扫描进度），
    // 帮助页原来挂一个固定的「本地离线」，看着像应用断网了。
    title: t("帮助"),
    subtitle: t("快速开始清单与常见问题"),
  });

  container.style.flexDirection = "column";
  container.style.overflow = "auto";

  buildHero(container);

  const grid = document.createElement("div");
  grid.className = "help-grid";
  for (const spec of CARDS) grid.append(buildCard(spec));
  container.append(grid);
}

export function unmount(): void {
  // 帮助页没有订阅、定时器或 SSE 连接需要清理。
}
