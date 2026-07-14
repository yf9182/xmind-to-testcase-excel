#!/usr/bin/env node
/** Build and verify a test-case workbook from validated JSON. */

import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";


const MAX_CELL_CHARS = 32_767;
const INVALID_EXCEL_CHARS = /[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g;

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--overwrite") {
      values.overwrite = true;
      continue;
    }
    if (!["--input", "--output", "--preview"].includes(token)) {
      throw new Error(`未知参数：${token}`);
    }
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) {
      throw new Error(`${token} 缺少参数值`);
    }
    values[token.slice(2)] = value;
    index += 1;
  }
  for (const required of ["input", "output", "preview"]) {
    if (!values[required]) {
      throw new Error(`缺少必需参数：--${required}`);
    }
  }
  if (path.extname(values.output).toLowerCase() !== ".xlsx") {
    throw new Error("--output 必须使用 .xlsx 扩展名");
  }
  if (path.extname(values.preview).toLowerCase() !== ".png") {
    throw new Error("--preview 必须使用 .png 扩展名");
  }
  return values;
}

async function exists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function collisionSafePath(requestedPath, overwrite) {
  if (overwrite || !(await exists(requestedPath))) {
    return requestedPath;
  }
  const extension = path.extname(requestedPath);
  const stem = requestedPath.slice(0, -extension.length);
  for (let index = 2; index < 10_000; index += 1) {
    const candidate = `${stem}-${index}${extension}`;
    if (!(await exists(candidate))) {
      return candidate;
    }
  }
  throw new Error(`无法为输出文件找到可用名称：${requestedPath}`);
}

function validatePayload(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("输入 JSON 顶层必须是对象");
  }
  const topKeys = Object.keys(payload).sort();
  if (JSON.stringify(topKeys) !== JSON.stringify(["columns", "test_cases"])) {
    throw new Error("输入 JSON 顶层必须且只能包含 columns 和 test_cases");
  }
  const { columns, test_cases: testCases } = payload;
  if (!Array.isArray(columns) || columns.length === 0) {
    throw new Error("columns 必须是非空数组");
  }
  if (!columns.every((column) => typeof column === "string" && column.trim() === column && column)) {
    throw new Error("每个列名必须是首尾无空格的非空字符串");
  }
  if (new Set(columns).size !== columns.length) {
    throw new Error("columns 中存在重复列名");
  }
  if (!Array.isArray(testCases) || testCases.length === 0) {
    throw new Error("test_cases 必须是非空数组");
  }
  const expected = [...columns].sort();
  for (let index = 0; index < testCases.length; index += 1) {
    const row = testCases[index];
    if (!row || typeof row !== "object" || Array.isArray(row)) {
      throw new Error(`第 ${index + 1} 条用例不是对象`);
    }
    if (JSON.stringify(Object.keys(row).sort()) !== JSON.stringify(expected)) {
      throw new Error(`第 ${index + 1} 条用例字段与 columns 不一致`);
    }
    if (!columns.every((column) => typeof row[column] === "string")) {
      throw new Error(`第 ${index + 1} 条用例的所有值必须是字符串`);
    }
  }
  return { columns, testCases };
}

function safeExcelText(rawValue) {
  let value = rawValue.replace(INVALID_EXCEL_CHARS, "");
  if (value.length > MAX_CELL_CHARS) {
    value = `${value.slice(0, MAX_CELL_CHARS - 1)}…`;
  }
  if (["=", "+", "-", "@"].some((prefix) => value.startsWith(prefix))) {
    value = `'${value}`;
  }
  return value;
}

function displayWidth(value) {
  let width = 0;
  for (const character of value) {
    width += character.codePointAt(0) > 0xff ? 2 : 1;
  }
  return width;
}

function semanticWidth(column, values) {
  const preferred = {
    用例ID: 14,
    模块: 22,
    功能点: 28,
    用例标题: 36,
    前置条件: 30,
    测试步骤: 48,
    测试数据: 30,
    预期结果: 42,
    优先级: 12,
    用例类型: 16,
    备注: 32,
  }[column];
  if (preferred) {
    return preferred;
  }
  const widest = Math.max(displayWidth(column), ...values.slice(0, 100).map(displayWidth));
  return Math.min(48, Math.max(12, widest + 3));
}

function estimatedLines(value, width) {
  const usableWidth = Math.max(1, Math.floor(width) - 2);
  return value.split("\n").reduce(
    (total, line) => total + Math.max(1, Math.ceil(displayWidth(line) / usableWidth)),
    0,
  );
}

