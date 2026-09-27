"""闲鱼聊天报文内容解析。

闲鱼 IM 推送里的聊天消息大致长这样（解密并把 MessagePack 转成 JSON 之后）::

    {
      "1": {
        "2": "<chatId>@goofish",
        "5": <时间戳ms>,
        "6": {"3": {"5": "<明文JSON内容>"}},          # 部分批次是明文
        "10": {
            "reminderTitle":   "买家昵称",
            "reminderContent": "你好",                 # 图片时是占位符 "[图片]"
            "reminderUrl":     "...itemId=123&...",
            "senderUserId":    "2200xxxx"
        }
      }
    }

真正的消息载荷是:
    {"contentType": 1, "text": {"text": "你好"}}
    {"contentType": 2, "image": {"pics": [{"url": "...", "width": .., "height": ..}]}}

本模块只做“从原始报文里把 (文本, 图片URL列表, 类型) 挖出来”，
不掺任何业务判断，方便单测。
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_CONTENT_KEYS = ("text", "image", "picUrl", "audio")


def load_content_json(raw: Any) -> Optional[Dict[str, Any]]:
    """载荷字符串（明文 JSON 或 base64 JSON）→ 内容 dict，失败返回 None。"""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    try:
        obj = json.loads(base64.b64decode(raw).decode("utf-8"))
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return None


def looks_like_content(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    return "contentType" in obj or any(k in obj for k in _CONTENT_KEYS)


def interpret_content(decoded: Dict[str, Any]) -> Tuple[str, List[str], str]:
    """内容 dict → (文本, 图片URL列表, 类型)；类型 ∈ {text, image, ''}。"""
    if not isinstance(decoded, dict):
        return "", [], ""

    ctype = decoded.get("contentType", 0)

    if ctype == 1 and "text" in decoded:
        return _extract_text(decoded["text"]), [], "text"
    if ctype == 2 and "image" in decoded:
        return "", _extract_image_urls(decoded), "image"
    if ctype == 3 and "audio" in decoded:
        return "[语音消息]", [], "text"

    # 兜底：没有标准 contentType 但结构可识别
    if "text" in decoded:
        return _extract_text(decoded["text"]), [], "text"
    if decoded.get("picUrl"):
        return "", [str(decoded["picUrl"])], "image"
    return "", [], ""


def _extract_text(text_obj: Any) -> str:
    if isinstance(text_obj, dict):
        return str(text_obj.get("text", ""))
    return str(text_obj)


def _extract_image_urls(decoded: Dict[str, Any]) -> List[str]:
    image = decoded.get("image")
    if not isinstance(image, dict):
        return []
    pics = image.get("pics")
    urls: List[str] = []
    if isinstance(pics, list):
        for p in pics:
            if isinstance(p, dict) and p.get("url"):
                urls.append(str(p["url"]))
            elif isinstance(p, str):
                urls.append(p)
    if not urls and image.get("url"):
        urls.append(str(image["url"]))
    return urls


def iter_strings(obj: Any, _depth: int = 0) -> Iterable[str]:
    """深度遍历，产出所有字符串值（限制深度，避免异常报文把 CPU 打满）。"""
    if _depth > 8:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iter_strings(v, _depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from iter_strings(v, _depth + 1)


def find_content_dict(message: Any) -> Optional[Dict[str, Any]]:
    """从整条解密后的报文里找出第一个"像消息内容"的载荷。"""
    # 1) 优先走已知路径，最快
    try:
        m1 = message["1"]
        m6 = m1.get("6", {}) if isinstance(m1, dict) else {}
        m6_3 = m6.get("3", {}) if isinstance(m6, dict) else {}
        if isinstance(m6_3, dict):
            for cand in (m6_3.get("5"), m6_3.get("1")):
                decoded = load_content_json(cand)
                if decoded is not None and looks_like_content(decoded):
                    return decoded
    except Exception:
        pass

    # 2) 兜底：深度搜索
    for s in iter_strings(message):
        if len(s) < 8 or len(s) > 20000:
            continue
        if '"contentType"' not in s and '"image"' not in s and '"text"' not in s:
            # 明文或 base64 都可能；做一次轻量预筛
            if "Y29udGVudFR5cGU" not in s:
                continue
        decoded = load_content_json(s)
        if decoded is not None and looks_like_content(decoded):
            return decoded
    return None


def parse_chat_message(message: Dict[str, Any]) -> Dict[str, Any]:
    """把一条已解密解包的聊天消息整理成业务字段。

    Returns dict:
        text            展示/给 AI 的文本（图片消息时为图片 URL，多张用换行拼接）
        images          图片 URL 列表
        msg_type        text / image / ''
        reminder_text   原始 reminderContent（图片时是 "[图片]"）
        user_id         发送者 ID
        user_name       发送者昵称
        chat_id         会话 ID
        item_id         商品 ID
        create_time_ms  消息时间戳（毫秒）
    """
    m1 = message.get("1") if isinstance(message, dict) else None
    if not isinstance(m1, dict):
        return {}

    m10 = m1.get("10") if isinstance(m1.get("10"), dict) else {}
    reminder_text = str(m10.get("reminderContent", "") or "")
    user_id = str(m10.get("senderUserId", "") or "")
    user_name = str(m10.get("senderNick") or m10.get("reminderTitle") or "买家")

    chat_raw = str(m1.get("2", "") or "")
    chat_id = chat_raw.split("@")[0] if "@" in chat_raw else chat_raw

    url_info = str(m10.get("reminderUrl", "") or "")
    item_id = ""
    if "itemId=" in url_info:
        item_id = url_info.split("itemId=")[1].split("&")[0]

    try:
        create_time_ms = int(m1.get("5", 0) or 0)
    except (TypeError, ValueError):
        create_time_ms = 0

    text, images, msg_type = "", [], ""
    content = find_content_dict(message)
    if content is not None:
        text, images, msg_type = interpret_content(content)

    if msg_type == "image" and images:
        display_text = "\n".join(images)
    elif text:
        display_text = text
    else:
        display_text = reminder_text

    return {
        "text": display_text,
        "images": images,
        "msg_type": msg_type or "text",
        "reminder_text": reminder_text,
        "user_id": user_id,
        "user_name": user_name,
        "chat_id": chat_id,
        "item_id": item_id,
        "create_time_ms": create_time_ms,
    }


def pick_candidates(message: Any) -> Sequence[Any]:
    """调试用：列出报文里所有可能的内容载荷字符串。"""
    out: List[Any] = []
    for s in iter_strings(message):
        if isinstance(s, str) and 8 <= len(s) <= 20000:
            if load_content_json(s) is not None:
                out.append(s)
    return out
