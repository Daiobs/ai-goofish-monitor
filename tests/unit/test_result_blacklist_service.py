import pytest

from src.services import result_blacklist_service as blacklist_service
from src.services.result_blacklist_service import match_blacklist_keywords


def _build_record(title: str) -> dict:
    return {
        "商品信息": {
            "商品标题": title,
        },
        "卖家信息": {},
    }


def test_regex_blacklist_rule_matches_aliases_case_insensitively():
    keywords = [r"re:\b(pm|pro[\s-]?max)\b"]

    assert match_blacklist_keywords(_build_record("iPhone 15 Pm 256G"), keywords) == keywords
    assert match_blacklist_keywords(_build_record("iPhone 15 Pro Max 256G"), keywords) == keywords
    assert match_blacklist_keywords(_build_record("iPhone 15 promax 256G"), keywords) == keywords
    assert match_blacklist_keywords(_build_record("iPhone 15 pro-max 256G"), keywords) == keywords


def test_regex_blacklist_rule_does_not_hide_plain_pro_models():
    keywords = [r"re:\b(pm|pro[\s-]?max)\b"]

    assert match_blacklist_keywords(_build_record("iPhone 15 Pro 256G"), keywords) == []


def test_empty_blacklist_rules_do_not_read_malformed_record(monkeypatch):
    def fail_build_search_text(_record):
        raise AssertionError("empty rules must not inspect the record")

    monkeypatch.setattr(
        blacklist_service,
        "build_search_text",
        fail_build_search_text,
    )

    assert match_blacklist_keywords({"商品信息": []}, []) == []


@pytest.mark.parametrize(
    "record",
    [
        {"商品信息": []},
        {"商品信息": "invalid"},
        {"ai_analysis": []},
        {"卖家信息": []},
    ],
)
def test_nonempty_blacklist_rules_safely_ignore_malformed_records(record):
    assert match_blacklist_keywords(record, ["fictional rule"]) == []


def test_null_mapping_fields_remain_valid_and_match_other_mapping_text():
    record = {
        "商品信息": None,
        "卖家信息": {"卖家昵称": "Fictional Blocked Seller"},
        "ai_analysis": None,
    }

    assert match_blacklist_keywords(record, ["blocked seller"]) == [
        "blocked seller"
    ]
