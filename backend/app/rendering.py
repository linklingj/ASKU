"""브라우저로 렌더링한 뒤 HTML 을 돌려주는 수집 보조기.

대부분의 학교는 서버가 만들어 준 HTML 에 공지 목록이 들어 있어 `requests` 로
충분하다. 그런데 목록을 자바스크립트로 그리는 학교가 있다 — 중앙대는 정적
HTML 에 공지 링크가 하나도 없고, 브라우저가 스크립트를 실행한 뒤에야 15건이
나타난다. 선택자 문제가 아니라 **데이터가 아직 없는** 문제라 어떤 선택자로도
읽을 수 없다.

브라우저는 비싸다. 이미지가 수백 MB 커지고, 요청 하나에 수 초가 걸리며,
메모리를 많이 쓴다. 그래서 **필요하다고 규격에 적힌 학교의, 필요한 페이지에만**
쓴다(`AdapterSpec.render`). 중앙대는 목록만 그려지고 상세는 정적이라 `listing`
이면 충분하다 — 공지 300건이면 브라우저 호출이 300번에서 몇 번으로 줄어든다.

robots.txt·요청 간격·예산·재시도는 `Crawler` 가 이미 적용한 뒤에 이 모듈을
부른다. 여기서는 정책을 다시 판단하지 않는다.
"""

from __future__ import annotations

import logging
from typing import Protocol


LOGGER = logging.getLogger(__name__)

# 렌더링 상한. `networkidle` 만 믿고 기다리면 분석 스크립트나 롱폴링이 붙은
# 사이트에서 영영 끝나지 않는다.
DEFAULT_TIMEOUT_MS = 20_000
# 문서 로드 뒤 목록이 채워질 때까지 주는 여유. 짧으면 빈 목록을 읽고,
# 길면 학교마다 그만큼씩 느려진다.
DEFAULT_SETTLE_MS = 1_500


class Renderer(Protocol):
    """URL 하나를 렌더링해 HTML 을 돌려준다. 실패하면 None."""

    def render(self, url: str, *, referer: str | None = None) -> str | None: ...

    def close(self) -> None: ...


class PlaywrightRenderer:
    """headless Chromium 으로 페이지를 그려 HTML 을 얻는다.

    브라우저는 처음 쓸 때 띄우고 `close()` 까지 재사용한다. 페이지마다 새로
    띄우면 기동 비용(수 초)이 페이지 수만큼 붙는다. 반대로 탭은 매번 새로 연다 —
    이전 페이지의 쿠키·자바스크립트 상태가 다음 수집에 섞이지 않게 한다.

    Playwright 는 생성 시점이 아니라 **처음 렌더링할 때** import 한다. 정적
    학교만 수집하는 서버에 무거운 의존을 강제하지 않기 위해서다.
    """

    def __init__(
        self,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        settle_ms: int = DEFAULT_SETTLE_MS,
        user_agent: str | None = None,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.user_agent = user_agent
        self._playwright = None
        self._browser = None

    def _browser_or_start(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch()
        return self._browser

    def render(self, url: str, *, referer: str | None = None) -> str | None:
        try:
            browser = self._browser_or_start()
        except Exception:
            # Playwright 미설치·브라우저 미다운로드는 이 학교만의 문제가 아니라
            # 배포 문제다. 크롤 전체를 세우지 않고 정적 수집으로 넘긴다.
            LOGGER.exception("브라우저를 띄우지 못했습니다: %s", url)
            return None

        page = None
        try:
            page = browser.new_page(**({"user_agent": self.user_agent} if self.user_agent else {}))
            page.goto(
                url,
                wait_until="networkidle",
                timeout=self.timeout_ms,
                **({"referer": referer} if referer else {}),
            )
            page.wait_for_timeout(self.settle_ms)
            return page.content()
        except Exception as error:
            # 시간 초과·탐색 실패는 이 URL 하나의 문제다. 호출자가 실패로 기록한다.
            LOGGER.warning("렌더링 실패: %s (%s)", url, type(error).__name__)
            return None
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def close(self) -> None:
        for resource, stop in ((self._browser, "close"), (self._playwright, "stop")):
            if resource is None:
                continue
            try:
                getattr(resource, stop)()
            except Exception:
                LOGGER.warning("브라우저 정리 실패", exc_info=True)
        self._browser = None
        self._playwright = None
