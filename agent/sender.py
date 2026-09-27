"""工具 ↔ 闲鱼连接之间的桥。

为什么需要它：
    工具是在**工作线程**里同步执行的（`asyncio.to_thread`），
    但发消息必须回到主事件循环的 WebSocket 上。
    所以这里保存事件循环和当前的 XianyuLive 实例，
    工具线程通过 `run_coroutine_threadsafe` 把发送任务甩回主循环并等结果。

    另外工具还需要知道「现在在跟哪个会话说话」——这个由 XianyuLive
    在处理每条消息前通过 `bind_session()` 设置（调用方是主循环，所以是安全的）。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Optional

from loguru import logger


class Sender:
    """把 Agent 的工具调用转成真实的闲鱼发送动作。"""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._live: Any = None          # XianyuLive 实例
        self._session: Optional[dict] = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    def bind_loop(self, loop: asyncio.AbstractEventLoop, live: Any) -> None:
        with self._lock:
            self._loop = loop
            self._live = live

    def unbind(self) -> None:
        with self._lock:
            self._loop = None
            self._live = None
            self._session = None

    def bind_session(self, chat_id: str, user_id: str, item_id: str = "") -> None:
        """标记当前正在处理哪个会话（工具发送时的目标）。"""
        with self._lock:
            self._session = {"chat_id": chat_id, "user_id": user_id, "item_id": item_id}

    def clear_session(self) -> None:
        with self._lock:
            self._session = None

    @property
    def available(self) -> bool:
        with self._lock:
            return self._loop is not None and self._live is not None and self._session is not None

    # ------------------------------------------------------------------ #
    def _run(self, coro, timeout: float = 90.0):
        with self._lock:
            loop, live = self._loop, self._live
        if loop is None or live is None:
            raise RuntimeError("闲鱼连接未就绪，无法发送")
        if loop.is_closed():
            raise RuntimeError("事件循环已关闭")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

    # ------------------------------------------------------------------ #
    def send_file(self, path: str) -> str:
        """把 workspace 里的图片发给当前买家。返回给模型看的结果文字。"""
        if not self.available:
            raise RuntimeError("当前没有正在对话的买家，无法发送文件")
        from agent.tools import resolve_path

        local = resolve_path(path)
        if not local.exists():
            raise RuntimeError(f"文件不存在: {path}")
        if local.is_dir():
            raise RuntimeError(f"{path} 是目录，不能发送")
        if local.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
            raise RuntimeError(
                f"闲鱼聊天只支持发送图片，不支持 {local.suffix or '该类型'}。"
                "请先用工具把内容变成图片，或直接把内容写在回复里。"
            )

        with self._lock:
            session = dict(self._session or {})
        reply = self._run(
            self._live.send_file_to(session["chat_id"], session["user_id"], local)
        )
        if not reply:
            raise RuntimeError("图片发送失败（上传或投递失败），请检查 Cookie 是否失效")
        return f"已把 {local.name} 发给买家"

    def send_item(self, item_id: str, note: str = "") -> str:
        """把闲鱼商品（宝贝）发给当前买家，方便对方点开下单。"""
        if not self.available:
            raise RuntimeError("当前没有正在对话的买家，无法发送宝贝")
        item_id = str(item_id or "").strip()
        if not item_id:
            with self._lock:
                item_id = str((self._session or {}).get("item_id") or "")
        if not item_id:
            raise RuntimeError("没有可发送的宝贝 ID，请显式传入 item_id")

        with self._lock:
            session = dict(self._session or {})
        ok = self._run(self._live.send_item_to(session["chat_id"], session["user_id"], item_id, note))
        if not ok:
            raise RuntimeError("宝贝发送失败")
        return f"已把宝贝 {item_id} 发给买家，并附了一句引导下单的话"


sender = Sender()