function columnName(index) {
  let value = index;
  let output = "";
  while (value > 0) {
    value -= 1;
    output = String.fromCharCode(65 + (value % 26)) + output;
    value = Math.floor(value / 26);
  }
  return output;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const inputText = await fs.readFile(args.input, "utf8");
  const { columns, testCases } = validatePayload(JSON.parse(inputText));
  const outputPath = await collisionSafePath(path.resolve(args.output), args.overwrite);
  const previewPath = await collisionSafePath(path.resolve(args.preview), args.overwrite);
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.mkdir(path.dirname(previewPath), { recursive: true });

  const safeRows = testCases.map((testCase) => columns.map((column) => safeExcelText(testCase[column])));
  const widths = columns.map((column, columnIndex) =>
    semanticWidth(column, safeRows.map((row) => row[columnIndex])),
  );
  const matrix = [columns, ...safeRows];
  const rowCount = matrix.length;
  const columnCount = columns.length;
  const lastColumn = columnName(columnCount);

  const workbook = Workbook.create();
  const sheet = workbook.worksheets.add("测试用例");
  sheet.showGridLines = false;

  const usedRange = sheet.getRangeByIndexes(0, 0, rowCount, columnCount);
  usedRange.values = matrix;
  const table = sheet.tables.add(`A1:${lastColumn}${rowCount}`, true, "TestCasesTable");
  table.style = "TableStyleMedium2";
  table.showFilterButton = true;

  const header = sheet.getRangeByIndexes(0, 0, 1, columnCount);
  header.format = {
    fill: "#1F4E78",
    font: { bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
  };
  header.format.rowHeight = 28;

  const body = sheet.getRangeByIndexes(1, 0, testCases.length, columnCount);
  body.format = {
    font: { color: "#1F2937" },
    verticalAlignment: "top",
    wrapText: true,
    borders: {
      insideHorizontal: { style: "hair", color: "#D9E2F3" },
    },
  };

  for (let columnIndex = 0; columnIndex < columnCount; columnIndex += 1) {
    sheet.getRangeByIndexes(0, columnIndex, rowCount, 1).format.columnWidth = widths[columnIndex];
  }
  for (let rowIndex = 0; rowIndex < safeRows.length; rowIndex += 1) {
    const lines = Math.max(
      1,
      ...safeRows[rowIndex].map((value, columnIndex) => estimatedLines(value, widths[columnIndex])),
    );
    sheet.getRangeByIndexes(rowIndex + 1, 0, 1, columnCount).format.rowHeight = Math.min(
      360,
      Math.max(24, lines * 18),
    );
  }

  const priorityIndex = columns.indexOf("优先级");
  if (priorityIndex >= 0) {
    const priorityRange = sheet.getRangeByIndexes(1, priorityIndex, testCases.length, 1);
    priorityRange.dataValidation = {
      rule: { type: "list", values: ["P0", "P1", "P2", "P3"] },
    };
    const priorityStyles = [
      ["P0", "#F4CCCC", "#9C0006"],
      ["P1", "#FCE5CD", "#9C5700"],
      ["P2", "#FFF2CC", "#7F6000"],
    ];
    for (const [priority, fill, color] of priorityStyles) {
      priorityRange.conditionalFormats.add("cellIs", {
        operator: "equal",
        formula: `"${priority}"`,
        format: { fill, font: { color, bold: true } },
      });
    }
  }

  // Apply panes after table and formatting mutations; some facade operations
  // rebuild the worksheet view and would otherwise discard an earlier freeze.
  await sheet.freezePanes.freezeRows(1);

  const inspection = await workbook.inspect({
    kind: "workbook,sheet,table",
    maxChars: 5_000,
    tableMaxRows: Math.min(12, rowCount),
    tableMaxCols: Math.min(12, columnCount),
    tableMaxCellChars: 100,
  });
  const formulaErrors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 100 },
    summary: "final formula error scan",
  });

  const previewRows = Math.min(rowCount, 25);
  const preview = await workbook.render({
    sheetName: "测试用例",
    range: `A1:${lastColumn}${previewRows}`,
    scale: 1,
    format: "png",
  });
  await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));

  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(outputPath);

  console.log(JSON.stringify({
    output: outputPath,
    preview: previewPath,
    sheet: "测试用例",
    column_count: columnCount,
    case_count: testCases.length,
    inspection: inspection.ndjson,
    formula_errors: formulaErrors.ndjson,
  }));
}

main().catch((error) => {
  console.error(`Excel 生成失败：${error instanceof Error ? error.message : String(error)}`);
  process.exitCode = 1;
});
