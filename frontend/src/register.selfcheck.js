// Runs register.html's REAL script under the __RG_TEST__ hook and asserts the pure helpers
// that gate the backend call: valid() / normalizeUrl() / deriveName() / stageToPhase(),
// plus the attachment ones: extOf() / fileError() / formatBytes() / attachSummary().
const fs = require("fs");
const assert = require("assert");
// CRLF 로 체크아웃되는 환경(Windows)에서도 같은 오프셋이 나오도록 개행을 먼저 맞춘다.
const html = fs.readFileSync(process.argv[2], "utf8").replace(/\r\n/g, "\n");

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

// extOf(): lowercased suffix; a leading dot is not an extension (matches PurePosixPath.suffix)
assert.equal(RG.extOf("2026_수강편람.PDF"), ".pdf");
assert.equal(RG.extOf("C:\\docs\\학칙.hwpx"), ".hwpx");
assert.equal(RG.extOf("noext"), "");
assert.equal(RG.extOf(".pdf"), "");

// fileError(): mirrors the backend's upload validation (01_backend-api.md §2.4-1)
assert.equal(RG.fileError("수강편람.pdf", 1024), null, "supported file passes");
RG.SUPPORTED_EXT.forEach(ext => assert.equal(RG.fileError("a" + ext, 10), null, "must accept " + ext));
assert.equal(RG.fileError("캠퍼스맵.png", 10).code, "UNSUPPORTED_FILE_TYPE");
assert.equal(RG.fileError("", 10).code, "INVALID_FILENAME");
assert.equal(RG.fileError("빈파일.txt", 0).code, "EMPTY_FILE");
assert.equal(RG.fileError("큰파일.pdf", RG.MAX_FILE_BYTES + 1).code, "FILE_TOO_LARGE");
assert.equal(RG.fileError("경계.pdf", RG.MAX_FILE_BYTES), null, "exactly the limit is allowed");
// 상한이 바뀌어도 안내 문구가 따라오도록 상수에서 만들어야 한다 (백엔드 MAX_ATTACHMENT_MB 와 같은 값)
assert.equal(RG.MAX_FILE_BYTES, RG.MAX_FILE_MB * 1024 * 1024);
assert(RG.fileError("큰파일.pdf", RG.MAX_FILE_BYTES + 1).message.includes(String(RG.MAX_FILE_MB)));

// formatBytes(): bytes stay integral, larger units get one decimal
assert.equal(RG.formatBytes(0), "0 B");
assert.equal(RG.formatBytes(512), "512 B");
assert.equal(RG.formatBytes(1024), "1.0 KB");
assert.equal(RG.formatBytes(4823910), "4.6 MB");

// attachSummary(): settled only when nothing is pending/indexing; chunks count ready ones
const sum = RG.attachSummary([
  { status: "ready", chunk_count: 12 },
  { status: "ready", chunk_count: 3 },
  { status: "failed", error_code: "HWP_ENCRYPTED" },
  { status: "indexing" },
]);
assert.deepEqual(
  { total: sum.total, ready: sum.ready, failed: sum.failed, working: sum.working, chunks: sum.chunks, settled: sum.settled },
  { total: 4, ready: 2, failed: 1, working: 1, chunks: 15, settled: false }
);
// 분량 상한에 걸린 첨부는 ready 로 세되 따로 표시해 완료 화면에서 알린다
const capped = RG.attachSummary([
  { status: "ready", chunk_count: 2000, truncated: true },
  { status: "ready", chunk_count: 5 },
]);
assert.equal(capped.ready, 2);
assert.equal(capped.truncated, 1, "truncated counted separately from ready");
assert.equal(capped.settled, true);
assert.equal(RG.attachSummary([{ status: "ready", chunk_count: 1 }]).truncated, 0);

assert.equal(RG.attachSummary([]).settled, true, "no attachments = nothing to wait for");
assert.equal(RG.attachSummary([{ status: "ready", chunk_count: 1 }, { status: "failed" }]).settled, true);
assert.equal(RG.attachSummary([{ status: "pending" }]).settled, false);

console.log(
  "OK — url helpers + attachment helpers pass; phases:%d, formats:%s",
  RG.PHASES.length, RG.SUPPORTED_EXT.join(",")
);
