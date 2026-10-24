// MCP 视图 —— 让智能体（Claude Code、Cursor、Claude Desktop 等）调用 Translator 翻译文件。
// 主操作只有一个：复制一段「接入说明」发给智能体，由它自己完成配置（说明里带本机引擎路径、
// 各客户端的配置写法和自检步骤）。程序位置、MCP 配置 JSON 与命令行示例收在「高级」里，默认折叠。
// 路径取自 sidecar_info 的 executable（打包版本才有，不含令牌）；服务端见 cli/mcp_server.py。
// 智能体发起的翻译只用应用里配置的模型，所以页面同时显示文档翻译、PDF 与图片翻译两个模型
// 是否已配置（判断与引擎开始任务前的检查一致：有模型名，云端服务还要有密钥），没配时给入口。

import { clearElement, navigate, type ViewParams } from "../router";
import { setTopbar } from "../shell";
import { createButton, createCard, createStatus, flashCopied, reserveCopied, showToast } from "../components";
import { icon } from "../icons";
import { t } from "../i18n";
import { ApiClient, sidecarExecutable } from "../api-client";
import { isWindows } from "../platform";
import "./mcp.css";

// 拿不到本机路径时（开发构建）用默认安装位置，与 tauri.conf.json 的 resources
// （resources/sidecar → sidecar）和 NSIS 当前用户安装目录（%LOCALAPPDATA%\Translator）一致。
const DEFAULT_EXECUTABLE = isWindows()
  ? "%LOCALAPPDATA%\\Translator\\sidecar\\translator-sidecar\\translator-sidecar.exe"
  : "/Applications/Translator.app/Contents/Resources/sidecar/translator-sidecar/translator-sidecar";

const CLAUDE_DESKTOP_CONFIG = isWindows()
  ? "%APPDATA%\\Claude\\claude_desktop_config.json"
  : "~/Library/Application Support/Claude/claude_desktop_config.json";
const CURSOR_CONFIG = isWindows() ? "%USERPROFILE%\\.cursor\\mcp.json" : "~/.cursor/mcp.json";

type RolePayload = { model?: unknown; mode?: unknown; has_api_key?: unknown; availability_status?: unknown };
/** undefined：还在读取；null：没读到。 */
type Roles = Record<string, RolePayload> | null | undefined;

let mounted = false;
let state: { executable: string; roles: Roles } = { executable: "", roles: undefined };

export function mcpConfig(executable: string): string {
  return JSON.stringify({ mcpServers: { translator: { command: executable, args: ["mcp"] } } }, null, 2);
}

function claudeCodeCommand(executable: string): string {
  return `claude mcp add --scope user translator -- "${executable}" mcp`;
}

function cliExample(executable: string): string {
  // PowerShell 要用 & 才能运行带引号的路径；macOS 的终端直接写。
  const source = t("文件或文件夹路径");
  return `${isWindows() ? "& " : ""}"${executable}" translate "${source}" --to en --wait`;
}

/** 发给智能体的接入说明：写给智能体看，让它按自己所在的客户端完成配置并自检。 */
export function agentPrompt(executable: string): string {
  return [
    t("请帮我把这台电脑上的 Translator 接入为 MCP 服务器。接好后，我可以让你用它翻译 Excel、Word、PDF、图片和 CAD 图纸文件。"),
    "",
    t("服务器信息（本机程序，不需要网址或密钥）："),
    t("- 名称：{0}", "translator"),
    t("- 启动命令：{0}", executable),
    t("- 参数：{0}", "mcp"),
    t("- 传输方式：{0}", "stdio"),
    "",
    t("请按你所在的客户端添加："),
    t("- Claude Code：在终端运行 {0}", claudeCodeCommand(executable)),
    t("- Cursor：将下面的配置合并到 {0}", CURSOR_CONFIG),
    t("- Claude Desktop：将下面的配置合并到 {0}，然后完全退出并重新打开 Claude Desktop", CLAUDE_DESKTOP_CONFIG),
    t("- 其他客户端：按其 MCP 配置方式添加上述服务器"),
    "",
    mcpConfig(executable),
    "",
    t("配置文件中已有其他服务器时，只加入 translator 一项，不要改动其他内容。如果你无法修改本机文件或运行命令，请逐步告诉我如何手动添加；需要重新启动才能生效时，请提醒我。接好后，请调用 list_types 工具，确认能看到 Excel、Word、PDF、CAD 等文件类型，并查看结果中的 model_setup：如果显示模型未配置，请告诉我。"),
    t("之后我让你翻译文件时，请使用文件的完整路径。"),
    t("翻译使用的是 Translator 中配置的模型，不是你自己的模型。如果 Translator 提示模型或 API 密钥未配置，请提醒我在 Translator 的「设置 → 模型服务」中配置并测试连接，不要改用其他方式翻译这些文件。"),
  ].join("\n");
}

