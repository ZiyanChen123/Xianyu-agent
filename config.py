"""配置中心。

**唯一事实来源是 `data/config.json`**，因为这套配置要能被 GUI 读写并在运行时热加载。
首次运行时会自动生成，并尽量从旧的 `.env` 迁移已有密钥，避免用户重填。

用法:
    from config import settings
    settings.llm["model"]            # dict 风格
    settings.get("llm.model")        # 点号路径
    settings.set("llm.model", "xxx"); settings.save()
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent

DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
CONFIG_PATH = DATA_DIR / "config.json"

# --------------------------------------------------------------------------- #
DEFAULT_SYSTEM_PROMPT = """你是闲鱼店铺的客服助手，负责替店主接待买家咨询。

# 你的目标
- 先准确理解买家在问什么，再给有用的回答。
- 拿不准的信息不要编造；不知道就礼貌说明并引导买家等店主确认。

# 说话风格
- 中文，口语化，简短（一般不超过 80 字），像真人店主在打字。
- 一次只说清楚一件事，不要长篇大论，不要用 markdown 标题和分点符号。
- 态度礼貌热情，但不过度承诺。

# 硬性规则
1. 绝不承诺无法兑现的事（无限次修改、绝对满意、包过等）。
2. 不引导买家脱离平台交易（微信/支付宝/线下），一律让其在闲鱼内沟通下单。
3. 涉及账号、密码、验证码、银行卡、身份信息，一律拒绝索要或提供。
4. 遇到纠纷、投诉、退款等敏感问题，不要自行承诺，回复「我帮你反馈店主确认一下」。
5. 不确定商品细节（价格、库存、规格、发货时间）时，先如实说不确定，不要瞎猜。

# 可用工具
你可以调用工具来查资料、读写文件、检索技能库。需要精确信息时先查再答，不要凭空发挥。

- `read_skill`：**遇到具体业务问题（退货、议价、发货、售后）先查技能库**，按店主写好的流程回答
- `read_file` / `write_file` / `edit_file` / `grep`：读写 workspace 里的文件、检索内容
- `send_file`：把图片直接发给买家（截图、示意图等）
- `send_item`：**把店里的宝贝发给买家**，对方点开即可下单。买家说想买、问怎么拍、或者在犹豫时，
  主动发宝贝引导成交，这是最有效的转化手段

