"""AI 请求兼容性辅助逻辑。"""

import copy
from typing import Any, Dict, Iterable, List


RESPONSES_API_MODE = "responses"
CHAT_COMPLETIONS_API_MODE = "chat_completions"
INPUT_TEXT_TYPE = "input_text"
INPUT_IMAGE_TYPE = "input_image"
IMAGE_DETAIL_AUTO = "auto"
JSON_OUTPUT_TYPE = "json_object"
JSON_SCHEMA_OUTPUT_MODE = "json_schema"
FUNCTION_TOOL_OUTPUT_MODE = "function_tool"
JSON_OBJECT_OUTPUT_MODE = JSON_OUTPUT_TYPE
TEXT_OUTPUT_MODE = "text"
AI_ANALYSIS_SCHEMA_NAME = "goofish_product_analysis"
AI_ANALYSIS_TOOL_NAME = "submit_goofish_analysis"
REASONING_EFFORT_VALUES = (
    "none",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
TARGET_CATEGORY_TARGET_ONLY = "target_only"
TARGET_CATEGORY_TARGET_BUNDLE = "target_bundle"
TARGET_CATEGORY_NOT_TARGET = "not_target"
TARGET_CATEGORY_UNCERTAIN = "uncertain"
TARGET_CATEGORY_VALUES = (
    TARGET_CATEGORY_TARGET_ONLY,
    TARGET_CATEGORY_TARGET_BUNDLE,
    TARGET_CATEGORY_NOT_TARGET,
    TARGET_CATEGORY_UNCERTAIN,
)
UNSUPPORTED_JSON_OUTPUT_MARKERS = (
    "not supported by this model",
    "json_object",
    "json_schema",
    "text.format",
    "response_format.type",
)
RESPONSES_API_UNSUPPORTED_MARKERS = (
    "404 page not found",
    "page not found",
    "/responses",
    "/v1/responses",
)
CHAT_COMPLETIONS_API_UNSUPPORTED_MARKERS = (
    "404 page not found",
    "page not found",
    "/chat/completions",
    "/v1/chat/completions",
)
UNSUPPORTED_TEMPERATURE_MARKERS = (
    "temperature",
    "sampling temperature",
)


def _nonempty_string_schema() -> Dict[str, Any]:
    return {"type": "string", "minLength": 1}


def _criterion_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": _nonempty_string_schema(),
            "comment": _nonempty_string_schema(),
            "evidence": _nonempty_string_schema(),
        },
        "required": ["status", "comment", "evidence"],
    }


def _seller_analysis_detail_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "comment": _nonempty_string_schema(),
            "evidence": _nonempty_string_schema(),
        },
        "required": ["comment", "evidence"],
    }


AI_ANALYSIS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "is_recommended": {"type": "boolean"},
        "target_category": {
            "type": "string",
            "enum": list(TARGET_CATEGORY_VALUES),
        },
        "market_comparable": {"type": "boolean"},
        "reason": _nonempty_string_schema(),
        "risk_tags": {
            "type": "array",
            "items": {"type": "string"},
        },
        "criteria_analysis": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "model_chip": _criterion_schema(),
                "battery_health": _criterion_schema(),
                "condition": _criterion_schema(),
                "history": _criterion_schema(),
                "seller_type": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "status": _nonempty_string_schema(),
                        "persona": _nonempty_string_schema(),
                        "comment": _nonempty_string_schema(),
                        "analysis_details": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "temporal_analysis": _seller_analysis_detail_schema(),
                                "selling_behavior": _seller_analysis_detail_schema(),
                                "buying_behavior": _seller_analysis_detail_schema(),
                                "behavioral_summary": _seller_analysis_detail_schema(),
                            },
                            "required": [
                                "temporal_analysis",
                                "selling_behavior",
                                "buying_behavior",
                                "behavioral_summary",
                            ],
                        },
                    },
                    "required": [
                        "status",
                        "persona",
                        "comment",
                        "analysis_details",
                    ],
                },
                "shipping": _criterion_schema(),
                "seller_credit": _criterion_schema(),
            },
            "required": [
                "model_chip",
                "battery_health",
                "condition",
                "history",
                "seller_type",
                "shipping",
                "seller_credit",
            ],
        },
    },
    "required": [
        "is_recommended",
        "target_category",
        "market_comparable",
        "reason",
        "risk_tags",
        "criteria_analysis",
    ],
}


