import asyncio
import json
from types import SimpleNamespace

import pytest

import src.ai_handler as ai_handler
import src.config as app_config


def _build_fake_client(responses_create_impl, chat_create_impl=None):
    responses = SimpleNamespace(create=responses_create_impl)
    chat = SimpleNamespace(
        completions=SimpleNamespace(create=chat_create_impl or responses_create_impl)
    )
    return SimpleNamespace(responses=responses, chat=chat)


def test_get_ai_analysis_stops_after_internal_retries_when_content_is_none(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    call_count = {"value": 0}

    async def fake_create(**_kwargs):
        call_count["value"] += 1
        return SimpleNamespace(output_text="")

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)
    with pytest.raises(ValueError, match="AI响应内容为空"):
        asyncio.run(
            ai_handler.get_ai_analysis(
                {"商品信息": {"商品ID": "1", "商品标题": "测试商品"}},
                image_paths=[],
                prompt_text="请输出 JSON",
            )
        )

    assert call_count["value"] == 2


def test_get_ai_analysis_returns_parsed_json(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    call_count = {"value": 0}
    requests = []

    async def fake_create(**kwargs):
        call_count["value"] += 1
        requests.append(kwargs)
        return SimpleNamespace(
            output_text=json.dumps(_valid_analysis(), ensure_ascii=False)
        )

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "REASONING_EFFORT", "xhigh")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)
    monotonic_values = iter((10.0, 12.3456))
    monkeypatch.setattr(ai_handler, "monotonic", lambda: next(monotonic_values))

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "2", "商品标题": "测试商品2"}},
            image_paths=[],
            prompt_text="请输出 JSON",
            prompt_version="EagleEye-V6.4",
        )
    )

    assert result["is_recommended"] is True
    assert result["prompt_version"] == "EagleEye-V6.4"
    assert "model_prompt_version_mismatch" not in result
    assert result["request_duration_seconds"] == 2.346
    assert call_count["value"] == 1
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert requests[0]["response_format"]["json_schema"]["strict"] is True
    assert requests[0]["reasoning_effort"] == "xhigh"


def test_get_ai_analysis_disables_sdk_level_retries(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    options = []

    async def fake_create(**_kwargs):
        return SimpleNamespace(
            output_text=json.dumps(_valid_analysis(), ensure_ascii=False)
        )

    request_client = _build_fake_client(fake_create)

    class RetryAwareClient:
        def with_options(self, **kwargs):
            options.append(kwargs)
            return request_client

    monkeypatch.setattr(ai_handler, "client", RetryAwareClient())
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "retry", "商品标题": "测试商品"}},
            image_paths=[],
            prompt_text="请输出 JSON",
        )
    )

    assert result["is_recommended"] is True
    assert options == [{"max_retries": 0}]


def test_get_ai_analysis_keeps_canonical_version_when_model_returns_mismatch(
    monkeypatch,
    tmp_path,
):
    monkeypatch.chdir(tmp_path)
    call_count = {"value": 0}

    async def fake_create(**_kwargs):
        call_count["value"] += 1
        response = _valid_analysis(
            is_recommended=False,
            reason="not suitable",
            risk_tags=["price"],
        )
        response["prompt_version"] = "model-controlled-secret-version"
        return SimpleNamespace(
            output_text=json.dumps(response, ensure_ascii=False)
        )

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "mismatch", "商品标题": "测试商品"}},
            image_paths=[],
            prompt_text='"prompt_version": "EagleEye-V6.4"',
        )
    )

    assert result["prompt_version"] == "EagleEye-V6.4"
    assert result["model_prompt_version_mismatch"] is True
    assert result["is_recommended"] is False
    assert result["reason"] == "not suitable"
    assert result["risk_tags"] == ["price"]
    assert "model-controlled-secret-version" not in str(result)
    assert call_count["value"] == 1


