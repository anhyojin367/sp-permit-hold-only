import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";


const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.resolve(SCRIPT_DIR, "..");
const DEFAULT_CATALOG = path.join(PROJECT_ROOT, "sp_pdf_judger", "validation_audit_catalog.json");
const DEFAULT_OUTPUT = path.join(PROJECT_ROOT, "outputs", "SP_검증_회귀_종합.xlsx");
const DEFAULT_ARTIFACT_MODULE =
  "C:\\Users\\User\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\@oai\\artifact-tool\\dist\\artifact_tool.mjs";

const COLORS = {
  navy: "#1F4E78",
  blue: "#2F75B5",
  paleBlue: "#D9EAF7",
  paleGreen: "#DCFCE7",
  green: "#15803D",
  paleAmber: "#FEF3C7",
  amber: "#B45309",
  paleRed: "#FEE2E2",
  red: "#B91C1C",
  paleGray: "#F3F4F6",
  gray: "#64748B",
  border: "#CBD5E1",
  white: "#FFFFFF",
};

const FONT = "Malgun Gothic";
const EXPECTED_OPTIONS = ["충족", "불충족", "보류", "해당없음", "미설정"];
const IMPLEMENTATION_ORDER = ["구현", "부분 구현", "미구현"];


function parseArgs(argv) {
  const args = { catalog: DEFAULT_CATALOG, output: DEFAULT_OUTPUT, input: "" };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--catalog") args.catalog = path.resolve(argv[++index]);
    else if (token === "--output") args.output = path.resolve(argv[++index]);
    else if (token === "--input") args.input = path.resolve(argv[++index]);
    else if (token === "--help") {
      process.stdout.write(
        "Usage: node scripts/build_validation_workbook.mjs [--catalog FILE] [--input FILE] [--output FILE]\n",
      );
      process.exit(0);
    } else {
      throw new Error(`알 수 없는 인자입니다: ${token}`);
    }
  }
  return args;
}


function columnName(index) {
  let value = index + 1;
  let result = "";
  while (value > 0) {
    value -= 1;
    result = String.fromCharCode(65 + (value % 26)) + result;
    value = Math.floor(value / 26);
  }
  return result;
}


function formatTitle(sheet, endColumn, title, subtitle) {
  sheet.showGridLines = false;
  sheet.mergeCells(`A1:${endColumn}1`);
  sheet.getRange("A1").values = [[title]];
  sheet.getRange(`A1:${endColumn}1`).format = {
    fill: COLORS.navy,
    font: { name: FONT, size: 16, bold: true, color: COLORS.white },
    verticalAlignment: "center",
  };
  sheet.getRange(`A1:${endColumn}1`).format.rowHeight = 34;

  sheet.mergeCells(`A2:${endColumn}2`);
  sheet.getRange("A2").values = [[subtitle]];
  sheet.getRange(`A2:${endColumn}2`).format = {
    fill: COLORS.paleBlue,
    font: { name: FONT, size: 10, color: COLORS.navy },
    wrapText: true,
    verticalAlignment: "center",
  };
  sheet.getRange(`A2:${endColumn}2`).format.rowHeight = 34;
}


function formatHeader(range) {
  range.format = {
    fill: COLORS.blue,
    font: { name: FONT, size: 10, bold: true, color: COLORS.white },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: COLORS.border },
  };
  range.format.rowHeight = 32;
}


function formatBody(range) {
  range.format = {
    font: { name: FONT, size: 10, color: "#0F172A" },
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: COLORS.border },
  };
}


function addStatusFormatting(range) {
  range.conditionalFormats.add("cellIs", {
    operator: "equal",
    formula: '"구현"',
    format: { fill: COLORS.paleGreen, font: { color: COLORS.green, bold: true } },
  });
  range.conditionalFormats.add("cellIs", {
    operator: "equal",
    formula: '"부분 구현"',
    format: { fill: COLORS.paleAmber, font: { color: COLORS.amber, bold: true } },
  });
  range.conditionalFormats.add("cellIs", {
    operator: "equal",
    formula: '"미구현"',
    format: { fill: COLORS.paleRed, font: { color: COLORS.red, bold: true } },
  });
}


