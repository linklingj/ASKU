// Loads model.js in node and asserts the pure parts of the answer-model selection:
// normalize/validate/label/mixedContent/classifyError/geminiText/ollamaModelNames.
// Network calls (generate/listOllamaModels) are not exercised here.
const assert = require("assert");
const path = process.argv[2] || require("path").join(__dirname, "model.js");

globalThis.window = globalThis;
globalThis.location = { origin: "https://example.github.io", protocol: "https:" };
const M = require(path);

// normalize(): 기본값을 채우고, 모르는 제공자·뒤 슬래시를 정리한다
assert.strictEqual(M.normalize(null).provider, "server");
assert.strictEqual(M.normalize({ provider: "해킹" }).provider, "server", "unknown provider falls back to server");
assert.strictEqual(M.normalize({ ollamaHost: "http://localhost:11434//" }).ollamaHost, "http://localhost:11434");
assert.strictEqual(M.normalize({ provider: "ollama" }).provider, "ollama");

// validate(): 서버 기본은 설정이 없어도 되고, 사용자 모델은 빠진 값을 먼저 막는다
assert.ok(M.validate({ provider: "server" }).ok);
assert.strictEqual(M.validate({ provider: "gemini" }).code, "NO_KEY");
assert.ok(M.validate({ provider: "gemini", geminiKey: "k" }).ok, "model name has a default");
assert.strictEqual(M.validate({ provider: "ollama" }).code, "NO_MODEL");
assert.ok(M.validate({ provider: "ollama", ollamaModel: "llama3.1" }).ok);

// label(): 질문창에 보이는 이름
assert.strictEqual(M.label({ provider: "server" }), "기본 Gemini");
assert.strictEqual(M.label({ provider: "ollama", ollamaModel: "llama3.1" }), "llama3.1");

// mixedContent(): https 페이지 → http://localhost 는 브라우저가 막는다
assert.strictEqual(M.mixedContent("https:", "http://localhost:11434"), true);
assert.strictEqual(M.mixedContent("http:", "http://localhost:11434"), false);
assert.strictEqual(M.mixedContent("https:", "https://ollama.example"), false);

// classifyError(): 사용자가 원인을 구분할 수 있게 코드가 갈린다(계획 §1-5)
const err = (status) => Object.assign(new Error("boom"), { status });
assert.strictEqual(M.classifyError(err(undefined), { provider: "ollama" }).code, "OLLAMA_UNREACHABLE");
assert.strictEqual(M.classifyError(err(404), { provider: "ollama", ollamaModel: "x" }).code, "OLLAMA_MODEL_NOT_FOUND");
assert.strictEqual(M.classifyError(err(403), { provider: "gemini" }).code, "GEMINI_AUTH");
assert.strictEqual(M.classifyError(err(429), { provider: "gemini" }).code, "GEMINI_QUOTA");
assert.strictEqual(M.classifyError(err(404), { provider: "gemini" }).code, "GEMINI_MODEL_NOT_FOUND");
assert.strictEqual(M.classifyError(Object.assign(new Error("m"), { code: "MIXED_CONTENT" }), { provider: "ollama" }).code, "MIXED_CONTENT");
assert.match(M.classifyError(err(undefined), { provider: "ollama" }).message, /OLLAMA_ORIGINS/, "CORS 안내에 사이트 주소를 넣는다");

// geminiText(): 여러 part 를 잇고, 빈 응답(안전 필터·길이 제한)은 오류로 구분한다
assert.strictEqual(M.geminiText({ candidates: [{ content: { parts: [{ text: "가" }, { text: "나" }] } }] }), "가나");
assert.throws(() => M.geminiText({ candidates: [{ finishReason: "SAFETY", content: { parts: [] } }] }), /SAFETY/);
assert.throws(() => M.geminiText({}), /unknown/);

// ollamaModelNames(): /api/tags 응답 → 정렬된 이름 목록
assert.deepStrictEqual(M.ollamaModelNames({ models: [{ name: "qwen2" }, { name: "llama3.1" }] }), ["llama3.1", "qwen2"]);
assert.deepStrictEqual(M.ollamaModelNames(null), []);

console.log("OK — normalize/validate/label/mixedContent/classifyError/geminiText/ollamaModelNames pass");
