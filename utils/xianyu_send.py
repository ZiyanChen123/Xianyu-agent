"""闲鱼消息构造（纯文本）。

文本消息的 LWP 帧格式（逆向自公开实现，2026-09 有效）::

    {"lwp": "/r/MessageSend/sendByReceiverScope",
     "headers": {"mid": ...},
     "body": [
        {"uuid": ..., "cid": "<chatId>@goofish", "conversationType": 1,
         "content": {"contentType": 101,
                     "custom": {"type": 1, "data": base64(JSON)}},
         ...},
        {"actualReceivers": ["<toId>@goofish", "<myId>@goofish"]}]}

其中 base64 解出来的 JSON 是::

    {"contentType": 1, "text": {"text": "你好"}}
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict

from utils.xianyu_utils import generate_mid, generate_uuid

APP_KEY = "444e9908a51d1cb236a27862abc769c9"  # 闲鱼 Web IM 的 app-key（公开常量）
SEND_LWP = "/r/MessageSend/sendByReceiverScope"


def build_text_message(cid: str, toid: str, myid: str, text: str) -> Dict[str, Any]:
    """构造一条可直接 ws.send(json.dumps(...)) 的文本消息帧。"""
    payload = base64.b64encode(
        json.dumps({"contentType": 1, "text": {"text": text}}, ensure_ascii=False).encode("utf-8")
    ).decode("utf-8")

    return {
        "lwp": SEND_LWP,
        "headers": {"mid": generate_mid()},
        "body": [
            {
                "uuid": generate_uuid(),
                "cid": f"{cid}@goofish",
                "conversationType": 1,
                "content": {"contentType": 101, "custom": {"type": 1, "data": payload}},
                "redPointPolicy": 0,
                "extension": {"extJson": "{}"},
                "ctx": {"appVersion": "1.0", "platform": "web"},
                "mtags": {},
                "msgReadStatusSetting": 1,
            },
            {"actualReceivers": [f"{toid}@goofish", f"{myid}@goofish"]},
        ],
    }


def build_image_message(
    cid: str, toid: str, myid: str, url: str, width: int, height: int
) -> Dict[str, Any]:
    """构造图片消息帧（url 必须是先用 utils.xianyu_upload 上传拿到的 CDN 地址）。"""
    payload = base64.b64encode(json.dumps({
        "contentType": 2,
        "image": {"pics": [{"height": int(height), "type": 0, "url": url,
                            "width": int(width)}]},
    }, ensure_ascii=False).encode("utf-8")).decode("utf-8")

    return {
        "lwp": SEND_LWP,
        "headers": {"mid": generate_mid()},
        "body": [
            {
                "uuid": generate_uuid(),
                "cid": f"{cid}@goofish",
                "conversationType": 1,
                "content": {"contentType": 101, "custom": {"type": 1, "data": payload}},
                "redPointPolicy": 0,
                "extension": {"extJson": "{}"},
                "ctx": {"appVersion": "1.0", "platform": "web"},
                "mtags": {},
                "msgReadStatusSetting": 1,
            },
            {"actualReceivers": [f"{toid}@goofish", f"{myid}@goofish"]},
        ],
    }


def build_create_conversation(to_user_id: str, myid: str, item_id: str = "") -> Dict[str, Any]:
    """构造「主动创建会话」的 LWP 请求，响应里会带回 cid。"""
    return {
        "lwp": "/r/SingleChatConversation/create",
        "headers": {"mid": generate_mid()},
        "body": [
            {
                "pairFirst": f"{to_user_id}@goofish",
                "pairSecond": f"{myid}@goofish",
                "bizType": "1",
                "extension": {"itemId": str(item_id or "")},
                "ctx": {"appVersion": "1.0", "platform": "web"},
            }
        ],
    }


def split_text(text: str, limit: int = 480) -> list:
    """把长回复切成多条（闲鱼单条有长度上限）。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks, current = [], ""
    for para in text.split("\n"):
        while len(para) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:limit])
            para = para[limit:]
        if not current:
            current = para
        elif len(current) + len(para) + 1 <= limit:
            current = f"{current}\n{para}"
        else:
            chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]
