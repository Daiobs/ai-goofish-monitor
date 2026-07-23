"""
AI 请求消息构造辅助函数
"""
from typing import Dict, List, Union


TEXT_ONLY_ANALYSIS_NOTE = (
    "补充说明：本次未提供商品图片，请仅根据商品文字字段和卖家信息判断，不要推断图片内容。"
)

TARGET_CLASSIFICATION_NOTE = """
必须先按本任务描述的目标商品进行分类：
- target_only：当前链接出售的主体就是目标商品，标价主要对应目标商品本身；
- target_bundle：包含目标商品，但同时捆绑电池、主设备或其他有显著价值的商品；
- not_target：不包含目标商品，或只是名称相似的其他型号/品类；
- uncertain：现有信息不足以确认商品主体或兼容性。

market_comparable 只有在 target_category 为 target_only，且当前标价明确对应一件
目标商品、不是定金/引流价/多选价/整批含糊价时才能为 true。套装、非目标商品、
无法确认的商品一律为 false。is_recommended 只有在 target_category 为 target_only
且 market_comparable 为 true 时才允许为 true。不要使用未分类的关键词搜索均价
反向证明商品价格合理。
"""


def build_analysis_text_prompt(
    product_json: str,
    prompt_text: str,
    *,
    include_images: bool,
) -> str:
    note = "" if include_images else f"\n{TEXT_ONLY_ANALYSIS_NOTE}\n"
    value_note = (
        "\n如果商品 JSON 中包含“价格参考”或 price_insight，请结合价格位置、历史走势、"
        "配置、成色、附件、卖家信息综合判断性价比，但只信任明确标记为可比样本的统计。"
        "必须保留 is_recommended/reason 等规定字段。\n"
    )
    return f"""请基于你的专业知识和我的要求，分析以下完整的商品JSON数据：

```json
{product_json}
```

    {prompt_text}
    {TARGET_CLASSIFICATION_NOTE}
    {value_note}
    {note}"""


def build_user_message_content(
    text_prompt: str,
    image_data_urls: List[str],
) -> Union[str, List[Dict[str, object]]]:
    if not image_data_urls:
        return text_prompt

    user_content: List[Dict[str, object]] = [
        {"type": "image_url", "image_url": {"url": url}}
        for url in image_data_urls
    ]
    user_content.append({"type": "text", "text": text_prompt})
    return user_content
