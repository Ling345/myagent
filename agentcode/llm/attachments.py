"""图片附件：按魔数识别类型，并拼成 OpenAI 兼容的 ``image_url`` 内容块。

为什么按魔数而不是扩展名：``photo.png`` 完全可以是一段任意二进制。模型
那边收到坏图只会回一个含糊的 400，用户看不出是哪一步出的问题，所以在
入口处就按文件头判断，认不出来的一律拒绝。
"""

from __future__ import annotations

import base64
from typing import Any, Mapping, Sequence

#: 一次任务最多带几张图（再多的话请求体与 token 都会失控）
MAX_IMAGES = 4
#: 单张图片上限，与网页上传的单文件上限保持一致
MAX_IMAGE_BYTES = 2 * 1024 * 1024

#: 带图但没有视觉后端时的统一提示（网页入口与运行器共用，措辞保持一致）
VISION_REQUIRED = (
    "任务带了图片，但当前没有配置视觉模型：请把 AGENT_VISION_PROFILE "
    "指向一个支持图片输入的档位；如果默认模型本身就支持视觉，"
    "把 AGENT_MODEL_VISION 设为 true。"
)


def sniff_image_media_type(raw: bytes) -> str | None:
    """从文件头判断图片类型；不是支持的图片就返回 ``None``。"""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def make_attachment(name: str, raw: bytes, media_type: str | None = None) -> dict[str, Any]:
    """把一张图片整理成可放进上下文的结构（只在这里做 base64）。"""
    kind = media_type or sniff_image_media_type(raw)
    if kind is None:
        raise ValueError("不是支持的图片格式（只收 PNG / JPEG / GIF / WebP）。")
    encoded = base64.b64encode(raw).decode("ascii")
    return {
        "name": name,
        "media_type": kind,
        "bytes": len(raw),
        "data_url": f"data:{kind};base64,{encoded}",
    }


def user_content(text: str, images: Sequence[Mapping[str, Any]] | None) -> Any:
    """把文字与图片拼成一条 user 消息的 ``content``。

    没有图片时仍然返回字符串——不改变老路径的请求体形状，供应商那边
    前缀缓存与日志也都照旧。
    """
    if not images:
        return text
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for image in images:
        url = str(image.get("data_url") or "")
        if not url:
            continue
        blocks.append({"type": "image_url", "image_url": {"url": url}})
    # 全是坏数据时退回纯文本，别给模型发一个空 content
    return blocks or text


#: 估算用量时一张图折算多少字符（≈1000 token）。真模型按尺寸/分块计费，
#: 但把 base64 的长度当文本数会离谱地高（2 MB 图片能"算"出几十万 token）。
IMAGE_ESTIMATE_CHARS = 2000


def estimate_prompt_chars(content: Any) -> int:
    """估算一条消息的提示词字符数；图片内容块按固定值折算。"""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, (list, tuple)):
        total = 0
        for part in content:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "text":
                total += len(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                total += IMAGE_ESTIMATE_CHARS
        return total
    return len(str(content or ""))