function statusCellFormat(status) {
  if (status === "구현") {
    return { fill: COLORS.paleGreen, font: { name: FONT, size: 10, color: COLORS.green, bold: true } };
  }
  if (status === "부분 구현") {
    return { fill: COLORS.paleAmber, font: { name: FONT, size: 10, color: COLORS.amber, bold: true } };
  }
  return { fill: COLORS.paleRed, font: { name: FONT, size: 10, color: COLORS.red, bold: true } };
}


function addAgreementFormatting(range) {
  range.conditionalFormats.add("cellIs", {
    operator: "equal",
    formula: '"O"',
    format: { fill: COLORS.paleGreen, font: { color: COLORS.green, bold: true } },
  });
  range.conditionalFormats.add("cellIs", {
    operator: "equal",
    formula: '"X"',
    format: { fill: COLORS.paleRed, font: { color: COLORS.red, bold: true } },
  });
  range.conditionalFormats.add("containsText", {
    text: "미실행",
    format: { fill: COLORS.paleGray, font: { color: COLORS.gray } },
  });
}


function normalizeInput(payload, rules) {
  const defaultExpected = Object.fromEntries(rules.map((rule) => [rule.rule_id, "충족"]));
  const documents = payload?.documents?.length
    ? payload.documents
    : [
        {
          document_id: "D00",
          pdf_file: "D00_정상기준본.pdf",
          company: "",
          product: "",
          document_type: "정상기준본",
          expected: defaultExpected,
        },
        ...Array.from({ length: 8 }, (_, index) => ({
          document_id: `D${String(index + 1).padStart(2, "0")}`,
          pdf_file: "",
          company: "",
          product: "",
          document_type: "오류본",
          expected: {},
        })),
      ];

  const documentMap = new Map(documents.map((document) => [String(document.document_id), document]));
  const results = (payload?.results || []).map((result) => {
    const document = documentMap.get(String(result.document_id)) || {};
    const expected = String(
      result.expected_status || document.expected?.[result.rule_id] || "미설정",
    );
    const actual = String(result.actual_status || "미실행");
    let agreement = String(result.agreement || "");
    if (!agreement) {
      if (expected === "해당없음") agreement = "해당없음";
      else if (actual === "미실행" || expected === "미설정") agreement = "미실행";
      else agreement = expected === actual ? "O" : "X";
    }
    return {
      document_id: String(result.document_id || ""),
      pdf_file: String(result.pdf_file || document.pdf_file || ""),
      rule_id: String(result.rule_id || ""),
      expected_status: expected,
      actual_status: actual,
      agreement,
      reason: String(result.reason || ""),
      evidence_page: String(result.evidence_page || ""),
      criteria_text: String(result.criteria_text || ""),
      result_text: String(result.result_text || ""),
      extracted_value: String(result.extracted_value || ""),
      elapsed_seconds: Number.isFinite(Number(result.elapsed_seconds)) ? Number(result.elapsed_seconds) : null,
      note: String(result.note || ""),
    };
  });

  return { documents, results };
}


