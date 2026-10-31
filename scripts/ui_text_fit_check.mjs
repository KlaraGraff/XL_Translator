#!/usr/bin/env node
/**
 * 界面文字检查：中文、英文、法文 × 常用（1380×900）和最小（1080×700）窗口，逐页找放不下的文字。
 * 规则见 docs/UI_TEXT_FIT.md：
 *   - 名称（按钮、标签、标题、菜单、下拉选项、提示、报错）被截断、被裁掉或伸出框外 → 报红；
 *   - 内容（文件名、路径、任务名……，元素带 .fit-clip）可以截断，但鼠标停上去要能看到全文，看不到 → 报红；
 *   - 右栏出现横向滚动、整页出现横向滚动 → 报红；
 *   - 「法文加长 30%」假语言另跑一遍，只出报告，不报红。
 *
 * 覆盖的画面：首次启动的快速开始、各页空白状态（展开「高级」等折叠块、点遍子页和页签）、
 * 配好模型后的设置页、添加文件后、一次翻译完成后的工作区 / 任务 / 首页 / 反馈页、
 * 「反馈此任务」弹窗、搜索面板、PDF 图文指引。
 *
 * 用法（仓库根目录）：
 *   node scripts/ui_text_fit_check.mjs [--out 目录] [--locales zh,en,fr] [--sizes std,min] [--no-pseudo]
 * 需要：Python 依赖（requirements.txt）、ui/node_modules、Playwright 和 Chromium。
 * 自己起后端（scripts/ui_text_fit_server.py：临时数据目录 + 样例文件 + 本机假模型）和 Vite 开发页，
 * 跑完全部关掉。退出码：0 全绿，1 有红项，2 检查本身出错。
 */
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { cpSync, mkdtempSync, mkdirSync, openSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(path.join(ROOT, "ui", "package.json"));
const { chromium } = require("playwright");

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const index = args.indexOf(`--${name}`);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};
const OUT = path.resolve(opt("out", path.join(ROOT, ".runtime", "self-tests", "ui-text-fit", "report")));
const LOCALES = opt("locales", "zh,en,fr").split(",");
const SIZE_DEFS = { std: { w: 1380, h: 900 }, min: { w: 1080, h: 700 } };
const SIZES = opt("sizes", "std,min").split(",").map((tag) => ({ tag, ...SIZE_DEFS[tag] }));
const PSEUDO = !args.includes("--no-pseudo");
const API_PORT = Number(opt("api-port", "18795"));
const UI_PORT = Number(opt("ui-port", "1431"));
const TOKEN = "uitextfit";
const PYTHON = process.env.PYTHON || (existsSync(path.join(ROOT, ".venv", "bin", "python3")) ? path.join(ROOT, ".venv", "bin", "python3") : "python");

const UI = `http://127.0.0.1:${UI_PORT}`;
const API = `http://127.0.0.1:${API_PORT}`;
const children = [];

function cleanup() {
  for (const child of children) {
    try { child.kill("SIGTERM"); } catch { /* 已退出 */ }
  }
}
process.on("exit", cleanup);
process.on("SIGINT", () => { cleanup(); process.exit(2); });

