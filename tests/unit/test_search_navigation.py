import asyncio
from typing import Any

import pytest

from src.services.search_navigation import navigate_search_and_capture


class FakeRequest:
    def __init__(self, method: str, resource_type: str) -> None:
        self.method = method
        self.resource_type = resource_type


class FakeResponse:
    def __init__(
        self,
        url: str,
        payload: Any,
        *,
        method: str = "POST",
        resource_type: str = "xhr",
        status: int = 200,
        ok: bool = True,
    ) -> None:
        self.url = url
        self.payload = payload
        self.request = FakeRequest(method, resource_type)
        self.status = status
        self.ok = ok

    async def json(self) -> Any:
        return self.payload


class FakeLocator:
    def __init__(self, page: "FakePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "FakeLocator":
        return self

    async def is_visible(self, timeout: int) -> bool:
        assert timeout == 300
        return self.selector in self.page.visible_selectors

    async def inner_text(self, timeout: int) -> str:
        assert timeout == 500
        return self.page.body_text if self.selector == "body" else ""

    async def count(self) -> int:
        return int(self.selector in self.page.counted_selectors)


class FakePage:
    def __init__(
        self,
        *,
        responses: tuple[FakeResponse, ...] = (),
        initial_url: str = "https://www.goofish.com/",
        page_title: str = "闲鱼搜索",
        body_text: str = "",
        visible_selectors: tuple[str, ...] = (),
        counted_selectors: tuple[str, ...] = (),
    ) -> None:
        self.responses = responses
        self.url = initial_url
        self.page_title = page_title
        self.body_text = body_text
        self.visible_selectors = set(visible_selectors)
        self.counted_selectors = set(counted_selectors)
        self.response_listeners: list[Any] = []
        self.goto_calls: list[tuple[str, str, int]] = []

    def on(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.response_listeners.append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.response_listeners.remove(callback)

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        self.url = url
        for response in self.responses:
            for callback in tuple(self.response_listeners):
                callback(response)

    async def title(self) -> str:
        return self.page_title


def _navigate(page: FakePage, *, timeout_ms: int = 200):
    return asyncio.run(
        navigate_search_and_capture(
            page,
            "https://www.goofish.com/search?q=camera&trace=fixture",
            timeout_ms=timeout_ms,
        )
    )


def test_navigation_accepts_existing_exact_search_endpoint() -> None:
    payload = {"data": {"resultList": [{"itemId": "exact-1"}]}}
    response = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/?jsv=2.7.2&trace=fixture",
        payload,
    )
    page = FakePage(responses=(response,))

    result = _navigate(page)

    assert result.success is True
    assert result.failure_kind == "success"
    assert result.response is not None
    assert asyncio.run(result.response.json()) == payload
    assert result.source == (
        "POST https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/ status=200"
    )
    assert result.observed_requests == (result.source,)
    assert result.current_url == "https://www.goofish.com/search"
    assert page.response_listeners == []


def test_navigation_accepts_alternative_goofish_json_with_nested_result_list() -> None:
    items = [{"itemId": "alternative-1"}, {"itemId": "alternative-2"}]
    response = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search.shade/1.0/?trace=fixture",
        {"code": 0, "data": {"sections": [{"resultList": items}]}},
        method="GET",
        resource_type="fetch",
    )
    page = FakePage(responses=(response,))

    result = _navigate(page)

    assert result.success is True
    assert result.response is not None
    assert asyncio.run(result.response.json()) == {"data": {"resultList": items}}
    assert result.source == (
        "GET https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search.shade/1.0/ status=200"
    )
    assert "trace" not in result.source
    assert page.response_listeners == []


def test_navigation_identifies_login_before_the_navigation_timeout() -> None:
    page = FakePage(
        initial_url="https://passport.goofish.com/mini_login.htm?trace=fixture",
        page_title="登录",
    )

    async def run_with_short_guard():
        return await asyncio.wait_for(
            navigate_search_and_capture(
                page,
                "https://www.goofish.com/search?q=camera",
                timeout_ms=60_000,
            ),
            timeout=0.5,
        )

    result = asyncio.run(run_with_short_guard())

    assert result.success is False
    assert result.failure_kind == "login_required"
    assert "登录" in result.reason
    assert result.suggestion
    assert result.current_url == "https://passport.goofish.com/mini_login.htm"
    assert result.response is None
    assert page.response_listeners == []


def test_navigation_identifies_risk_control_before_the_navigation_timeout() -> None:
    page = FakePage(visible_selectors=("div.baxia-dialog-mask",))

    async def run_with_short_guard():
        return await asyncio.wait_for(
            navigate_search_and_capture(
                page,
                "https://www.goofish.com/search?q=camera",
                timeout_ms=60_000,
            ),
            timeout=0.5,
        )

    result = asyncio.run(run_with_short_guard())

    assert result.success is False
    assert result.failure_kind == "risk_control"
    assert "验证" in result.reason
    assert result.suggestion
    assert result.response is None
    assert page.response_listeners == []


def test_navigation_returns_structured_failure_when_no_response_arrives() -> None:
    page = FakePage(page_title="相机 - 闲鱼搜索")

    result = _navigate(page, timeout_ms=25)

    assert result.success is False
    assert result.failure_kind == "search_response_missing"
    assert result.reason == "搜索页面未产生可解析的商品数据响应"
    assert result.suggestion == "检查登录状态、验证页面和当前搜索接口"
    assert result.current_url == "https://www.goofish.com/search"
    assert result.page_title == "相机 - 闲鱼搜索"
    assert result.source is None
    assert result.response is None
    assert result.observed_requests == ()
    assert page.goto_calls == [
        (
            "https://www.goofish.com/search?q=camera&trace=fixture",
            "domcontentloaded",
            25,
        )
    ]
    assert page.response_listeners == []


def test_navigation_stops_immediately_on_api_risk_marker() -> None:
    response = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/",
        {"ret": ["FAIL_SYS_USER_VALIDATE::RGV587_ERROR"]},
    )

    result = _navigate(FakePage(responses=(response,)))

    assert result.success is False
    assert result.failure_kind == "risk_control"
    assert "FAIL_SYS_USER_VALIDATE" in result.reason


def test_navigation_rejects_non_success_http_search_response() -> None:
    response = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/",
        {"data": {"resultList": []}},
        status=503,
        ok=False,
    )

    result = _navigate(FakePage(responses=(response,)))

    assert result.success is False
    assert result.failure_kind == "search_page_failed"
    assert result.reason == "闲鱼搜索接口返回 HTTP 503"


def test_navigation_cancellation_propagates_and_removes_listener() -> None:
    class BlockingPage(FakePage):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.blocker = asyncio.Event()

        async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
            self.goto_calls.append((url, wait_until, timeout))
            self.url = url
            self.started.set()
            await self.blocker.wait()

    async def scenario() -> BlockingPage:
        page = BlockingPage()
        task = asyncio.create_task(
            navigate_search_and_capture(
                page,
                "https://www.goofish.com/search?q=camera",
                timeout_ms=60_000,
            )
        )
        await page.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return page

    page = asyncio.run(scenario())

    assert page.response_listeners == []
