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
import "./help.css";

async function openExternalUrl(url: string): Promise<void> {
  await invoke("open_external_url", { url });
}

interface HelpCardSpec {
  iconName: IconName;
  title: string;
  body: string;
  footer?: (host: HTMLElement) => void;
}

const CARDS: HelpCardSpec[] = [
  {
    iconName: "gear",
    title: "1. 配置翻译模型",
    body: "填好服务商、模型和 API 密钥，再点「测试连接」确认能用。文档翻译、PDF 与图片、记忆库清洗可以各用各的模型。",
    footer: (host) => {
      host.append(
        createButton({ label: "打开模型配置", size: "mini", onClick: () => navigate("settings", { page: "models" }) }),
      );
    },
  },
  {
    iconName: "doc-file",
    title: "2. 选择文件与语言",
    body: "支持 Excel、Word、PDF / 图片和 CAD 图纸（DWG / DXF）。用「浏览」选好文件或文件夹后会自动扫描。源语言默认自动识别，PDF 和图片只需选目标语言。",
    footer: (host) => {
      const row = document.createElement("div");
      row.className = "help-links";
      row.append(
        createButton({ label: "Excel", size: "mini", onClick: () => navigate("excel") }),
        createButton({ label: "Word", size: "mini", onClick: () => navigate("word") }),
        createButton({ label: "PDF / 图片", size: "mini", onClick: () => navigate("pdf") }),
        createButton({ label: "CAD 图纸", size: "mini", onClick: () => navigate("cad") }),
      );
      host.append(row);
    },
  },
  {
    iconName: "book",
    title: "3. 记忆库",
    body: "Excel 和 Word 翻好的词句会存进记忆库，之后翻译 Excel、Word 和 CAD 图纸时优先复用。固定的词条一定按你定的译法翻；语言混杂、识别不准的内容照常翻译，但不会存进记忆库。",
    footer: (host) => {
      host.append(createButton({ label: "打开记忆库", size: "mini", onClick: () => navigate("library") }));
    },
  },
  {
    iconName: "tasks",
    title: "任务、停止与并行",
    // 429 是 HTTP 状态码，界面上不给用户看内部代号，说清后果就够了。
    body: "不同类型的任务可以同时进行。两个任务要用同一条模型连接时，会先提醒可能变慢、排队或超时，并且两边都会计费，你确认后才会开始第二个。",
    footer: (host) => {
      host.append(createButton({ label: "打开任务中心", size: "mini", onClick: () => navigate("tasks") }));
    },
  },
  {
    iconName: "folder",
    title: "输出与兼容格式",
    body: "标准 .xlsx、.docx、PDF、图片和 DXF 图纸不需要安装 Office。旧版 .xls / .doc 可以用本机 Office 高质量转换，没装或没授权时会先问你要不要改用兼容转换。DWG 图纸需要先在「插件」里连接图纸转换工具。",
    footer: (host) => {
      const details = document.createElement("details");
      details.className = "help-details";
      const summary = document.createElement("summary");
      summary.textContent = "macOS 自动化权限";
      const note = document.createElement("p");
      note.textContent =
        "macOS 12：系统偏好设置 → 安全性与隐私 → 隐私 → 自动化。macOS 13 及以上：系统设置 → 隐私与安全性 → 自动化。允许 Translator 控制 Microsoft Excel 或 Word 后再重试。";
      details.append(summary, note);
      host.append(details);
    },
  },
  {
    iconName: "ext",
    title: "更新、支持与隐私",
    // 这句话过去写的是「更新只会打开 GitHub 的适配 DMG，不会替换应用或自动重启」——
    // 应用内更新上线之后它就不成立了（会下载、验签、就地替换），而且 Windows 上根本没有
    // DMG。帮助页说错更新怎么工作，用户就会在真的替换应用时以为出了别的事。
    body: "设置里可以直接检查更新：下载后先验证签名再安装，什么时候重启由你决定（Windows 会打开安装程序，装完自动重新打开）。诊断信息只在你主动导出时生成，不包含 API 密钥、原文、译文、提示词或文件路径。",
    footer: (host) => {
      const row = document.createElement("div");
      row.className = "help-links";
      row.append(
        createButton({ label: "维护与诊断", size: "mini", onClick: () => navigate("settings", { page: "data" }) }),
      );
      const feedbackBtn = createButton({
        label: "反馈问题",
        icon: "ext",
        size: "mini",
        onClick: () => void openExternalUrl("https://github.com/KlaraGraff/XL_Translator/issues"),
      });
      row.append(feedbackBtn);
      host.append(row);
    },
  },
];

function buildHero(container: HTMLElement): void {
  const hero = document.createElement("div");
  hero.className = "card help-hero";

  const label = document.createElement("span");
  label.className = "section-label";
  label.textContent = "本地离线帮助";
  hero.append(label);

  const heading = document.createElement("h2");
  heading.textContent = "从模型配置到结果文件";
  hero.append(heading);

  const description = document.createElement("p");
  description.textContent = "帮助内容随应用提供，不需要联网。实际开始翻译前，模型配置与文件扫描都由你主动触发。";
  hero.append(description);

  const actions = document.createElement("div");
  actions.className = "help-hero-actions";
  actions.append(
    createButton({
      label: "重新查看快速开始",
      variant: "primary",
      onClick: () => void showQuickStart(),
    }),
    createButton({
      label: "检查更新",
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
    title: "帮助",
    status: { label: "本地离线", tone: "idle" },
    subtitle: "快速开始清单与常见问题",
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
