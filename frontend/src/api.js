// ASKU 프론트–백엔드 연동 공통 헬퍼. 모든 페이지가 <script src="api.js"> 로 공유한다.
// 백엔드 주소는 ?api=<url> → localStorage.asku_api → 기본값(localhost:8000) 순으로 결정.
// CORS 는 백엔드에서 allow_origins=["*"] 로 열려 있다(docs/01_SYSTEM/01_backend-api.md).
(function () {
  "use strict";
  var qp = new URLSearchParams(location.search).get("api");
  if (qp) { try { localStorage.setItem("asku_api", qp); } catch (_) {} }
  var stored = null;
  try { stored = localStorage.getItem("asku_api"); } catch (_) {}
  var BASE = (qp || stored || "http://localhost:8000").replace(/\/+$/, "");

  function req(method, path, body) {
    var opts = { method: method, headers: { Accept: "application/json" } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(BASE + path, opts).then(function (res) {
      return res.text().then(function (txt) {
        var data = txt ? JSON.parse(txt) : null;
        if (!res.ok) {
          var e = data && data.error;
          var err = new Error((e && e.message) || ("요청 실패 (" + res.status + ")"));
          err.status = res.status;
          err.code = e && e.code;
          throw err;
        }
        return data;
      });
    });
  }

  window.ASKU = {
    base: BASE,
    get: function (p) { return req("GET", p); },
    post: function (p, b) { return req("POST", p, b || {}); },
  };
})();
