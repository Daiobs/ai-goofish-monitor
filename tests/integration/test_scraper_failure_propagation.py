import asyncio
from types import SimpleNamespace

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src import scraper


class FakeFailureGuard:
    threshold = 1

    def __init__(self):
        self.failure_calls = []
        self.success_calls = []

    def should_skip_start(self, task_name, *, cookie_path=None):
        return SimpleNamespace(
            skip=False,
            should_notify=False,
            reason="",
            paused_until=None,
            consecutive_failures=0,
        )

    def record_failure(self, task_name, reason, **kwargs):
        self.failure_calls.append((task_name, reason, kwargs))
        return {
            "should_notify": True,
            "consecutive_failures": 1,
            "paused_until": None,
        }

    def record_success(self, task_name):
        self.success_calls.append(task_name)


class FakeDispatcher:
    def __init__(self, **_kwargs):
        self.jobs = []
        self.join_calls = 0
        self.cancel_and_join_calls = 0

    def submit(self, job):
        self.jobs.append(job)

    async def join(self):
        self.join_calls += 1

    async def cancel_and_join(self):
        self.cancel_and_join_calls += 1


class FakeResponseInfo:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    @property
    def value(self):
        return self._resolve()

    async def _resolve(self):
        return self._response


class FakeResponse:
    def __init__(self, payload, *, url="https://h5api.m.goofish.com/mock"):
        self._payload = payload
        self.url = url
        self.ok = True
        self.status = 200

    async def json(self):
        return self._payload


class FakeLocator:
    def __init__(self, selector, environment):
        self.selector = selector
        self.environment = environment

    @property
    def first(self):
        return self

    async def wait_for(self, *, state, timeout):
        expected = {
            "baxia": "div.baxia-dialog-mask",
            "middleware": "div.J_MIDDLEWARE_FRAME_WIDGET",
        }.get(self.environment.mode)
        if self.selector == expected:
            return None
        raise PlaywrightTimeoutError(f"{self.selector} is not visible")

    async def is_visible(self):
        return False


class FakeMainPage:
    def __init__(self, environment):
        self.environment = environment
        self.url = "about:blank"
        self.closed = False

    async def goto(self, url, **_kwargs):
        self.environment.main_goto_urls.append(url)
        if self.environment.mode == "cancel" and url == "https://www.goofish.com/":
            self.environment.cancel_started.set()
            await self.environment.cancel_blocker.wait()
        if "search?" in url and self.environment.login_url:
            self.url = self.environment.login_url
        else:
            self.url = url

    async def evaluate(self, _script):
        return None

    def expect_response(self, _predicate, timeout):
        return FakeResponseInfo(self.environment.search_response)

    async def wait_for_selector(self, _selector, timeout):
        return None

    def locator(self, selector):
        return FakeLocator(selector, self.environment)

    async def click(self, _selector, timeout=None):
        raise PlaywrightTimeoutError("no ad dialog")

    async def close(self):
        self.closed = True


class FakeDetailPage:
    def __init__(self, environment, response):
        self.environment = environment
        self.response = response
        self.url = "about:blank"
        self.closed = False

    def expect_response(self, _predicate, timeout):
        return FakeResponseInfo(self.response)

    async def goto(self, url, **_kwargs):
        self.environment.detail_goto_urls.append(url)
        self.url = self.environment.detail_login_url or url

    async def close(self):
        self.closed = True
        if self.environment.detail_close_error is not None:
            raise self.environment.detail_close_error


class FakeContext:
    def __init__(self, environment):
        self.environment = environment
        self.closed = False
        self.main_page = None
        self.detail_pages = []

    async def add_init_script(self, _script):
        return None

    async def new_page(self):
        if self.main_page is None:
            self.main_page = FakeMainPage(self.environment)
            return self.main_page
        response = self.environment.next_detail_response()
        page = FakeDetailPage(self.environment, response)
        self.detail_pages.append(page)
        return page

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, environment):
        self.environment = environment
        self.context = None
        self.closed = False

    async def new_context(self, **_kwargs):
        self.context = FakeContext(self.environment)
        self.environment.contexts.append(self.context)
        return self.context

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, environment):
        self.environment = environment

    async def launch(self, **_kwargs):
        self.environment.launch_calls += 1
        if self.environment.launch_errors:
            error = self.environment.launch_errors.pop(0)
            if error is not None:
                raise error
        browser = FakeBrowser(self.environment)
        self.environment.browsers.append(browser)
        return browser