function buildSummarySheet(sheet, rules) {
  formatTitle(
    sheet,
    "H",
    "SP 검증 회귀 종합",
    "O/X는 문서의 합격·불합격이 아니라 기대판정과 실제판정의 일치 여부입니다. 구현 상태와 실행 결과를 분리해 관리합니다.",
  );

  sheet.getRange("A4:B4").values = [["구현 감사", "건수"]];
  formatHeader(sheet.getRange("A4:B4"));
  sheet.getRange("A5:A8").values = [["구현"], ["부분 구현"], ["미구현"], ["전체"]];
  sheet.getRange("B5").formulas = [['=COUNTIF(Implementation!$D$5:$D$100,"구현")']];
  sheet.getRange("B6").formulas = [['=COUNTIF(Implementation!$D$5:$D$100,"부분 구현")']];
  sheet.getRange("B7").formulas = [['=COUNTIF(Implementation!$D$5:$D$100,"미구현")']];
  sheet.getRange("B8").formulas = [["=SUM(B5:B7)"]];
  formatBody(sheet.getRange("A5:B8"));
  ["구현", "부분 구현", "미구현"].forEach((status, index) => {
    sheet.getRange(`A${5 + index}`).format = statusCellFormat(status);
  });

  sheet.getRange("D4:E4").values = [["회귀검증 결과", "건수"]];
  formatHeader(sheet.getRange("D4:E4"));
  sheet.getRange("D5:D8").values = [["O"], ["X"], ["미실행"], ["전체 상세결과"]];
  sheet.getRange("E5").formulas = [['=COUNTIF(Details!$F$5:$F$1004,"O")']];
  sheet.getRange("E6").formulas = [['=COUNTIF(Details!$F$5:$F$1004,"X")']];
  sheet.getRange("E7").formulas = [['=COUNTIF(Details!$F$5:$F$1004,"미실행")']];
  sheet.getRange("E8").formulas = [["=SUM(E5:E7)"]];
  formatBody(sheet.getRange("D5:E8"));
  addAgreementFormatting(sheet.getRange("D5:D7"));

  sheet.getRange("A11:H11").merge();
  sheet.getRange("A11").values = [["운영 순서"]];
  sheet.getRange("A11:H11").format = {
    fill: COLORS.navy,
    font: { name: FONT, size: 11, bold: true, color: COLORS.white },
  };
  sheet.getRange("A12:H15").merge(true);
  sheet.getRange("A12:A15").values = [
    ["1. 구현현황에서 코드상 지원 범위를 확인합니다."],
    ["2. 정답정의에 문서별 기대판정을 입력합니다."],
    ["3. 검증 실행 결과를 로직별상세결과 형식으로 적재합니다."],
    ["4. 문서별매트릭스에서 O/X/미실행을 확인하고 X의 판정사유와 근거를 추적합니다."],
  ];
  formatBody(sheet.getRange("A12:H15"));
  sheet.getRange("A12:H15").format.fill = COLORS.paleGray;

  sheet.getRange("A4:H15").format.font = { name: FONT, size: 10 };
  sheet.getRange("A:A").format.columnWidth = 24;
  sheet.getRange("B:B").format.columnWidth = 12;
  sheet.getRange("C:C").format.columnWidth = 4;
  sheet.getRange("D:D").format.columnWidth = 24;
  sheet.getRange("E:E").format.columnWidth = 12;
  sheet.getRange("F:H").format.columnWidth = 16;
  sheet.freezePanes.freezeRows(2);
  sheet.tabColor = COLORS.navy;
}


function buildImplementationSheet(sheet, catalog) {
  formatTitle(
    sheet,
    "G",
    "A/B/C 검증 로직 구현 현황",
    `코드 감사 기준일 ${catalog.audited_at}. 부분 구현은 추출 또는 하위 조건만 존재하고 요구사항 전체 판정은 아직 완결되지 않은 상태입니다.`,
  );
  const headers = ["로직 ID", "구분", "검증 요구사항", "구현 상태", "현재 실행 주체", "코드 근거", "감사 메모"];
  sheet.getRange("A4:G4").values = [headers];
  formatHeader(sheet.getRange("A4:G4"));
  const rows = catalog.rules.map((rule) => [
    rule.rule_id,
    rule.category,
    rule.requirement,
    rule.implementation_status,
    rule.executor,
    (rule.code_evidence || []).join("\n"),
    rule.audit_note,
  ]);
  sheet.getRange(`A5:G${4 + rows.length}`).values = rows;
  formatBody(sheet.getRange(`A5:G${4 + rows.length}`));
  sheet.getRange(`A5:A${4 + rows.length}`).format = {
    font: { name: FONT, size: 10, bold: true, color: COLORS.navy },
    horizontalAlignment: "center",
  };
  sheet.getRange(`D5:D${4 + rows.length}`).format.horizontalAlignment = "center";
  catalog.rules.forEach((rule, index) => {
    const cell = sheet.getRange(`D${5 + index}`);
    cell.format = statusCellFormat(rule.implementation_status);
    cell.format.horizontalAlignment = "center";
  });
  sheet.getRange("A:A").format.columnWidth = 10;
  sheet.getRange("B:B").format.columnWidth = 25;
  sheet.getRange("C:C").format.columnWidth = 62;
  sheet.getRange("D:D").format.columnWidth = 14;
  sheet.getRange("E:E").format.columnWidth = 28;
  sheet.getRange("F:F").format.columnWidth = 48;
  sheet.getRange("G:G").format.columnWidth = 64;
  sheet.getRange(`A5:G${4 + rows.length}`).format.rowHeight = 52;
  sheet.freezePanes.freezeRows(4);
  sheet.freezePanes.freezeColumns(1);
  sheet.tabColor = COLORS.blue;
}


