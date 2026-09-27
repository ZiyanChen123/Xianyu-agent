"""扫码登录 / Cookie 管理。

GUI 与管理后台共用这套逻辑：
    login_and_save()     扫码登录并把 Cookie 写回 data/config.json（同时存完整 Cookie Jar）
    check_cookie()       校验 Cookie 是否仍有效
    start_login_async()  后台线程跑一次登录，供 GUI 轮询进度

命令行：
    python cookie_login.py            # 终端扫码登录
    python cookie_login.py --check    # 体检当前 Cookie
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from typing import Any, Dict, Optional

import requests
from loguru import logger

from config import settings
from utils.build_cookies import UA
from utils.qr_login import qrcode_login

# 供 GUI 查询的登录状态（单进程内共享）
LOGIN_STATE: Dict[str, Any] = {
    "status": "idle",      # idle / waiting / scanned / confirmed / success / failed
    "message": "",
    "account": "",
    "started_at": 0.0,
    "qr_png_base64": "",
}
_state_lock = threading.Lock()


def _set_state(**kwargs: Any) -> None:
    with _state_lock:
        LOGIN_STATE.update(kwargs)


def get_login_state() -> Dict[str, Any]:
    with _state_lock:
        return dict(LOGIN_STATE)


# --------------------------------------------------------------------------- #
def check_cookie(cookies_str: str, timeout: int = 15) -> bool:
    """调用 hasLogin.do 判断 Cookie 是否仍然登录有效。"""
    if not cookies_str or "unb=" not in cookies_str:
        return False
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Referer": "https://www.goofish.com/"})
        for part in cookies_str.split("; "):
            if "=" in part:
                k, v = part.split("=", 1)
                s.cookies.set(k, v, domain=".goofish.com")
        resp = s.post(
            "https://passport.goofish.com/newlogin/hasLogin.do",
            params={"appName": "xianyu", "fromSite": "77"},
            data={
                "hid": s.cookies.get("unb", ""), "ltl": "true", "appName": "xianyu",
                "appEntrance": "web", "_csrf_token": s.cookies.get("XSRF-TOKEN", ""),
                "umidToken": "", "hsiz": s.cookies.get("cookie2", ""),
                "bizParams": "taobaoBizLoginFrom=web", "mainPage": "false",
                "isMobile": "false", "lang": "zh_CN", "returnUrl": "", "fromSite": "77",
                "isIframe": "true", "documentReferer": "https://www.goofish.com/",
                "defaultView": "hasLogin", "umidTag": "SERVER",
                "deviceId": s.cookies.get("cna", ""),
            },
            timeout=timeout,
        )
        return bool(resp.json().get("content", {}).get("success"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Cookie 校验异常: {exc}")
        return False


# --------------------------------------------------------------------------- #
def login_and_save(*, timeout: Optional[int] = None, headless: bool = True) -> bool:
    """执行一次扫码登录：Cookie 写回 config.json，完整 Jar 存 data/cookies.json。"""
    timeout = timeout or 180
    try:
        cookies, account = qrcode_login(
            timeout=timeout,
            show_qrcode_in_terminal=not headless,
            on_qr_url=_on_qr_url,
            on_status=_on_status,
        )
    except TimeoutError as exc:
        logger.error(f"扫码登录超时/过期: {exc}")
        _set_state(status="failed", message=str(exc))
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error(f"扫码登录失败: {exc}")
        _set_state(status="failed", message=str(exc))
        return False

    from utils.cookie_store import save_jar
    from utils.qr_login import cookies_to_str

    jar = account.get("cookie_jar")
    if jar:
        save_jar(jar, settings.data_dir / "cookies.json")

    settings.set("xianyu.cookies_str", cookies_to_str(cookies))
    settings.save()

    _set_state(
        status="success",
        message="登录成功",
        account=account.get("tracknick") or account.get("unb") or "",
    )
    logger.success(f"登录成功，账号: {account.get('tracknick') or account.get('unb')}")
    return True


def _on_qr_url(qr_url: str) -> None:
    """拿到二维码：存 PNG 并把 base64 放进状态，供 GUI 直接显示。"""
    import base64
    import io

    b64 = ""
    try:
        import qrcode

        img = qrcode.make(qr_url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        png = settings.qr_png
        png.parent.mkdir(parents=True, exist_ok=True)
        png.write_bytes(buf.getvalue())
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"二维码生成失败: {exc}")

    _set_state(status="waiting", message="请用闲鱼 App 扫码", qr_png_base64=b64)
    logger.info("登录二维码已就绪，请用闲鱼 App 扫码")


def _on_status(status: str) -> None:
    mapping = {"NEW": "waiting", "SCANED": "scanned", "SCANNED": "scanned",
               "CONFIRMED": "confirmed", "EXPIRED": "failed"}
    text = {"waiting": "等待扫码", "scanned": "已扫码，请在手机上确认",
            "confirmed": "已确认，正在登录", "failed": "二维码已过期"}.get(status, status)
    _set_state(status=mapping.get(status, "waiting"), message=text)


def start_login_async(timeout: Optional[int] = None) -> Dict[str, Any]:
    """给 GUI 用：后台线程跑一次登录，立刻返回，进度靠 LOGIN_STATE 轮询。"""
    state = get_login_state()
    if state["status"] in ("waiting", "scanned", "confirmed"):
        return {"ok": True, "message": "登录流程已在进行中"}

    _set_state(status="waiting", message="正在获取二维码...", account="",
               qr_png_base64="", started_at=time.time())

    thread = threading.Thread(
        target=login_and_save, kwargs={"timeout": timeout or 180},
        daemon=True, name="xianyu-qr-login",
    )
    thread.start()
    # 稍等一下，让前端第一次轮询就能拿到二维码图片
    for _ in range(60):
        if get_login_state()["qr_png_base64"] or get_login_state()["status"] == "failed":
            break
        time.sleep(0.2)
    return {"ok": True, "message": "已开始登录流程"}


# --------------------------------------------------------------------------- #
def doctor() -> int:
    """体检：Cookie 是否有效 + 完整 Jar 里关键项是否齐全（只打印名字）。"""
    from utils.cookie_store import jar_to_str, load_jar, required_missing, summarize

    settings.load()
    entries = load_jar(settings.data_dir / "cookies.json")
    print("=" * 66)
    print(f"Cookie Jar 文件: {settings.data_dir / 'cookies.json'}")
    if entries:
        print(f"共 {len(entries)} 条，按域分布（只列名字）:")
        print(summarize(entries))
        missing = required_missing(entries)
        print(f"缺失必需项: {', '.join(missing) if missing else '无 ✅'}")
        print(f"发给 h5api 的 Cookie 头长度: {len(jar_to_str(entries))}")
    else:
        print("（没有 cookies.json，请先扫码登录）")
    print("-" * 66)
    ok = check_cookie(settings.cookies_str)
    print("hasLogin 校验: " + ("✅ Cookie 有效" if ok else "❌ Cookie 已失效"))
    print("=" * 66)
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="闲鱼扫码登录")
    parser.add_argument("--check", action="store_true", help="体检当前 Cookie")
    parser.add_argument("--timeout", type=int, default=None, help="扫码等待秒数")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <level>{message}</level>")

    if args.check:
        return doctor()

    ok = login_and_save(timeout=args.timeout, headless=False)
    if ok and check_cookie(settings.cookies_str):
        logger.success("Cookie 校验通过 ✅")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
