// Runs register.html's REAL script under the __RG_TEST__ hook and asserts the pure helpers
// that gate the backend call: valid() / normalizeUrl() / deriveName() / stageToPhase().
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

// normalizeUrl(): keeps existing scheme, prepends https:// otherwise (POST /schools requires http[s])
assert.equal(RG.normalizeUrl("sejong.ac.kr/notice"), "https://sejong.ac.kr/notice");
assert.equal(RG.normalizeUrl("http://a.bc"), "http://a.bc");
assert(/^https:\/\//.test(RG.normalizeUrl("www.snu.ac.kr")), "scheme-less gets https");

// deriveName(): first host label, www./TLD stripped (name is required by the API)
assert.equal(RG.deriveName("https://www.sejong.ac.kr/board"), "sejong");
assert.equal(RG.deriveName("snu.ac.kr"), "snu");

// stageToPhase(): backend stage → visual index; terminal stages clamp to last; unknown → 0
assert.equal(RG.stageToPhase("crawling"), 0);
assert.equal(RG.stageToPhase("indexing"), RG.PHASES.length - 1);
assert.equal(RG.stageToPhase("ready"), RG.PHASES.length - 1);
assert.equal(RG.stageToPhase("done"), RG.PHASES.length - 1);
assert.equal(RG.stageToPhase("weird"), 0);

console.log("OK — valid()/normalizeUrl()/deriveName()/stageToPhase() pass; phases:%d", RG.PHASES.length);