def test_get_ai_analysis_accepts_hash_prompt_version_without_model_metadata(
    monkeypatch,
    tmp_path,
):
    monkeypatch.chdir(tmp_path)

    async def fake_create(**_kwargs):
        return SimpleNamespace(
            output_text=json.dumps(_valid_analysis(), ensure_ascii=False)
        )

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "hash", "商品标题": "测试商品"}},
            image_paths=[],
            prompt_text="prompt without metadata",
        )
    )

    assert result["prompt_version"].startswith("sha256:")


@pytest.mark.parametrize(
    "invalid_response",
    (
        {
            "reason": "missing recommendation",
            "risk_tags": [],
            "criteria_analysis": {"seller_type": "个人"},
        },
        {
            "is_recommended": True,
            "reason": "",
            "risk_tags": [],
            "criteria_analysis": {"seller_type": "个人"},
        },
        {
            "is_recommended": True,
            "reason": "ok",
            "risk_tags": "invalid",
            "criteria_analysis": {"seller_type": "个人"},
        },
        {
            "is_recommended": True,
            "reason": "ok",
            "risk_tags": ["valid", 1],
            "criteria_analysis": {"seller_type": "个人"},
        },
        {
            "is_recommended": True,
            "reason": "ok",
            "risk_tags": [],
            "criteria_analysis": {},
        },
        {
            "is_recommended": True,
            "reason": "ok",
            "risk_tags": [],
            "criteria_analysis": {"value": "missing seller type"},
        },
        {
            "is_recommended": True,
            "reason": "ok",
            "risk_tags": [],
            "criteria_analysis": {"seller_type": ""},
        },
    ),
)
def test_normalize_ai_response_rejects_missing_or_invalid_semantics(
    invalid_response,
):
    assert (
        ai_handler.normalize_ai_response(invalid_response, "EagleEye-V6.4")
        is None
    )


def _valid_analysis(**overrides):
    criterion = {"status": "PASS", "comment": "ok", "evidence": "synthetic"}
    seller_detail = {"comment": "ok", "evidence": "synthetic"}
    analysis = {
        "is_recommended": True,
        "target_category": "target_only",
        "market_comparable": True,
        "reason": "clear decision",
        "risk_tags": [],
        "criteria_analysis": {
            "model_chip": dict(criterion),
            "battery_health": dict(criterion),
            "condition": dict(criterion),
            "history": dict(criterion),
            "seller_type": {
                "status": "PASS",
                "persona": "个人",
                "comment": "ok",
                "analysis_details": {
                    "temporal_analysis": dict(seller_detail),
                    "selling_behavior": dict(seller_detail),
                    "buying_behavior": dict(seller_detail),
                    "behavioral_summary": dict(seller_detail),
                },
            },
            "shipping": dict(criterion),
            "seller_credit": dict(criterion),
        },
    }
    analysis.update(overrides)
    return analysis


@pytest.mark.parametrize(
    "target_category",
    ("target_bundle", "not_target", "uncertain"),
)
def test_ai_response_rejects_non_target_marked_market_comparable(
    target_category,
):
    response = _valid_analysis(
        target_category=target_category,
        market_comparable=True,
    )

    assert ai_handler.get_ai_response_validation_errors(response) == [
        "market_comparable"
    ]
    assert ai_handler.normalize_ai_response(response, "EagleEye-V6.4") is None


@pytest.mark.parametrize(
    ("target_category", "market_comparable"),
    (
        ("target_bundle", False),
        ("not_target", False),
        ("uncertain", False),
        ("target_only", False),
    ),
)
def test_ai_response_rejects_recommendation_outside_comparable_target(
    target_category,
    market_comparable,
):
    response = _valid_analysis(
        is_recommended=True,
        target_category=target_category,
        market_comparable=market_comparable,
    )

    assert ai_handler.get_ai_response_validation_errors(response) == [
        "is_recommended"
    ]
    assert ai_handler.normalize_ai_response(response, "EagleEye-V6.4") is None


