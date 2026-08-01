// Runs register.html's REAL script under the __RG_TEST__ hook and asserts valid()+phaseAt().
const fs = require("fs");
const assert = require("assert");
const html = fs.readFileSync(process.argv[2], "utf8");

// Grab the IIFE body verbatim (the inline <script> without src).
const start = html.indexOf("(function () {\n  \"use strict\";");
const end = html.indexOf("})();", start) + "})();".length;
const iife = html.slice(start, end);

globalThis.__RG_TEST__ = true; // hook returns before any DOM wiring, so no stubs needed
eval(iife);
const RG = globalThis.__RG__;
assert(RG, "test hook not exposed");

// valid(): accepts real-looking notice URLs, rejects junk
["https://sejong.ac.kr", "sejong.ac.kr", "http://a.bc/notice?x=1", "www.snu.ac.kr/board"]
  .forEach(u => assert(RG.valid(u), "should accept: " + u));
["", "abc", "http://", "no dots here", "ftp:/x"]
  .forEach(u => assert(!RG.valid(u), "should reject: " + u));

// phaseAt(): starts at 0, ends on the last phase, clamps past total, non-decreasing
assert.equal(RG.phaseAt(0), 0, "starts at phase 0");
assert.equal(RG.phaseAt(RG.TOTAL - 1), RG.PHASES.length - 1, "last phase just before total");
assert.equal(RG.phaseAt(RG.TOTAL + 9999), RG.PHASES.length - 1, "clamps past total");
let prev = -1;
for (let e = 0; e <= RG.TOTAL; e += 50) { const p = RG.phaseAt(e); assert(p >= prev, "monotonic non-decreasing at " + e); prev = p; }

console.log("OK — valid()/phaseAt() pass; phases:%d total:%dms", RG.PHASES.length, RG.TOTAL);
