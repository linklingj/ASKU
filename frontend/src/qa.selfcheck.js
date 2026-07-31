// Runs qa.html's REAL script under DOM/d3 stubs and asserts build()+answer().
const fs = require("fs");
const assert = require("assert");
const html = fs.readFileSync(process.argv[2], "utf8");

// Grab the IIFE body verbatim (the inline <script> without src).
const start = html.indexOf("(function () {\n  \"use strict\";");
const end = html.indexOf("})();", start) + "})();".length;
const iife = html.slice(start, end);

// Stubs so the IIFE loads without a browser.
const listeners = {};
globalThis.__QA_TEST__ = true;
globalThis.window = { addEventListener() {}, history: { length: 1 }, matchMedia: () => ({ matches: false }) };
globalThis.location = { search: "" };
globalThis.URLSearchParams = require("url").URLSearchParams;
globalThis.document = {
  addEventListener(ev, cb) { listeners[ev] = cb; },
  getElementById: () => ({ style: {}, addEventListener() {}, set innerHTML(v) {}, get innerHTML() { return ""; } }),
  querySelector: () => ({ style: {} }),
};
globalThis.ResizeObserver = class { observe() {} disconnect() {} };

eval(iife);
const QA = globalThis.__QA__;
assert(QA, "test hook not exposed");

QA.build();
const { nodes, adj, byId } = QA.get();

// build() invariants
assert(nodes.length > 30, "expected a sizable graph, got " + nodes.length);
assert.equal(nodes.filter(n => n.type === "school").length, 1, "exactly one school root");
assert(nodes.filter(n => n.type === "dept").length === 6, "6 depts");
nodes.forEach(n => assert(adj.get(n.id).size > 0, "no orphan node: " + n.name)); // everything connected

// answer() — a graduation query should surface the graduation doc as evidence, with ids
const r = QA.answer("졸업요건이 어떻게 되나요?");
assert(r.text && r.text.length > 20, "answer text present");
assert(r.ids.length >= 1 && r.ids.length <= 3, "1..3 evidence ids, got " + r.ids.length);
assert(r.ids.every(id => byId.has(id)), "evidence ids are real nodes");
assert(r.text.includes("졸업요건"), "graduation answer references the graduation node");

// a nonsense query still returns a graceful fallback (no crash, still 1..3 ids)
const r2 = QA.answer("asdfqwer zzz");
assert(r2.text.length > 20 && r2.ids.length >= 1, "fallback answer is graceful");

console.log("OK — nodes:%d links-per-node ok, answer ids:%s", nodes.length, JSON.stringify(r.ids));
