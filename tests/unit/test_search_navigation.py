import asyncio
import json
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
        content_type: str = "application/json; charset=utf-8",
    ) -> None:
        self.url = url
        self.payload = payload
        self.request = FakeRequest(method, resource_type)
        self.status = status
        self.ok = ok
        self.headers = {"content-type": content_type}

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


def _search_item(item_id: str = "fixture-1") -> dict[str, Any]:
    return {
        "data": {
            "item": {
                "main": {
                    "exContent": {
                        "itemId": item_id,
                        "title": "redacted",
                    },
                    "targetUrl": f"fleamarket://item?id={item_id}",
                }
            }
        }
    }


def test_navigation_accepts_existing_exact_search_endpoint() -> None:
    payload = {
        "ret": ["SUCCESS::fixture detail"],
        "data": {"resultList": [_search_item("exact-1")]},
    }
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
    items = [
        _search_item("alternative-1"),
        _search_item("alternative-2"),
    ]
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


def test_navigation_ignores_unrelated_non_success_xhr_before_search_result() -> None:
    unrelated = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get/1.0/"
        "?access_token=fixture-secret",
        {
            "ret": ["FAIL_SYS_TOKEN_EMPTY::fixture detail"],
            "data": {"siteConfig": {"token": "fixture-secret"}},
        },
    )
    search_payload = {
        "ret": ["SUCCESS::fixture detail"],
        "data": {"resultList": [_search_item("search-1")]},
    }
    search = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/",
        search_payload,
    )

    result = _navigate(FakePage(responses=(unrelated, search)))

    assert result.success is True
    assert result.response is not None
    assert asyncio.run(result.response.json()) == search_payload
    assert len(result.diagnostics) == 2
    assert result.diagnostics[0].is_search_response is False
    assert result.diagnostics[0].ret_codes == ("FAIL_SYS_TOKEN_EMPTY",)
    assert result.diagnostics[1].is_search_response is True
    serialized = json.dumps(
        [diagnostic.to_dict() for diagnostic in result.diagnostics]
    )
    assert "fixture-secret" not in serialized
    assert "fixture detail" not in serialized


def test_navigation_ignores_search_activation_before_search_result() -> None:
    activation = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.item.search.activate/1.0/",
        {
            "ret": ["SUCCESS::fixture detail"],
            "data": {"activated": True},
        },
    )
    search_payload = {
        "ret": ["SUCCESS::fixture detail"],
        "data": {"resultList": [_search_item("search-1")]},
    }
    search = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/",
        search_payload,
    )

    result = _navigate(FakePage(responses=(activation, search)))

    assert result.success is True
    assert result.result_count == 1
    assert len(result.diagnostics) == 2
    assert result.diagnostics[0].is_search_response is False
    assert result.diagnostics[0].has_result_list is False
    assert result.diagnostics[1].is_search_response is True


def test_navigation_rejects_unknown_non_success_search_with_results() -> None:
    response = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/",
        {
            "ret": ["FAIL_BIZ_UNKNOWN::fixture sensitive detail"],
            "data": {"resultList": [_search_item()]},
        },
    )

    result = _navigate(FakePage(responses=(response,)))

    assert result.success is False
    assert result.failure_kind == "search_page_failed"
    assert result.reason == "闲鱼搜索接口返回失败状态 (FAIL_BIZ_UNKNOWN)"
    assert "fixture sensitive detail" not in result.reason


def test_navigation_rejects_mixed_success_and_unknown_failure_status() -> None:
    response = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/",
        {
            "ret": [
                "SUCCESS::fixture detail",
                "FAIL_BIZ_UNKNOWN::fixture detail",
            ],
            "data": {"resultList": [_search_item()]},
        },
    )

    result = _navigate(FakePage(responses=(response,)))

    assert result.success is False
    assert result.failure_kind == "search_page_failed"
    assert result.reason == "闲鱼搜索接口返回失败状态 (FAIL_BIZ_UNKNOWN)"


def test_navigation_rejects_non_success_search_without_result_list() -> None:
    response = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/",
        {"ret": ["FAIL_BIZ_BUSY::fixture detail"], "data": {}},
    )

    result = _navigate(FakePage(responses=(response,)))

    assert result.success is False
    assert result.failure_kind == "search_page_failed"
    assert result.reason == "闲鱼搜索接口返回失败状态 (FAIL_BIZ_BUSY)"


def test_navigation_accepts_successful_empty_search_result() -> None:
    response = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/",
        {"ret": ["SUCCESS::fixture detail"], "data": {"resultList": []}},
    )

    result = _navigate(FakePage(responses=(response,)))

    assert result.success is True
    assert result.result_count == 0
    assert result.response is not None
    assert asyncio.run(result.response.json())["data"]["resultList"] == []


