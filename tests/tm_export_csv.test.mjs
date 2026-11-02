// 记忆库「导出 CSV」的写出端：ui/src/views/library.ts 的 csvCell / toTmCsv。
// 运行：node tests/tm_export_csv.test.mjs
//
// 审计 CODE_AUDIT_2026-08-31（M1「没来得及验」第 1 项）只验到
// get_all_entries_for_export / get_full_export 返回的数据是对的，写进文件的
// 格式没查。导出的第一去处是 Excel：多行译文不包引号会被拆成好几行，译文里
// 本来带双引号时还要翻倍。这里直接加载生产代码里的两个函数来断言，不另抄一份
// 实现；读回来的往返校验在 tests/test_tm_export_coverage.py 里做。
import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { stripTypeScriptTypes } from "node:module";

const libraryPath = fileURLToPath(new URL("../ui/src/views/library.ts", import.meta.url));

/** 从源码里截出一个顶层函数（含配对的右花括号）。 */
function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `library.ts 里找不到 function ${name}`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    const char = source[index];
    if (char === "{") depth += 1;
    else if (char === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`function ${name} 的花括号不配对`);
}

const source = fs.readFileSync(libraryPath, "utf8");
const extracted = `${functionSource(source, "csvCell")}\n${functionSource(source, "toTmCsv")}\n`
  + "export { csvCell, toTmCsv };\n";
const moduleUrl = "data:text/javascript;base64,"
  + Buffer.from(stripTypeScriptTypes(extracted), "utf8").toString("base64");
const { csvCell, toTmCsv } = await import(moduleUrl);

// csvCell：只在真正需要时加引号，已有的双引号翻倍。
assert.equal(csvCell("普通译文"), "普通译文", "没有特殊字符就不加引号");
assert.equal(csvCell(0), "0", "0 不能被当成空值");
assert.equal(csvCell(null), "", "空值写成空串");
assert.equal(csvCell(undefined), "", "undefined 写成空串");
assert.equal(csvCell("a,b"), '"a,b"', "逗号要加引号");
assert.equal(csvCell('say "hi"'), '"say ""hi"""', "双引号翻倍");
assert.equal(csvCell("第一行\n第二行"), '"第一行\n第二行"', "换行要加引号");
assert.equal(csvCell("第一行\r\n第二行"), '"第一行\r\n第二行"', "CRLF 也算换行");

const entries = [
  {
    source_text: "多行单元格",
    target_text: "第一行\n第二行\n第三行",
    word_type: "auto",
    pinned: 0,
    updated_at: "2026-10-02 10:00:00",
  },
  {
    source_text: '带"引号"的原文',
    target_text: 'He said "ok", then left.',
    word_type: "manual",
    pinned: 1,
    updated_at: "2026-10-02 10:01:00",
  },
  {
    source_text: "普通",
    target_text: "plain",
    word_type: "auto",
    pinned: 0,
    updated_at: "2026-10-02 10:02:00",
  },
];
const csv = toTmCsv(entries);

// 这里只测 toTmCsv 这一层：它自己带一个 BOM（少了它 Excel 按本地编码打开，中文整份乱码）。
// 注意调用方 exportTm() 又在外面拼了一个 `\ufeff${toTmCsv(list)}`，真正写盘的文件目前是
// 两个 BOM——那是本模块之外的已知问题（见 REPORT.md「发现的问题」），不在这个文件的断言里，
// 否则这里就得钉住一个错误的行为。
assert.ok(csv.startsWith("\uFEFF"), "toTmCsv 的输出必须以 UTF-8 BOM 开头");
assert.equal((csv.match(/\uFEFF/g) || []).length, 1, "toTmCsv 只该带一个 BOM");
assert.ok(csv.includes('"第一行\n第二行\n第三行"'), "多行译文整段包在引号里，换行原样保留");
assert.ok(csv.includes('"He said ""ok"", then left."'), "译文里的双引号翻倍");
assert.ok(csv.includes('"带""引号""的原文"'), "原文里的双引号同样翻倍");

// 行分隔统一 CRLF（Excel 认这个），但引号内的换行不能被一起换成 CRLF——
// 那是译文内容的一部分，改了就还原不出原来的多行。
const body = csv.replace(/^\uFEFF/, "");
const rows = body.split("\r\n");
assert.equal(rows.at(-1), "", "整份文件以 CRLF 结尾");
assert.equal(rows.length - 1, entries.length + 1, "表头 + 每条词条一行，没被译文里的换行拆碎");
assert.equal(rows[0], "source_text,target_text,word_type,pinned,updated_at");
assert.equal(rows[1], '多行单元格,"第一行\n第二行\n第三行",auto,0,2026-10-02 10:00:00');

// 输出给 tests/test_tm_export_coverage.py 做读回往返校验。
process.stdout.write(`${JSON.stringify({ csv, entries })}\n`);
