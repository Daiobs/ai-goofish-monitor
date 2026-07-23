from src.services.price_history_service import (
    build_item_price_context,
    build_market_reference,
    build_price_history_insights,
    load_price_snapshots,
    record_market_snapshots,
)


def test_record_market_snapshots_and_build_price_history_insights(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen_item_ids = set()

    run1_items = [
        {
            "商品ID": "1001",
            "商品标题": "Sony A7M4 单机",
            "当前售价": "¥10000",
            "商品标签": ["验货宝"],
            "发货地区": "上海",
            "卖家昵称": "卖家A",
            "商品链接": "https://www.goofish.com/item?id=1001",
            "发布时间": "2026-01-01 09:00",
        },
        {
            "商品ID": "1002",
            "商品标题": "Sony A7M4 套机",
            "当前售价": "¥12000",
            "商品标签": ["包邮"],
            "发货地区": "杭州",
            "卖家昵称": "卖家B",
            "商品链接": "https://www.goofish.com/item?id=1002",
            "发布时间": "2026-01-01 10:00",
        },
    ]
    run2_items = [
        {
            "商品ID": "1001",
            "商品标题": "Sony A7M4 单机",
            "当前售价": "¥9500",
            "商品标签": ["验货宝"],
            "发货地区": "上海",
            "卖家昵称": "卖家A",
            "商品链接": "https://www.goofish.com/item?id=1001",
            "发布时间": "2026-01-02 09:00",
        },
        {
            "商品ID": "1003",
            "商品标题": "Sony A7M4 全套",
            "当前售价": "¥13000",
            "商品标签": ["同城"],
            "发货地区": "南京",
            "卖家昵称": "卖家C",
            "商品链接": "https://www.goofish.com/item?id=1003",
            "发布时间": "2026-01-02 11:00",
        },
    ]

    inserted_run1 = record_market_snapshots(
        keyword="sony a7m4",
        task_name="Sony A7M4 监控",
        items=run1_items,
        run_id="run-1",
        snapshot_time="2026-01-01T12:00:00",
        seen_item_ids=seen_item_ids,
    )
    assert len(inserted_run1) == 2

    inserted_run2 = record_market_snapshots(
        keyword="sony a7m4",
        task_name="Sony A7M4 监控",
        items=run2_items,
        run_id="run-2",
        snapshot_time="2026-01-02T12:00:00",
        seen_item_ids=set(),
    )
    assert len(inserted_run2) == 2

    snapshots = load_price_snapshots("sony a7m4")
    assert len(snapshots) == 4

    insights = build_price_history_insights("sony a7m4")
    assert insights["market_summary"]["sample_count"] == 2
    assert insights["market_summary"]["avg_price"] == 11250.0
    assert insights["market_summary"]["min_price"] == 9500.0
    assert insights["history_summary"]["unique_items"] == 3
    assert len(insights["daily_trend"]) == 2
    assert insights["daily_trend"][0]["day"] == "2026-01-01"
    assert insights["daily_trend"][1]["day"] == "2026-01-02"

    item_context = build_item_price_context(
        snapshots,
        item_id="1001",
        current_price=9500.0,
    )
    assert item_context["observation_count"] == 2
    assert item_context["min_price"] == 9500.0
    assert item_context["max_price"] == 10000.0
    assert item_context["price_change_amount"] == -500.0
    assert item_context["deal_label"] == "高性价比"


def test_market_reference_uses_only_confirmed_comparable_items():
    current_items = [
        {"商品ID": "charger", "当前售价": "20"},
        {"商品ID": "battery", "当前售价": "200"},
        {"商品ID": "bundle", "当前售价": "300"},
    ]
    snapshots = [
        {
            "item_id": "charger",
            "price": 20.0,
            "run_id": "run-1",
            "snapshot_time": "2026-07-24T10:00:00",
        },
        {
            "item_id": "battery",
            "price": 200.0,
            "run_id": "run-1",
            "snapshot_time": "2026-07-24T10:00:00",
        },
        {
            "item_id": "bundle",
            "price": 300.0,
            "run_id": "run-1",
            "snapshot_time": "2026-07-24T10:00:00",
        },
    ]

    reference = build_market_reference(
        keyword="970电池充电器",
        item=current_items[0],
        current_market_items=current_items,
        historical_snapshots=snapshots,
        comparable_item_ids={"charger"},
    )
    battery_reference = build_market_reference(
        keyword="970电池充电器",
        item=current_items[1],
        current_market_items=current_items,
        historical_snapshots=snapshots,
        comparable_item_ids={"charger"},
    )

    assert reference["当前搜索样本"]["sample_count"] == 1
    assert reference["当前搜索样本"]["avg_price"] == 20.0
    assert reference["历史价格概览"]["avg_price"] == 20.0
    assert reference["本商品价格位置"]["market_comparable"] is True
    assert battery_reference["本商品价格位置"]["market_comparable"] is None
    assert battery_reference["本商品价格位置"]["deal_score"] is None
    assert battery_reference["本商品价格位置"]["deal_label"] == "待AI分类"


def test_latest_run_without_comparable_items_does_not_reuse_old_market_summary(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    record_market_snapshots(
        task_id=88,
        keyword="970电池充电器",
        task_name="970 charger",
        items=[{"商品ID": "charger", "当前售价": "20"}],
        run_id="run-1",
        snapshot_time="2026-07-23T10:00:00",
    )
    record_market_snapshots(
        task_id=88,
        keyword="970电池充电器",
        task_name="970 charger",
        items=[{"商品ID": "battery", "当前售价": "200"}],
        run_id="run-2",
        snapshot_time="2026-07-24T10:00:00",
    )

    insights = build_price_history_insights(
        task_id=88,
        visible_item_ids={"charger"},
    )

    assert insights["market_summary"]["sample_count"] == 0
    assert insights["market_summary"]["avg_price"] is None
    assert insights["market_summary"]["snapshot_time"] == "2026-07-24T10:00:00"
    assert insights["history_summary"]["sample_count"] == 1
    assert insights["history_summary"]["avg_price"] == 20.0
    assert insights["latest_snapshot_at"] == "2026-07-24T10:00:00"