function buildExpectationsSheet(sheet, rules, documents) {
  const metadataHeaders = ["문서 ID", "PDF 파일", "회사", "제품", "문서 구분"];
  const headers = [...metadataHeaders, ...rules.map((rule) => rule.rule_id)];
  const endColumn = columnName(headers.length - 1);
  formatTitle(
    sheet,
    endColumn,
    "문서별 기대판정 정의",
    "정상기준본과 오류본의 기대판정을 로직별로 입력합니다. D00은 정상기준본 기본값으로 전 항목 충족이며, 적용하지 않는 로직은 해당없음으로 변경합니다.",
  );
  sheet.getRange(`A4:${endColumn}4`).values = [headers];
  formatHeader(sheet.getRange(`A4:${endColumn}4`));

  const rows = documents.map((document) => [
    String(document.document_id || ""),
    String(document.pdf_file || ""),
    String(document.company || ""),
    String(document.product || ""),
    String(document.document_type || "오류본"),
    ...rules.map((rule) => String(document.expected?.[rule.rule_id] || "미설정")),
  ]);
  const endRow = Math.max(5, 4 + rows.length);
  if (rows.length) sheet.getRange(`A5:${endColumn}${endRow}`).values = rows;
  formatBody(sheet.getRange(`A5:${endColumn}${endRow}`));
  sheet.getRange(`A5:E${endRow}`).format.fill = COLORS.paleGray;
  const firstRuleColumn = columnName(metadataHeaders.length);
  sheet.getRange(`${firstRuleColumn}5:${endColumn}${endRow}`).dataValidation = {
    rule: { type: "list", values: EXPECTED_OPTIONS },
  };
  sheet.getRange(`${firstRuleColumn}5:${endColumn}${endRow}`).format.horizontalAlignment = "center";
  sheet.getRange("A:A").format.columnWidth = 10;
  sheet.getRange("B:B").format.columnWidth = 34;
  sheet.getRange("C:D").format.columnWidth = 18;
  sheet.getRange("E:E").format.columnWidth = 15;
  sheet.getRange(`${firstRuleColumn}:${endColumn}`).format.columnWidth = 10;
  sheet.freezePanes.freezeRows(4);
  sheet.freezePanes.freezeColumns(5);
  sheet.tabColor = "#0F766E";
}


function matrixFormula(documentRow, ruleColumn) {
  const detailDocuments = "Details!$A$5:$A$1004";
  const detailRules = "Details!$C$5:$C$1004";
  const detailAgreements = "Details!$F$5:$F$1004";
  return (
    `=IF($A${documentRow}="","",` +
    `IF(COUNTIFS(${detailDocuments},$A${documentRow},${detailRules},${ruleColumn}$4)=0,"미실행",` +
    `IF(COUNTIFS(${detailDocuments},$A${documentRow},${detailRules},${ruleColumn}$4,${detailAgreements},"X")>0,"X",` +
    `IF(COUNTIFS(${detailDocuments},$A${documentRow},${detailRules},${ruleColumn}$4,${detailAgreements},"O")>0,"O","해당없음"))))`
  );
}


