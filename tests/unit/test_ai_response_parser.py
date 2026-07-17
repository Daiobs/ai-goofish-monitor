import pytest

from src.services.ai_response_parser import (
    extract_ai_response_content,
    parse_ai_response_json,
    EmptyAIResponseError,
)


def test_parse_ai_response_json_uses_first_object_when_multiple_json_objects_are_concatenated():
    content = """```json
{"is_recommended": true, "reason": "first"}
{"is_recommended": false, "reason": "second"}
```"""

    result = parse_ai_response_json(content)

    assert result == {"is_recommended": True, "reason": "first"}


def test_parse_ai_response_json_extracts_json_from_wrapped_text():
    content = """分析结果如下：

```json
{"is_recommended": true, "reason": "wrapped"}
```

请按第一份结果处理。"""

    result = parse_ai_response_json(content)

    assert result == {"is_recommended": True, "reason": "wrapped"}


def test_parse_ai_response_json_raises_when_no_json_exists():
    with pytest.raises(ValueError):
        parse_ai_response_json("没有任何 JSON 内容")


def test_extract_ai_response_content_with_none_content_but_valid_reasoning_content():
    """当 content 为 None 但 reasoning_content 有值时，应该成功提取 reasoning_content 的内容"""
    # 创建 mock 对象模拟 OpenAI 风格的响应
    message = type('Message', (), {
        'content': None,
        'reasoning_content': '这是推理内容'
    })()
    choice = type('Choice', (), {'message': message})()
    response = type('Response', (), {'choices': [choice]})()

    result = extract_ai_response_content(response)

    assert result == '这是推理内容'


def test_extract_ai_response_content_raises_when_content_and_reasoning_content_are_empty():
    """当 content 和 reasoning_content 都为空时，应该抛出 EmptyAIResponseError"""
    # 创建 mock 对象
    message = type('Message', (), {
        'content': None,
        'reasoning_content': None
    })()
    choice = type('Choice', (), {'message': message})()
    response = type('Response', (), {'choices': [choice]})()

    with pytest.raises(EmptyAIResponseError):
        extract_ai_response_content(response)


def test_extract_ai_response_content_from_chat_function_arguments():
    function = type("Function", (), {"arguments": '{"is_recommended":true}'})()
    tool_call = type("ToolCall", (), {"function": function})()
    message = type(
        "Message",
        (),
        {"content": None, "reasoning_content": None, "tool_calls": [tool_call]},
    )()
    choice = type("Choice", (), {"message": message})()
    response = type("Response", (), {"choices": [choice]})()

    result = extract_ai_response_content(response)

    assert result == '{"is_recommended":true}'


def test_extract_ai_response_content_from_responses_function_call_arguments():
    response = {
        "output_text": "",
        "output": [
            {"type": "reasoning"},
            {
                "type": "function_call",
                "arguments": '{"is_recommended":false}',
            },
        ],
    }

    result = extract_ai_response_content(response)

    assert result == '{"is_recommended":false}'


def test_extract_ai_response_content_prefers_chat_function_arguments_when_present():
    message = {
        "content": '{"source":"content"}',
        "reasoning_content": None,
        "tool_calls": [
            {"function": {"arguments": '{"source":"tool"}'}}
        ],
    }
    response = {"choices": [{"message": message}]}

    result = extract_ai_response_content(response)

    assert result == '{"source":"tool"}'


def test_extract_ai_response_content_prefers_responses_function_call_when_present():
    response = {
        "output_text": '{"source":"output_text"}',
        "output": [
            {"type": "function_call", "arguments": '{"source":"tool"}'}
        ],
    }

    result = extract_ai_response_content(response)

    assert result == '{"source":"tool"}'


def test_extract_ai_response_content_keeps_chat_text_behavior_without_tool_call():
    response = {
        "choices": [
            {
                "message": {
                    "content": '{"source":"content"}',
                    "reasoning_content": None,
                }
            }
        ]
    }

    result = extract_ai_response_content(response)

    assert result == '{"source":"content"}'


def test_extract_ai_response_content_keeps_output_text_behavior_without_function_call():
    response = {"output_text": '{"source":"output_text"}', "output": []}

    result = extract_ai_response_content(response)

    assert result == '{"source":"output_text"}'