/** 复制成功时按钮原地打勾；done 给了才另外弹提示（需要告诉用户下一步做什么的时候）。 */
async function copyText(text: string, button: HTMLElement, done?: string): Promise<void> {
  try {
    if (!navigator.clipboard?.writeText) throw new Error(t("当前环境不支持写入剪贴板"));
    await navigator.clipboard.writeText(text);
    flashCopied(button);
    if (done) showToast({ message: done });
  } catch {
    showToast({ message: t("复制失败，请手动复制：{0}", text), error: true });
  }
}

function codeBlock(label: string, text: string): HTMLElement {
  const root = document.createElement("div");
  root.className = "mcp-code";
  const head = document.createElement("div");
  head.className = "mcp-code-head";
  const title = document.createElement("span");
  title.textContent = label;
  head.append(title, reserveCopied(createButton({ label: t("复制"), size: "mini", onClick: (event) => void copyText(text, event.currentTarget as HTMLElement) })));
  const pre = document.createElement("pre");
  pre.className = "mcp-code-body";
  const code = document.createElement("code");
  code.textContent = text;
  pre.append(code);
  root.append(head, pre);
  return root;
}

function connectCard(executable: string, fallback: boolean): HTMLElement {
  const card = createCard([]);
  const body = document.createElement("div");
  body.className = "mcp-card-body";

  const titleRow = document.createElement("div");
  titleRow.className = "mcp-title-row";
  const iconBox = document.createElement("div");
  iconBox.className = "mcp-icon";
  iconBox.append(icon("link", { size: "lg" }));
  const copy = document.createElement("div");
  copy.className = "mcp-copy";
  const title = document.createElement("h2");
  title.textContent = t("接入智能体");
  const desc = document.createElement("p");
  desc.textContent = t("适用于 Claude Code、Cursor、Claude Desktop 等支持 MCP 的智能体。");
  copy.append(title, desc);
  titleRow.append(iconBox, copy);
  body.append(titleRow);

  const steps = document.createElement("ol");
  steps.className = "mcp-steps";
  for (const text of [
    t("复制接入说明。"),
    t("粘贴并发送给智能体，由它完成配置。"),
    t("按提示重新启动智能体后，即可让它翻译文件，例如：「用 Translator 把这个文件翻译成法语」。"),
  ]) {
    const item = document.createElement("li");
    item.textContent = text;
    steps.append(item);
  }
  body.append(steps);

  const prompt = agentPrompt(executable);
  const actions = document.createElement("div");
  actions.className = "mcp-actions";
  actions.append(
    reserveCopied(createButton({
      label: t("复制接入说明"),
      variant: "primary",
      onClick: (event) => void copyText(prompt, event.currentTarget as HTMLElement, t("已复制接入说明，请粘贴发送给智能体。")),
    })),
  );
  body.append(actions);

  if (fallback) body.append(note(t("未获取到本机路径，接入说明中为默认安装位置。")));

  const preview = document.createElement("details");
  preview.className = "mcp-details";
  const summary = document.createElement("summary");
  summary.textContent = t("查看接入说明");
  const pre = document.createElement("pre");
  pre.className = "mcp-code-body";
  pre.textContent = prompt;
  preview.append(summary, pre);
  body.append(preview);

  card.append(body);
  return card;
}

const MODEL_ROLES: { key: string; label: () => string }[] = [
  { key: "translation", label: () => t("文本翻译（Excel / Word / CAD）") },
  { key: "image", label: () => t("图像翻译") },
];

function roleConfigured(role: RolePayload | undefined): boolean {
  const model = String(role?.model ?? "").trim();
  return Boolean(model) && (role?.mode === "local" || role?.has_api_key === true);
}