工具调用要克制：一句话能答完的就不要调工具；不要在回复里把工具返回的原始内容照抄给买家。
"""

DEFAULTS: Dict[str, Any] = {
    "xianyu": {
        "cookies_str": "",
        "toggle_keywords": "。",
        "simulate_human_typing": True,
        "manual_mode_timeout": 3600,
        "message_expire_time": 300000,
        "heartbeat_interval": 15,
        "heartbeat_timeout": 5,
        "token_refresh_interval": 3600,
        "token_retry_interval": 300,
        "allow_no_item_context": True,
    },
    "llm": {
        "api_key": "",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-flash",
        "temperature": 0.4,
        "max_tokens": 800,
        "timeout": 120,
    },
    "vision": {
        "enabled": False,
        "api_key": "",
        "base_url": "",
        "model": "",
        "timeout": 45,
    },
    "agent": {
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "max_iterations": 6,
        "history_limit": 10,
        "reply_max_chars": 480,
        "workspace_dir": "workspace",
        "tools": {
            "read_file": True,
            "write_file": True,
            "edit_file": True,
            "grep": True,
            "read_skill": True,
            "send_file": True,
            "send_item": True,
        },
    },
    "gui": {
        "host": "127.0.0.1",
        "port": 8787,
        "autostart_bot": True,
        "log_lines": 500,
    },
}

# 键 -> (类型, 最小值, 最大值)
_VALIDATORS: Dict[str, tuple] = {
    "gui.port": (int, 1, 65535),
    "gui.log_lines": (int, 50, 10000),
    "llm.temperature": (float, 0.0, 2.0),
    "llm.max_tokens": (int, 64, 32000),
    "llm.timeout": (int, 10, 600),
    "vision.timeout": (int, 5, 600),
    "agent.max_iterations": (int, 1, 20),
    "agent.history_limit": (int, 0, 100),
    "agent.reply_max_chars": (int, 50, 5000),
    "xianyu.manual_mode_timeout": (int, 60, 86400),
    "xianyu.message_expire_time": (int, 10000, 3600000),
    "xianyu.heartbeat_interval": (int, 5, 300),
    "xianyu.token_refresh_interval": (int, 300, 86400),
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """把 override 合并进 base 的副本（只覆盖 base 里已存在的键，忽略未知键）。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key not in out:
            continue
        if isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """配置对象。GUI 线程与机器人协程都会读写，所以加锁。"""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data: Dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load(migrate=True)

    # ---------------- 读写 ---------------- #
    def load(self, migrate: bool = False) -> None:
        with self._lock:
            existed = self.path.exists()
            raw: Dict[str, Any] = {}
            if existed:
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception:
                    raw = {}
            elif migrate:
                raw = self._migrate_from_env()
            self.data = _deep_merge(DEFAULTS, raw)
            self._normalize()
        if not existed:
            self.save()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def replace(self, incoming: Dict[str, Any]) -> None:
        """用 GUI 提交的配置整体替换（只保留已知键，保持类型正确）。"""
        with self._lock:
            self.data = _deep_merge(DEFAULTS, incoming or {})
            self._normalize()

    # ---------------- 点号路径访问 ---------------- #
    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path: str, value: Any) -> None:
        with self._lock:
            parts = path.split(".")
            node = self.data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value

    # ---------------- 便捷属性 ---------------- #
    @property
    def xianyu(self) -> Dict[str, Any]:
        return self.data["xianyu"]

    @property
    def llm(self) -> Dict[str, Any]:
        return self.data["llm"]

    @property
    def vision(self) -> Dict[str, Any]:
        return self.data["vision"]

    @property
    def agent(self) -> Dict[str, Any]:
        return self.data["agent"]

    @property
    def gui(self) -> Dict[str, Any]:
        return self.data["gui"]

    @property
    def cookies_str(self) -> str:
        return str(self.data["xianyu"].get("cookies_str") or "")

    @property
    def data_dir(self) -> Path:
        return DATA_DIR

    @property
    def log_dir(self) -> Path:
        return LOG_DIR

    @property
    def qr_png(self) -> Path:
        return DATA_DIR / "login_qrcode.png"

    @property
    def workspace_dir(self) -> Path:
        d = Path(str(self.data["agent"].get("workspace_dir") or "workspace"))
        return d if d.is_absolute() else (ROOT / d)

    @property
    def skills_dir(self) -> Path:
        return ROOT / "skills"

    @property
    def system_prompt(self) -> str:
        return str(self.data["agent"].get("system_prompt") or "").strip() or DEFAULT_SYSTEM_PROMPT

    @property
    def is_default_prompt(self) -> bool:
        return str(self.data["agent"].get("system_prompt") or "").strip() == DEFAULT_SYSTEM_PROMPT.strip()

    # ---------------- 校验 ---------------- #
    def problems(self) -> List[str]:
        """返回阻塞运行的问题列表（空 = 可以启动）。"""
        out: List[str] = []
        if not self.llm.get("api_key"):
            out.append("大模型 API Key 未配置")
        if not self.llm.get("base_url"):
            out.append("大模型 Base URL 未配置")
        if not self.llm.get("model"):
            out.append("大模型名称未配置")
        cookies = self.cookies_str
        if not cookies:
            out.append("闲鱼 Cookie 未配置（点「扫码登录」获取）")
        elif "unb=" not in cookies:
            out.append("闲鱼 Cookie 不完整（缺少 unb），请重新扫码登录")
        if self.vision.get("enabled") and not self.vision.get("api_key"):
            out.append("识图已启用但未填识图 API Key")
        return out

    # ---------------- 内部 ---------------- #
    def _normalize(self) -> None:
        for key, (caster, low, high) in _VALIDATORS.items():
            try:
                value = caster(self.get(key))
            except (TypeError, ValueError):
                value = None
            if value is None:
                # 取默认值
                node: Any = DEFAULTS
                for part in key.split("."):
                    node = node[part]
                value = node
            self.set(key, max(low, min(high, value)))

        tools = self.data["agent"].setdefault("tools", {})
        for name in DEFAULTS["agent"]["tools"]:
            tools[name] = bool(tools.get(name, True))

        self.data["xianyu"]["allow_no_item_context"] = bool(
            self.data["xianyu"].get("allow_no_item_context", True)
        )
        self.data["xianyu"]["simulate_human_typing"] = bool(
            self.data["xianyu"].get("simulate_human_typing", True)
        )
        self.data["gui"]["autostart_bot"] = bool(self.data["gui"].get("autostart_bot", True))

        vision = self.data["vision"]
        vision["enabled"] = bool(vision.get("enabled", False))
        if vision["enabled"]:
            vision["api_key"] = vision.get("api_key") or self.llm.get("api_key", "")
            vision["base_url"] = vision.get("base_url") or self.llm.get("base_url", "")
            vision["model"] = vision.get("model") or self.llm.get("model", "")

        # 路径类配置统一成字符串
        self.data["agent"]["workspace_dir"] = str(self.data["agent"].get("workspace_dir") or "workspace")

    def _migrate_from_env(self) -> Dict[str, Any]:
        """从旧版 .env 迁移密钥，避免老用户重填。"""
        env_file = ROOT / ".env"
        if not env_file.exists():
            return {}
        values: Dict[str, str] = {}
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            return {}

        out: Dict[str, Any] = {}
        if values.get("COOKIES_STR"):
            out.setdefault("xianyu", {})["cookies_str"] = values["COOKIES_STR"]
        api_key = values.get("CHAT_API_KEY") or values.get("API_KEY")
        if api_key:
            out.setdefault("llm", {})["api_key"] = api_key
        if values.get("CHAT_BASE_URL") or values.get("MODEL_BASE_URL"):
            out.setdefault("llm", {})["base_url"] = (
                values.get("CHAT_BASE_URL") or values["MODEL_BASE_URL"]
            )
        if values.get("CHAT_MODEL") or values.get("MODEL_NAME"):
            out.setdefault("llm", {})["model"] = values.get("CHAT_MODEL") or values["MODEL_NAME"]
        if values.get("VISION_MODEL"):
            out.setdefault("vision", {}).update({
                "enabled": True,
                "api_key": values.get("VISION_API_KEY", ""),
                "base_url": values.get("VISION_BASE_URL", ""),
                "model": values["VISION_MODEL"],
            })
        return out


settings = Config()

__all__ = [
    "settings", "Config", "ROOT", "DATA_DIR", "LOG_DIR", "CONFIG_PATH",
    "DEFAULTS", "DEFAULT_SYSTEM_PROMPT",
]