def build_responses_input(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将 Chat Completions 风格的消息转换为 Responses API 输入。"""
    input_items: List[Dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        input_items.append(
            {
                "role": role,
                "content": _build_input_content(message.get("content")),
            }
        )
    return input_items


def add_json_text_format(
    request_params: Dict[str, Any],
    enabled: bool,
) -> Dict[str, Any]:
    """按需附加 Responses API 的结构化 JSON 输出参数。"""
    next_params = dict(request_params)
    if not enabled:
        return next_params

    text_config = dict(next_params.get("text") or {})
    text_config["format"] = {"type": JSON_OUTPUT_TYPE}
    next_params["text"] = text_config
    return next_params


def add_json_response_format(
    request_params: Dict[str, Any],
    enabled: bool,
) -> Dict[str, Any]:
    """按需附加 Chat Completions 的 JSON 输出参数。"""
    next_params = dict(request_params)
    if enabled:
        next_params["response_format"] = {"type": JSON_OUTPUT_TYPE}
    return next_params


def add_output_contract(
    request_params: Dict[str, Any],
    api_mode: str,
    output_mode: str,
    analysis_schema: Dict[str, Any],
) -> Dict[str, Any]:
    """Attach one explicit, side-effect-free analysis output contract."""
    next_params = dict(request_params)
    schema = copy.deepcopy(analysis_schema)

    if output_mode == TEXT_OUTPUT_MODE:
        return next_params
    if output_mode == JSON_OBJECT_OUTPUT_MODE:
        if api_mode == RESPONSES_API_MODE:
            return add_json_text_format(next_params, True)
        return add_json_response_format(next_params, True)
    if output_mode == JSON_SCHEMA_OUTPUT_MODE:
        if api_mode == RESPONSES_API_MODE:
            next_params["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": AI_ANALYSIS_SCHEMA_NAME,
                    "strict": True,
                    "schema": schema,
                }
            }
        else:
            next_params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": AI_ANALYSIS_SCHEMA_NAME,
                    "strict": True,
                    "schema": schema,
                },
            }
        return next_params
    if output_mode == FUNCTION_TOOL_OUTPUT_MODE:
        if api_mode == RESPONSES_API_MODE:
            next_params["tools"] = [
                {
                    "type": "function",
                    "name": AI_ANALYSIS_TOOL_NAME,
                    "description": "Return the structured product analysis.",
                    "parameters": schema,
                    "strict": True,
                }
            ]
            next_params["tool_choice"] = {
                "type": "function",
                "name": AI_ANALYSIS_TOOL_NAME,
            }
        else:
            next_params["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": AI_ANALYSIS_TOOL_NAME,
                        "description": "Return the structured product analysis.",
                        "parameters": schema,
                        "strict": True,
                    },
                }
            ]
            next_params["tool_choice"] = {
                "type": "function",
                "function": {"name": AI_ANALYSIS_TOOL_NAME},
            }
        next_params["parallel_tool_calls"] = False
        return next_params

    raise ValueError(f"不支持的 AI 输出模式: {output_mode}")


def is_json_output_unsupported_error(error: Exception) -> bool:
    """识别模型或网关不支持结构化 JSON 输出参数的错误。"""
    body = getattr(error, "body", None)
    if isinstance(body, dict) and body.get("param") in (
        "response_format",
        "response_format.type",
    ):
        return True

    message = str(error)
    return (
        "not supported" in message.lower()
        and any(marker in message for marker in UNSUPPORTED_JSON_OUTPUT_MARKERS)
    )


def is_output_mode_unsupported_error(error: Exception, output_mode: str) -> bool:
    """Identify an explicit gateway rejection of the selected output mode."""
    body = getattr(error, "body", None)
    error_body = body.get("error") if isinstance(body, dict) else None
    body_params = {
        candidate.get("param")
        for candidate in (body, error_body)
        if isinstance(candidate, dict)
    }
    message_parts = [str(error)]
    for candidate in (body, error_body):
        if isinstance(candidate, dict):
            message_parts.append(str(candidate.get("message") or ""))
    message = " ".join(message_parts).lower()
    unsupported = any(
        marker in message
        for marker in ("not supported", "unsupported", "not implemented", "invalid")
    )

    if output_mode == JSON_SCHEMA_OUTPUT_MODE:
        schema_params = {
            "response_format",
            "response_format.type",
            "response_format.json_schema",
            "text",
            "text.format",
            "text.format.type",
        }
        return bool(body_params & schema_params) or (
            unsupported
            and any(
                marker in message
                for marker in ("json_schema", "response_format", "text.format")
            )
        )
    if output_mode == FUNCTION_TOOL_OUTPUT_MODE:
        tool_params = {"tools", "tool_choice", "parallel_tool_calls"}
        return bool(body_params & tool_params) or (
            unsupported
            and any(
                marker in message
                for marker in (
                    "tool_choice",
                    "tools",
                    "function calling",
                    "function_call",
                )
            )
        )
    if output_mode == JSON_OBJECT_OUTPUT_MODE:
        return is_json_output_unsupported_error(error)
    return False


def is_responses_api_unsupported_error(error: Exception) -> bool:
    """识别 OpenAI 兼容服务未实现 Responses API 的错误。"""
    return _is_api_unsupported_error(error, RESPONSES_API_UNSUPPORTED_MARKERS)


def is_chat_completions_api_unsupported_error(error: Exception) -> bool:
    """识别 OpenAI 兼容服务未实现 Chat Completions API 的错误。"""
    return _is_api_unsupported_error(error, CHAT_COMPLETIONS_API_UNSUPPORTED_MARKERS)


def build_ai_request_params(
    api_mode: str,
    *,
    model: str,
    messages: Iterable[Dict[str, Any]],
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
    enable_json_output: bool = False,
    output_mode: str | None = None,
    analysis_schema: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """根据 API 模式构建请求参数。"""
    request_params = {"model": model}
    normalized_reasoning_effort = normalize_reasoning_effort(reasoning_effort)
    if api_mode == RESPONSES_API_MODE:
        request_params["input"] = build_responses_input(messages)
        if max_output_tokens is not None:
            request_params["max_output_tokens"] = max_output_tokens
        if temperature is not None:
            request_params["temperature"] = temperature
        if normalized_reasoning_effort is not None:
            request_params["reasoning"] = {
                "effort": normalized_reasoning_effort,
            }
        selected_mode = output_mode or (
            JSON_OBJECT_OUTPUT_MODE if enable_json_output else TEXT_OUTPUT_MODE
        )
        return add_output_contract(
            request_params,
            api_mode,
            selected_mode,
            analysis_schema or AI_ANALYSIS_SCHEMA,
        )

    if api_mode == CHAT_COMPLETIONS_API_MODE:
        request_params["messages"] = copy.deepcopy(list(messages))
        if max_output_tokens is not None:
            request_params["max_tokens"] = max_output_tokens
        if temperature is not None:
            request_params["temperature"] = temperature
        if normalized_reasoning_effort is not None:
            request_params["reasoning_effort"] = normalized_reasoning_effort
        selected_mode = output_mode or (
            JSON_OBJECT_OUTPUT_MODE if enable_json_output else TEXT_OUTPUT_MODE
        )
        return add_output_contract(
            request_params,
            api_mode,
            selected_mode,
            analysis_schema or AI_ANALYSIS_SCHEMA,
        )

    raise ValueError(f"不支持的 AI API 模式: {api_mode}")


def normalize_reasoning_effort(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized not in REASONING_EFFORT_VALUES:
        supported = ", ".join(REASONING_EFFORT_VALUES)
        raise ValueError(f"不支持的 reasoning effort；可选值: {supported}")
    return normalized


async def create_ai_response_async(
    client: Any,
    api_mode: str,
    request_params: Dict[str, Any],
) -> Any:
    """根据 API 模式发起异步请求。"""
    if api_mode == RESPONSES_API_MODE:
        return await client.responses.create(**request_params)
    if api_mode == CHAT_COMPLETIONS_API_MODE:
        return await client.chat.completions.create(**request_params)
    raise ValueError(f"不支持的 AI API 模式: {api_mode}")


def create_ai_response_sync(
    client: Any,
    api_mode: str,
    request_params: Dict[str, Any],
) -> Any:
    """根据 API 模式发起同步请求。"""
    if api_mode == RESPONSES_API_MODE:
        return client.responses.create(**request_params)
    if api_mode == CHAT_COMPLETIONS_API_MODE:
        return client.chat.completions.create(**request_params)
    raise ValueError(f"不支持的 AI API 模式: {api_mode}")


def is_temperature_unsupported_error(error: Exception) -> bool:
    """识别模型或中转站不支持 temperature 参数的错误。"""
    message = str(error).lower()
    return (
        "not supported" in message
        or "unsupported" in message
        or "invalid" in message
        or "参数错误" in message
    ) and any(marker in message for marker in UNSUPPORTED_TEMPERATURE_MARKERS)


def remove_temperature_param(request_params: Dict[str, Any]) -> Dict[str, Any]:
    """移除 temperature 参数，适配不支持采样温度的模型网关。"""
    next_params = dict(request_params)
    next_params.pop("temperature", None)
    return next_params


def _is_api_unsupported_error(
    error: Exception,
    markers: tuple[str, ...],
) -> bool:
    message = str(error).lower()
    if any(marker in message for marker in markers):
        return True

    status_code = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    response = getattr(error, "response", None)
    response_text = getattr(response, "text", None) if response else None
    return (
        status_code == 404
        and message.strip() == "error code: 404"
        and not body
        and not response_text
    )


def _build_input_content(content: Any) -> List[Dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": INPUT_TEXT_TYPE, "text": content}]
    if not isinstance(content, list):
        raise ValueError(f"AI消息内容类型不受支持: {type(content).__name__}")

    return [_coerce_content_item(item) for item in content]


def _coerce_content_item(item: Any) -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError(f"AI消息片段类型不受支持: {type(item).__name__}")

    item_type = item.get("type")
    if item_type in {"text", INPUT_TEXT_TYPE}:
        text = item.get("text")
        if not isinstance(text, str):
            raise ValueError("文本消息片段缺少 text 字段。")
        return {"type": INPUT_TEXT_TYPE, "text": text}

    if item_type in {"image_url", INPUT_IMAGE_TYPE}:
        return _build_image_input_item(item)

    raise ValueError(f"不支持的 AI 消息片段类型: {item_type}")


def _build_image_input_item(item: Dict[str, Any]) -> Dict[str, Any]:
    raw_image = item.get("image_url")
    if isinstance(raw_image, dict):
        image_url = raw_image.get("url")
    else:
        image_url = raw_image

    if not isinstance(image_url, str) or not image_url.strip():
        raise ValueError("图片消息片段缺少有效的 image_url。")

    return {
        "type": INPUT_IMAGE_TYPE,
        "image_url": image_url,
        "detail": item.get("detail", IMAGE_DETAIL_AUTO),
    }
