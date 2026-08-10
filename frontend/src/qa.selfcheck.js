// Runs qa.html's REAL script under the __QA_TEST__ hook and asserts the pure transforms
// that turn a backend /graph payload into d3 structures: buildGraph/assignTypeColors/parseEid.
const fs = require("fs");
const assert = require("assert");
// CRLF 로 체크아웃되는 환경(Windows)에서도 같은 오프셋이 나오도록 개행을 먼저 맞춘다.
const html = fs.readFileSync(process.argv[2], "utf8").replace(/\r\n/g, "\n");

// Grab the IIFE body verbatim (the inline <script> without src).
const start = html.indexOf("(function () {\n  \"use strict\";");
const end = html.indexOf("})();", start) + "})();".length;
const iife = html.slice(start, end);

globalThis.__QA_TEST__ = true; // hook returns before any DOM/d3 wiring, so no stubs needed
globalThis.location = { search: "" };
globalThis.URLSearchParams = require("url").URLSearchParams;
eval(iife);
const QA = globalThis.__QA__;
assert(QA, "test hook not exposed");

// parseEid(): strips the "e_" prefix the API uses for node ids
assert.strictEqual(QA.parseEid("e_123"), 123);
assert.strictEqual(QA.parseEid("e_7"), 7);

// buildGraph(): builds nodes/adj/byId and drops edges whose endpoints aren't loaded (core subgraph)
const payload = {
  nodes: [
    { id: "e_1", type: "장학금", name: "국가장학금", degree: 2, doc_count: 3 },
    { id: "e_2", type: "부서", name: "학생지원팀", degree: 1 },
    { id: "e_3", type: "장학금", name: "교내장학금", degree: 1 },
  ],
  edges: [
    { source: "e_1", target: "e_2", relation: "담당" },
    { source: "e_1", target: "e_3", relation: "관련" },
    { source: "e_1", target: "e_999", relation: "댕글링" }, // 끝점 없음 → 버려져야 함
  ],
};
const g = QA.buildGraph(payload);
assert.strictEqual(g.nodes.length, 3, "3 nodes");
assert.strictEqual(g.rawLinks.length, 2, "dangling edge dropped");
assert.strictEqual(g.adj.get("e_1").size, 2, "e_1 has 2 neighbors");
assert(g.byId.has("e_2"), "byId indexed");

// assignTypeColors(): a color per distinct type, most-frequent first, counts correct
const ti = QA.assignTypeColors(g.nodes);
assert.strictEqual(ti.types[0], "장학금", "most frequent type ranked first");
assert.strictEqual(ti.counts["장학금"], 2);
assert(ti.color["장학금"] && ti.color["부서"], "every type gets a color");
assert.notStrictEqual(ti.color["장학금"], ti.color["부서"], "distinct types get distinct colors");

// nodeRadius(): clamped and grows with degree
assert(QA.nodeRadius({ degree: 0 }) >= 6 && QA.nodeRadius({ degree: 100 }) <= 18, "radius clamped");
assert(QA.nodeRadius({ degree: 5 }) > QA.nodeRadius({ degree: 0 }), "radius grows with degree");

// hubMin()/hubLabeled(): 상시 라벨 기준 — 확대할수록 널널, 축소할수록 빡빡, 긴 이름은 제외
assert(QA.hubMin(0.4) > QA.hubMin(1) && QA.hubMin(1) > QA.hubMin(3), "축소할수록 기준 차수가 올라간다");
const shortHub = { name: "국가장학금", degree: 4 };
assert(QA.hubLabeled(shortHub, 1), "기본 배율에서 degree 4 허브는 라벨 표시");
assert(!QA.hubLabeled(shortHub, 0.4), "많이 축소하면 같은 허브도 라벨 숨김");
assert(QA.hubLabeled({ name: "잎", degree: 1 }, 3), "많이 확대하면 말단 노드까지 라벨 표시");
assert(!QA.hubLabeled({ name: "가".repeat(21), degree: 50 }, 3), "20자 초과 이름은 허브여도 상시 라벨 제외");
assert(QA.hubLabeled({ name: "가".repeat(20), degree: 4 }, 1), "20자까지는 허용");

// isLinkable()/sourceChip(): 웹 근거만 링크, 첨부 근거(attachment://{id})는 죽은 링크가 되지 않게 칩으로
assert(QA.isLinkable("https://sejong.ac.kr/notice/1"), "web source is a link");
assert(!QA.isLinkable("attachment://7"), "attachment URI is not a link");
assert(!QA.isLinkable(""), "missing url is not a link");
const webChip = QA.sourceChip({ title: "장학 공지", url: "https://a.bc/1" }, "");
assert(webChip.startsWith("<a ") && webChip.includes('href="https://a.bc/1"'), "web chip is an anchor");
const attChip = QA.sourceChip({ title: "수강편람.pdf - 12페이지", url: "attachment://7" }, "");
assert(attChip.startsWith("<span "), "attachment chip is not an anchor");
assert(!attChip.includes("attachment://"), "attachment chip shows the filename, not the URI");
assert(
  QA.sourceChip({ title: '<img src=x onerror=alert(1)>', url: "attachment://7" }, "").includes("&lt;img"),
  "source titles are escaped"
);

// parseAnswer(): splits structured tags or falls back safely
const parsedStructured = QA.parseAnswer("[핵심 답변]\n원격강좌 수강 가능\n\n[상세 답변]\n### 강의목록\n| 과목 | 학점 |\n|---|---|\n| 문학 | 1 |");
assert.strictEqual(parsedStructured.summary, "원격강좌 수강 가능");
assert(parsedStructured.detail.includes("### 강의목록"), "detail contains markdown body");

const parsedFallback = QA.parseAnswer("첫째 줄 핵심 요약입니다.\n\n두번째 줄 상세 내용입니다.");
assert.strictEqual(parsedFallback.summary, "첫째 줄 핵심 요약입니다.");
assert.strictEqual(parsedFallback.detail, "두번째 줄 상세 내용입니다.");

// simpleMarkdown(): renders headings, bold, italic, and tables
const rendered = QA.simpleMarkdown("### 개설 강의\n| 과목 | 학점 |\n|---|---|\n| **국어** | *1학점* |");
assert(rendered.includes("<h4 class=\"md-h3\">개설 강의</h4>"), "h3 heading rendered");
assert(rendered.includes("<table class=\"md-table\">"), "table rendered");
assert(rendered.includes("<strong>국어</strong>"), "bold rendered");
assert(rendered.includes("<em>1학점</em>"), "italic rendered");

// renderDelAsTilde(): GFM 이 취소선으로 먹는 물결을 원래 물결로 되돌린다 ("10:00~12:00" 깨짐 방지)
const fakeParser = { parser: { parseInline: (toks) => toks.join("") } };
assert.strictEqual(QA.renderDelAsTilde.call(fakeParser, { raw: "~12:00 및 14:00~", tokens: ["12:00 및 14:00"] }), "~12:00 및 14:00~");
assert.strictEqual(QA.renderDelAsTilde.call(fakeParser, { raw: "~~취소~~", tokens: ["취소"] }), "~~취소~~");
// simpleMarkdown 폴백 경로도 물결을 그대로 둔다
assert(QA.simpleMarkdown("접수 3/1~3/5, 상담 10:00~12:00").includes("3/1~3/5, 상담 10:00~12:00"), "fallback keeps tildes");

console.log(
  "OK — buildGraph/assignTypeColors/parseEid/nodeRadius/sourceChip/parseAnswer/simpleMarkdown/renderDelAsTilde pass; nodes:%d links:%d",
  g.nodes.length, g.rawLinks.length
);