class FakePlaywright:
    def __init__(self, environment):
        self.chromium = FakeChromium(environment)


class FakePlaywrightManager:
    def __init__(self, environment):
        self.playwright = FakePlaywright(environment)

    async def __aenter__(self):
        return self.playwright

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class ScrapeEnvironment:
    def __init__(
        self,
        *,
        mode="normal",
        items=None,
        detail_payloads=None,
        login_url=None,
        detail_login_url=None,
        detail_close_error=None,
        launch_errors=None,
    ):
        self.mode = mode
        self.items = list(items or [])
        self.detail_payloads = list(detail_payloads or [])
        self.login_url = login_url
        self.detail_login_url = detail_login_url
        self.detail_close_error = detail_close_error
        self.launch_errors = list(launch_errors or [])
        self.search_response = FakeResponse({"search": "ok"})
        self.main_goto_urls = []
        self.detail_goto_urls = []
        self.browsers = []
        self.contexts = []
        self.dispatchers = []
        self.advance_calls = 0
        self.cleanup_calls = []
        self.notifications = []
        self.launch_calls = 0
        self.cancel_started = asyncio.Event()
        self.cancel_blocker = asyncio.Event()

    def next_detail_response(self):
        if not self.detail_payloads:
            raise AssertionError("unexpected item detail page")
        return FakeResponse(self.detail_payloads.pop(0))


def _task_config(state_path, *, max_pages=2):
    return {
        "task_name": "risk-control-task",
        "keyword": "camera",
        "max_pages": max_pages,
        "decision_mode": "keyword",
        "keyword_rules": [],
        "account_state_file": str(state_path),
    }


def _item(item_id):
    return {
        "商品ID": item_id,
        "商品标题": f"item-{item_id}",
        "商品链接": f"https://www.goofish.com/item?id={item_id}",
        "商品图片列表": [],
    }


def _success_detail_payload():
    return {
        "ret": ["SUCCESS"],
        "data": {
            "itemDO": {"imageInfos": [], "wantCnt": 0, "browseCnt": 0},
            "sellerDO": {"sellerId": None, "userRegDay": 30},
        },
    }


