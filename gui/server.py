"""管理后台（GUI）后端。

用 FastAPI 提供一组 REST 接口 + 一个单页前端，跑在 127.0.0.1 上，只给本机用。
接口契约见 README，前端在 gui/static/index.html。

设计要点：
- 只绑定回环地址，不做鉴权（本机自用）。
- 机器人生命周期通过 main.runtime 控制。
- 配置走 config.settings（JSON 为准），保存后立刻生效，部分项需重启机器人。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from config import DEFAULT_SYSTEM_PROMPT, settings
from agent.tools import TOOLS

STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX = STATIC_DIR / "index.html"

# 技能名只允许中英文数字下划线连字符，避免路径穿越
_SKILL_NAME_RE = re.compile(r"^[\w\u4e00-\u9fa5\-]{1,64}$")
_DESC_RE = re.compile(r"^\s*<!--\s*desc:\s*(.*?)\s*-->\s*$", re.IGNORECASE)


def _skill_path(name: str) -> Path:
    name = (name or "").strip()
    if name.lower().endswith(".md"):
        name = name[:-3]
    if not _SKILL_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="技能名不合法（只允许中英文、数字、下划线、连字符）")
    return settings.skills_dir / f"{name}.md"


def _read_description(path: Path) -> str:
    """技能描述：优先读文件头部的 <!-- desc: ... --> 注释。"""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _DESC_RE.match(line)
            if m:
                return m.group(1)[:120]
            s = line.strip()
            if s and not s.startswith("#") and not s.startswith("<!--"):
                return s[:120]
    except Exception:
        pass
    return ""


def _write_skill(path: Path, content: str, description: str = "") -> None:
    """写入技能文件；description 作为头部注释保存，确保它是单文件、方便 git 管理。"""
    body = content or ""
    # 去掉旧的 desc 注释，避免重复堆积
    body = "\n".join(
        ln for ln in body.splitlines() if not _DESC_RE.match(ln)
    ).lstrip("\n")
    if description:
        body = f"<!-- desc: {description.strip()[:120]} -->\n{body}"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)


def create_app(runtime) -> FastAPI:
    app = FastAPI(title="闲鱼 Agent 管理后台", docs_url=None, redoc_url=None)

    # ---------------- 首页 ---------------- #
    @app.get("/")
    def index() -> Any:
        if not _INDEX.exists():
            return JSONResponse(
                status_code=500,
                content={"ok": False, "message": "前端文件缺失：gui/static/index.html"},
            )
        return FileResponse(_INDEX, media_type="text/html; charset=utf-8")

    # ---------------- 状态与生命周期 ---------------- #
    @app.get("/api/status")
    def status() -> Dict[str, Any]:
        return runtime.status()

    @app.post("/api/bot/start")
    async def bot_start() -> Dict[str, Any]:
        return runtime.start()

    @app.post("/api/bot/stop")
    async def bot_stop() -> Dict[str, Any]:
        return runtime.stop()

    @app.post("/api/bot/restart")
    async def bot_restart() -> Dict[str, Any]:
        return runtime.restart()

    # ---------------- 配置 ---------------- #
    @app.get("/api/config")
    def get_config() -> Dict[str, Any]:
        return {"config": settings.data, "problems": settings.problems()}

    @app.put("/api/config")
    def put_config(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        incoming = payload.get("config")
        if not isinstance(incoming, dict):
            raise HTTPException(status_code=400, detail="缺少 config 字段")
        # 空字符串的密钥不覆盖已有值，避免前端掩码回填把 key 清空
        old_llm_key = settings.llm.get("api_key", "")
        was_running = runtime.running
        settings.replace(incoming)
        if not settings.llm.get("api_key") and old_llm_key:
            settings.set("llm.api_key", old_llm_key)
        settings.save()
        logger_msg = "配置已保存"
        if was_running:
            logger_msg += "（部分配置需点「重启」后生效）"
        return {"ok": True, "message": logger_msg, "problems": settings.problems()}

    # ---------------- 系统提示词 ---------------- #
    @app.get("/api/prompt")
    def get_prompt() -> Dict[str, Any]:
        return {"system_prompt": settings.data["agent"].get("system_prompt", ""),
                "is_default": settings.is_default_prompt}

    @app.put("/api/prompt")
    def put_prompt(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        text = payload.get("system_prompt")
        if not isinstance(text, str):
            raise HTTPException(status_code=400, detail="缺少 system_prompt 字段")
        settings.set("agent.system_prompt", text)
        settings.save()
        return {"ok": True, "message": "系统提示词已保存（下一条消息即生效）"}

    @app.post("/api/prompt/reset")
    def reset_prompt() -> Dict[str, Any]:
        settings.set("agent.system_prompt", DEFAULT_SYSTEM_PROMPT)
        settings.save()
        return {"ok": True, "system_prompt": DEFAULT_SYSTEM_PROMPT}

    # ---------------- 技能库 ---------------- #
    @app.get("/api/skills")
    def list_skills() -> Dict[str, Any]:
        d = settings.skills_dir
        items: List[Dict[str, Any]] = []
        if d.exists():
            for p in sorted(d.glob("*.md")):
                if not p.is_file():
                    continue
                try:
                    st = p.stat()
                    items.append({
                        "name": p.stem,
                        "description": _read_description(p),
                        "size": st.st_size,
                        "updated_at": __import__("time").strftime(
                            "%Y-%m-%d %H:%M:%S", __import__("time").localtime(st.st_mtime)
                        ),
                    })
                except Exception:
                    continue
        return {"skills": items}

    @app.get("/api/skills/{name}")
    def get_skill(name: str) -> Dict[str, Any]:
        path = _skill_path(name)
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"技能「{name}」不存在")
        return {"name": path.stem, "content": path.read_text(encoding="utf-8")}

    @app.put("/api/skills/{name}")
    def put_skill(name: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        content = payload.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="缺少 content 字段")
        path = _skill_path(name)
        _write_skill(path, content, str(payload.get("description") or ""))
        return {"ok": True, "message": f"技能「{path.stem}」已保存"}

    @app.delete("/api/skills/{name}")
    def delete_skill(name: str) -> Dict[str, Any]:
        path = _skill_path(name)
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"技能「{name}」不存在")
        path.unlink()
        return {"ok": True, "message": f"技能「{name}」已删除"}

    # ---------------- 日志 ---------------- #
    @app.get("/api/logs")
    def logs(lines: int = 200) -> Dict[str, Any]:
        from main import log_buffer

        return {"lines": log_buffer.tail(lines)}

    # ---------------- 扫码登录 ---------------- #
    @app.post("/api/login/qr")
    def login_qr() -> Dict[str, Any]:
        from cookie_login import get_login_state, start_login_async

        result = start_login_async()
        state = get_login_state()
        return {
            "ok": True,
            "message": result.get("message", ""),
            "qr_png_base64": state.get("qr_png_base64", ""),
        }

    @app.get("/api/login/qr")
    def login_qr_status() -> Dict[str, Any]:
        from cookie_login import get_login_state

        state = get_login_state()
        return {
            "status": state.get("status", "idle"),
            "message": state.get("message", ""),
            "account": state.get("account", ""),
        }

    # ---------------- 会话 ---------------- #
    @app.get("/api/sessions")
    def sessions(limit: int = 30) -> Dict[str, Any]:
        try:
            from context_manager import ChatContextManager

            cm = runtime.live.context_manager if runtime.live else ChatContextManager()
            items = cm.recent_sessions(limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"读取会话失败: {exc}") from exc

        live = runtime.live
        for it in items:
            if live:
                it["manual_mode"] = live.is_manual_mode(it["chat_id"])
        return {"sessions": items}

    @app.get("/api/sessions/{chat_id}")
    def session_detail(chat_id: str) -> Dict[str, Any]:
        try:
            from context_manager import ChatContextManager

            cm = runtime.live.context_manager if runtime.live else ChatContextManager()
            return {"messages": cm.get_messages(chat_id)}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"读取会话失败: {exc}") from exc

    @app.post("/api/sessions/{chat_id}/manual")
    def session_manual(chat_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        if runtime.live is None:
            raise HTTPException(status_code=400, detail="机器人未运行，无法切换接管状态")
        manual = bool(payload.get("manual"))
        runtime.live.set_manual_mode(chat_id, manual)
        return {"ok": True, "message": ("已接管该会话" if manual else "已恢复 AI 自动回复")}

    # ---------------- 工具 ---------------- #
    @app.get("/api/tools")
    def tools() -> Dict[str, Any]:
        flags = settings.agent.get("tools", {}) or {}
        return {
            "tools": [
                {"name": name, "description": spec.description,
                 "enabled": bool(flags.get(name, True))}
                for name, spec in TOOLS.items()
            ]
        }

    return app
