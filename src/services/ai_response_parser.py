"""
AI 响应解析工具
"""
import json
from typing import Any


class EmptyAIResponseError(ValueError):
    """AI 返回了空内容。"""


def extract_ai_response_content(response: Any) -> str:
    """从不同形态的 AI 响应中提取文本内容。"""
    if response is None:
        raise EmptyAIResponseError("AI响应对象为空。")

    if isinstance(response, (bytes, bytearray)):
        text = response.decode("utf-8", errors="replace")
        return _normalize_text_content(text)

    if isinstance(response, str):
        return _normalize_text_content(response)

    function_arguments = _extract_responses_function_arguments(response)
    if function_arguments is not None:
        return _normalize_text_content(function_arguments)

    output_text = _get_field(response, "output_text")
    if isinstance(output_text, str):
        return _normalize_text_content(output_text)

    choices = _get_field(response, "choices")
    if choices:
        message = _get_field(choices[0], "message")
        if message is None:
            raise EmptyAIResponseError("AI响应缺少 message。")
        function_arguments = _extract_chat_function_arguments(message)
        if function_arguments is not None:
            return _normalize_text_content(function_arguments)

        content = _get_field(message, "content")

        # 智谱等 OpenAI 兼容网关在某些模式下会把输出放在 reasoning_content 而非 content
        try:
            return _normalize_text_content(_coerce_content_parts(content))
        except EmptyAIResponseError:
            reasoning_content = _get_field(message, "reasoning_content")
            if reasoning_content:
                return _normalize_text_content(_coerce_content_parts(reasoning_content))
            raise

    raise ValueError(f"无法识别的AI响应类型: {type(response).__name__}")


def parse_ai_response_json(content: str) -> dict:
    """解析 AI 文本响应中的 JSON。"""
    cleaned = _strip_code_fences(content)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return _extract_first_json_value(cleaned, exc)


def _get_field(value: Any, field_name: str) -> Any:
    if isinstance(value, dict):
        return value.get(field_name)
    return getattr(value, field_name, None)


def _extract_chat_function_arguments(message: Any) -> str | None:
    tool_calls = _get_field(message, "tool_calls")
    if not isinstance(tool_calls, (list, tuple)) or not tool_calls:
        return None

    function = _get_field(tool_calls[0], "function")
    return _coerce_function_arguments(_get_field(function, "arguments"))


def _extract_responses_function_arguments(response: Any) -> str | None:
    output = _get_field(response, "output")
    if not isinstance(output, (list, tuple)):
        return None

    for item in output:
        if _get_field(item, "type") != "function_call":
            continue
        arguments = _coerce_function_arguments(_get_field(item, "arguments"))
        if arguments is not None:
            return arguments
    return None


def _coerce_function_arguments(arguments: Any) -> str | None:
    if isinstance(arguments, str):
        return arguments if arguments.strip() else None
    if isinstance(arguments, (bytes, bytearray)):
        decoded = arguments.decode("utf-8", errors="replace")
        return decoded if decoded.strip() else None
    return None


def _coerce_content_parts(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (bytes, bytearray)):
        return content.decode("utf-8", errors="replace")
    if not isinstance(content, list):
        raise ValueError(f"AI响应内容类型不受支持: {type(content).__name__}")

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
            continue
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _normalize_text_content(content: str) -> str:
    text = str(content).strip()
    if not text:
        raise EmptyAIResponseError("AI响应内容为空。")
    return text


def _strip_code_fences(content: str) -> str:
    cleaned = content.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def _extract_first_json_value(
    content: str,
    fallback_error: json.JSONDecodeError,
):
    decoder = json.JSONDecoder()
    last_error: json.JSONDecodeError | None = None

    for start_index, char in enumerate(content):
        if char not in "{[":
            continue
        try:
            parsed, _ = decoder.raw_decode(content[start_index:])
            return parsed
        except json.JSONDecodeError as exc:
            last_error = exc

    if last_error is not None:
        raise last_error
    raise fallback_error