@pytest.mark.parametrize(
    ("response", "expected"),
    (
        ({"analysis": _valid_analysis(is_recommended=False)}, False),
        (_valid_analysis(is_recommended=None, recommended=True), True),
        (
            _valid_analysis(
                is_recommended=None,
                recommendation="recommended",
            ),
            True,
        ),
        (
            _valid_analysis(
                is_recommended=None,
                recommendation="not_recommended",
            ),
            False,
        ),
    ),
)
def test_normalize_ai_response_accepts_only_precise_legacy_shapes(
    response,
    expected,
):
    response = dict(response)
    if "analysis" not in response:
        response.pop("is_recommended", None)

    result = ai_handler.normalize_ai_response(response, "EagleEye-V6.4")

    assert result["is_recommended"] is expected


@pytest.mark.parametrize(
    "response",
    (
        _valid_analysis(is_recommended=None, recommendation="yes"),
        _valid_analysis(is_recommended=None, recommended=1),
        _valid_analysis(is_recommended=None),
    ),
)
def test_normalize_ai_response_rejects_ambiguous_recommendation_semantics(response):
    response = dict(response)
    response.pop("is_recommended", None)

    assert ai_handler.normalize_ai_response(response, "EagleEye-V6.4") is None


def test_summarize_ai_response_shape_contains_no_response_values():
    sentinel_reason = "reason-value-must-never-appear"
    sentinel_tag = "risk-value-must-never-appear"
    response = {
        "analysis": {
            "recommended": True,
            "reason": sentinel_reason,
            "risk_tags": [sentinel_tag],
        }
    }

    summary = ai_handler.summarize_ai_response_shape(response)
    rendered = str(summary)

    assert summary["top_level"] == {"analysis": "object"}
    assert summary["nested"]["analysis"] == {
        "recommended": "boolean",
        "reason": "string",
        "risk_tags": "array",
    }
    assert "analysis.recommended" in summary["boolean_candidates"]
    assert sentinel_reason not in rendered
    assert sentinel_tag not in rendered


def test_missing_recommendation_uses_at_most_two_distinct_contract_requests(
    monkeypatch,
    tmp_path,
    capsys,
):
    monkeypatch.chdir(tmp_path)
    requests = []
    diagnostics = {}

    async def fake_create(**kwargs):
        requests.append(kwargs)
        response = _valid_analysis(reason="response-value-must-not-be-logged")
        response.pop("is_recommended")
        return SimpleNamespace(
            output_text=json.dumps(response, ensure_ascii=False)
        )

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    with pytest.raises(ValueError, match="AI响应格式缺少必需字段"):
        asyncio.run(
            ai_handler.get_ai_analysis(
                {"商品信息": {"商品ID": "missing", "商品标题": "测试商品"}},
                image_paths=[],
                prompt_text="请输出 JSON",
                diagnostics=diagnostics,
            )
        )

    output = capsys.readouterr().out
    assert len(requests) == 2
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert requests[1]["tools"][0]["function"]["strict"] is True
    assert diagnostics["request_count"] == 2
    assert diagnostics["final_failure_fields"] == ["is_recommended"]
    assert diagnostics["last_response_shape"]["top_level"]["reason"] == "string"
    assert "response-value-must-not-be-logged" not in output
    assert "response-value-must-not-be-logged" not in str(diagnostics)


def test_get_ai_analysis_uses_function_tool_when_model_rejects_schema(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        if len(request_history) == 1:
            raise Exception(
                "Error code: 400 - {'error': {'code': 'InvalidParameter', "
                "'message': 'The parameter `response_format.type` specified in "
                "the request is not valid: `json_schema` is not supported by "
                "this model.', 'param': 'response_format.type'}}"
            )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(
                                    arguments=json.dumps(
                                        _valid_analysis(reason="ok"),
                                        ensure_ascii=False,
                                    )
                                )
                            )
                        ],
                    )
                )
            ]
        )

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "3", "商品标题": "测试商品3"}},
            image_paths=[],
            prompt_text="请输出 JSON",
        )
    )

    assert result["reason"] == "ok"
    assert request_history[0]["messages"][0]["role"] == "user"
    assert request_history[0]["response_format"]["type"] == "json_schema"
    assert request_history[1]["tools"][0]["function"]["strict"] is True
    assert request_history[1]["tool_choice"]["type"] == "function"
    assert ai_handler.ENABLE_RESPONSE_FORMAT is True