def _install_scraper_fakes(monkeypatch, environment, guard, *, retry_limit=1):
    monkeypatch.setattr(
        scraper,
        "async_playwright",
        lambda: FakePlaywrightManager(environment),
    )
    monkeypatch.setattr(scraper, "FAILURE_GUARD", guard)
    monkeypatch.setattr(scraper, "load_price_snapshots", lambda _keyword: [])
    monkeypatch.setattr(scraper, "load_processed_link_keys", lambda _keyword: set())
    monkeypatch.setattr(scraper, "record_market_snapshots", lambda **_kwargs: [])
    monkeypatch.setattr(scraper, "build_market_reference", lambda **_kwargs: {})
    monkeypatch.setattr(scraper, "load_state_files", lambda _directory: [])
    monkeypatch.setattr(scraper, "parse_proxy_pool", lambda _value: [])
    monkeypatch.setattr(
        scraper,
        "_get_rotation_settings",
        lambda _task: {
            "account_state_dir": "unused",
            "account_blacklist_ttl": 60,
            "proxy_pool": [],
            "proxy_blacklist_ttl": 60,
            "account_enabled": False,
            "proxy_enabled": False,
            "account_mode": "per_task",
            "proxy_mode": "per_task",
            "account_retry_limit": retry_limit,
            "proxy_retry_limit": retry_limit,
        },
    )

    async def parse_items(_payload, _page_label):
        return list(environment.items)

    async def no_sleep(*_args, **_kwargs):
        return None

    async def advance_page(**_kwargs):
        environment.advance_calls += 1
        raise AssertionError("crawler must not advance after terminal failure")

    async def notify(_product_data, reason):
        environment.notifications.append(reason)

    def build_dispatcher(**kwargs):
        dispatcher = FakeDispatcher(**kwargs)
        environment.dispatchers.append(dispatcher)
        return dispatcher

    monkeypatch.setattr(scraper, "_parse_search_results_json", parse_items)
    monkeypatch.setattr(scraper, "random_sleep", no_sleep)
    monkeypatch.setattr(scraper.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(scraper, "advance_search_page", advance_page)
    monkeypatch.setattr(scraper, "send_ntfy_notification", notify)
    monkeypatch.setattr(scraper, "ItemAnalysisDispatcher", build_dispatcher)
    monkeypatch.setattr(
        scraper,
        "cleanup_task_images",
        lambda task_name: environment.cleanup_calls.append(task_name),
    )


def _run_scrape(environment, guard, task_config, monkeypatch, *, retry_limit=1):
    _install_scraper_fakes(
        monkeypatch, environment, guard, retry_limit=retry_limit
    )
    return asyncio.run(scraper.scrape_xianyu(task_config))


def test_first_detail_risk_control_stops_items_pages_and_analysis(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    environment = ScrapeEnvironment(
        items=[_item("first"), _item("second")],
        detail_payloads=[{"ret": ["FAIL_SYS_USER_VALIDATE"]}],
        detail_close_error=RuntimeError("secondary detail close failure"),
    )
    guard = FakeFailureGuard()

    with pytest.raises(scraper.ScrapeTaskFailed) as exc_info:
        _run_scrape(environment, guard, _task_config(state_path), monkeypatch)

    failure = exc_info.value
    assert failure.failure_kind == "risk_control"
    assert failure.reason == "FAIL_SYS_USER_VALIDATE"
    assert failure.processed_item_count == 0
    assert environment.detail_goto_urls == [
        "https://www.goofish.com/item?id=first"
    ]
    assert environment.advance_calls == 0
    assert environment.dispatchers[0].jobs == []
    assert guard.failure_calls == [
        (
            "risk-control-task",
            "FAIL_SYS_USER_VALIDATE",
            {"cookie_path": str(state_path), "min_failures_to_pause": None},
        )
    ]
    assert len(environment.notifications) == 1
    assert environment.cleanup_calls == ["risk-control-task"]
    assert environment.dispatchers[0].join_calls == 0
    assert environment.dispatchers[0].cancel_and_join_calls == 1
    assert environment.contexts[0].detail_pages[0].closed is True
    assert environment.contexts[0].main_page.closed is True
    assert environment.contexts[0].closed is True
    assert environment.browsers[0].closed is True


@pytest.mark.parametrize(
    ("mode", "reason"),
    [("baxia", "baxia-dialog"), ("middleware", "J_MIDDLEWARE_FRAME_WIDGET")],
)
def test_risk_control_overlays_terminate_immediately(
    tmp_path, monkeypatch, mode, reason
):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    environment = ScrapeEnvironment(mode=mode, items=[_item("first")])
    guard = FakeFailureGuard()

    with pytest.raises(scraper.ScrapeTaskFailed) as exc_info:
        _run_scrape(environment, guard, _task_config(state_path), monkeypatch)

    assert exc_info.value.failure_kind == "risk_control"
    assert exc_info.value.reason == reason
    assert environment.detail_goto_urls == []
    assert environment.advance_calls == 0
    assert environment.dispatchers[0].jobs == []
    assert len(guard.failure_calls) == 1
    assert len(environment.notifications) == 1


@pytest.mark.parametrize(
    "login_url",
    [
        "https://passport.goofish.com/login.htm",
        "https://www.goofish.com/mini_login.htm",
    ],
)
def test_login_redirect_terminates_without_item_requests(
    tmp_path, monkeypatch, login_url
):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    environment = ScrapeEnvironment(
        items=[_item("first")],
        login_url=login_url,
    )
    guard = FakeFailureGuard()

    with pytest.raises(scraper.ScrapeTaskFailed) as exc_info:
        _run_scrape(environment, guard, _task_config(state_path), monkeypatch)

    assert exc_info.value.failure_kind == "login_required"
    assert login_url in exc_info.value.reason
    assert environment.detail_goto_urls == []
    assert environment.advance_calls == 0
    assert environment.dispatchers[0].jobs == []
    assert len(guard.failure_calls) == 1
    assert len(environment.notifications) == 1


def test_detail_login_redirect_stops_before_second_item(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    environment = ScrapeEnvironment(
        items=[_item("first"), _item("second")],
        detail_payloads=[_success_detail_payload()],
        detail_login_url="https://www.goofish.com/mini_login.htm",
    )
    guard = FakeFailureGuard()

    with pytest.raises(scraper.ScrapeTaskFailed) as exc_info:
        _run_scrape(environment, guard, _task_config(state_path), monkeypatch)

    assert exc_info.value.failure_kind == "login_required"
    assert environment.detail_goto_urls == [
        "https://www.goofish.com/item?id=first"
    ]
    assert environment.contexts[0].detail_pages[0].closed is True
    assert environment.advance_calls == 0
    assert environment.dispatchers[0].jobs == []


def test_scrape_success_returns_processed_count(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    environment = ScrapeEnvironment(
        items=[_item("first")],
        detail_payloads=[_success_detail_payload()],
    )
    guard = FakeFailureGuard()

    result = _run_scrape(
        environment,
        guard,
        _task_config(state_path, max_pages=1),
        monkeypatch,
    )

    assert result == 1
    assert len(environment.dispatchers[0].jobs) == 1
    assert environment.dispatchers[0].join_calls == 1
    assert environment.dispatchers[0].cancel_and_join_calls == 0
    assert guard.failure_calls == []
    assert guard.success_calls == ["risk-control-task"]
    assert environment.cleanup_calls == ["risk-control-task"]
    assert environment.contexts[0].detail_pages[0].closed is True
    assert environment.contexts[0].main_page.closed is True
    assert environment.contexts[0].closed is True
    assert environment.browsers[0].closed is True


def test_debug_limit_preserves_success_and_stops_after_limit(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    environment = ScrapeEnvironment(
        items=[_item("first"), _item("second")],
        detail_payloads=[_success_detail_payload()],
    )
    guard = FakeFailureGuard()
    input_calls = []
    _install_scraper_fakes(monkeypatch, environment, guard)
    monkeypatch.setattr(
        "builtins.input", lambda prompt: input_calls.append(prompt) or ""
    )

    result = asyncio.run(
        scraper.scrape_xianyu(
            _task_config(state_path, max_pages=2), debug_limit=1
        )
    )

    assert result == 1
    assert environment.detail_goto_urls == [
        "https://www.goofish.com/item?id=first"
    ]
    assert len(environment.dispatchers[0].jobs) == 1
    assert input_calls == ["按回车键关闭浏览器..."]


def test_transient_runtime_error_still_retries(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    environment = ScrapeEnvironment(
        items=[],
        launch_errors=[RuntimeError("temporary browser launch failure"), None],
    )
    guard = FakeFailureGuard()

    result = _run_scrape(
        environment,
        guard,
        _task_config(state_path, max_pages=1),
        monkeypatch,
        retry_limit=2,
    )

    assert result == 0
    assert environment.launch_calls == 2
    assert guard.failure_calls == []
    assert guard.success_calls == ["risk-control-task"]
    assert environment.cleanup_calls == ["risk-control-task"]


def test_final_runtime_error_is_reported_once_after_retries(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    environment = ScrapeEnvironment(
        launch_errors=[
            RuntimeError("temporary browser launch failure"),
            RuntimeError("permanent browser launch failure"),
        ],
    )
    guard = FakeFailureGuard()

    with pytest.raises(scraper.ScrapeTaskFailed) as exc_info:
        _run_scrape(
            environment,
            guard,
            _task_config(state_path, max_pages=1),
            monkeypatch,
            retry_limit=2,
        )

    assert exc_info.value.failure_kind == "runtime_error"
    assert environment.launch_calls == 2
    assert len(guard.failure_calls) == 1
    assert len(environment.notifications) == 1
    assert environment.cleanup_calls == ["risk-control-task"]


def test_user_cancellation_cleans_resources_without_failure_notification(
    tmp_path, monkeypatch
):
    async def scenario():
        state_path = tmp_path / "state.json"
        state_path.write_text("{}", encoding="utf-8")
        environment = ScrapeEnvironment(mode="cancel")
        guard = FakeFailureGuard()
        _install_scraper_fakes(monkeypatch, environment, guard)

        task = asyncio.create_task(
            scraper.scrape_xianyu(_task_config(state_path, max_pages=1))
        )
        await environment.cancel_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return environment, guard

    environment, guard = asyncio.run(scenario())

    assert guard.failure_calls == []
    assert environment.notifications == []
    assert environment.cleanup_calls == ["risk-control-task"]
    assert environment.dispatchers[0].join_calls == 0
    assert environment.dispatchers[0].cancel_and_join_calls == 1
    assert environment.contexts[0].main_page.closed is True
    assert environment.contexts[0].closed is True
    assert environment.browsers[0].closed is True
