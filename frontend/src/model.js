// 답변 모델 선택 — 설정 보관과 브라우저에서의 직접 호출을 담당한다.
// 계획 §1 "사용자 AI 모델 선택". 고르는 것은 **질문 답변 생성 모델뿐**이고,
// 검색·추출·임베딩은 서버가 정한 구현을 그대로 쓴다.
//
// 제공자 세 가지:
//   server — 기존 동작. 백엔드 POST /query 가 검색과 답변을 함께 한다.
//   gemini — 사용자 개인 키로 브라우저가 Gemini 를 직접 부른다.
//   ollama — 사용자 PC 의 Ollama 를 브라우저가 직접 부른다(서버에 설치하지 않는다).
//
// 키·주소는 sessionStorage 에만 둔다. 백엔드로 보내지 않고 localStorage 에도 쓰지
// 않는다(계획 §1-4) — 탭을 닫으면 사라지는 것이 기본값이다.
(function (global) {
  "use strict";

  var STORE_KEY = "asku_model";
  var DEFAULT_OLLAMA_HOST = "http://localhost:11434";
  var DEFAULT_GEMINI_MODEL = "gemini-2.0-flash";

  var PROVIDERS = [
    { id: "server", label: "ASKU 기본", hint: "ASKU 서버 키로 답변합니다" },
    { id: "gemini", label: "내 Gemini", hint: "내 API 키로 브라우저에서 답변합니다" },
    { id: "ollama", label: "내 PC Ollama", hint: "내 컴퓨터의 Ollama 로 답변합니다" },
  ];

  var DEFAULTS = {
    provider: "server",
    geminiKey: "",
    geminiModel: DEFAULT_GEMINI_MODEL,
    ollamaHost: DEFAULT_OLLAMA_HOST,
    ollamaModel: "",
  };

  // ── 순수 함수 (셀프체크 대상) ───────────────────────────────────────

  function normalize(raw) {
    var cfg = {};
    for (var k in DEFAULTS) cfg[k] = DEFAULTS[k];
    if (raw && typeof raw === "object") {
      for (var j in DEFAULTS) if (typeof raw[j] === "string") cfg[j] = raw[j];
    }
    // 모르는 제공자 값(구버전 설정·손댄 값)은 서버 기본으로 되돌린다.
    if (!PROVIDERS.some(function (p) { return p.id === cfg.provider; })) cfg.provider = DEFAULTS.provider;
    cfg.ollamaHost = String(cfg.ollamaHost || DEFAULT_OLLAMA_HOST).replace(/\/+$/, "");
    return cfg;
  }

  // 설정이 불완전하면 질문을 보내기 전에 막는다 — 모델을 부른 뒤 실패하면 어디가
  // 비었는지 사용자가 알기 어렵기 때문이다.
  function validate(cfg) {
    cfg = normalize(cfg);
    if (cfg.provider === "gemini") {
      if (!cfg.geminiKey.trim()) return { ok: false, code: "NO_KEY", message: "Gemini API 키를 설정에 입력해 주세요." };
      if (!cfg.geminiModel.trim()) return { ok: false, code: "NO_MODEL", message: "Gemini 모델명을 입력해 주세요." };
    }
    if (cfg.provider === "ollama") {
      if (!cfg.ollamaHost) return { ok: false, code: "NO_HOST", message: "Ollama 주소를 입력해 주세요." };
      if (!cfg.ollamaModel.trim()) return { ok: false, code: "NO_MODEL", message: "Ollama 모델을 선택해 주세요." };
    }
    return { ok: true };
  }

  function label(cfg) {
    cfg = normalize(cfg);
    if (cfg.provider === "gemini") return cfg.geminiModel || "내 Gemini";
    if (cfg.provider === "ollama") return cfg.ollamaModel || "내 PC Ollama";
    return "ASKU 기본";
  }

  // https 페이지에서 http://localhost 를 부르는 조합. **막지 않는다** — 대부분의
  // 브라우저는 localhost 를 신뢰할 수 있는 출처로 보고 허용한다(크롬 계열에서
  // 배포 사이트 → localhost:11434 의 /api/tags·/api/generate 호출을 실제로 확인).
  // 다만 사파리처럼 이 조합을 막는 브라우저가 있어, 연결이 실패했을 때 원인 후보로
  // 안내하는 데만 쓴다.
  function insecureLocal(pageProtocol, host) {
    return pageProtocol === "https:" && /^http:\/\//i.test(String(host || ""));
  }

  // fetch 예외·HTTP 상태를 사용자가 구분할 수 있는 안내로 옮긴다(계획 §1-5).
  function classifyError(err, cfg) {
    cfg = normalize(cfg);
    var status = err && err.status;
    if (err && err.code) return { code: err.code, message: err.message };

    if (cfg.provider === "ollama") {
      // 브라우저는 '연결 거부'·'CORS 거절'·'로컬 접근 차단'을 모두 같은 실패로
      // 준다(status 없음). 구분이 불가능하므로 원인 후보를 순서대로 안내한다.
      if (!status) {
        var causes = [
          "Ollama 가 실행 중인지 (`ollama serve`)",
          "이 사이트를 허용했는지 (`OLLAMA_ORIGINS=" + pageOrigin() + "`)",
          "주소가 맞는지 (" + cfg.ollamaHost + ")",
        ];
        if (insecureLocal(pageProtocol(), cfg.ollamaHost)) {
          causes.push("사파리 등 일부 브라우저는 https 페이지에서 http://localhost 호출을 막습니다 — 크롬·엣지·파이어폭스로 시도");
        }
        return {
          code: "OLLAMA_UNREACHABLE",
          message: "Ollama 에 연결하지 못했습니다. 확인해 주세요 — " + causes.join(" · "),
        };
      }
      if (status === 404) return { code: "OLLAMA_MODEL_NOT_FOUND", message: "Ollama 에 그 모델이 없습니다. `ollama pull " + (cfg.ollamaModel || "<모델>") + "` 로 먼저 받아 주세요." };
      return { code: "OLLAMA_ERROR", message: "Ollama 오류 (" + status + "): " + ((err && err.message) || "") };
    }

    if (cfg.provider === "gemini") {
      if (!status) return { code: "GEMINI_UNREACHABLE", message: "Gemini 에 연결하지 못했습니다. 네트워크 상태를 확인해 주세요." };
      if (status === 400 || status === 401 || status === 403) return { code: "GEMINI_AUTH", message: "Gemini API 키가 거부됐습니다. 키를 다시 확인해 주세요." };
      if (status === 404) return { code: "GEMINI_MODEL_NOT_FOUND", message: "그 모델명을 찾을 수 없습니다. 모델명을 확인해 주세요." };
      if (status === 429) return { code: "GEMINI_QUOTA", message: "Gemini 사용 한도를 넘었습니다. 잠시 후 다시 시도해 주세요." };
      return { code: "GEMINI_ERROR", message: "Gemini 오류 (" + status + "): " + ((err && err.message) || "") };
    }

    return { code: "SERVER_ERROR", message: (err && err.message) || "답변을 가져오지 못했습니다." };
  }

  // Gemini 응답에서 답변 텍스트를 꺼낸다. 안전 필터·길이 제한으로 후보가 비는
  // 경우가 있어(백엔드 GeminiProvider 와 같은 사정) 빈 응답을 오류로 구분한다.
  function geminiText(data) {
    var cands = (data && data.candidates) || [];
    var parts = (cands[0] && cands[0].content && cands[0].content.parts) || [];
    var text = parts.map(function (p) { return p.text || ""; }).join("").trim();
    if (!text) {
      var reason = (cands[0] && cands[0].finishReason) || (data && data.promptFeedback && data.promptFeedback.blockReason) || "unknown";
      var e = new Error("Gemini 가 빈 응답을 돌려줬습니다 (" + reason + ").");
      e.code = "GEMINI_EMPTY";
      throw e;
    }
    return text;
  }

  function ollamaModelNames(data) {
    return ((data && data.models) || [])
      .map(function (m) { return m && m.name; })
      .filter(Boolean)
      .sort();
  }

  // ── 브라우저 전용 ───────────────────────────────────────────────────

  function pageOrigin() {
    try { return global.location.origin; } catch (_) { return "이 사이트"; }
  }

  function pageProtocol() {
    try { return global.location.protocol; } catch (_) { return ""; }
  }

  function load() {
    var raw = null;
    try { raw = JSON.parse(global.sessionStorage.getItem(STORE_KEY)); } catch (_) {}
    return normalize(raw);
  }

  function save(cfg) {
    var next = normalize(cfg);
    try { global.sessionStorage.setItem(STORE_KEY, JSON.stringify(next)); } catch (_) {}
    return next;
  }

  function clear() {
    try { global.sessionStorage.removeItem(STORE_KEY); } catch (_) {}
    return normalize(null);
  }

  // 응답 본문까지 읽어 HTTP 오류에 status 를 달아 던진다(classifyError 입력).
  function send(url, opts) {
    return global.fetch(url, opts).then(function (res) {
      return res.text().then(function (txt) {
        var data = null;
        try { data = txt ? JSON.parse(txt) : null; } catch (_) {}
        if (!res.ok) {
          var detail = (data && data.error && (data.error.message || data.error)) || txt || "";
          var err = new Error(String(detail).slice(0, 300));
          err.status = res.status;
          throw err;
        }
        return data;
      });
    });
  }

  function generateGemini(cfg, prompt, context) {
    var url = "https://generativelanguage.googleapis.com/v1beta/models/" +
      encodeURIComponent(cfg.geminiModel.trim()) + ":generateContent";
    return send(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": cfg.geminiKey.trim() },
      body: JSON.stringify({
        contents: [{ role: "user", parts: [{ text: "[컨텍스트]\n" + context + "\n\n[요청]\n" + prompt }] }],
      }),
    }).then(geminiText);
  }

  function generateOllama(cfg, prompt, context) {
    return send(cfg.ollamaHost + "/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: cfg.ollamaModel.trim(),
        prompt: "[컨텍스트]\n" + context + "\n\n[요청]\n" + prompt,
        stream: false,
      }),
    }).then(function (data) { return String((data && data.response) || "").trim(); });
  }

  // 선택한 제공자로 답변을 만든다. context 는 백엔드 /retrieve 가 준 근거 그대로다.
  function generate(cfg, prompt, context) {
    cfg = normalize(cfg);
    if (cfg.provider === "gemini") return generateGemini(cfg, prompt, context);
    if (cfg.provider === "ollama") return generateOllama(cfg, prompt, context);
    return Promise.reject(new Error("서버 모델은 백엔드 /query 로 답합니다"));
  }

  // 설정창의 연결 확인 — 설치된 모델 목록을 읽어 드롭다운을 채운다(계획 §1-3).
  function listOllamaModels(host) {
    var target = String(host || DEFAULT_OLLAMA_HOST).replace(/\/+$/, "");
    return send(target + "/api/tags", { method: "GET" }).then(ollamaModelNames);
  }

  var API = {
    PROVIDERS: PROVIDERS,
    DEFAULT_OLLAMA_HOST: DEFAULT_OLLAMA_HOST,
    DEFAULT_GEMINI_MODEL: DEFAULT_GEMINI_MODEL,
    normalize: normalize,
    validate: validate,
    label: label,
    insecureLocal: insecureLocal,
    classifyError: classifyError,
    geminiText: geminiText,
    ollamaModelNames: ollamaModelNames,
    load: load,
    save: save,
    clear: clear,
    generate: generate,
    listOllamaModels: listOllamaModels,
  };

  global.ASKU_MODEL = API;
  if (typeof module !== "undefined" && module.exports) module.exports = API; // model.selfcheck.js
})(typeof window !== "undefined" ? window : globalThis);