async function api(method, url, body) {
  const response = await fetch(`${API}${url}`, {
    method,
    headers: { "X-Translator-Token": TOKEN, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${method} ${url} → ${response.status} ${await response.text()}`);
  return response.json().catch(() => ({}));
}

async function waitFor(url, label, timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { headers: { "X-Translator-Token": TOKEN } });
      if (response.status < 500) return;
    } catch { /* 还没起来 */ }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error(`${label} 没有在 ${timeoutMs / 1000} 秒内启动`);
}

async function startServers(root) {
  const log = openSync(path.join(OUT, "backend.log"), "w");
  const server = spawn(PYTHON, [path.join(ROOT, "scripts", "ui_text_fit_server.py"), "--root", root, "--port", String(API_PORT), "--token", TOKEN, "--origin", UI], {
    cwd: ROOT,
    stdio: ["ignore", "pipe", log],
  });
  children.push(server);
  const info = await new Promise((resolve, reject) => {
    const lines = createInterface({ input: server.stdout });
    lines.once("line", (line) => {
      try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
    });
    server.once("exit", (code) => reject(new Error(`后端提前退出（${code}）`)));
  });
  const vite = spawn(process.execPath, [path.join(ROOT, "ui", "node_modules", "vite", "bin", "vite.js"), "--host", "127.0.0.1", "--port", String(UI_PORT), "--strictPort"], {
    cwd: path.join(ROOT, "ui"),
    env: { ...process.env, VITE_DEV_SIDECAR_PORT: String(API_PORT), VITE_DEV_SIDECAR_TOKEN: TOKEN },
    stdio: ["ignore", "ignore", "inherit"],
  });
  children.push(vite);
  await waitFor(`${API}/api/settings`, "后端");
  await waitFor(UI, "界面开发页");
  return info;
}

// ---------------------------------------------------------------------------
// 在页面里找问题。返回 { issues, hscroll, docOverflow }。
// ---------------------------------------------------------------------------
function detect() {
  const issues = [];
  const seen = new Set();
  // 量提示文字（placeholder）要多宽：照输入框的字体排一行看不见的字。
  const ruler = document.createElement("span");
  ruler.style.cssText = "position:absolute;visibility:hidden;white-space:pre;left:-9999px;top:0";
  document.body.append(ruler);
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) return false;
    if (rect.bottom < 0 || rect.right < 0 || rect.top > innerHeight || rect.left > innerWidth) return false;
    const style = getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none") return false;
    for (let node = el; node && node !== document.body; node = node.parentElement) {
      if (Number(getComputedStyle(node).opacity) === 0 || node.hidden || node.getAttribute("aria-hidden") === "true") return false;
    }
    return true;
  };
  const describe = (el) => {
    const parts = [];
    for (let node = el, depth = 0; node && node !== document.body && depth < 4; node = node.parentElement, depth += 1) {
      const cls = typeof node.className === "string" && node.className.trim() ? `.${node.className.trim().split(/\s+/).slice(0, 2).join(".")}` : "";
      parts.unshift(node.tagName.toLowerCase() + cls);
    }
    return parts.join(" > ");
  };
  const inlineText = (el) => [...el.childNodes].some((node) =>
    (node.nodeType === 3 && node.textContent.trim())
    || (node.nodeType === 1 && getComputedStyle(node).display === "inline" && node.textContent.trim() && !node.closest("svg")));
  const add = (kind, el, extra = {}) => {
    const text = (el.tagName === "INPUT" ? (el.value || el.placeholder) : el.textContent).replace(/\s+/g, " ").trim().slice(0, 100);
    const key = `${kind}|${text}|${describe(el)}`;
    if (seen.has(key)) return;
    seen.add(key);
    issues.push({ kind, text, path: describe(el), ...extra });
  };

  // 开着弹窗或搜索面板时只查它：后面被遮住的页面在别的画面里已经查过。
  const layers = [...document.querySelectorAll(".overlay, .cmdk")].filter(visible);
  const scope = layers.length ? layers[layers.length - 1] : document.body;
  for (const el of scope.querySelectorAll("*")) {
    if (el.closest("svg, script, style, canvas")) continue;
    const isInput = el.tagName === "INPUT" && ["text", "search", ""].includes(el.type);
    if (!isInput && !inlineText(el)) continue;
    const style = getComputedStyle(el);
    if (style.display === "inline" || style.display === "contents") continue;
    if (!visible(el)) continue;

    if (isInput) {
      // 可输入下拉框没聚焦时显示的是上面那层会折行的 .combo-text，输入框本身的字是透明的。
      if (el.classList.contains("combo-input") && document.activeElement !== el) continue;
      const box = el.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      if (!el.value && el.placeholder) {
        ruler.style.font = style.font;
        ruler.style.letterSpacing = style.letterSpacing;
        ruler.textContent = el.placeholder;
        const need = Math.ceil(ruler.getBoundingClientRect().width);
        if (need > box + 1) add("提示文字放不下", el, { need, have: Math.floor(box) });
      }
      // 输入框里用户自己输入或粘贴的路径可以横着滚动看，不算问题。
      continue;
    }

    const overX = el.scrollWidth > el.clientWidth + 1;
    const overY = el.scrollHeight > el.clientHeight + 2;
    const clipsX = ["hidden", "clip"].includes(style.overflowX);
    const clipsY = ["hidden", "clip"].includes(style.overflowY);
    const clip = el.closest(".fit-clip");
    if (overX && (clipsX || style.textOverflow === "ellipsis")) {
      if (clip) {
        // 内容可以截断，但要能悬停看全文：模拟一次鼠标移入，看有没有 title。
        clip.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
        const titled = el.closest("[title]");
        if (!titled || !titled.getAttribute("title").trim()) add("内容截断但悬停看不到全文", el, { need: el.scrollWidth, have: el.clientWidth });
      } else {
        add(style.textOverflow === "ellipsis" ? "名称被截断（…）" : "名称被裁掉", el, { need: el.scrollWidth, have: el.clientWidth });
      }
    } else if (overY && clipsY && !clip && style.overflowY !== "auto") {
      add("名称被裁掉（上下）", el, { need: el.scrollHeight, have: el.clientHeight });
    } else if (overX && style.overflowX === "visible" && !clip) {
      add("文字伸出框外", el, { need: el.scrollWidth, have: el.clientWidth });
    }
  }

  const hscroll = [];
  for (const el of document.querySelectorAll(".ws-side, .ws-side *")) {
    const style = getComputedStyle(el);
    if (["auto", "scroll"].includes(style.overflowX) && el.scrollWidth > el.clientWidth + 1 && visible(el)) hscroll.push(describe(el));
  }
  const docOverflow = document.documentElement.scrollWidth > document.documentElement.clientWidth + 1;
  ruler.remove();
  return { issues, hscroll, docOverflow };
}

// ---------------------------------------------------------------------------
// 走一遍各个画面
// ---------------------------------------------------------------------------
const report = { generated: new Date().toISOString(), runs: [] };

async function settle(page, ms = 500) {
  await page.waitForLoadState("networkidle").catch(() => undefined);
  await page.waitForTimeout(ms);
}

/** 关掉弹窗：先按 Esc；关不掉就点图文指引的 ×，或弹窗底部第一个次要按钮（取消、跳过）。 */
async function closeOverlays(page) {
  if (await page.locator(".cmdk:visible").count()) {
    await page.keyboard.press("Escape");
    await page.locator(".cmdk").first().waitFor({ state: "hidden", timeout: 3000 }).catch(() => undefined);
  }
  for (let i = 0; i < 4 && await page.locator(".overlay").count(); i += 1) {
    await page.keyboard.press("Escape");
    await page.waitForTimeout(250);
    if (!await page.locator(".overlay").count()) break;
    const close = page.locator(".overlay .g-x, .overlay .macts button:not(.pri)").first();
    const target = await close.count() ? close : page.locator(".overlay .macts button").last();
    await target.click({ timeout: 1500 }).catch(() => undefined);
    await page.waitForTimeout(300);
  }
}

async function expandFolds(page) {
  const folds = page.locator(".fold-h[aria-expanded='false']");
  for (let i = await folds.count() - 1; i >= 0; i -= 1) {
    await folds.nth(i).click({ timeout: 1000 }).catch(() => undefined);
  }
  await page.waitForTimeout(250);
}

async function sideItem(page, index) {
  return page.locator(".side .side-item").nth(index);
}

function makeRun(locale, size, pseudo) {
  const run = { locale: pseudo ? `${locale}+30%` : locale, size: size.tag, blocking: !pseudo, states: {} };
  report.runs.push(run);
  return run;
}

/** 一个画面出错（找不到按钮、超时）只记成这一项红，不让整次检查中断。 */
async function step(run, name, action) {
  try {
    await action();
  } catch (error) {
    run.states[name] = { issues: [{ kind: "检查出错", text: String(error?.message ?? error).split("\n")[0].slice(0, 160), path: "" }], hscroll: [], docOverflow: false };
    console.log(`✗ ${run.locale}/${run.size} ${name} 检查出错：${run.states[name].issues[0].text}`);
  }
}

async function scan(page, run, name) {
  const result = await page.evaluate(detect);
  run.states[name] = result;
  const bad = result.issues.length || result.hscroll.length || result.docOverflow;
  if (bad) {
    const dir = path.join(OUT, "shots");
    mkdirSync(dir, { recursive: true });
    const file = `${run.locale.replace("+30%", "-long")}_${run.size}_${name.replace(/[^\w.-]+/g, "_")}.png`;
    await page.screenshot({ path: path.join(dir, file) }).catch(() => undefined);
    result.screenshot = `shots/${file}`;
  }
  const counts = result.issues.length ? ` ${result.issues.length} 处` : "";
  console.log(`${bad ? (run.blocking ? "✗" : "△") : "✓"} ${run.locale}/${run.size} ${name}${counts}${result.hscroll.length ? " 右栏横向滚动" : ""}${result.docOverflow ? " 整页横向滚动" : ""}`);
}

/** 点遍当前页上的子页（设置左侧小导航）和页签，每个都查一次。 */
async function scanTabs(page, run, base) {
  const navs = page.locator(".set-nav .si");
  const navCount = await navs.count();
  for (let i = 0; i < navCount; i += 1) {
    await navs.nth(i).click({ timeout: 2000 }).catch(() => undefined);
    await settle(page, 400);
    await expandFolds(page);
    await scan(page, run, `${base}_子页${i + 1}`);
  }
  const tabs = page.locator(".content [role='tab']:visible");
  const tabCount = Math.min(await tabs.count(), 8);
  for (let i = 1; i < tabCount; i += 1) {
    await tabs.nth(i).click({ timeout: 2000 }).catch(() => undefined);
    await settle(page, 400);
    await expandFolds(page);
    await scan(page, run, `${base}_页签${i + 1}`);
  }
}

async function openPage(page, index) {
  await closeOverlays(page);
  await (await sideItem(page, index)).click();
  await settle(page, 700);
  await closeOverlays(page);
  await expandFolds(page);
}

async function newPage(browser, locale, size, pseudo) {
  await api("PUT", "/api/settings", { appearance: { ui_language: locale } });
  const context = await browser.newContext({ viewport: { width: size.w, height: size.h }, deviceScaleFactor: 1, reducedMotion: "reduce" });
  await context.addInitScript(([value, long]) => {
    try {
      localStorage.setItem("translator.locale", value);
      if (long) localStorage.setItem("translator.pseudo-long", "1");
    } catch { /* 无痕模式 */ }
  }, [locale, pseudo]);
  const page = await context.newPage();
  page.on("dialog", (dialog) => dialog.dismiss().catch(() => undefined));
  await page.goto(`${UI}/`, { waitUntil: "networkidle" });
  await settle(page, 1200);
  return { context, page };
}

async function sideCount(page) {
  return page.locator(".side .side-item").count();
}

async function sideLabel(page, index) {
  return ((await (await sideItem(page, index)).innerText()).trim().split("\n")[0] || `item${index}`).slice(0, 24);
}

/** 第一轮：刚装好、还没配模型时的样子（快速开始、各页空白状态）。 */
async function blankStates(browser, locale, size, pseudo) {
  const run = makeRun(locale, size, pseudo);
  await api("PUT", "/api/updates/preferences", { quick_start_completed: false }).catch(() => undefined);
  const { context, page } = await newPage(browser, locale, size, pseudo);
  if (await page.locator(".overlay").count()) await scan(page, run, "00_快速开始");
  await closeOverlays(page);
  const count = await sideCount(page);
  for (let i = 0; i < count; i += 1) {
    const label = `${String(i).padStart(2, "0")}_${await sideLabel(page, i)}`;
    await openPage(page, i);
    await scan(page, run, label);
    await scanTabs(page, run, label);
  }
  await context.close();
  return run;
}

/** 配好假模型：设置页出现连接卡片、模型名；工作区可以开始翻译。 */
async function configureModel(info) {
  await api("PUT", "/api/models/roles/translation", { mode: "cloud", provider: "custom_openai", model: "fake-model", base_url: info.model_url });
  await api("PUT", "/api/keys/custom_openai", { api_key: "fake-key", base_url: info.model_url });
}

async function addSource(page, folder) {
  const input = page.locator(".ws-main .src input").first();
  await input.fill(folder);
  await input.press("Enter");
  await page.locator(".content .ws-files").first().waitFor({ state: "visible", timeout: 20000 });
  await settle(page, 600);
}

/** 第二轮：配好模型、加了文件、翻译完一次以后的各个画面和弹窗。 */
let workedRound = 0;
async function workedStates(browser, info, root, locale, size, pseudo) {
  const run = makeRun(locale, size, pseudo);
  // 每轮用一份新的样例：上一轮的译文输出文件夹会让这一轮先问「续译还是重来」。
  workedRound += 1;
  const samples = {};
  for (const [kind, folder] of Object.entries(info.samples)) {
    samples[kind] = path.join(root, `round-${workedRound}`, kind);
    cpSync(folder, samples[kind], { recursive: true });
  }
  const { context, page } = await newPage(browser, locale, size, pseudo);
  await closeOverlays(page);
  const count = await sideCount(page);
  // 侧栏顺序：0 翻译（首页） 1 Excel 2 Word 3 PPT 4 PDF 5 CAD 6 任务 7 记忆库 8 扩展 … 倒数第三 设置、倒数第一 反馈
  const pages = { home: 0, excel: 1, word: 2, pdf: 4, tasks: 6, settings: count - 3, feedback: count - 1 };

  await step(run, "10_Word_已添加文件", async () => {
    await openPage(page, pages.word);
    await addSource(page, samples.word);
    await expandFolds(page);
    await scan(page, run, "10_Word_已添加文件");
  });

  let finished = false;
  await step(run, "11_Excel_已添加文件", async () => {
    await openPage(page, pages.excel);
    await addSource(page, samples.excel);
    await scan(page, run, "11_Excel_已添加文件");
  });
  await step(run, "12_Excel_翻译完成", async () => {
    await page.locator(".ws-side .btn.pri.big").last().click({ timeout: 5000 });
    await page.waitForTimeout(800);
    // 开始前的确认弹窗（续译、费用提示之类）：点主按钮继续。
    if (await page.locator(".overlay .modal").count()) await page.locator(".overlay .modal .btn.pri").first().click({ timeout: 2000 });
    await page.locator(".content .res").first().waitFor({ state: "visible", timeout: 90000 });
    finished = true;
    await settle(page, 1500);
    await expandFolds(page);
    await scan(page, run, "12_Excel_翻译完成");
  });

  if (finished) {
    await step(run, "13_弹窗_反馈此任务", async () => {
      // 结果横幅上的「反馈此任务」（task-feedback.ts，按钮带 data-action）。
      const button = page.locator(".content .res button").filter({ hasText: /反馈|feedback|Signaler|retour/i }).first();
      await button.click({ timeout: 3000 });
      await page.locator(".overlay .modal").first().waitFor({ state: "visible", timeout: 5000 });
      await settle(page, 600);
      await scan(page, run, "13_弹窗_反馈此任务");
      await closeOverlays(page);
    });
  }

  for (const [name, index] of [["14_任务", pages.tasks], ["15_首页", pages.home], ["16_反馈", pages.feedback], ["17_设置", pages.settings]]) {
    await step(run, name, async () => {
      await openPage(page, index);
      await scan(page, run, name);
      if (name === "17_设置") await scanTabs(page, run, name);
    });
  }

  await step(run, "18_搜索面板", async () => {
    await closeOverlays(page);
    await page.locator(".side-search").click();
    await settle(page, 400);
    await page.keyboard.type("Rapport");
    await settle(page, 800);
    await scan(page, run, "18_搜索面板");
    await closeOverlays(page);
  });

  await step(run, "19_弹窗_PDF图文指引", async () => {
    // 「翻译方式」问号里的「查看图文说明」。
    await openPage(page, pages.pdf);
    await page.locator(".ws-side .selrow .hint").first().click({ timeout: 3000 });
    await settle(page, 300);
    await page.locator(".tip .tip-link").first().click({ timeout: 3000 });
    await page.locator(".overlay").first().waitFor({ state: "visible", timeout: 5000 });
    await settle(page, 800);
    await scan(page, run, "19_弹窗_PDF图文指引");
    await closeOverlays(page);
  });
  await context.close();
  return run;
}

function summarize() {
  const lines = ["# 界面文字检查报告", "", `生成时间：${report.generated}`, ""];
  let red = 0;
  let amber = 0;
  for (const run of report.runs) {
    for (const [state, result] of Object.entries(run.states)) {
      const problems = [
        ...result.issues.map((issue) => `${issue.kind}：「${issue.text}」 ${issue.need ?? ""}/${issue.have ?? ""}px · ${issue.path}`),
        ...result.hscroll.map((where) => `右栏横向滚动：${where}`),
        ...(result.docOverflow ? ["整页横向滚动"] : []),
      ];
      if (!problems.length) continue;
      if (run.blocking) red += problems.length;
      else amber += problems.length;
      lines.push(`## ${run.blocking ? "✗" : "△ 只报告"} ${run.locale} · ${run.size} · ${state}`, "");
      if (result.screenshot) lines.push(`截图：${result.screenshot}`, "");
      for (const problem of problems) lines.push(`- ${problem}`);
      lines.push("");
    }
  }
  lines.splice(4, 0, `红项 ${red} 处（中英法）；法文加长 30% 的报告项 ${amber} 处（不拦合并）。`, "");
  return { text: lines.join("\n"), red, amber };
}

async function main() {
  rmSync(OUT, { recursive: true, force: true });
  mkdirSync(OUT, { recursive: true });
  const root = mkdtempSync(path.join(tmpdir(), "ui-text-fit-"));
  let exitCode = 2;
  try {
    const info = await startServers(root);
    const browser = await chromium.launch();
    try {
      for (const locale of LOCALES) for (const size of SIZES) await blankStates(browser, locale, size, false);
      if (PSEUDO) for (const size of SIZES) await blankStates(browser, "fr", size, true);
      await configureModel(info);
      for (const locale of LOCALES) for (const size of SIZES) await workedStates(browser, info, root, locale, size, false);
      if (PSEUDO) for (const size of SIZES) await workedStates(browser, info, root, "fr", size, true);
    } finally {
      await browser.close();
    }
    exitCode = 0;
  } catch (error) {
    console.error(error);
  } finally {
    const { text, red, amber } = summarize();
    writeFileSync(path.join(OUT, "report.md"), text);
    writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 1));
    console.log(`\n红项 ${red} 处；法文加长 30% 报告项 ${amber} 处。报告：${path.join(OUT, "report.md")}`);
    if (exitCode === 0 && red) exitCode = 1;
    cleanup();
    rmSync(root, { recursive: true, force: true });
  }
  process.exit(exitCode);
}

await main();