def test_navigation_rejects_unparseable_search_item_structure() -> None:
    response = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/",
        {
            "ret": ["SUCCESS::fixture detail"],
            "data": {"resultList": [{"item": "redacted"}]},
        },
    )

    result = _navigate(FakePage(responses=(response,)))

    assert result.success is False
    assert result.failure_kind == "search_parse_failed"
    assert result.reason == "闲鱼搜索商品数据结构无法解析"


def test_navigation_rejects_non_json_search_response() -> None:
    class NonJsonResponse(FakeResponse):
        async def json(self) -> Any:
            raise ValueError("fixture response body must not be logged")

    response = NonJsonResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/",
        None,
        content_type="text/html; charset=utf-8",
    )

    result = _navigate(FakePage(responses=(response,)))

    assert result.success is False
    assert result.failure_kind == "search_parse_failed"
    assert result.reason == "闲鱼搜索数据响应不是可解析的 JSON"
    assert result.diagnostics[-1].json_parsed is False
    assert result.diagnostics[-1].content_type == "text/html"


def test_navigation_reports_http_error_before_non_json_error() -> None:
    class NonJsonResponse(FakeResponse):
        async def json(self) -> Any:
            raise ValueError("fixture response body must not be logged")

    response = NonJsonResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/",
        None,
        status=503,
        ok=False,
        content_type="text/html",
    )

    result = _navigate(FakePage(responses=(response,)))

    assert result.success is False
    assert result.failure_kind == "search_page_failed"
    assert result.reason == "闲鱼搜索接口返回 HTTP 503"


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


def test_navigation_stops_on_risk_marker_even_with_usable_results() -> None:
    response = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/",
        {
            "ret": ["FAIL_SYS_USER_VALIDATE::fixture detail"],
            "data": {"resultList": [_search_item()]},
        },
    )

    result = _navigate(FakePage(responses=(response,)))

    assert result.success is False
    assert result.failure_kind == "risk_control"
    assert result.diagnostics[-1].risk_marker is True


def test_navigation_stops_on_login_marker_even_with_usable_results() -> None:
    response = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/",
        {
            "ret": ["FAIL_SYS_TOKEN_EXPIRED::fixture detail"],
            "data": {"resultList": [_search_item()]},
        },
    )

    result = _navigate(FakePage(responses=(response,)))

    assert result.success is False
    assert result.failure_kind == "login_required"
    assert result.diagnostics[-1].login_marker is True


def test_navigation_stops_on_expired_login_probe_before_search_result() -> None:
    login_probe = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemessage.pc.loginuser.get/1.0/",
        {"ret": ["FAIL_SYS_SESSION_EXPIRED::fixture detail"], "data": {}},
    )
    search = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/",
        {
            "ret": ["SUCCESS::fixture detail"],
            "data": {"resultList": [_search_item()]},
        },
    )

    result = _navigate(FakePage(responses=(login_probe, search)))

    assert result.success is False
    assert result.failure_kind == "login_required"
    assert result.diagnostics[-1].api_path.endswith(
        "idlemessage.pc.loginuser.get/1.0/"
    )
    assert result.diagnostics[-1].login_marker is True
    assert len(result.diagnostics) == 1


def test_navigation_rejects_non_success_http_search_response() -> None:
    response = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/",
        {"ret": ["SUCCESS::fixture detail"], "data": {"resultList": [_search_item()]}},
        status=503,
        ok=False,
    )

    result = _navigate(FakePage(responses=(response,)))

    assert result.success is False
    assert result.failure_kind == "search_page_failed"
    assert result.reason == "闲鱼搜索接口返回 HTTP 503"


def test_navigation_diagnostic_contains_structure_without_values() -> None:
    response = FakeResponse(
        "https://h5api.m.goofish.com/h5/"
        "mtop.taobao.idlemtopsearch.pc.search/1.0/"
        "?token=fixture-query-secret#fixture-fragment",
        {
            "ret": ["SUCCESS::fixture-ret-secret"],
            "data": {"resultList": [_search_item("fixture-product-secret")]},
        },
    )

    result = _navigate(FakePage(responses=(response,)))
    serialized = json.dumps(result.diagnostics[-1].to_dict())

    assert result.success is True
    assert result.diagnostics[-1].api_path.endswith("/1.0/")
    assert result.diagnostics[-1].content_type == "application/json"
    assert result.diagnostics[-1].result_count == 1
    assert result.diagnostics[-1].first_result_is_object is True
    assert "fixture-query-secret" not in serialized
    assert "fixture-fragment" not in serialized
    assert "fixture-ret-secret" not in serialized
    assert "fixture-product-secret" not in serialized


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
