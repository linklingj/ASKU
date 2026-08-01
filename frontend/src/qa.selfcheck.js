// Runs qa.html's REAL script under the __QA_TEST__ hook and asserts the pure transforms
// that turn a backend /graph payload into d3 structures: buildGraph/assignTypeColors/parseEid.
const fs = require("fs");
const assert = require("assert");
const html = fs.readFileSync(process.argv[2], "utf8");

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

console.log("OK — buildGraph/assignTypeColors/parseEid/nodeRadius pass; nodes:%d links:%d", g.nodes.length, g.rawLinks.length);
