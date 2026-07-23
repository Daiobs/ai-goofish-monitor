import asyncio
import base64
import json
import os
import re
import sys
import shutil
from datetime import datetime, timedelta
from time import monotonic
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import requests

# 设置标准输出编码为UTF-8，解决Windows控制台编码问题
if sys.platform.startswith('win'):
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.detach())
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.detach())

from src.config import (
    AI_DEBUG_MODE,
    IMAGE_DOWNLOAD_HEADERS,
    IMAGE_SAVE_DIR,
    TASK_IMAGE_DIR_PREFIX,
    MODEL_NAME,
    ENABLE_RESPONSE_FORMAT,
    client,
)
from src.ai_message_builder import (
    build_analysis_text_prompt,
    build_user_message_content,
)
from src.services.ai_response_parser import (
    EmptyAIResponseError,
    extract_ai_response_content,
    parse_ai_response_json,
)
from src.services.ai_request_compat import (
    AI_ANALYSIS_SCHEMA,
    FUNCTION_TOOL_OUTPUT_MODE,
    JSON_OBJECT_OUTPUT_MODE,
    JSON_SCHEMA_OUTPUT_MODE,
    TEXT_OUTPUT_MODE,
    CHAT_COMPLETIONS_API_MODE,
    RESPONSES_API_MODE,
    build_ai_request_params,
    create_ai_response_async,
    is_chat_completions_api_unsupported_error,
    is_output_mode_unsupported_error,
    is_responses_api_unsupported_error,
    is_temperature_unsupported_error,
    remove_temperature_param,
    TARGET_CATEGORY_TARGET_ONLY,
)
from src.services.notification_service import build_notification_service
from src.services.prompt_version import resolve_canonical_prompt_version
from src.utils import convert_goofish_link, retry_on_failure


