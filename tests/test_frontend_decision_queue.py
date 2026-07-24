from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_repo_file(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_canonical_results_use_task_decision_endpoint_and_legacy_is_preserved():
    api = read_repo_file("web-ui/src/api/results.ts")
    composable = read_repo_file("web-ui/src/composables/useResults.ts")

    assert "`/api/results/tasks/${taskId}`" in api
    assert "decision_view: decisionView" in api
    assert "getTaskResultContent(taskId, decisionView.value" in composable
    assert "getResultContent(selectedFile.value" in composable
    assert "ref<DecisionView>('worth_viewing')" in composable


def test_decision_queue_uses_fixed_views_and_summary_contract():
    api = read_repo_file("web-ui/src/api/results.ts")
    component = read_repo_file(
        "web-ui/src/components/results/ResultsDecisionQueue.vue"
    )
    expected_views = {
        "worth_viewing",
        "comparable_targets",
        "bundles",
        "excluded",
        "ai_issues",
    }
    expected_summary_fields = {
        "all_count",
        "target_only_count",
        "target_bundle_count",
        "not_target_count",
        "uncertain_count",
        "comparable_count",
        "excluded_count",
        "ai_recommended_count",
        "ai_not_recommended_count",
        "ai_issue_count",
    }

    for key in expected_views:
        assert f"'{key}'" in api
    for field in expected_summary_fields:
        assert f"{field}: number" in api
        assert f"key: '{field}'" in component

    assert "<Tabs" in component
    assert "<TabsTrigger" in component
    assert "overflow-x-auto" in component
    assert "currentViewCount" in component


def test_decision_queue_has_bilingual_copy_and_exact_worth_empty_state():
    zh_cn = read_repo_file("web-ui/src/i18n/messages/zh-CN.ts")
    en_us = read_repo_file("web-ui/src/i18n/messages/en-US.ts")

    assert "当前没有 AI 推荐且可比的独立目标商品" in zh_cn
    for key in (
        "worth_viewing",
        "comparable_targets",
        "bundles",
        "excluded",
        "ai_issues",
    ):
        assert f"{key}:" in zh_cn
        assert f"{key}:" in en_us
    assert "currentViewCount:" in zh_cn
    assert "currentViewCount:" in en_us


def test_result_card_never_fabricates_a_missing_value_score():
    card = read_repo_file("web-ui/src/components/results/ResultCard.vue")

    assert "value_score ?? 0" not in card
    assert "typeof ai?.value_score === 'number'" in card
    assert 'v-if="matchScore !== null"' in card
    assert "exclusionReason" in card
    assert "marketPositionLabel" in card
    assert "analysisStatusLabel" in card
    assert "priceInsight?.market_median_price ?? priceInsight?.median_price" not in card
