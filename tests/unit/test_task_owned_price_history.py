from src.infrastructure.persistence.storage_names import build_task_result_filename
from src.services import result_file_service
from src.services.price_history_service import (
    build_price_history_insights,
    build_task_price_history_insights,
    delete_price_snapshots,
    delete_task_price_snapshots,
    load_price_snapshots,
    load_task_price_snapshots,
    record_market_snapshots,
)


def _item(item_id: str, price: int) -> dict:
    return {
        "商品ID": item_id,
        "商品标题": f"Camera {item_id}",
        "当前售价": str(price),
        "商品链接": f"https://www.goofish.com/item?id={item_id}",
    }


def _record(item_id: str, price: int) -> dict:
    return {
        "搜索关键字": "camera",
        "商品信息": {
            "商品ID": item_id,
            "商品标题": f"Camera {item_id}",
            "当前售价": str(price),
        },
    }


def _record_run(*, task_id: int | None, prices: tuple[int, int], run_id: str) -> None:
    record_market_snapshots(
        task_id=task_id,
        keyword="camera",
        task_name="Camera monitor",
        items=[_item("shared", prices[0]), _item("peer", prices[1])],
        run_id=run_id,
        snapshot_time="2026-07-14T08:00:00",
        seen_item_ids=set(),
    )


def test_same_keyword_task_snapshots_insights_and_deletes_are_isolated(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)

    _record_run(task_id=71, prices=(100, 200), run_id="same-run")
    _record_run(task_id=72, prices=(1000, 2000), run_id="same-run")
    _record_run(task_id=None, prices=(500, 700), run_id="same-run")

    task_a = load_task_price_snapshots(71)
    task_b = load_price_snapshots(task_id=72)
    legacy = load_price_snapshots("camera")

    assert [snapshot["price"] for snapshot in task_a] == [100.0, 200.0]
    assert [snapshot["price"] for snapshot in task_b] == [1000.0, 2000.0]
    assert [snapshot["price"] for snapshot in legacy] == [500.0, 700.0]
    assert {snapshot["task_id"] for snapshot in task_a} == {71}
    assert {snapshot["task_id"] for snapshot in task_b} == {72}
    assert {snapshot["task_id"] for snapshot in legacy} == {None}

    task_a_insights = build_task_price_history_insights(71)
    task_b_insights = build_price_history_insights(task_id=72)
    legacy_insights = build_price_history_insights("camera")
    assert task_a_insights["market_summary"]["sample_count"] == 2
    assert task_a_insights["market_summary"]["avg_price"] == 150.0
    assert task_b_insights["market_summary"]["sample_count"] == 2
    assert task_b_insights["market_summary"]["avg_price"] == 1500.0
    assert legacy_insights["market_summary"]["avg_price"] == 600.0

    assert delete_price_snapshots("ignored", task_id=71) == 2
    assert load_task_price_snapshots(71) == []
    assert len(load_task_price_snapshots(72)) == 2
    assert len(load_price_snapshots("camera")) == 2

    assert delete_price_snapshots("camera") == 2
    assert load_price_snapshots("camera") == []
    assert len(load_task_price_snapshots(72)) == 2
    assert delete_task_price_snapshots(72) == 2


def test_canonical_task_filename_enriches_from_only_its_task_snapshots(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)

    _record_run(task_id=81, prices=(100, 200), run_id="run-1")
    _record_run(task_id=81, prices=(80, 180), run_id="run-2")
    _record_run(task_id=82, prices=(1000, 2000), run_id="run-1")
    _record_run(task_id=82, prices=(800, 1800), run_id="run-2")

    visible_calls = []

    def load_visible_task_ids(task_id: int) -> set[str]:
        visible_calls.append(task_id)
        return {"shared", "peer"}

    def fail_legacy_visibility(_filename: str) -> set[str]:
        raise AssertionError("canonical task filenames must not use legacy visibility")

    monkeypatch.setattr(
        result_file_service,
        "load_visible_task_result_item_ids",
        load_visible_task_ids,
    )
    monkeypatch.setattr(
        result_file_service,
        "load_visible_result_item_ids",
        fail_legacy_visibility,
    )

    task_a = result_file_service.enrich_records_with_price_insight(
        [_record("shared", 80)],
        build_task_result_filename(81),
    )
    task_b = result_file_service.enrich_records_with_price_insight(
        [_record("shared", 800)],
        build_task_result_filename(82),
    )

    assert visible_calls == [81, 82]
    assert task_a[0]["price_insight"]["observation_count"] == 2
    assert task_a[0]["price_insight"]["avg_price"] == 90.0
    assert task_a[0]["price_insight"]["market_avg_price"] == 130.0
    assert task_b[0]["price_insight"]["observation_count"] == 2
    assert task_b[0]["price_insight"]["avg_price"] == 900.0
    assert task_b[0]["price_insight"]["market_avg_price"] == 1300.0