def test_get_ai_analysis_falls_back_to_responses_when_chat_completions_api_is_missing(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    request_history = []

    async def fake_chat_create(**kwargs):
        request_history.append(("chat", kwargs))
        raise Exception("Error code: 404 - page not found")

    async def fake_responses_create(**kwargs):
        request_history.append(("responses", kwargs))
        return SimpleNamespace(
            output_text=json.dumps(_valid_analysis(reason="ok"), ensure_ascii=False)
        )

    monkeypatch.setattr(
        ai_handler,
        "client",
        _build_fake_client(fake_responses_create, fake_chat_create),
    )
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "4", "商品标题": "测试商品4"}},
            image_paths=[],
            prompt_text="请输出 JSON",
        )
    )

    assert result["reason"] == "ok"
    assert request_history[0][0] == "chat"
    assert request_history[0][1]["messages"][0]["role"] == "user"
    assert request_history[1][0] == "responses"
    assert request_history[1][1]["text"]["format"]["type"] == "json_schema"
    assert len(request_history) == 2


def test_get_ai_analysis_retries_without_temperature_when_gateway_rejects_it(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        if len(request_history) == 1:
            raise Exception("temperature is unsupported for this model")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            _valid_analysis(reason="ok"),
                            ensure_ascii=False,
                        )
                    )
                )
            ]
        )

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "4", "商品标题": "测试商品4"}},
            image_paths=[],
            prompt_text="请输出 JSON",
        )
    )

    assert result["reason"] == "ok"
    assert request_history[0]["temperature"] == 0.1
    assert "temperature" not in request_history[1]


def test_get_ai_analysis_preserves_unstructured_legacy_mode_when_disabled(
    monkeypatch,
    tmp_path,
):
    monkeypatch.chdir(tmp_path)
    requests = []

    async def fake_create(**kwargs):
        requests.append(kwargs)
        response = _valid_analysis()
        response.pop("is_recommended")
        response["recommended"] = True
        return SimpleNamespace(
            output_text=json.dumps({"analysis": response}, ensure_ascii=False)
        )

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", False)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", False)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "legacy", "商品标题": "测试商品"}},
            image_paths=[],
            prompt_text="请输出 JSON",
        )
    )

    assert result["is_recommended"] is True
    assert "recommended" not in result
    assert "response_format" not in requests[0]
    assert "tools" not in requests[0]


def test_get_ai_analysis_uses_first_json_object_when_model_returns_multiple_objects(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)

    async def fake_create(**_kwargs):
        first = _valid_analysis(reason="first")
        first["prompt_version"] = "v1"
        second = _valid_analysis(is_recommended=False, reason="second")
        second["prompt_version"] = "v1"
        return SimpleNamespace(
            output_text=(
                "```json\n"
                + json.dumps(first, ensure_ascii=False)
                + "\n"
                + json.dumps(second, ensure_ascii=False)
                + "\n```"
            )
        )

    monkeypatch.setattr(ai_handler, "client", _build_fake_client(fake_create))
    monkeypatch.setattr(ai_handler, "MODEL_NAME", "fake-model")
    monkeypatch.setattr(ai_handler, "ENABLE_RESPONSE_FORMAT", True)
    monkeypatch.setattr(app_config, "ENABLE_RESPONSE_FORMAT", True)

    result = asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": "5", "商品标题": "测试商品5"}},
            image_paths=[],
            prompt_text="请输出 JSON",
        )
    )

    assert result["is_recommended"] is True
    assert result["reason"] == "first"
