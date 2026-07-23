import asyncio

from src.services.item_analysis_dispatcher import (
    ItemAnalysisDispatcher,
    ItemAnalysisJob,
)


def test_item_analysis_dispatcher_uses_bounded_concurrency():
    active_ai_calls = 0
    max_active_ai_calls = 0
    pending_records = []
    saved_records = []
    notifications = []

    async def seller_loader(user_id: str):
        await asyncio.sleep(0.005)
        return {"卖家ID": user_id}

    async def image_downloader(product_id: str, image_urls: list[str], task_name: str):
        return []

    async def ai_analyzer(record: dict, image_paths: list[str], prompt_text: str):
        nonlocal active_ai_calls, max_active_ai_calls
        active_ai_calls += 1
        max_active_ai_calls = max(max_active_ai_calls, active_ai_calls)
        await asyncio.sleep(0.03)
        active_ai_calls -= 1
        return {
            "analysis_source": "ai",
            "is_recommended": True,
            "reason": f"推荐 {record['商品信息']['商品ID']}",
            "keyword_hit_count": 0,
        }

    async def notifier(item_data: dict, reason: str):
        notifications.append((item_data["商品ID"], reason))

    async def saver(record: dict, keyword: str):
        pending_records.append((keyword, record))
        return True

    async def final_saver(record: dict, keyword: str):
        saved_records.append((keyword, record))
        return True

    async def run():
        dispatcher = ItemAnalysisDispatcher(
            concurrency=2,
            skip_ai_analysis=False,
            seller_loader=seller_loader,
            image_downloader=image_downloader,
            ai_analyzer=ai_analyzer,
            notifier=notifier,
            saver=saver,
            final_saver=final_saver,
        )
        for index in range(3):
            dispatcher.submit(
                ItemAnalysisJob(
                    keyword="demo",
                    task_name="Demo",
                    decision_mode="ai",
                    analyze_images=False,
                    prompt_text="prompt",
                    keyword_rules=(),
                    final_record={
                        "商品信息": {"商品ID": str(index), "商品图片列表": []},
                        "卖家信息": {},
                    },
                    seller_id=f"seller-{index}",
                    zhima_credit_text="优秀",
                    registration_duration_text="来闲鱼1年",
                )
            )
        await dispatcher.join()
        return dispatcher

    dispatcher = asyncio.run(run())
    assert dispatcher.completed_count == 3
    assert len(pending_records) == 3
    assert len(saved_records) == 3
    assert len(notifications) == 3
    assert max_active_ai_calls == 2
    assert all(
        record["ai_analysis"]["analysis_status"] == "pending"
        for _, record in pending_records
    )
    assert all(
        record["ai_analysis"]["analysis_status"] == "completed"
        for _, record in saved_records
    )
    assert all(
        record["卖家信息"]["卖家ID"].startswith("seller-")
        for _, record in saved_records
    )


def test_item_analysis_failure_is_saved_and_does_not_stop_other_items():
    saved_records = []
    notifications = []

    async def seller_loader(_user_id: str):
        return {}

    async def image_downloader(_product_id, _image_urls, _task_name):
        return []

    async def ai_analyzer(record: dict, _image_paths: list[str], _prompt_text: str):
        if record["商品信息"]["商品ID"] == "failed-item":
            raise RuntimeError("fictional AI outage")
        return {
            "analysis_source": "ai",
            "is_recommended": True,
            "reason": "通过验收规则",
            "keyword_hit_count": 0,
        }

    async def notifier(item_data: dict, reason: str):
        notifications.append((item_data["商品ID"], reason))

    async def saver(record: dict, _keyword: str):
        saved_records.append(record)
        return True

    async def run():
        dispatcher = ItemAnalysisDispatcher(
            concurrency=1,
            skip_ai_analysis=False,
            seller_loader=seller_loader,
            image_downloader=image_downloader,
            ai_analyzer=ai_analyzer,
            notifier=notifier,
            saver=saver,
        )
        for item_id in ("failed-item", "successful-item"):
            dispatcher.submit(
                ItemAnalysisJob(
                    keyword="demo",
                    task_name="Demo",
                    decision_mode="ai",
                    analyze_images=False,
                    prompt_text="prompt",
                    keyword_rules=(),
                    final_record={
                        "商品信息": {
                            "商品ID": item_id,
                            "商品标题": "演示商品",
                            "商品图片列表": [],
                        },
                        "卖家信息": {},
                    },
                    seller_id=None,
                    zhima_credit_text=None,
                    registration_duration_text="",
                )
            )
        await dispatcher.join()
        return dispatcher

    dispatcher = asyncio.run(run())

    assert dispatcher.completed_count == 2
    assert [record["商品信息"]["商品ID"] for record in saved_records] == [
        "failed-item",
        "failed-item",
        "successful-item",
        "successful-item",
    ]
    assert saved_records[0]["ai_analysis"]["analysis_status"] == "pending"
    failed_analysis = saved_records[1]["ai_analysis"]
    assert failed_analysis["analysis_status"] == "failed"
    assert failed_analysis["is_recommended"] is False
    assert failed_analysis["reason"] == "AI分析异常: RuntimeError"
    assert "fictional AI outage" not in str(failed_analysis)
    assert saved_records[2]["ai_analysis"]["analysis_status"] == "pending"
    assert saved_records[3]["ai_analysis"]["analysis_status"] == "completed"
    assert notifications == [("successful-item", "通过验收规则")]


