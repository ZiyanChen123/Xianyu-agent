"""Cookie 持久化：保留完整 Cookie Jar（含 domain / path），而不是压成一个字符串。

为什么要这么麻烦：
    登录后不同域下可能存在**同名但不同值**的 Cookie（例如 `cookie2`、`_m_h5_tk` 在
    `.goofish.com`、`passport.goofish.com`、`www.goofish.com` 上各有一份）。
    如果只把它拍平成一个 `name=value` 字符串，再灌回 Session，就只能靠名字去重，
    很容易把 h5api 需要的那一份覆盖掉，导致 mtop 接口一直返回
    `FAIL_SYS_SESSION_EXPIRED::Session过期`。

    所以：运行中的进程优先从 `data/cookies.json` 精确还原整个 Jar；
    `.env` 里的 `COOKIES_STR` 仅作为人肉可读的兜底。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from loguru import logger

# 与闲鱼 h5api / wss 相关的域，只有这些域下的 Cookie 才可能有用
RELEVANT_DOMAINS = (".goofish.com", "goofish.com", ".taobao.com", ".mmstat.com", ".dingtalk.com")


def _iter_cookies(obj: Any) -> Iterable[Any]:
    """兼容传入 requests.Session / CookieJar / cookie 列表。"""
    if obj is None:
        return []
    if isinstance(obj, requests.Session):
        return list(obj.cookies)
    if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes, dict)):
        return list(obj)
    return []


def dump_jar(cookies: Any) -> List[Dict[str, Any]]:
    """CookieJar / Session / 已序列化的 list[dict] → 可 JSON 序列化的列表。

    注意：必须同时接受「Cookie 对象」和「已经是 dict 的条目」。
    之前只处理前者，导致 qr_login 里 dump 过的结果再 dump 一次时全部静默丢弃，
    cookies.json 写成了 0 条（踩过的坑）。
    """
    out: List[Dict[str, Any]] = []
    for c in _iter_cookies(cookies):
        # 已经是 dict（来自上一次 dump_jar）→ 直接规范化
        if isinstance(c, dict):
            if c.get("name"):
                out.append({
                    "name": c["name"],
                    "value": c.get("value", ""),
                    "domain": c.get("domain", "") or "",
                    "path": c.get("path", "/") or "/",
                    "secure": bool(c.get("secure", False)),
                    "expires": c.get("expires"),
                })
            continue
        try:
            out.append({
                "name": c.name,
                "value": c.value,
                "domain": c.domain or "",
                "path": c.path or "/",
                "secure": bool(getattr(c, "secure", False)),
                "expires": getattr(c, "expires", None),
            })
        except Exception:
            continue
    return out


def save_jar(cookies: Any, path: Path) -> int:
    """原子写入 cookies.json，返回写入条数。"""
    entries = dump_jar(cookies)
    if not entries:
        logger.warning(f"save_jar: 解析出 0 条 Cookie，未写入 {path}（这通常意味着上游传参有问题）")
        return 0
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cookies": entries,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return len(entries)


def load_jar(path: Path) -> List[Dict[str, Any]]:
    """读取 cookies.json，失败返回空列表。"""
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("cookies") if isinstance(data, dict) else data
        return [e for e in entries if isinstance(e, dict) and e.get("name")] if isinstance(entries, list) else []
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取 {path} 失败: {exc}")
        return []


def apply_jar(session: requests.Session, entries: List[Dict[str, Any]]) -> int:
    """把 cookies.json 精确还原到 Session（含 domain / path）。"""
    session.cookies.clear()
    n = 0
    for e in entries:
        try:
            session.cookies.set(
                e["name"],
                e["value"],
                domain=e.get("domain", "") or "",
                path=e.get("path", "/") or "/",
                secure=bool(e.get("secure", False)),
            )
            n += 1
        except Exception:
            continue
    return n


def jar_of_domain(entries: List[Dict[str, Any]], domain: str = ".goofish.com") -> List[Dict[str, Any]]:
    """取出指定域下的 Cookie。domain 用后缀匹配，`.goofish.com` 也能匹配到 `www.goofish.com`。"""
    return [e for e in entries if domain in (e.get("domain") or "")]


def jar_to_str(entries: List[Dict[str, Any]], domain: Optional[str] = ".goofish.com") -> str:
    """拍平成 Cookie 头字符串。默认只取 .goofish.com 相关域，且同名以「更精确的域」优先。"""
    chosen: Dict[str, str] = {}
    for e in entries:
        d = e.get("domain") or ""
        if domain and domain not in d:
            continue
        name = e["name"]
        # 同名的场景：优先接受 domain 正好是 .goofish.com 的那一份
        if name in chosen and d != domain:
            continue
        chosen[name] = e["value"]
    return "; ".join(f"{k}={v}" for k, v in chosen.items() if v)


def summarize(entries: List[Dict[str, Any]]) -> str:
    """给日志用的概览：各域下有哪些 Cookie 名字（不打印值，避免泄漏）。"""
    by_domain: Dict[str, List[str]] = {}
    for e in entries:
        by_domain.setdefault(e.get("domain") or "(host-only)", []).append(e["name"])
    lines = []
    for d, names in sorted(by_domain.items()):
        names = sorted(set(names))
        lines.append(f"    {d or '(空)'}: {len(names)} 个 -> {', '.join(names)}")
    return "\n".join(lines)


def important_present(entries: List[Dict[str, Any]]) -> Dict[str, bool]:
    """体检：闲鱼登录/取 token 必需的关键 Cookie 是否齐全。

    required 里的缺一个就基本取不到 token；`cna` 只影响设备指纹，缺了通常也能跑，
    所以单独列为 optional，避免刷无谓的告警。
    """
    names = {e["name"] for e in entries}
    need = ["unb", "cookie2", "_m_h5_tk", "_m_h5_tk_enc", "_tb_token_", "tfstk"]
    optional = ["cna", "sgcookie", "csg"]
    flags = {k: (k in names) for k in need}
    flags.update({k: (k in names) for k in optional})
    return flags


def required_missing(entries: List[Dict[str, Any]]) -> List[str]:
    """只返回真正卡住取 token 的缺失项（不含 cna 这类可选项）。"""
    names = {e["name"] for e in entries}
    return [k for k in ("unb", "cookie2", "_m_h5_tk", "_m_h5_tk_enc", "tfstk") if k not in names]