function modelCard(roles: Roles): HTMLElement {
  const card = createCard([], "mcp-models");
  const head = document.createElement("div");
  head.className = "mcp-models-head";
  const copy = document.createElement("div");
  copy.className = "mcp-copy";
  const title = document.createElement("h2");
  title.textContent = t("翻译模型");
  const desc = document.createElement("p");
  desc.textContent = t("智能体发起的翻译使用 Translator 中配置的模型，不使用智能体自己的模型。");
  copy.append(title, desc);
  head.append(copy);
  card.append(head);
  if (roles === undefined) return card;

  const missing = roles === null || MODEL_ROLES.some(({ key }) => !roleConfigured(roles[key]));
  head.append(
    createButton({
      label: missing ? t("去配置模型") : t("模型设置"),
      variant: missing ? "primary" : undefined,
      size: missing ? undefined : "mini",
      onClick: () => navigate("settings", { page: "models" }),
    }),
  );
  if (roles === null) {
    card.append(note(t("未能读取模型配置。")));
    return card;
  }

  const list = document.createElement("dl");
  list.className = "mcp-model-list";
  for (const { key, label } of MODEL_ROLES) {
    const role = roles[key];
    const configured = roleConfigured(role);
    const model = String(role?.model ?? "").trim();
    const status = String(role?.availability_status ?? "");
    const name = document.createElement("dt");
    name.textContent = label();
    const value = document.createElement("dd");
    if (configured) {
      const text = document.createElement("span");
      text.className = "mcp-model-name";
      text.textContent = role?.mode === "local" ? t("本地 · {0}", model) : model;
      value.append(text);
    }
    value.append(
      !configured
        ? createStatus({ label: t("未配置"), tone: "warn" })
        : status === "available"
          ? createStatus({ label: t("测试通过"), tone: "ok" })
          : status === "unavailable"
            ? createStatus({ label: t("测试失败"), tone: "warn" })
            : createStatus({ label: t("未测试"), tone: "idle" }),
    );
    list.append(name, value);
  }
  card.append(list);
  if (missing) card.append(note(t("未配置模型时，对应文件的翻译会被拒绝，智能体会提醒你先完成配置。")));
  return card;
}

function note(text: string): HTMLElement {
  const element = document.createElement("p");
  element.className = "mcp-note";
  element.textContent = text;
  return element;
}

function manualCard(executable: string): HTMLElement {
  const card = createCard([]);
  const details = document.createElement("details");
  details.className = "mcp-details mcp-manual";
  const summary = document.createElement("summary");
  summary.textContent = t("高级：手动配置与命令行");
  const grid = document.createElement("div");
  grid.className = "mcp-code-grid";
  grid.append(
    codeBlock(t("MCP 配置（加入智能体的 MCP 配置文件）"), mcpConfig(executable)),
    codeBlock(t("命令行示例"), cliExample(executable)),
  );
  details.append(summary, codeBlock(t("程序位置"), executable), grid);
  card.append(details);
  return card;
}

function render(container: HTMLElement): void {
  const { executable, roles } = state;
  const path = executable || DEFAULT_EXECUTABLE;
  clearElement(container);
  const column = document.createElement("div");
  column.className = "mcp-column";

  const info = document.createElement("div");
  info.className = "card mcp-info";
  info.textContent = t("Translator 打开时，智能体发起的任务显示在任务中心；未打开时，翻译引擎在后台运行，结束后自动退出。模型与翻译参数沿用应用中的设置，原文件不会被修改。");

  column.append(connectCard(path, !executable), modelCard(roles), info, manualCard(path));
  container.append(column);
}

export function mount(container: HTMLElement, params: ViewParams): void {
  mounted = true;
  // 嵌在「扩展」页的「智能体接入」页签里时，顶栏由扩展页管。
  if (params.embedded !== true) setTopbar({ title: "MCP", subtitle: t("让智能体调用 Translator 翻译文件") });
  container.classList.add("mcp-content");
  state = { executable: "", roles: undefined };
  render(container);
  void sidecarExecutable().then((executable) => {
    if (!mounted || !executable) return;
    state.executable = executable;
    render(container);
  });
  void loadRoles().then((roles) => {
    if (!mounted) return;
    state.roles = roles;
    render(container);
  });
}

async function loadRoles(): Promise<Roles> {
  try {
    const client = new ApiClient();
    await client.connect();
    const payload = await client.request<{ roles?: Record<string, RolePayload> }>("/api/models/roles");
    return payload.roles ?? null;
  } catch {
    return null;
  }
}

export function unmount(): void {
  mounted = false;
}