function buildMatrixSheet(sheet, rules, documents) {
  const headers = ["문서 ID", "PDF 파일", ...rules.map((rule) => rule.rule_id), "O 합계", "X 합계", "미실행 합계"];
  const endColumn = columnName(headers.length - 1);
  const lastRuleColumn = columnName(1 + rules.length);
  const firstSummaryColumn = columnName(2 + rules.length);
  formatTitle(
    sheet,
    endColumn,
    "문서별 회귀검증 매트릭스",
    "O는 기대판정과 실제판정 일치, X는 불일치입니다. 미실행은 결과가 아직 적재되지 않은 로직입니다.",
  );
  sheet.getRange(`A4:${endColumn}4`).values = [headers];
  formatHeader(sheet.getRange(`A4:${endColumn}4`));
  const rows = documents.map((document) => [String(document.document_id || ""), String(document.pdf_file || "")]);
  const endRow = Math.max(5, 4 + rows.length);
  if (rows.length) sheet.getRange(`A5:B${endRow}`).values = rows;

  for (let row = 5; row <= endRow; row += 1) {
    const formulas = [];
    for (let index = 0; index < rules.length; index += 1) {
      const matrixColumn = columnName(2 + index);
      formulas.push(matrixFormula(row, matrixColumn));
    }
    const ruleRange = `C${row}:${lastRuleColumn}${row}`;
    sheet.getRange(ruleRange).formulas = [formulas];
    sheet.getRange(`${firstSummaryColumn}${row}`).formulas = [[`=COUNTIF(C${row}:${lastRuleColumn}${row},"O")`]];
    sheet.getRange(`${columnName(3 + rules.length)}${row}`).formulas = [[`=COUNTIF(C${row}:${lastRuleColumn}${row},"X")`]];
    sheet.getRange(`${columnName(4 + rules.length)}${row}`).formulas = [[`=COUNTIF(C${row}:${lastRuleColumn}${row},"미실행")`]];
  }

  formatBody(sheet.getRange(`A5:${endColumn}${endRow}`));
  sheet.getRange(`A5:B${endRow}`).format.fill = COLORS.paleGray;
  sheet.getRange(`C5:${endColumn}${endRow}`).format.horizontalAlignment = "center";
  addAgreementFormatting(sheet.getRange(`C5:${lastRuleColumn}${endRow}`));
  sheet.getRange("A:A").format.columnWidth = 10;
  sheet.getRange("B:B").format.columnWidth = 34;
  sheet.getRange(`C:${lastRuleColumn}`).format.columnWidth = 10;
  sheet.getRange(`${firstSummaryColumn}:${endColumn}`).format.columnWidth = 12;
  sheet.freezePanes.freezeRows(4);
  sheet.freezePanes.freezeColumns(2);
  sheet.tabColor = "#7C3AED";
}


function buildDetailsSheet(sheet, results) {
  formatTitle(
    sheet,
    "M",
    "로직별 상세 결과",
    "한 행은 한 문서의 한 로직 결과입니다. X 행은 판정사유, 근거 페이지, 기준 원문과 결과 원문을 함께 남겨 재현 가능하게 관리합니다.",
  );
  const headers = [
    "문서 ID",
    "PDF 파일",
    "로직 ID",
    "기대판정",
    "실제판정",
    "정답일치(O/X)",
    "판정사유",
    "근거 페이지",
    "기준 원문",
    "결과 원문",
    "추출값",
    "실행시간(초)",
    "비고",
  ];
  sheet.getRange("A4:M4").values = [headers];
  formatHeader(sheet.getRange("A4:M4"));
  const rows = results.map((result) => [
    result.document_id,
    result.pdf_file,
    result.rule_id,
    result.expected_status,
    result.actual_status,
    result.agreement,
    result.reason,
    result.evidence_page,
    result.criteria_text,
    result.result_text,
    result.extracted_value,
    result.elapsed_seconds,
    result.note,
  ]);
  const capacity = Math.max(50, rows.length);
  if (rows.length) sheet.getRange(`A5:M${4 + rows.length}`).values = rows;
  formatBody(sheet.getRange(`A5:M${4 + capacity}`));
  sheet.getRange(`C5:C${4 + capacity}`).dataValidation = {
    rule: { type: "list", formula1: "Implementation!$A$5:$A$33" },
  };
  sheet.getRange(`D5:E${4 + capacity}`).dataValidation = {
    rule: { type: "list", values: EXPECTED_OPTIONS },
  };
  sheet.getRange(`F5:F${4 + capacity}`).dataValidation = {
    rule: { type: "list", values: ["O", "X", "미실행", "해당없음"] },
  };
  addAgreementFormatting(sheet.getRange(`F5:F${4 + capacity}`));
  sheet.getRange("A:A").format.columnWidth = 10;
  sheet.getRange("B:B").format.columnWidth = 34;
  sheet.getRange("C:C").format.columnWidth = 10;
  sheet.getRange("D:F").format.columnWidth = 15;
  sheet.getRange("G:G").format.columnWidth = 58;
  sheet.getRange("H:H").format.columnWidth = 14;
  sheet.getRange("I:K").format.columnWidth = 42;
  sheet.getRange("L:L").format.columnWidth = 14;
  sheet.getRange("M:M").format.columnWidth = 30;
  sheet.freezePanes.freezeRows(4);
  sheet.freezePanes.freezeColumns(3);
  sheet.tabColor = "#DC2626";
}