def test_item_analysis_dispatcher_supports_keyword_mode_without_ai():
    saved_records = []

    async def seller_loader(user_id: str):
        return {"卖家标签": "个人闲置"}

    async def image_downloader(product_id: str, image_urls: list[str], task_name: str):
        raise AssertionError("关键词模式不应下载图片")

    async def ai_analyzer(record: dict, image_paths: list[str], prompt_text: str):
        raise AssertionError("关键词模式不应调用 AI")

    async def notifier(item_data: dict, reason: str):
        return None

    async def saver(record: dict, keyword: str):
        saved_records.append(record)
        return True

    async def run():
        dispatcher = ItemAnalysisDispatcher(
            concurrency=1,
            skip_ai_analysis=False,
            seller_loader=seller_loader,
            image_downloader=image_downloader,
            ai_analyzer=ai_analyzer,
            notifier=notifier,
            saver=saver,
        )
        dispatcher.submit(
            ItemAnalysisJob(
                keyword="demo",
                task_name="Demo",
                decision_mode="keyword",
                analyze_images=False,
                prompt_text="",
                keyword_rules=("个人闲置",),
                final_record={
                    "商品信息": {"商品ID": "1", "商品标题": "演示商品"},
                    "卖家信息": {},
                },
                seller_id="seller-1",
                zhima_credit_text="优秀",
                registration_duration_text="来闲鱼1年",
            )
        )
        await dispatcher.join()

    asyncio.run(run())
    assert saved_records[0]["ai_analysis"]["analysis_source"] == "keyword"
    assert saved_records[0]["ai_analysis"]["is_recommended"] is True


def test_skip_ai_analysis_never_recommends_or_notifies():
    saved_records = []
    notifications = []

    async def seller_loader(_user_id: str):
        return {}

    async def image_downloader(*_args):
        raise AssertionError("skipped AI must not download images")

    async def ai_analyzer(*_args):
        raise AssertionError("skipped AI must not call the model")

    async def notifier(item_data: dict, reason: str):
        notifications.append((item_data, reason))

    async def saver(record: dict, _keyword: str):
        saved_records.append(record)
        return True

    async def run():
        dispatcher = ItemAnalysisDispatcher(
            concurrency=1,
            skip_ai_analysis=True,
            seller_loader=seller_loader,
            image_downloader=image_downloader,
            ai_analyzer=ai_analyzer,
            notifier=notifier,
            saver=saver,
        )
        dispatcher.submit(
            ItemAnalysisJob(
                keyword="demo",
                task_name="Demo",
                decision_mode="ai",
                analyze_images=True,
                prompt_text="prompt",
                keyword_rules=(),
                final_record={
                    "商品信息": {
                        "商品ID": "skipped",
                        "商品标题": "synthetic item",
                        "商品图片列表": ["https://example.invalid/image.jpg"],
                    },
                    "卖家信息": {},
                },
                seller_id=None,
                zhima_credit_text=None,
                registration_duration_text="",
            )
        )
        await dispatcher.join()

    asyncio.run(run())

    skipped = saved_records[-1]["ai_analysis"]
    assert skipped == {
        "analysis_source": "ai",
        "analysis_status": "skipped",
        "is_recommended": False,
        "target_category": "uncertain",
        "market_comparable": False,
        "reason": "AI分析已禁用，商品未自动推荐。",
        "keyword_hit_count": 0,
    }
    assert notifications == []


def test_item_analysis_dispatcher_can_cancel_submitted_work():
    seller_started = asyncio.Event()
    seller_cancelled = asyncio.Event()
    saved_records = []

    async def seller_loader(_user_id: str):
        seller_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            seller_cancelled.set()

    async def image_downloader(_product_id, _image_urls, _task_name):
        return []

    async def ai_analyzer(_record, _image_paths, _prompt_text):
        return None

    async def notifier(_item_data, _reason):
        return None

    async def saver(record, _keyword):
        saved_records.append(record)
        return True

    async def run():
        dispatcher = ItemAnalysisDispatcher(
            concurrency=1,
            skip_ai_analysis=False,
            seller_loader=seller_loader,
            image_downloader=image_downloader,
            ai_analyzer=ai_analyzer,
            notifier=notifier,
            saver=saver,
        )
        dispatcher.submit(
            ItemAnalysisJob(
                keyword="demo",
                task_name="Demo",
                decision_mode="keyword",
                analyze_images=False,
                prompt_text="",
                keyword_rules=(),
                final_record={
                    "商品信息": {"商品ID": "1", "商品标题": "演示商品"},
                    "卖家信息": {},
                },
                seller_id="seller-1",
                zhima_credit_text="优秀",
                registration_duration_text="来闲鱼1年",
            )
        )
        await seller_started.wait()
        await dispatcher.cancel_and_join()
        await asyncio.sleep(0)

    asyncio.run(run())

    assert seller_cancelled.is_set()
    assert saved_records == []
