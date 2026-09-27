"""闲鱼扫码登录（纯 HTTP，无需 Playwright / 浏览器）。

流程:
  1. build_initial_session 拿 cna / _m_h5_tk / cookie2 / tfstk
  2. 打开 passport.goofish.com/mini_login.htm 拿 XSRF-TOKEN
  3. newlogin/qrcode/generate.do 生成二维码
  4. 轮询 newlogin/qrcode/query.do，直到 CONFIRMED
  5. login_token/login.do 完成登录，并刷新用户态 _m_h5_tk
  6. 收集 .goofish.com 域下全部 Cookie

改造自 GuDong2003/xianyu-auto-reply-fix 的 utils/qr_login_lite.py（AGPL-3.0）。
"""
from __future__ import annotations

import sys
import time
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import quote

import requests
from loguru import logger

from utils.build_cookies import UA, _MTOP_HEADERS, build_initial_session

_PASSPORT_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en,zh-CN;q=0.9,zh;q=0.8,zh-TW;q=0.7,ja;q=0.6",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "priority": "u=1, i",
}

STATUS_DESC = {
    "NEW": "等待扫码",
    "SCANNED": "已扫码，请在手机上确认",
    "CONFIRMED": "已确认",
    "EXPIRED": "二维码已过期",
}


def render_qr_terminal(qr_url: str) -> None:
    """在终端用 ▀▄█ 字符画二维码（备用，主用是存 PNG 文件）。"""
    try:
        import qrcode as qr_lib
    except ImportError:
        logger.warning(f"qrcode 包未安装，跳过终端渲染；请手动扫码: {qr_url}")
        return
    qr = qr_lib.QRCode(border=1, box_size=1)
    qr.add_data(qr_url)
    qr.make()
    matrix = qr.get_matrix()
    rows = len(matrix)
    lines = []
    for r in range(0, rows, 2):
        line = ""
        for c in range(len(matrix[r])):
            top = matrix[r][c]
            bot = matrix[r + 1][c] if r + 1 < rows else False
            line += "█" if (top and bot) else ("▀" if top else ("▄" if bot else " "))
        lines.append(line)
    text = "\n".join(lines) + "\n"
    try:
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
    except Exception:
        # 控制台编码受限（如 Windows GBK）时不要影响登录流程
        try:
            sys.stdout.write(text)
        except Exception:
            logger.debug("终端无法渲染二维码，请直接扫 data/login_qrcode.png")