function buildRunInfoSheet(sheet, catalog, sourceInput, catalogBytes, rules, documents, results) {
  formatTitle(
    sheet,
    "D",
    "회귀검증 실행 정보",
    "같은 문서와 같은 코드·로직 카탈로그로 결과를 재현할 수 있도록 생성 조건을 기록합니다.",
  );
  sheet.getRange("A4:B4").values = [["항목", "값"]];
  formatHeader(sheet.getRange("A4:B4"));
  const rows = [
    ["템플릿 버전", "1.0"],
    ["생성 시각", new Date().toISOString()],
    ["카탈로그 버전", String(catalog.catalog_version || "")],
    ["카탈로그 기준일", String(catalog.audited_at || "")],
    ["카탈로그 SHA-256", crypto.createHash("sha256").update(catalogBytes).digest("hex")],
    ["전체 로직 수", rules.length],
    ["구현", rules.filter((rule) => rule.implementation_status === "구현").length],
    ["부분 구현", rules.filter((rule) => rule.implementation_status === "부분 구현").length],
    ["미구현", rules.filter((rule) => rule.implementation_status === "미구현").length],
    ["등록 문서 수", documents.length],
    ["상세 결과 수", results.length],
    ["입력 데이터", sourceInput ? path.basename(sourceInput) : "기본 템플릿"],
    ["상세결과 JSON 필드", "document_id, pdf_file, rule_id, expected_status, actual_status, reason, evidence_page, criteria_text, result_text, extracted_value, elapsed_seconds, note"],
  ];
  sheet.getRange(`A5:B${4 + rows.length}`).values = rows;
  formatBody(sheet.getRange(`A5:B${4 + rows.length}`));
  sheet.getRange("B6").setNumberFormat("yyyy-mm-dd hh:mm:ss");
  sheet.getRange("A:A").format.columnWidth = 28;
  sheet.getRange("B:B").format.columnWidth = 90;
  sheet.getRange(`A5:A${4 + rows.length}`).format = {
    fill: COLORS.paleGray,
    font: { name: FONT, size: 10, bold: true, color: COLORS.navy },
  };
  sheet.freezePanes.freezeRows(4);
  sheet.tabColor = COLORS.gray;
}


async function main() {
  const args = parseArgs(process.argv.slice(2));
  const artifactModulePath = process.env.ARTIFACT_TOOL_MODULE || DEFAULT_ARTIFACT_MODULE;
  const { SpreadsheetFile, Workbook } = await import(pathToFileURL(artifactModulePath).href);

  const catalogBytes = await fs.readFile(args.catalog);
  const catalog = JSON.parse(catalogBytes.toString("utf8"));
  const rules = catalog.rules || [];
  const actualRuleIds = rules.map((rule) => rule.rule_id);
  const expectedRuleIds = [
    ...Array.from({ length: 13 }, (_, index) => `A.${index + 1}`),
    ...Array.from({ length: 11 }, (_, index) => `B.${index + 1}`),
    ...Array.from({ length: 5 }, (_, index) => `C.${index + 1}`),
  ];
  if (JSON.stringify(actualRuleIds) !== JSON.stringify(expectedRuleIds)) {
    throw new Error("카탈로그 로직 ID가 A.1~C.5 순서와 일치하지 않습니다.");
  }
  if (rules.some((rule) => !IMPLEMENTATION_ORDER.includes(rule.implementation_status))) {
    throw new Error("카탈로그에 알 수 없는 구현 상태가 있습니다.");
  }

  let payload = null;
  if (args.input) payload = JSON.parse(await fs.readFile(args.input, "utf8"));
  const { documents, results } = normalizeInput(payload, rules);

  const workbook = Workbook.create();
  const sheets = {
    summary: workbook.worksheets.add("Summary"),
    implementation: workbook.worksheets.add("Implementation"),
    expectations: workbook.worksheets.add("Expectations"),
    matrix: workbook.worksheets.add("Matrix"),
    details: workbook.worksheets.add("Details"),
    runInfo: workbook.worksheets.add("RunInfo"),
  };
  buildSummarySheet(sheets.summary, rules);
  buildImplementationSheet(sheets.implementation, catalog);
  buildExpectationsSheet(sheets.expectations, rules, documents);
  buildMatrixSheet(sheets.matrix, rules, documents);
  buildDetailsSheet(sheets.details, results);
  buildRunInfoSheet(sheets.runInfo, catalog, args.input, catalogBytes, rules, documents, results);

  await fs.mkdir(path.dirname(args.output), { recursive: true });
  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(args.output);
  process.stdout.write(`OUTPUT=${args.output}\n`);
}


await main();
