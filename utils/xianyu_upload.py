"""把本地图片上传到闲鱼 CDN。

接口（已用真实账号验证，2026-09）::

    POST https://stream-upload.goofish.com/api/upload.api
         ?floderId=0&appkey=xy_chat&_input_charset=utf-8
    multipart 字段名: file
    带登录 Cookie

真实响应示例::

    {"object": {"fileId": "...", "url": "https://img.alicdn.com/imgextra/.../xxx.jpg",
                "pix": "256x256", "size": "1653", "quality": 100},
     "success": true, "status": 0}

注意 URL 在 `object.url`，尺寸在 `object.pix`（"宽x高"），比再下载一次图片量尺寸更准。
"""
from __future__ import annotations

import io
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Union

import requests
from loguru import logger

UPLOAD_URL = (
    "https://stream-upload.goofish.com/api/upload.api"
    "?floderId=0&appkey=xy_chat&_input_charset=utf-8"
)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")

# 闲鱼聊天只可靠地支持图片，其它类型不保证能渲染
SUPPORTED_SUFFIX = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


@dataclass
class UploadedImage:
    url: str
    width: int
    height: int
    file_id: str = ""


def prepare_image(data: bytes, max_dim: int = 1600, max_bytes: int = 5 * 1024 * 1024) -> bytes:
    """统一转 JPEG 并压到闲鱼能接受的尺寸/体积；失败时原样返回。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            w, h = img.size
            if max(w, h) > max_dim:
                if w >= h:
                    nw, nh = max_dim, max(1, int(h * max_dim / w))
                else:
                    nh, nw = max_dim, max(1, int(w * max_dim / h))
                img = img.resize((nw, nh), Image.Resampling.LANCZOS)

            quality = 88
            out = io.BytesIO()
            img.save(out, "JPEG", quality=quality, optimize=True)
            while out.tell() > max_bytes and quality > 40:
                quality -= 10
                out = io.BytesIO()
                img.save(out, "JPEG", quality=quality, optimize=True)
            return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"图片预处理失败，按原图上传: {exc}")
        return data


def parse_pix(pix: str) -> Optional[Tuple[int, int]]:
    """解析 "256x256" 形式的尺寸。"""
    try:
        w, h = str(pix).lower().split("x")
        return int(w), int(h)
    except Exception:
        return None


def upload_image(
    cookies_str: str, image: Union[bytes, Path, str], *, max_dim: int = 1600, retries: int = 2
) -> Optional[UploadedImage]:
    """上传图片，返回 (url, 宽, 高)。失败返回 None。"""
    if isinstance(image, (str, Path)):
        path = Path(image)
        if not path.exists():
            logger.error(f"待上传文件不存在: {path}")
            return None
        try:
            raw = path.read_bytes()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"读取待上传文件失败: {exc}")
            return None
    else:
        raw = image

    raw = prepare_image(raw, max_dim=max_dim)
    # 尺寸从压缩后的字节里量，避免上传后再下载一次
    width, height = 800, 600
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as img:
            width, height = int(img.width), int(img.height)
    except Exception:
        pass

    filename = f"img_{uuid.uuid4().hex[:12]}.jpg"
    headers = {
        "cookie": cookies_str,
        "Referer": "https://www.goofish.com/",
        "User-Agent": _UA,
        "x-requested-with": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }

    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                UPLOAD_URL, files={"file": (filename, raw, "image/jpeg")},
                headers=headers, timeout=60,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            text = resp.text
            if "<!DOCTYPE html>" in text or "<html" in text.lower():
                raise RuntimeError("返回 HTML，通常是 Cookie 失效")
            payload = json.loads(text)
            obj = payload.get("object") or {}
            url = obj.get("url") or payload.get("url")
            if not url:
                raise RuntimeError(f"响应里没有 url: {text[:200]}")

            size = parse_pix(obj.get("pix", ""))
            if size:
                width, height = size
            logger.info(f"图片已上传: {url} ({width}x{height})")
            return UploadedImage(url=url, width=width, height=height,
                                 file_id=str(obj.get("fileId", "")))
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < retries:
                logger.warning(f"上传失败，重试中: {exc}")
    logger.error(f"图片上传最终失败: {last_err}")
    return None
