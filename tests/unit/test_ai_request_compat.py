from src.services.ai_request_compat import (
    AI_ANALYSIS_SCHEMA,
    CHAT_COMPLETIONS_API_MODE,
    FUNCTION_TOOL_OUTPUT_MODE,
    JSON_SCHEMA_OUTPUT_MODE,
    RESPONSES_API_MODE,
    build_ai_request_params,
    is_json_output_unsupported_error,
    is_output_mode_unsupported_error,
    is_responses_api_unsupported_error,
    is_temperature_unsupported_error,
    remove_temperature_param,
)


MESSAGES = [{"role": "user", "content": "analyze"}]


def test_build_chat_request_with_strict_analysis_schema():
    params = build_ai_request_params(
        CHAT_COMPLETIONS_API_MODE,
        model="test-model",
        messages=MESSAGES,
        output_mode=JSON_SCHEMA_OUTPUT_MODE,
    )

    response_format = params["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"] == AI_ANALYSIS_SCHEMA
    assert "prompt_version" not in AI_ANALYSIS_SCHEMA["properties"]
    assert "request_duration_seconds" not in AI_ANALYSIS_SCHEMA["properties"]
    assert "analysis_status" not in AI_ANALYSIS_SCHEMA["properties"]
    assert "analysis_source" not in AI_ANALYSIS_SCHEMA["properties"]
    assert AI_ANALYSIS_SCHEMA["properties"]["target_category"]["enum"] == [
        "target_only",
        "target_bundle",
        "not_target",
        "uncertain",
    ]
    assert "target_category" in AI_ANALYSIS_SCHEMA["required"]
    assert "market_comparable" in AI_ANALYSIS_SCHEMA["required"]


def test_build_responses_request_with_strict_analysis_schema():
    params = build_ai_request_params(
        RESPONSES_API_MODE,
        model="test-model",
        messages=MESSAGES,
        output_mode=JSON_SCHEMA_OUTPUT_MODE,
    )

    output_format = params["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["strict"] is True
    assert output_format["schema"] == AI_ANALYSIS_SCHEMA


def test_build_chat_request_with_forced_strict_function_tool():
    params = build_ai_request_params(
        CHAT_COMPLETIONS_API_MODE,
        model="test-model",
        messages=MESSAGES,
        output_mode=FUNCTION_TOOL_OUTPUT_MODE,
    )

    function = params["tools"][0]["function"]
    assert function["strict"] is True
    assert function["parameters"] == AI_ANALYSIS_SCHEMA
    assert params["tool_choice"]["function"]["name"] == function["name"]
    assert params["parallel_tool_calls"] is False


def test_build_responses_request_with_forced_strict_function_tool():
    params = build_ai_request_params(
        RESPONSES_API_MODE,
        model="test-model",
        messages=MESSAGES,
        output_mode=FUNCTION_TOOL_OUTPUT_MODE,
    )

    function = params["tools"][0]
    assert function["strict"] is True
    assert function["parameters"] == AI_ANALYSIS_SCHEMA
    assert params["tool_choice"]["name"] == function["name"]
    assert params["parallel_tool_calls"] is False


def test_is_temperature_unsupported_error_detects_unsupported_message():
    err = Exception("temperature is not supported by this gateway")
    assert is_temperature_unsupported_error(err) is True


def test_remove_temperature_param_removes_only_temperature():
    params = {"model": "x", "temperature": 0.5, "max_output_tokens": 128}
    result = remove_temperature_param(params)

    assert "temperature" not in result
    assert result["model"] == "x"
    assert result["max_output_tokens"] == 128


def test_is_responses_api_unsupported_error_detects_gemini_plain_404():
    class _Resp:
        text = ""

    class _Err(Exception):
        status_code = 404
        body = ""
        response = _Resp()

        def __str__(self):
            return "Error code: 404"

    assert is_responses_api_unsupported_error(_Err()) is True


# -- is_json_output_unsupported_error tests --


def test_json_output_error_detected_via_body_param_response_format():
    """Vercel AI Gateway returns 400 with param='response_format'."""

    class _Err(Exception):
        body = {
            "message": "Invalid input",
            "type": "invalid_request_error",
            "param": "response_format",
            "code": "invalid_request_error",
        }

    assert is_json_output_unsupported_error(_Err()) is True


def test_json_output_error_detected_via_body_param_response_format_type():
    class _Err(Exception):
        body = {
            "message": "Invalid input",
            "param": "response_format.type",
        }

    assert is_json_output_unsupported_error(_Err()) is True


def test_json_output_error_detected_via_legacy_string_matching():
    err = Exception(
        "response_format.type is not supported by this model"
    )
    assert is_json_output_unsupported_error(err) is True


def test_json_output_error_not_triggered_by_unrelated_400():
    class _Err(Exception):
        body = {
            "message": "Invalid input",
            "param": "messages",
        }

    assert is_json_output_unsupported_error(_Err()) is False


def test_json_output_error_not_triggered_without_body():
    err = Exception("some random 400 error")
    assert is_json_output_unsupported_error(err) is False


def test_schema_rejection_is_scoped_to_schema_mode():
    err = Exception("response_format json_schema is not supported")

    assert is_output_mode_unsupported_error(err, JSON_SCHEMA_OUTPUT_MODE) is True
    assert is_output_mode_unsupported_error(err, FUNCTION_TOOL_OUTPUT_MODE) is False


def test_tool_rejection_is_scoped_to_function_mode():
    err = Exception("tool_choice is unsupported by this gateway")

    assert is_output_mode_unsupported_error(err, FUNCTION_TOOL_OUTPUT_MODE) is True
    assert is_output_mode_unsupported_error(err, JSON_SCHEMA_OUTPUT_MODE) is False
