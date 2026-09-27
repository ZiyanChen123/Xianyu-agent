"""Agent 核心：带工具调用的对话循环。

一次客户消息 → 一次 `Assistant.run()`：
    系统提示词 + 历史 + 客户消息
      → 模型可能要求调用工具
      → 本地执行工具，把结果回灌
      → 继续下一轮，直到模型给出最终文字（或超过 max_iterations）
      → 返回要发给客户的文字

设计取舍：
- **不使用流式**：闲鱼一次只发一条消息，流式没有收益，反而更难处理工具调用。
- **工具失败不算致命**：错误信息原样回给模型，让它自己纠正重试。
- **超轮次兜底**：不把模型逼死，直接用最后一轮的文字或一句客气话回复客户。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger
from openai import OpenAI

from agent import tools as tool_mod
from config import settings


@dataclass
class AgentRun:
    """一次 Agent 运行的完整轨迹，便于 GUI 展示与排查。"""

    reply: str = ""
    iterations: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0


class Agent:
    """通用客服 Agent。线程安全性：每次 run 都用局部变量，实例本身无状态。"""

    def __init__(self) -> None:
        self._client: Optional[OpenAI] = None
        self._client_key: str = ""

    # ------------------------------------------------------------------ #
    def _get_client(self) -> OpenAI:
        """按当前配置返回客户端；配置变了（GUI 改过 key/base_url）会自动重建。"""
        llm = settings.llm
        fingerprint = f"{llm.get('api_key')}|{llm.get('base_url')}|{llm.get('timeout')}"
        if self._client is None or fingerprint != self._client_key:
            self._client = OpenAI(
                api_key=llm.get("api_key") or "EMPTY",
                base_url=llm.get("base_url") or None,
                timeout=float(llm.get("timeout") or 120),
            )
            self._client_key = fingerprint
        return self._client

    # ------------------------------------------------------------------ #
    def build_messages(
        self,
        user_text: str,
        history: Optional[List[Dict[str, str]]] = None,
        item_desc: str = "",
        images: Optional[List[str]] = None,
        image_notes: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        system = settings.system_prompt

        extra: List[str] = []
        if item_desc:
            extra.append(f"【当前商品信息】{item_desc}")
        if images:
            extra.append(
                f"【买家本条消息带了 {len(images)} 张图片】"
                + ("，图片已保存到：" + "、".join(image_notes) if image_notes else "")
            )
        if extra:
            system = system + "\n\n" + "\n".join(extra)

        messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]

        limit = int(settings.agent.get("history_limit") or 10)
        for msg in (history or [])[-limit:]:
            role = msg.get("role")
            content = str(msg.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": user_text or "（买家只发了图片，没有文字）"})
        return messages

    # ------------------------------------------------------------------ #
    def run(
        self,
        user_text: str,
        history: Optional[List[Dict[str, str]]] = None,
        item_desc: str = "",
        images: Optional[List[str]] = None,
        image_notes: Optional[List[str]] = None,
    ) -> AgentRun:
        """跑一轮完整对话，返回最终要发给客户的文字。"""
        started = time.time()
        run = AgentRun()
        messages = self.build_messages(user_text, history, item_desc, images, image_notes)

        schemas = tool_mod.build_schemas()
        max_iter = int(settings.agent.get("max_iterations") or 6)
        llm = settings.llm

        for i in range(1, max_iter + 1):
            run.iterations = i
            try:
                kwargs: Dict[str, Any] = {
                    "model": llm.get("model"),
                    "messages": messages,
                    "temperature": float(llm.get("temperature") or 0.4),
                    "max_tokens": int(llm.get("max_tokens") or 800),
                }
                if schemas:
                    kwargs["tools"] = schemas
                    kwargs["tool_choice"] = "auto"
                resp = self._get_client().chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001
                run.error = str(exc)
                logger.error(f"大模型调用失败: {exc}")
                break

            if not resp.choices:
                run.error = "模型返回空 choices"
                break

            msg = resp.choices[0].message
            calls = getattr(msg, "tool_calls", None) or []

            if not calls:
                content = (msg.content or "").strip()
                if content:
                    run.reply = content
                    break
                # 既没有文字也没有工具调用：可能是纯推理被截断
                reason = (getattr(msg, "reasoning_content", "") or "").strip()
                if reason and i < max_iter:
                    messages.append({"role": "assistant", "content": "（继续）"})
                    continue
                run.error = "模型既没有回复文字也没有调用工具"
                break

            # 记录 assistant 的工具调用消息（协议要求原样回灌）
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.function.name, "arguments": c.function.arguments},
                    }
                    for c in calls
                ],
            })

            for call in calls:
                name = call.function.name
                try:
                    import json

                    args = json.loads(call.function.arguments or "{}")
                except Exception:
                    args = {}
                result = tool_mod.execute(name, args)
                run.tool_calls.append({"name": name, "arguments": args, "result": result[:1500]})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })
        else:
            # 循环跑满都没给出文字
            run.error = run.error or f"达到最大工具调用轮次（{max_iter}）仍未给出回复"

        if not run.reply:
            run.reply = self._fallback_reply(run)

        run.elapsed = time.time() - started
        logger.info(
            f"[agent] {run.iterations} 轮 / {len(run.tool_calls)} 次工具调用 / "
            f"{run.elapsed:.1f}s → {run.reply[:60]}"
        )
        return run

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fallback_reply(run: AgentRun) -> str:
        """兜底话术：宁可让客户等一下，也不要发奇怪的半成品。"""
        if run.tool_calls:
            return "稍等一下哈，我确认一下细节再回你～"
        return "抱歉，我这边刚才卡了一下，能再说一遍吗？"
