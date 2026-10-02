/** Run through an initialized, hidden cua Tab with an isolated, local-mock sidecar.
 * Fixtures: excel/, word/, pdf/ (three files each), pdf-single/单文件样本.pdf.
 * Never run against a real provider or user data. No native Office/Translator window is needed.
 */
import assert from "node:assert/strict";
import { writeFile, mkdir } from "node:fs/promises";

export async function runWorkspaceStatusRegression(tab, { samples, artifacts }) {
  await mkdir(artifacts, { recursive: true });
  const checks = [];
  const names = { excel: "Excel 表格", word: "Word 文档", pdf: "PDF 与图片" };
  const sidebar = (name) => tab.playwright.locator(".side-item").filter({ hasText: name });
  const button = (name) => tab.playwright.getByRole("button", { name, exact: true });
  const observe = () => tab.playwright.domSnapshot();
  async function capture(name, required = [], forbidden = []) {
    const state = await observe();
    for (const value of required) assert.ok(state.includes(value), `${name}: missing ${value}`);
    for (const value of forbidden) assert.ok(!state.includes(value), `${name}: retained ${value}`);
    assert.ok(!state.includes("排队中"), `${name}: old queue wording`);
    await writeFile(`${artifacts}/${name}.txt`, state);
    await tab.playwright.locator(".toast").last().waitFor({ state: "hidden", timeoutMs: 15000 });
    await writeFile(`${artifacts}/${name}.jpg`, await tab.getScreenshot({ emit: false }));
    checks.push({ name, passed: true });
  }
  async function scan(path) {
    await tab.playwright.locator(".srcbar input").fill(path);
    await observe();
    await tab.playwright.getByRole("button", { name: /^(扫描|重新扫描)$/ }).click();
    await tab.playwright.getByRole("button", { name: /^开始翻译（/ }).waitFor({ state: "visible", timeoutMs: 15000 });
    await observe();
    if (await button("全部重新翻译").count()) {
      await button("全部重新翻译").click();
      await observe();
    }
  }
  async function run(surface, path, count, label) {
    await sidebar(names[surface]).click();
    await observe();
    await scan(path);
    await button(`开始翻译（${count} 个文件）`).click();
    await button("查看完整报告").waitFor({ state: "visible", timeoutMs: 30000 });
    await capture(`${label}_completed`, [`已生成 ${count} 个文件`]);
    await button("查看完整报告").click();
    await observe();
    const taskId = await tab.playwright.locator(".tkcard.sel").getAttribute("data-task-id");
    assert.ok(taskId, "selected task must have a stable identity");
    await sidebar(names[surface]).click();
    await observe();
    return taskId;
  }
  async function freshRescan(label) {
    await button("重新扫描").click();
    await button("全部重新翻译").waitFor({ state: "visible", timeoutMs: 15000 });
    await button("全部重新翻译").click();
    await observe();
    const table = await tab.playwright.getByRole("table").innerText();
    assert.equal((table.match(/未开始/g) ?? []).length, 3);
    assert.ok(!table.includes("已生成") && !table.includes("需复核"));
    await capture(label, ["本次将全部重新翻译"], ["已生成"]);
  }

  const excel = await run("excel", `${samples}/excel`, 3, "suite_excel");
  await freshRescan("suite_excel_rescan");
  const word = await run("word", `${samples}/word`, 3, "suite_word");
  await freshRescan("suite_word_rescan");
  const batch = await run("pdf", `${samples}/pdf`, 3, "suite_pdf_batch");
  await tab.playwright.locator(".srcbar input").fill(`${samples}/pdf-single/单文件样本.pdf`);
  await capture("suite_pdf_change_source", [], ["任务已结束", "3 页全部通过", "已生成 3 个文件"]);
  const single = await run("pdf", `${samples}/pdf-single/单文件样本.pdf`, 1, "suite_pdf_single");

  for (const [surface, taskId, expected, suffix] of [
    ["pdf", batch, 3, "/pdf"], ["pdf", single, 1, "/pdf-single/单文件样本.pdf"],
    ["excel", excel, 3, "/excel"], ["word", word, 3, "/word"],
  ]) {
    await sidebar("任务中心").click();
    await observe();
    await tab.playwright.locator(`.tkcard[data-task-id="${taskId}"]`).click();
    await observe();
    await button("打开工作区").click();
    await button("查看完整报告").waitFor({ state: "visible", timeoutMs: 15000 });
    await capture(`suite_history_${surface}_${expected}`, [`已生成 ${expected} 个文件`]);
    assert.equal(await tab.playwright.locator(".srcbar input").evaluate((el) => el.value), samples + suffix);
    if (surface === "pdf" && expected === 3) {
      assert.equal(await tab.playwright.getByRole("button", { name: /3 页全部通过/ }).count(), 3);
    }
  }
  await writeFile(`${artifacts}/checks.json`, JSON.stringify(checks, null, 2));
  return { checks: checks.length, tasks: { excel, word, batch, single } };
}