def qrcode_login(
    poll_interval: float = 3.0,
    timeout: float = 180.0,
    show_qrcode_in_terminal: bool = True,
    on_qr_url: Optional[Callable[[str], None]] = None,
    on_status: Optional[Callable[[str], None]] = None,
    proxies: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """纯 HTTP 扫码登录闲鱼。

    Returns:
        (cookies_dict, account_info)   cookies 为 .goofish.com 域下扁平键值对，
        account_info 含 unb / tracknick / device_id。
    Raises:
        TimeoutError: 超时或二维码过期。
        RuntimeError: passport 接口异常 / 登录后拿不到 unb。
    """
    # ── 1. 初始 Cookie ──
    s = build_initial_session(proxies=proxies)
    cna = (
        s.cookies.get("cna", domain=".goofish.com")
        or s.cookies.get("cna", domain=".mmstat.com")
        or ""
    )
    cookie2 = s.cookies.get("cookie2", domain=".goofish.com") or ""

    # ── 2. mini_login.htm 拿 XSRF-TOKEN ──
    s.get(
        "https://passport.goofish.com/mini_login.htm",
        params={
            "lang": "zh_cn",
            "appName": "xianyu",
            "appEntrance": "web",
            "styleType": "vertical",
            "bizParams": "",
            "notLoadSsoView": "false",
            "notKeepLogin": "false",
            "isMobile": "false",
            "qrCodeFirst": "false",
            "stie": "77",
            "rnd": "0.6842814084442211",
        },
        headers={
            **_PASSPORT_HEADERS,
            "Referer": "https://www.goofish.com/",
            "sec-fetch-site": "same-site",
            "sec-fetch-dest": "iframe",
            "sec-fetch-mode": "navigate",
        },
        timeout=15,
    )
    csrf_token = s.cookies.get("XSRF-TOKEN", domain="passport.goofish.com") or ""

    # ── 3. 生成二维码 ──
    biz_params = f"taobaoBizLoginFrom=web&renderRefer={quote('https://www.goofish.com/')}"
    common_params = {
        "appName": "xianyu",
        "fromSite": "77",
        "appEntrance": "web",
        "_csrf_token": csrf_token,
        "umidToken": "",
        "hsiz": cookie2,
        "bizParams": biz_params,
        "mainPage": "false",
        "isMobile": "false",
        "lang": "zh_CN",
        "returnUrl": "",
        "umidTag": "SERVER",
    }
    gen_resp_raw = s.get(
        "https://passport.goofish.com/newlogin/qrcode/generate.do",
        params=common_params,
        headers={**_PASSPORT_HEADERS, "Referer": "https://passport.goofish.com/mini_login.htm"},
        timeout=15,
    )
    try:
        gen_resp = gen_resp_raw.json()
    except ValueError as exc:
        raise RuntimeError(f"生成二维码响应非 JSON: status={gen_resp_raw.status_code}") from exc

    gen_data = (gen_resp.get("content") or {}).get("data") or {}
    qr_url = gen_data.get("codeContent")
    qr_t = gen_data.get("t")
    qr_ck = gen_data.get("ck")
    if not (qr_url and qr_t and qr_ck):
        raise RuntimeError(f"生成二维码失败: {gen_resp}")

    logger.info(f"获取到登录二维码: {qr_url}")
    if on_qr_url is not None:
        try:
            on_qr_url(qr_url)
        except Exception:
            logger.exception("on_qr_url 回调异常")
    if show_qrcode_in_terminal:
        render_qr_terminal(qr_url)

    # ── 4. 轮询等待扫码确认 ──
    query_base = {
        **common_params,
        "navlanguage": "en",
        "navUserAgent": UA,
        "navPlatform": "Win32",
        "isIframe": "true",
        "documentReferer": "https://www.goofish.com/",
        "defaultView": "sms",
        "deviceId": cna,
    }
    deadline = time.time() + timeout
    login_token: Optional[str] = None
    last_status = ""

    while time.time() < deadline:
        try:
            resp = s.post(
                "https://passport.goofish.com/newlogin/qrcode/query.do?appName=xianyu&fromSite=77",
                data={**query_base, "t": str(qr_t), "ck": qr_ck},
                headers={
                    **_PASSPORT_HEADERS,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://passport.goofish.com",
                    "Referer": "https://passport.goofish.com/mini_login.htm",
                },
                timeout=10,
            )
            qdata = (resp.json().get("content") or {}).get("data") or {}
        except Exception as exc:
            logger.debug(f"轮询二维码状态异常（忽略）: {exc}")
            qdata = {}

        status = qdata.get("qrCodeStatus", "")
        if status != last_status:
            remaining = max(0, int(deadline - time.time()))
            logger.info(f"二维码状态: [{status or '???'}] {STATUS_DESC.get(status, status)} (剩余 {remaining}s)")
            last_status = status
            if on_status is not None and status:
                try:
                    on_status(status)
                except Exception:
                    logger.exception("on_status 回调异常")

        if status == "CONFIRMED":
            login_token = qdata.get("token") or qdata.get("lgToken")
            break
        if status == "EXPIRED":
            raise TimeoutError("二维码已过期，请重新登录")
        time.sleep(poll_interval)

    if not login_token and not s.cookies.get("unb"):
        raise TimeoutError("扫码超时，未完成登录")

    # ── 5. 用 login_token 完成登录 ──
    if login_token:
        try:
            s.post(
                "https://passport.goofish.com/login_token/login.do",
                params={
                    "token": login_token,
                    "subFlow": "DIALOG_CHECK_LOGIN_RPC",
                    "nextCode": "0018",
                    "bizScene": "qrcode",
                    "confirm": "true",
                },
                data={"deviceId": cna},
                headers={
                    **_PASSPORT_HEADERS,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://passport.goofish.com",
                    "Referer": "https://passport.goofish.com/mini_login.htm",
                },
                timeout=15,
            )
        except Exception as exc:
            logger.warning(f"login_token/login.do 调用异常（继续尝试）: {exc}")

    # ── 6. 刷新用户态 _m_h5_tk ──
    try:
        s.post(
            "https://h5api.m.goofish.com/h5/mtop.idle.web.user.page.nav/1.0/",
            params={
                "jsv": "2.7.2",
                "appKey": "34839810",
                "t": str(int(time.time() * 1000)),
                "sign": "",
                "v": "1.0",
                "type": "originaljson",
                "dataType": "json",
                "timeout": "20000",
                "api": "mtop.idle.web.user.page.nav",
                "sessionOption": "AutoLoginOnly",
                "spm_cnt": "a21ybx.home.0.0",
            },
            data="data=%7B%7D",
            headers=_MTOP_HEADERS,
            timeout=10,
        )
    except Exception:
        pass

    # ── 7. 收集 Cookie ──
    unb = s.cookies.get("unb", domain=".goofish.com") or ""
    tracknick = s.cookies.get("tracknick", domain=".goofish.com") or ""
    if not unb:
        raise RuntimeError("登录链路完成但未拿到 unb cookie，扫码登录失败")

    cookies_dict: Dict[str, str] = {}
    for c in s.cookies:
        if c.domain and (".goofish.com" in c.domain or ".mmstat.com" in c.domain):
            cookies_dict[c.name] = c.value

    from utils.cookie_store import dump_jar, required_missing, summarize

    cookie_jar = dump_jar(s.cookies)

    from utils.xianyu_utils import generate_device_id

    account_info = {
        "unb": unb,
        "tracknick": tracknick,
        "device_id": generate_device_id(unb),
        # 完整 Cookie Jar（含 domain/path），供精确还原 Session，避免同名 Cookie 串域
        "cookie_jar": cookie_jar,
    }

    missing = required_missing(cookie_jar)
    logger.info("扫码登录成功 unb={} tracknick={}".format(unb, tracknick))
    logger.info(f"Cookie 概览（只列名字，共 {len(cookie_jar)} 条）:\n" + summarize(cookie_jar))
    if missing:
        logger.warning(f"⚠️ 缺失必需 Cookie: {', '.join(missing)}（会导致取 token 失败）")
    else:
        logger.info("✅ 取 token 所需的 Cookie 齐全")
    return cookies_dict, account_info


def cookies_to_str(cookies: Dict[str, str]) -> str:
    """扁平 dict → Cookie 头字符串。"""
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
