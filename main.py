"""闲鱼 Agent 服务框架 —— 主程序。

同一进程里跑两件事：
  1. uvicorn 提供管理后台（GUI）           http://127.0.0.1:8787
  2. 闲鱼 WebSocket 长连接 + Agent 自动回复

GUI 可以随时启动/停止机器人、改配置、改系统提示词、管技能库、看日志和会话。

命令行：
    python main.py                #  GUI + 机器人（默认）
    python main.py --no-gui       #  只跑机器人（无后台）
    python main.py --port 9000    #  换 GUI 端口
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import websockets
from loguru import logger

from agent import Agent
from config import ROOT, settings
from context_manager import ChatContextManager
from utils.xianyu_message_parser import iter_strings, parse_chat_message
from utils.xianyu_send import build_text_message, split_text
from utils.xianyu_utils import decrypt, generate_device_id, generate_mid, trans_cookies
from XianyuApis import CookieExpiredError, XianyuApis


# --------------------------------------------------------------------------- #
# 日志环形缓冲：给 GUI 的「运行日志」页用
# --------------------------------------------------------------------------- #
class LogBuffer:
    def __init__(self, maxlen: int = 3000) -> None:
        self.lines: collections.deque = collections.deque(maxlen=maxlen)

    def write(self, text: str) -> None:
        self.lines.append(text)

    def tail(self, n: int = 200) -> List[str]:
        try:
            n = max(1, min(int(n or 200), 3000))
        except (TypeError, ValueError):
            n = 200
        return list(self.lines)[-n:]


log_buffer = LogBuffer()

# 闲鱼官方插进聊天流的提示语，不是买家说的话，不能当成客户消息回复
_PLATFORM_NOTICE_MARKERS = (
    "恭喜新手卖家", "宝贝有人来询单", "闲鱼客服不会", "缴纳保证金", "开通服务保障",
    "请勿轻信", "如遇以上问题请立刻举报", "请勿脱离平台交易", "请通过闲鱼平台沟通",
    "您的宝贝已被", "安全提醒",
)


def _deep_find_first(obj: Any, keys, depth: int = 0) -> Optional[str]:
    if depth > 8:
        return None
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, (str, int)) and str(v):
                return str(v)
        for v in obj.values():
            found = _deep_find_first(v, keys, depth + 1)
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found = _deep_find_first(v, keys, depth + 1)
            if found:
                return found
    return None


def _extract_conversation_id(resp: Any) -> Optional[str]:
    cid = _deep_find_first(resp, ("cid", "conversationId", "chatId", "conversation_id"))
    if cid:
        return cid.split("@")[0]
    for s in iter_strings(resp):
        if isinstance(s, str) and s.endswith("@goofish"):
            return s.split("@")[0]
    return None


# --------------------------------------------------------------------------- #
class XianyuLive:
    """闲鱼 WebSocket 长连接 + Agent 自动回复。"""

    def __init__(self, cookies_str: str, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self.xianyu = XianyuApis()
        self.base_url = "wss://wss-goofish.dingtalk.com/"
        self.cookies_str = cookies_str
        self.agent = Agent()
        self.context_manager = ChatContextManager()

        self.cookies = trans_cookies(cookies_str)
        if "unb" not in self.cookies:
            raise RuntimeError("Cookie 缺少 unb 字段，请重新扫码登录")
        self.myid = self.cookies["unb"]
        self.device_id = generate_device_id(self.myid)

        self.xianyu.session.cookies.update(self.cookies)

        cfg = settings.xianyu
        self.heartbeat_interval = int(cfg.get("heartbeat_interval") or 15)
        self.heartbeat_timeout = int(cfg.get("heartbeat_timeout") or 5)
        self.last_heartbeat_time = 0.0
        self.last_heartbeat_response = 0.0
        self.heartbeat_task: Optional[asyncio.Task] = None

        self.token_refresh_interval = int(cfg.get("token_refresh_interval") or 3600)
        self.token_retry_interval = int(cfg.get("token_retry_interval") or 300)
        self.last_token_refresh_time = 0.0
        self.current_token: Optional[str] = None
        self.token_refresh_task: Optional[asyncio.Task] = None
        self.connection_restart_flag = False
        self.ws = None
        self._relogin_attempts = 0
        self._pending_mid: Dict[str, asyncio.Future] = {}

        self.manual_mode_conversations: set = set()
        self.manual_mode_timeout = int(cfg.get("manual_mode_timeout") or 3600)
        self.manual_mode_timestamps: Dict[str, float] = {}
        self.toggle_keywords = str(cfg.get("toggle_keywords") or "。")
        self.message_expire_time = int(cfg.get("message_expire_time") or 300000)
        self.simulate_human_typing = bool(cfg.get("simulate_human_typing", True))

        self._queues: Dict[str, asyncio.Queue] = {}
        self._workers: Dict[str, asyncio.Task] = {}
        self._shutdown = asyncio.Event()

        # 把「工具 → 真实发送」的桥接好（工具在工作线程里跑，需要回到主事件循环）
        from agent.sender import sender

        sender.bind_loop(loop or asyncio.get_event_loop(), self)

        self._load_cookie_jar()

    # ------------------------------------------------------------------ #
    def _load_cookie_jar(self) -> None:
        """优先用 data/cookies.json 精确还原 Session（保留 domain/path，避免同名串域）。"""
        from utils.cookie_store import apply_jar, load_jar

        entries = load_jar(settings.data_dir / "cookies.json")
        if not entries:
            return
        n = apply_jar(self.xianyu.session, entries)
        logger.info(f"已从 cookies.json 还原 {n} 条 Cookie")

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    async def _send_text(self, chat_id: str, to_id: str, text: str) -> bool:
        ws = self.ws
        if ws is None:
            logger.warning("WebSocket 未连接，无法发送")
            return False
        if self.simulate_human_typing and text:
            delay = min(0.8 + len(text) * random.uniform(0.03, 0.08), 6.0)
            await asyncio.sleep(delay)
        try:
            await ws.send(json.dumps(build_text_message(chat_id, to_id, self.myid, text)))
            logger.info(f"→ 回复 {to_id}: {text[:80]}")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"发送失败: {exc}")
            return False

    async def _send_long(self, chat_id: str, to_id: str, text: str) -> None:
        for chunk in split_text(text, int(settings.agent.get("reply_max_chars") or 480)):
            if not await self._send_text(chat_id, to_id, chunk):
                break
            await asyncio.sleep(0.4)

    # ------------------------------------------------------------------ #
    # 供 Agent 工具调用的发送能力（由 agent.sender 从工作线程调回主循环）
    # ------------------------------------------------------------------ #
    async def send_file_to(self, chat_id: str, to_id: str, path: Path) -> bool:
        """上传图片并发给买家。"""
        from utils.xianyu_send import build_image_message
        from utils.xianyu_upload import upload_image

        ws = self.ws
        if ws is None:
            logger.warning("WebSocket 未连接，无法发送图片")
            return False
        try:
            uploaded = await asyncio.to_thread(upload_image, self.cookies_str, path)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"图片上传异常: {exc}")
            return False
        if not uploaded:
            return False
        try:
            await ws.send(json.dumps(build_image_message(
                chat_id, to_id, self.myid, uploaded.url, uploaded.width, uploaded.height)))
            logger.info(f"→ 已发送图片给 {to_id}: {uploaded.url}")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"发送图片失败: {exc}")
            return False

    async def send_item_to(self, chat_id: str, to_id: str, item_id: str, note: str = "") -> bool:
        """把宝贝发给买家。

        闲鱼聊天里发商品链接会自动渲染成可点击的宝贝卡片，买家点开即可下单。
        先发卡片链接，再补一句引导话术。
        """
        link = f"https://www.goofish.com/item?id={item_id}"
        text = note.strip() or "就是这个宝贝，拍下我马上给你安排～"
        ok = await self._send_text(chat_id, to_id, f"{text}\n{link}")
        if ok:
            logger.info(f"→ 已发送宝贝 {item_id} 给 {to_id}")
        return ok

    # ------------------------------------------------------------------ #
    # Token
    # ------------------------------------------------------------------ #
    async def refresh_token(self) -> Optional[str]:
        try:
            result = await asyncio.to_thread(self.xianyu.get_token, self.device_id)
            if isinstance(result, dict) and "data" in result and "accessToken" in result["data"]:
                self.current_token = result["data"]["accessToken"]
                self.last_token_refresh_time = time.time()
                self._relogin_attempts = 0
                logger.info("Token 刷新成功")
                return self.current_token
            logger.error(f"Token 刷新失败: {str(result)[:200]}")
            return None
        except CookieExpiredError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Token 刷新异常: {exc}")
            return None

    async def token_refresh_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                if time.time() - self.last_token_refresh_time >= self.token_refresh_interval:
                    logger.info("Token 即将过期，准备刷新...")
                    try:
                        new_token = await self.refresh_token()
                    except CookieExpiredError:
                        self.connection_restart_flag = False
                        try:
                            await self._handle_cookie_expired()
                        except Exception as inner:  # noqa: BLE001
                            logger.error(f"自动重新登录失败: {inner}")
                        return
                    if new_token:
                        self.connection_restart_flag = True
                        if self.ws:
                            await self.ws.close()
                        return
                    await asyncio.sleep(self.token_retry_interval)
                    continue
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Token 刷新循环出错: {exc}")
                await asyncio.sleep(30)

    async def _handle_cookie_expired(self) -> None:
        self._relogin_attempts += 1
        if self._relogin_attempts > 3:
            logger.error("连续重新登录失败，疑似账号被风控；30 分钟后再试")
            self._relogin_attempts = 0
            await asyncio.sleep(1800)
            raise CookieExpiredError("连续重新登录失败")

        logger.error("=" * 60)
        logger.error("Cookie 已失效，请到管理后台点「扫码登录」重新登录")
        logger.error("=" * 60)
        from cookie_login import login_and_save

        ok = await asyncio.to_thread(login_and_save, auto_open=False,
                                     show_terminal=False, timeout=180)
        if not ok:
            await asyncio.sleep(600)
            raise CookieExpiredError("重新登录失败")

        settings.load()
        self.cookies_str = settings.cookies_str
        self.cookies = trans_cookies(self.cookies_str)
        self.myid = self.cookies.get("unb", self.myid)
        self.xianyu = XianyuApis()
        self._load_cookie_jar()
        self.device_id = generate_device_id(self.myid)
        self.current_token = None
        self.last_token_refresh_time = 0.0
        logger.success("Cookie 已更新，即将重连")
        if self.ws:
            await self.ws.close()

    # ------------------------------------------------------------------ #
    # 连接
    # ------------------------------------------------------------------ #
    async def init(self, ws) -> None:
        if not self.current_token:
            await self.refresh_token()
        if not self.current_token:
            raise RuntimeError("Token 获取失败")

        await ws.send(json.dumps({
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": "444e9908a51d1cb236a27862abc769c9",
                "token": self.current_token,
                "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 "
                       "DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) "
                       "DingWeb/2.1.5 IMPaaS DingWeb/2.1.5"),
                "dt": "j", "wv": "im:3,au:3,sy:6", "sync": "0,0;0;0;",
                "did": self.device_id, "mid": generate_mid(),
            },
        }))
        await asyncio.sleep(1)
        await ws.send(json.dumps({
            "lwp": "/r/SyncStatus/ackDiff",
            "headers": {"mid": "5701741704675979 0"},
            "body": [{
                "pipeline": "sync", "tooLong2Tag": "PNM,1", "channel": "sync", "topic": "sync",
                "highPts": 0, "pts": int(time.time() * 1000) * 1000, "seq": 0,
                "timestamp": int(time.time() * 1000),
            }],
        }))
        logger.info("连接注册完成 ✅")

    async def heartbeat_loop(self, ws) -> None:
        while not self._shutdown.is_set():
            try:
                now = time.time()
                if now - self.last_heartbeat_time >= self.heartbeat_interval:
                    await ws.send(json.dumps({"lwp": "/!", "headers": {"mid": generate_mid()}}))
                    self.last_heartbeat_time = now
                if (now - self.last_heartbeat_response) > (self.heartbeat_interval + self.heartbeat_timeout):
                    logger.warning("心跳超时，主动断开重连")
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"心跳循环出错: {exc}")
                return

    def _handle_heartbeat_response(self, message_data: Any) -> bool:
        try:
            if (isinstance(message_data, dict) and "headers" in message_data
                    and "mid" in message_data["headers"] and message_data.get("code") == 200):
                self.last_heartbeat_response = time.time()
                return True
        except Exception:
            pass
        return False

    def _resolve_pending(self, message_data: Any) -> bool:
        try:
            mid = (message_data or {}).get("headers", {}).get("mid")
        except Exception:
            return False
        if not mid:
            return False
        future = self._pending_mid.get(mid)
        if future is None or future.done():
            return False
        future.set_result(message_data)
        return True

    # ------------------------------------------------------------------ #
    # 消息处理
    # ------------------------------------------------------------------ #
    def is_sync_package(self, message_data: Any) -> bool:
        try:
            return (isinstance(message_data, dict) and "body" in message_data
                    and "syncPushPackage" in message_data["body"]
                    and len(message_data["body"]["syncPushPackage"]["data"]) > 0)
        except Exception:
            return False

    def is_typing_status(self, message: Any) -> bool:
        try:
            return (isinstance(message, dict) and isinstance(message.get("1"), list)
                    and message["1"] and isinstance(message["1"][0], dict)
                    and isinstance(message["1"][0].get("1"), str)
                    and "@goofish" in message["1"][0]["1"])
        except Exception:
            return False

    def is_system_message(self, message: Any) -> bool:
        try:
            return (isinstance(message, dict) and isinstance(message.get("3"), dict)
                    and message["3"].get("needPush") == "false")
        except Exception:
            return False

    @staticmethod
    def _is_bracket_system_message(text: str) -> bool:
        t = (text or "").strip()
        return bool(t) and t.startswith("[") and t.endswith("]")

    @staticmethod
    def _is_platform_notice(text: str) -> bool:
        return bool(text) and any(m in text for m in _PLATFORM_NOTICE_MARKERS)

    def is_manual_mode(self, chat_id: str) -> bool:
        if chat_id not in self.manual_mode_conversations:
            return False
        if time.time() - self.manual_mode_timestamps.get(chat_id, 0) > self.manual_mode_timeout:
            self.exit_manual_mode(chat_id)
            return False
        return True

    def enter_manual_mode(self, chat_id: str) -> None:
        self.manual_mode_conversations.add(chat_id)
        self.manual_mode_timestamps[chat_id] = time.time()

    def exit_manual_mode(self, chat_id: str) -> None:
        self.manual_mode_conversations.discard(chat_id)
        self.manual_mode_timestamps.pop(chat_id, None)

    def set_manual_mode(self, chat_id: str, manual: bool) -> None:
        if manual:
            self.enter_manual_mode(chat_id)
        else:
            self.exit_manual_mode(chat_id)

    @staticmethod
    def build_item_description(item_info: Dict[str, Any]) -> str:
        def money(v: Any) -> float:
            try:
                return round(float(v) / 100, 2)
            except (TypeError, ValueError):
                return 0.0

        skus = []
        for sku in item_info.get("skuList", []) or []:
            specs = [p["valueText"] for p in sku.get("propertyList", []) if p.get("valueText")]
            skus.append({"spec": " ".join(specs) if specs else "默认规格",
                         "price": money(sku.get("price", 0)),
                         "stock": sku.get("quantity", 0)})
        prices = [s["price"] for s in skus if s["price"] > 0]
        if prices:
            price_display = (f"¥{min(prices)}" if min(prices) == max(prices)
                             else f"¥{min(prices)}-¥{max(prices)}")
        else:
            price_display = f"¥{round(float(item_info.get('soldPrice', 0) or 0), 2)}"
        return json.dumps({
            "标题": item_info.get("title", ""),
            "描述": item_info.get("desc", ""),
            "价格": price_display,
            "库存": item_info.get("quantity", 0),
            "规格": skus,
        }, ensure_ascii=False)

    async def handle_message(self, message_data: Any, websocket) -> None:
        try:
            headers = message_data.get("headers", {}) if isinstance(message_data, dict) else {}
            ack = {"code": 200, "headers": {"mid": headers.get("mid", generate_mid()),
                                            "sid": headers.get("sid", "")}}
            for key in ("app-key", "ua", "dt"):
                if key in headers:
                    ack["headers"][key] = headers[key]
            await websocket.send(json.dumps(ack))
        except Exception:
            pass

        if not self.is_sync_package(message_data):
            return

        try:
            sync_data = message_data["body"]["syncPushPackage"]["data"][0]
            if "data" not in sync_data:
                return
            raw = sync_data["data"]
            try:
                raw = base64.b64decode(raw).decode("utf-8")
                json.loads(raw)
                return
            except Exception:
                message = json.loads(decrypt(raw))
        except CookieExpiredError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"消息解密失败: {exc}")
            return

        try:
            await self._route_message(message, websocket)
        except CookieExpiredError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"处理消息出错: {exc}")
            logger.debug(f"原始报文: {str(message_data)[:600]}")

    async def _route_message(self, message: Dict[str, Any], websocket) -> None:
        try:
            reminder = message["3"]["redReminder"]
        except Exception:
            reminder = None
        if reminder:
            logger.info(f"[订单事件] {reminder}")
            return

        if self.is_typing_status(message):
            return

        parsed = parse_chat_message(message)
        if not parsed or not parsed.get("chat_id"):
            return

        create_time = parsed.get("create_time_ms") or 0
        if create_time and (time.time() * 1000 - create_time) > self.message_expire_time:
            return

        chat_id = parsed["chat_id"]
        user_id = parsed["user_id"]
        item_id = parsed.get("item_id") or ""
        text = parsed["text"]
        images = parsed["images"]

        # 卖家自己发的消息
        if user_id == self.myid:
            if text.strip() in self.toggle_keywords:
                manual = not self.is_manual_mode(chat_id)
                self.set_manual_mode(chat_id, manual)
                logger.info(f"{'🔴 已接管' if manual else '🟢 已恢复自动'} 会话 {chat_id}")
                return
            self.context_manager.add_message_by_chat(chat_id, self.myid, item_id, "assistant", text)
            logger.info(f"卖家人工回复（会话 {chat_id}）: {text[:60]}")
            return

        if self.is_manual_mode(chat_id):
            self.context_manager.add_message_by_chat(chat_id, user_id, item_id, "user", text)
            logger.info(f"会话 {chat_id} 处于人工接管，跳过自动回复")
            return
        if self._is_bracket_system_message(text) or self.is_system_message(message):
            return
        if self._is_platform_notice(text):
            logger.info(f"识别为闲鱼官方提示，不回复: {text[:50]}")
            return
        if not item_id and not settings.xianyu.get("allow_no_item_context", True):
            return

        logger.info(f"买家 {parsed.get('user_name')} (ID {user_id}) 商品 {item_id or '-'} "
                    f"会话 {chat_id}: {text[:100] or '[图片]'}"
                    f"{f' +{len(images)}张图' if images else ''}")

        item_description = ""
        if item_id:
            try:
                info = self.context_manager.get_item_info(item_id)
                if not info:
                    result = await asyncio.to_thread(self.xianyu.get_item_info, item_id)
                    if isinstance(result, dict) and "data" in result and "itemDO" in result["data"]:
                        info = result["data"]["itemDO"]
                        self.context_manager.save_item_info(item_id, info)
                if info:
                    item_description = self.build_item_description(info)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"获取商品信息失败: {exc}")

        self._enqueue(chat_id, lambda: self._reply(chat_id, user_id, item_id, text, images,
                                                   item_description))

    # ------------------------------------------------------------------ #
    def _save_incoming(self, chat_id: str, urls: List[str]) -> List[str]:
        """把买家发来的图片存到 workspace/inbox，返回给 Agent 看的相对路径。"""
        import requests

        inbox = settings.workspace_dir / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        notes: List[str] = []
        for i, url in enumerate(urls[:4], start=1):
            if not url or not url.startswith("http"):
                continue
            try:
                resp = requests.get(url, timeout=25, headers={
                    "User-Agent": "Mozilla/5.0", "Referer": "https://www.goofish.com/"})
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"下载买家图片失败: {exc}")
                continue
            ext = ".jpg"
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "png" in ctype:
                ext = ".png"
            elif "webp" in ctype:
                ext = ".webp"
            elif "gif" in ctype:
                ext = ".gif"
            name = f"{chat_id}_{int(time.time())}_{i}{ext}"
            try:
                (inbox / name).write_bytes(resp.content)
                notes.append(f"inbox/{name}")
                logger.info(f"买家图片已保存: inbox/{name}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"保存买家图片失败: {exc}")
        return notes

    async def _reply(self, chat_id: str, user_id: str, item_id: str,
                     text: str, images: List[str], item_desc: str) -> None:
        started = time.time()
        notes = await asyncio.to_thread(self._save_incoming, chat_id, images) if images else []

        history: List[Dict[str, str]] = []
        try:
            history = [m for m in self.context_manager.get_context_by_chat(chat_id)
                       if m.get("role") in ("user", "assistant")]
        except Exception:
            history = []

        # 告诉 sender「现在在跟谁说话」，工具发送时才知道目标
        from agent.sender import sender

        sender.bind_session(chat_id, user_id, item_id)
        try:
            run = await asyncio.to_thread(self.agent.run, text, history, item_desc, images, notes)
            reply = run.reply or "稍等一下～"
            tool_names = ", ".join(t["name"] for t in run.tool_calls)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"Agent 运行失败: {exc}")
            reply = "稍等一下，我确认一下再回你～"
            tool_names = ""
        finally:
            sender.clear_session()

        self.context_manager.add_message_by_chat(chat_id, user_id, item_id, "user",
                                                 text or "[图片]")
        await self._send_long(chat_id, user_id, reply)
        self.context_manager.add_message_by_chat(chat_id, self.myid, item_id, "assistant", reply)

        if tool_names:
            logger.info(f"本次调用了工具: {tool_names}")
        logger.info(f"处理完成 会话 {chat_id}，耗时 {time.time() - started:.1f}s")

    def _enqueue(self, chat_id: str, factory) -> None:
        queue = self._queues.get(chat_id)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[chat_id] = queue
        worker = self._workers.get(chat_id)
        if worker is None or worker.done():
            self._workers[chat_id] = asyncio.create_task(self._worker(chat_id, queue))
        queue.put_nowait(factory)

    async def _worker(self, chat_id: str, queue: asyncio.Queue) -> None:
        while True:
            factory = await queue.get()
            try:
                await factory()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"会话 {chat_id} 任务失败: {exc}")
            finally:
                queue.task_done()

    # ------------------------------------------------------------------ #
    async def main(self) -> None:
        backoff = 5
        while not self._shutdown.is_set():
            try:
                self.connection_restart_flag = False
                headers = {
                    "Cookie": self.cookies_str,
                    "Host": "wss-goofish.dingtalk.com",
                    "Connection": "Upgrade",
                    "Pragma": "no-cache", "Cache-Control": "no-cache",
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"),
                    "Origin": "https://www.goofish.com",
                    "Accept-Encoding": "gzip, deflate, br, zstd",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                }
                async with websockets.connect(self.base_url, extra_headers=headers) as websocket:
                    self.ws = websocket
                    await self.init(websocket)
                    backoff = 5
                    self.last_heartbeat_time = time.time()
                    self.last_heartbeat_response = time.time()
                    self.heartbeat_task = asyncio.create_task(self.heartbeat_loop(websocket))
                    self.token_refresh_task = asyncio.create_task(self.token_refresh_loop())

                    async for raw in websocket:
                        if self.connection_restart_flag:
                            break
                        try:
                            message_data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if self._handle_heartbeat_response(message_data):
                            continue
                        if self._resolve_pending(message_data):
                            continue
                        try:
                            await self.handle_message(message_data, websocket)
                        except CookieExpiredError:
                            raise
                        except Exception as exc:  # noqa: BLE001
                            logger.error(f"消息处理异常: {exc}")

            except CookieExpiredError as exc:
                logger.error(f"Cookie 失效: {exc}")
                try:
                    await self._handle_cookie_expired()
                except Exception as inner:  # noqa: BLE001
                    logger.error(f"自动重新登录失败: {inner}")
            except websockets.exceptions.ConnectionClosed as exc:
                logger.warning(f"WebSocket 已关闭: {exc}")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"连接错误: {exc}")
            finally:
                for task in (self.heartbeat_task, self.token_refresh_task):
                    if task:
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                self.heartbeat_task = None
                self.token_refresh_task = None
                self.ws = None

            if self._shutdown.is_set():
                break
            if self.connection_restart_flag:
                continue
            logger.info(f"{backoff} 秒后重连...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)

        for task in list(self._workers.values()):
            task.cancel()


# --------------------------------------------------------------------------- #
class BotRuntime:
    """机器人生命周期管理器，供 GUI 调用。

    调用方既可能来自事件循环（uvicorn 的 async 路由），
    也可能来自线程池（FastAPI 的同步路由会被丢进 threadpool）。
    所以这里统一先拿到「主事件循环」，再决定用 create_task 还是
    run_coroutine_threadsafe —— 否则会报 `no running event loop`。
    """

    def __init__(self) -> None:
        self.live: Optional[XianyuLive] = None
        self.task = None
        self.started_at: Optional[float] = None
        self.last_error: str = ""
        self._starting = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """启动时把主事件循环记下来，供线程池里的调用复用。"""
        self._loop = loop

    def _resolve_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            loop = self._loop
            if loop is None or loop.is_closed():
                return None
            return loop

    def _spawn(self, coro):
        """在当前线程所在的事件循环里建任务；不在循环里就跨线程投递。"""
        loop = self._resolve_loop()
        if loop is None:
            raise RuntimeError("事件循环未就绪，无法启动（请重启程序）")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            return loop.create_task(coro)
        return asyncio.run_coroutine_threadsafe(coro, loop)

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self) -> Dict[str, Any]:
        if self.running or self._starting:
            return {"ok": True, "message": "机器人已在运行中"}
        problems = settings.problems()
        if problems:
            self.last_error = "；".join(problems)
            return {"ok": False, "message": "配置不完整：" + self.last_error}

        loop = self._resolve_loop()
        if loop is None:
            self.last_error = "事件循环未就绪"
            return {"ok": False, "message": "事件循环未就绪，请重启程序"}

        self._starting = True
        self.last_error = ""
        try:
            self.live = XianyuLive(settings.cookies_str, loop=loop)
            self.task = self._spawn(self._supervise())
            self.started_at = time.time()
            logger.info("🤖 机器人已启动")
            return {"ok": True, "message": "机器人已启动"}
        except Exception as exc:  # noqa: BLE001
            self.live = None
            self.task = None
            self.started_at = None
            self.last_error = str(exc)
            logger.error(f"启动失败: {exc}")
            return {"ok": False, "message": f"启动失败：{exc}"}
        finally:
            self._starting = False

    async def _supervise(self) -> None:
        try:
            assert self.live is not None
            await self.live.main()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            logger.exception(f"机器人异常退出: {exc}")

    def stop(self) -> Dict[str, Any]:
        if not self.running:
            self.task = None
            self.started_at = None
            return {"ok": True, "message": "机器人本来就没在运行"}

        if self.live:
            # asyncio.Event 不是线程安全的：不在主循环里就投递过去再设
            try:
                on_loop = asyncio.get_running_loop() is self._loop
            except RuntimeError:
                on_loop = False
            if on_loop or self._loop is None:
                self.live._shutdown.set()
            else:
                try:
                    self._loop.call_soon_threadsafe(self.live._shutdown.set)
                except RuntimeError:
                    pass

        if self.task:
            self.task.cancel()
        self.task = None
        self.started_at = None
        logger.info("🛑 机器人已停止")
        return {"ok": True, "message": "机器人已停止"}

    def restart(self) -> Dict[str, Any]:
        self.stop()
        return self.start()

    def status(self) -> Dict[str, Any]:
        counts = {"chats": 0, "messages": 0}
        try:
            cm = self.live.context_manager if self.live else ChatContextManager()
            counts = cm.counts()
        except Exception:
            pass

        if self.live:
            account = self.live.cookies.get("tracknick") or self.live.myid or ""
        else:
            account = trans_cookies(settings.cookies_str).get("tracknick", "")

        return {
            "running": self.running,
            "account": account,
            "started_at": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at))
                           if self.started_at else ""),
            "uptime_sec": int(time.time() - self.started_at) if self.started_at else 0,
            "last_error": self.last_error,
            "chats": counts.get("chats", 0),
            "messages": counts.get("messages", 0),
            "version": "2.0",
        }


runtime = BotRuntime()


# --------------------------------------------------------------------------- #
def setup_logging() -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <level>{message}</level>")
    logger.add(lambda m: log_buffer.write(str(m).rstrip()), level="INFO",
               format="{time:HH:mm:ss} | {level: <7} | {message}")
    logger.add(settings.log_dir / "app_{time:YYYY-MM-DD}.log", level="DEBUG",
               rotation="00:00", retention="30 days", encoding="utf-8", enqueue=True,
               format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}")


async def amain(no_gui: bool, port: Optional[int], host: Optional[str]) -> None:
    setup_logging()
    runtime.bind_loop(asyncio.get_running_loop())
    logger.info("=" * 60)
    logger.info("闲鱼 Agent 服务框架 v2.0")
    logger.info("=" * 60)

    for d in (settings.workspace_dir, settings.skills_dir, settings.data_dir):
        d.mkdir(parents=True, exist_ok=True)

    problems = settings.problems()
    if problems:
        for p in problems:
            logger.warning(f"配置待完善: {p}")
        logger.warning("（可先打开管理后台，在网页里补齐配置后点「启动」）")
    else:
        logger.info(f"大模型: {settings.llm.get('model')} @ {settings.llm.get('base_url')}")

    if no_gui:
        if problems:
            logger.error("配置不完整且指定了 --no-gui，退出")
            return
        runtime.start()
        while runtime.running:
            await asyncio.sleep(1)
        return

    import uvicorn

    from gui.server import create_app

    gui_host = host or str(settings.gui.get("host") or "127.0.0.1")
    gui_port = int(port or settings.gui.get("port") or 8787)
    logger.info(f"管理后台: http://{gui_host}:{gui_port}")

    app = create_app(runtime)
    config = uvicorn.Config(app, host=gui_host, port=gui_port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    if settings.gui.get("autostart_bot", True) and not problems:
        runtime.start()

    try:
        await server.serve()
    except asyncio.CancelledError:
        pass
    finally:
        runtime.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="闲鱼 Agent 服务框架")
    parser.add_argument("--no-gui", action="store_true", help="只跑机器人，不启动管理后台")
    parser.add_argument("--port", type=int, default=None, help="管理后台端口")
    parser.add_argument("--host", type=str, default=None, help="管理后台监听地址")
    args = parser.parse_args()

    os.chdir(ROOT)
    try:
        asyncio.run(amain(args.no_gui, args.port, args.host))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