def _positive_int(value, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


DEFAULT_IMAGE_DOWNLOAD_CONCURRENCY = max(
    1,
    _positive_int(os.getenv("IMAGE_DOWNLOAD_CONCURRENCY", "3"), 3),
)


def safe_print(text):
    """安全的打印函数，处理编码错误"""
    try:
        print(text)
    except UnicodeEncodeError:
        # 如果遇到编码错误，尝试用ASCII编码并忽略无法编码的字符
        try:
            print(text.encode('ascii', errors='ignore').decode('ascii'))
        except:
            # 如果还是失败，打印一个简化的消息
            print("[输出包含无法显示的字符]")


def _build_debug_request_summary(api_mode: str, request_params: dict) -> dict:
    summary = {
        "api_mode": api_mode,
        "model": request_params.get("model"),
    }
    if "temperature" in request_params:
        summary["temperature"] = request_params["temperature"]
    if "max_output_tokens" in request_params:
        summary["max_output_tokens"] = request_params["max_output_tokens"]
    if "max_tokens" in request_params:
        summary["max_tokens"] = request_params["max_tokens"]
    if "text" in request_params:
        summary["text"] = request_params["text"]
    if "response_format" in request_params:
        summary["response_format"] = request_params["response_format"]
    if "input" in request_params:
        summary["input_content_types"] = [
            [item.get("type") for item in message.get("content", [])]
            for message in request_params["input"]
        ]
    if "messages" in request_params:
        summary["message_content_types"] = [
            _extract_message_content_types(message)
            for message in request_params["messages"]
        ]
    return summary


def _extract_message_content_types(message: dict) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return ["text"]
    if not isinstance(content, list):
        return [type(content).__name__]
    return [str(item.get("type")) for item in content if isinstance(item, dict)]


@retry_on_failure(retries=2, delay=3)
async def _download_single_image(url, save_path):
    """一个带重试的内部函数，用于异步下载单个图片。"""
    loop = asyncio.get_running_loop()
    # 使用 run_in_executor 运行同步的 requests 代码，避免阻塞事件循环
    response = await loop.run_in_executor(
        None,
        lambda: requests.get(url, headers=IMAGE_DOWNLOAD_HEADERS, timeout=20, stream=True)
    )
    response.raise_for_status()
    with open(save_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    return save_path


def _build_image_save_path(
    product_id: str,
    index: int,
    url: str,
    task_image_dir: str,
) -> str:
    clean_url = url.split('.heic')[0] if '.heic' in url else url
    file_name_base = os.path.basename(clean_url).split('?')[0]
    file_name = f"product_{product_id}_{index}_{file_name_base}"
    file_name = re.sub(r'[\\/*?:"<>|]', "", file_name)
    if not os.path.splitext(file_name)[1]:
        file_name += ".jpg"
    return os.path.join(task_image_dir, file_name)


async def download_all_images(product_id, image_urls, task_name="default", concurrency=None):
    """异步下载一个商品的所有图片。如果图片已存在则跳过。支持任务隔离。"""
    if not image_urls:
        return []

    # 为每个任务创建独立的图片目录
    task_image_dir = os.path.join(IMAGE_SAVE_DIR, f"{TASK_IMAGE_DIR_PREFIX}{task_name}")
    os.makedirs(task_image_dir, exist_ok=True)

    urls = [url.strip() for url in image_urls if url.strip().startswith('http')]
    if not urls:
        return []

    max_concurrency = _positive_int(concurrency, DEFAULT_IMAGE_DOWNLOAD_CONCURRENCY)
    semaphore = asyncio.Semaphore(max_concurrency)
    total_images = len(urls)

    async def _download_one(index: int, url: str):
        save_path = _build_image_save_path(product_id, index, url, task_image_dir)
        if os.path.exists(save_path):
            safe_print(
                f"   [图片] 图片 {index}/{total_images} 已存在，跳过下载: {os.path.basename(save_path)}"
            )
            return save_path
        async with semaphore:
            safe_print(f"   [图片] 正在下载图片 {index}/{total_images}: {url}")
            if await _download_single_image(url, save_path):
                safe_print(
                    f"   [图片] 图片 {index}/{total_images} 已成功下载到: {os.path.basename(save_path)}"
                )
                return save_path
        return None

    tasks = [
        asyncio.create_task(_download_one(index, url))
        for index, url in enumerate(urls, start=1)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    saved_paths = []
    for url, result in zip(urls, results):
        try:
            if isinstance(result, Exception):
                raise result
            if result:
                saved_paths.append(result)
        except Exception as e:
            safe_print(f"   [图片] 处理图片 {url} 时发生错误，已跳过此图: {e}")

    return saved_paths


def cleanup_task_images(task_name):
    """清理指定任务的图片目录"""
    task_image_dir = os.path.join(IMAGE_SAVE_DIR, f"{TASK_IMAGE_DIR_PREFIX}{task_name}")
    if os.path.exists(task_image_dir):
        try:
            shutil.rmtree(task_image_dir)
            safe_print(f"   [清理] 已删除任务 '{task_name}' 的临时图片目录: {task_image_dir}")
        except Exception as e:
            safe_print(f"   [清理] 删除任务 '{task_name}' 的临时图片目录时出错: {e}")
    else:
        safe_print(f"   [清理] 任务 '{task_name}' 的临时图片目录不存在: {task_image_dir}")


def cleanup_ai_logs(logs_dir: str, keep_days: int = 1) -> None:
    try:
        cutoff = datetime.now() - timedelta(days=keep_days)
        for filename in os.listdir(logs_dir):
            if not filename.endswith(".log"):
                continue
            try:
                timestamp = datetime.strptime(filename[:15], "%Y%m%d_%H%M%S")
            except ValueError:
                continue
            if timestamp < cutoff:
                os.remove(os.path.join(logs_dir, filename))
    except Exception as e:
        safe_print(f"   [日志] 清理AI日志时出错: {e}")


def encode_image_to_base64(image_path):
    """将本地图片文件编码为 Base64 字符串。"""
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
        safe_print(f"编码图片时出错: {e}")
        return None


def _json_shape_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def _validate_schema_value(
    value: object,
    schema: dict,
    path: str,
) -> list[str]:
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, dict):
            return [path]
        errors: list[str] = []
        properties = schema.get("properties") or {}
        for field in schema.get("required") or []:
            field_path = f"{path}.{field}" if path else field
            if field not in value:
                errors.append(field_path)
                continue
            field_schema = properties.get(field)
            if isinstance(field_schema, dict):
                errors.extend(
                    _validate_schema_value(value[field], field_schema, field_path)
                )
        return errors
    if expected_type == "array":
        if not isinstance(value, list):
            return [path]
        errors = []
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(
                    _validate_schema_value(item, item_schema, f"{path}[{index}]")
                )
        return errors
    if expected_type == "string":
        if not isinstance(value, str):
            return [path]
        if schema.get("minLength") and not value.strip():
            return [path]
        return []
    if expected_type == "boolean":
        return [] if isinstance(value, bool) else [path]
    return []


def summarize_ai_response_shape(parsed_response: object) -> dict:
    """Describe response keys and JSON types without retaining any values."""
    if not isinstance(parsed_response, dict):
        return {
            "top_level_type": _json_shape_type(parsed_response),
            "top_level": {},
            "nested": {},
            "boolean_candidates": [],
            "recommendation_fields": [],
        }

    top_level = {
        str(key): _json_shape_type(value)
        for key, value in parsed_response.items()
    }
    nested: dict[str, dict[str, str]] = {}
    candidate_items = list(parsed_response.items())
    if len(candidate_items) == 1 and isinstance(candidate_items[0][1], dict):
        wrapper_name = str(candidate_items[0][0])
        wrapper_value = candidate_items[0][1]
        nested[wrapper_name] = {
            str(key): _json_shape_type(value)
            for key, value in wrapper_value.items()
        }
        candidate_items.extend(
            (f"{wrapper_name}.{key}", value)
            for key, value in wrapper_value.items()
        )

    boolean_candidates = sorted(
        str(key) for key, value in candidate_items if isinstance(value, bool)
    )
    recommendation_fields = sorted(
        str(key)
        for key, _value in candidate_items
        if "recommend" in str(key).lower()
    )
    return {
        "top_level_type": "object",
        "top_level": top_level,
        "nested": nested,
        "boolean_candidates": boolean_candidates,
        "recommendation_fields": recommendation_fields,
    }


def _normalize_legacy_analysis_shape(parsed_response: object) -> object:
    if not isinstance(parsed_response, dict):
        return parsed_response

    normalized = dict(parsed_response)
    if set(normalized) == {"analysis"} and isinstance(normalized["analysis"], dict):
        normalized = dict(normalized["analysis"])

    if "is_recommended" not in normalized:
        recommended = normalized.get("recommended")
        recommendation = normalized.get("recommendation")
        if isinstance(recommended, bool):
            normalized["is_recommended"] = recommended
        elif recommendation == "recommended":
            normalized["is_recommended"] = True
        elif recommendation == "not_recommended":
            normalized["is_recommended"] = False
    normalized.pop("recommended", None)
    normalized.pop("recommendation", None)
    return normalized


def _is_transient_ai_error(error: Exception) -> bool:
    if isinstance(error, (EmptyAIResponseError, TimeoutError, ConnectionError)):
        return True
    status_code = getattr(error, "status_code", None)
    return status_code in {408, 409, 425, 429} or (
        isinstance(status_code, int) and status_code >= 500
    )


def _client_without_sdk_retries(ai_client):
    with_options = getattr(ai_client, "with_options", None)
    if callable(with_options):
        return with_options(max_retries=0)
    return ai_client


def get_ai_response_validation_errors(parsed_response: object) -> list[str]:
    """Return model-owned semantic field names that violate the contract."""
    if not isinstance(parsed_response, dict):
        return ["top_level"]
    errors = _validate_schema_value(parsed_response, AI_ANALYSIS_SCHEMA, "")
    if (
        not errors
        and parsed_response.get("market_comparable") is True
        and parsed_response.get("target_category") != TARGET_CATEGORY_TARGET_ONLY
    ):
        errors.append("market_comparable")
    if (
        not errors
        and parsed_response.get("is_recommended") is True
        and (
            parsed_response.get("target_category") != TARGET_CATEGORY_TARGET_ONLY
            or parsed_response.get("market_comparable") is not True
        )
    ):
        errors.append("is_recommended")
    return errors


def validate_ai_response_format(parsed_response):
    """Validate model-owned analysis semantics before app metadata is added."""
    errors = get_ai_response_validation_errors(parsed_response)
    if errors:
        safe_print(
            "   [AI分析] 警告：响应语义字段无效: " + ", ".join(errors)
        )
        return False

    return True


def normalize_ai_response(
    parsed_response: object,
    canonical_prompt_version: str,
    *,
    allow_legacy_compatibility: bool = True,
) -> dict | None:
    """Validate model semantics and attach application-owned prompt metadata."""
    candidate = (
        _normalize_legacy_analysis_shape(parsed_response)
        if allow_legacy_compatibility
        else parsed_response
    )
    if not validate_ai_response_format(candidate):
        return None

    canonical_version = resolve_canonical_prompt_version(
        "",
        explicit_version=canonical_prompt_version,
    )
    normalized = dict(candidate)
    model_version = normalized.pop("prompt_version", None)
    normalized.pop("model_prompt_version_mismatch", None)
    normalized["prompt_version"] = canonical_version
    if model_version is not None and model_version != canonical_version:
        normalized["model_prompt_version_mismatch"] = True
    return normalized


@retry_on_failure(retries=3, delay=5)
async def send_ntfy_notification(product_data, reason):
    """兼容旧调用名，内部统一走 NotificationService。"""
    service = build_notification_service()
    if not service.clients:
        safe_print(
            "警告：未在 .env 文件中配置任何通知服务，跳过通知。"
        )
        return {}

    results = await service.send_notification(product_data, reason)
    for channel, result in results.items():
        if result["success"]:
            safe_print(f"   -> {channel} 通知发送成功。")
            continue
        safe_print(f"   -> {channel} 通知发送失败: {result['message']}")
    return results


async def get_ai_analysis(
    product_data,
    image_paths=None,
    prompt_text="",
    prompt_version=None,
    diagnostics=None,
):
    """将完整的商品JSON数据和所有图片发送给 AI 进行分析（异步）。"""
    if not client:
        safe_print("   [AI分析] 错误：AI客户端未初始化，跳过分析。")
        return None

    item_info = product_data.get('商品信息', {})
    product_id = item_info.get('商品ID', 'N/A')

    safe_print(f"\n   [AI分析] 开始分析商品 #{product_id} (含 {len(image_paths or [])} 张图片)...")
    safe_print(f"   [AI分析] 标题: {item_info.get('商品标题', '无')}")

    if not prompt_text:
        safe_print("   [AI分析] 错误：未提供AI分析所需的prompt文本。")
        return None

    canonical_prompt_version = resolve_canonical_prompt_version(
        prompt_text,
        explicit_version=prompt_version,
    )

    product_details_json = json.dumps(product_data, ensure_ascii=False, indent=2)
    system_prompt = prompt_text

    if AI_DEBUG_MODE:
        safe_print("\n--- [AI DEBUG] ---")
        safe_print(
            json.dumps(
                {
                    "product_payload_chars": len(product_details_json),
                    "prompt_chars": len(prompt_text),
                    "image_count": len(image_paths or []),
                },
                ensure_ascii=True,
            )
        )
        safe_print("-------------------\n")

    image_data_urls = []
    if image_paths:
        for path in image_paths:
            base64_image = encode_image_to_base64(path)
            if base64_image:
                image_data_urls.append(f"data:image/jpeg;base64,{base64_image}")

    combined_text_prompt = build_analysis_text_prompt(
        product_details_json,
        system_prompt,
        include_images=bool(image_data_urls),
    )
    user_content = build_user_message_content(combined_text_prompt, image_data_urls)
    messages = [{"role": "user", "content": user_content}]

    # 保存最终传输内容到日志文件
    try:
        # 创建logs文件夹
        logs_dir = os.path.join("logs", "ai")
        os.makedirs(logs_dir, exist_ok=True)
        cleanup_ai_logs(logs_dir, keep_days=1)

        # 生成日志文件名（当前时间）
        current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = f"{current_time}.log"
        log_filepath = os.path.join(logs_dir, log_filename)

        task_name = product_data.get("任务名称") or product_data.get("任务名") or "unknown"
        log_payload = {
            "timestamp": current_time,
            "task_name": task_name,
            "product_id": product_id,
            "title": item_info.get("商品标题", "无"),
            "image_count": len(image_data_urls),
        }
        log_content = json.dumps(log_payload, ensure_ascii=False)

        # 写入日志文件
        with open(log_filepath, 'w', encoding='utf-8') as f:
            f.write(log_content)

        safe_print(f"   [日志] AI分析请求已保存到: {log_filepath}")

    except Exception as e:
        safe_print(f"   [日志] 保存AI分析日志时出错: {e}")

    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update(
            {
                "status": "pending",
                "request_count": 0,
                "attempts": [],
                "last_response_shape": None,
                "final_failure_fields": [],
            }
        )

    # One primary request plus one reasoned compatibility request at most.
    max_attempts = 2
    api_mode = CHAT_COMPLETIONS_API_MODE
    output_mode = (
        JSON_SCHEMA_OUTPUT_MODE
        if ENABLE_RESPONSE_FORMAT
        else TEXT_OUTPUT_MODE
    )
    use_temperature = True
    request_client = _client_without_sdk_retries(client)
    request_started_at = monotonic()
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            from src.config import get_ai_request_params

            request_params = build_ai_request_params(
                api_mode,
                model=MODEL_NAME,
                messages=messages,
                temperature=0.1,
                max_output_tokens=4000,
                output_mode=output_mode,
            )
            if not use_temperature:
                request_params = remove_temperature_param(request_params)

            request_params = get_ai_request_params(**request_params)

            if diagnostics is not None:
                diagnostics["request_count"] += 1
                diagnostics["attempts"].append(
                    {
                        "api_mode": api_mode,
                        "output_mode": output_mode,
                        "temperature_enabled": use_temperature,
                    }
                )

            if AI_DEBUG_MODE:
                safe_print(f"\n--- [AI DEBUG] 第{attempt + 1}次尝试 REQUEST ---")
                safe_print(
                    json.dumps(
                        _build_debug_request_summary(api_mode, request_params),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                safe_print("-----------------------------------\n")

            response = await create_ai_response_async(
                request_client,
                api_mode,
                request_params,
            )
            ai_response_content = extract_ai_response_content(response)
            try:
                parsed_response = parse_ai_response_json(ai_response_content)
                response_shape = summarize_ai_response_shape(parsed_response)
                if diagnostics is not None:
                    diagnostics["last_response_shape"] = response_shape
                allow_legacy_compatibility = output_mode in {
                    JSON_OBJECT_OUTPUT_MODE,
                    TEXT_OUTPUT_MODE,
                }
                normalized_response = normalize_ai_response(
                    parsed_response,
                    canonical_prompt_version,
                    allow_legacy_compatibility=allow_legacy_compatibility,
                )
                if normalized_response is not None:
                    request_duration = round(
                        max(0.0, monotonic() - request_started_at), 3
                    )
                    normalized_response["request_duration_seconds"] = request_duration
                    safe_print(
                        f"   [AI分析] 第{attempt + 1}次尝试成功，响应格式验证通过，"
                        f"请求耗时 {request_duration:.3f} 秒"
                    )
                    if diagnostics is not None:
                        diagnostics["status"] = "success"
                        diagnostics["final_api_mode"] = api_mode
                        diagnostics["final_output_mode"] = output_mode
                    return normalized_response

                validation_candidate = (
                    _normalize_legacy_analysis_shape(parsed_response)
                    if allow_legacy_compatibility
                    else parsed_response
                )
                failure_fields = get_ai_response_validation_errors(
                    validation_candidate
                )
                if diagnostics is not None:
                    diagnostics["final_failure_fields"] = failure_fields
                safe_print(
                    f"   [AI分析] 第{attempt + 1}次尝试未满足语义契约；"
                    f"字段: {', '.join(failure_fields)}；"
                    f"安全形状: {json.dumps(response_shape, ensure_ascii=False)}"
                )
                last_error = ValueError(
                    "AI响应格式缺少必需字段或字段类型不正确。"
                )
                if (
                    attempt < max_attempts - 1
                    and output_mode == JSON_SCHEMA_OUTPUT_MODE
                ):
                    output_mode = FUNCTION_TOOL_OUTPUT_MODE
                    safe_print(
                        "   [AI分析] 网关未执行严格 Schema 契约，改用强制函数工具进行一次兼容请求。"
                    )
                    continue
                raise last_error
            except json.JSONDecodeError as e:
                last_error = e
                safe_print(f"   [AI分析] 第{attempt + 1}次尝试返回了无效 JSON。")
                if (
                    attempt < max_attempts - 1
                    and output_mode == JSON_SCHEMA_OUTPUT_MODE
                ):
                    output_mode = FUNCTION_TOOL_OUTPUT_MODE
                    safe_print(
                        "   [AI分析] 严格 Schema 响应不可解析，改用强制函数工具进行一次兼容请求。"
                    )
                    continue
                raise

        except Exception as e:
            last_error = e
            transition = None
            if (
                api_mode == CHAT_COMPLETIONS_API_MODE
                and is_chat_completions_api_unsupported_error(e)
            ):
                api_mode = RESPONSES_API_MODE
                transition = "当前服务未实现 Chat Completions API，改用 Responses API。"
            elif (
                api_mode == RESPONSES_API_MODE
                and is_responses_api_unsupported_error(e)
            ):
                api_mode = CHAT_COMPLETIONS_API_MODE
                transition = "当前服务未实现 Responses API，改用 Chat Completions API。"
            elif is_output_mode_unsupported_error(e, output_mode):
                if output_mode == JSON_SCHEMA_OUTPUT_MODE:
                    output_mode = FUNCTION_TOOL_OUTPUT_MODE
                    transition = "当前服务明确拒绝 JSON Schema，改用强制函数工具。"
                elif output_mode == FUNCTION_TOOL_OUTPUT_MODE:
                    output_mode = JSON_OBJECT_OUTPUT_MODE
                    transition = "当前服务明确拒绝函数工具，改用 Legacy JSON Object。"
                elif output_mode == JSON_OBJECT_OUTPUT_MODE:
                    output_mode = TEXT_OUTPUT_MODE
                    transition = "当前服务明确拒绝 JSON Object，移除输出格式参数。"
            elif use_temperature and is_temperature_unsupported_error(e):
                use_temperature = False
                transition = "当前模型明确拒绝 temperature，移除该参数。"
            elif attempt < max_attempts - 1 and _is_transient_ai_error(e):
                transition = "AI 请求出现临时故障，执行最后一次重试。"

            if AI_DEBUG_MODE:
                safe_print(f"\n--- [AI DEBUG] 第{attempt + 1}次尝试 EXCEPTION ---")
                safe_print(f"exception_type={type(e).__name__}")
                safe_print("-------------------------------------\n")
            safe_print(
                f"   [AI分析] 第{attempt + 1}次尝试失败 "
                f"({type(e).__name__})。"
            )
            if transition and attempt < max_attempts - 1:
                safe_print(f"   [AI分析] {transition}")
                continue
            if diagnostics is not None:
                diagnostics["status"] = "failed"
                diagnostics["failure_type"] = type(e).__name__
            raise

    if diagnostics is not None:
        diagnostics["status"] = "failed"
        diagnostics["failure_type"] = type(last_error).__name__
    if last_error is not None:
        raise last_error
    raise RuntimeError("AI调用未返回结果。")
