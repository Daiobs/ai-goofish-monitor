from src.ai_message_builder import build_analysis_text_prompt


def test_analysis_prompt_requires_target_classification_before_price_comparison():
    prompt = build_analysis_text_prompt(
        '{"商品信息":{"商品标题":"虚构商品"}}',
        "只寻找970电池充电器",
        include_images=False,
    )

    assert "target_only" in prompt
    assert "target_bundle" in prompt
    assert "not_target" in prompt
    assert "uncertain" in prompt
    assert "market_comparable" in prompt
    assert "套装、非目标商品" in prompt
    assert "is_recommended 只有在 target_category 为 target_only" in prompt
