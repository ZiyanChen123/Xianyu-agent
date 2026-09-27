"""闲鱼网关初始 Cookie 链（纯 requests + node 补环境，无需浏览器）。

作用：在真正登录之前，先把闲鱼反爬需要的“环境类” Cookie 拿齐：
    cna / _m_h5_tk / _m_h5_tk_enc / cookie2 / tfstk / xlly_s

其中 tfstk 由 utils/gen_tfstk.js（node 补环境）生成，缺失时可以降级继续，
但被风控的概率会明显升高。

改造自 GuDong2003/xianyu-auto-reply-fix 的 utils/build_cookies.py（AGPL-3.0）。
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

_HERE = Path(__file__).resolve().parent

# 跟真浏览器抓包一致的 mtop XHR 头，缺项容易触发风控
_MTOP_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json",
    "Accept-Language": "en,zh-CN;q=0.9,zh;q=0.8,zh-TW;q=0.7,ja;q=0.6",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Origin": "https://www.goofish.com",
    "Referer": "https://www.goofish.com/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "priority": "u=1, i",
    "Content-Type": "application/x-www-form-urlencoded",
}


def _gen_tfstk(timeout: int = 30) -> str:
    """调用 node 补环境脚本生成 tfstk；失败返回空串（不阻塞登录）。"""
    script = _HERE / "gen_tfstk.js"
    if not script.exists():
        return ""
    try:
        out = subprocess.check_output(
            ["node", str(script)], timeout=timeout, stderr=subprocess.DEVNULL, cwd=str(_HERE)
        )
        return out.decode(errors="ignore").strip()
    except Exception:
        return ""


def build_initial_session(
    *, with_tfstk: bool = True, proxies: Optional[Dict[str, str]] = None
) -> requests.Session:
    """跑完闲鱼网关初始 Cookie 链，返回带 cna / _m_h5_tk / cookie2 / tfstk 的 Session。

    扫码登录、密码登录等需要继续在同一 Session 上发后续请求的流程必须复用本函数。
    """
    s = requests.Session()
    if proxies:
        s.proxies.update(proxies)
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})

    # 1) cna —— mmstat 链路
    try:
        s.get("https://log.mmstat.com/eg.js", timeout=10)
        cna = s.cookies.get("cna", domain=".mmstat.com")
        if cna:
            s.cookies.set("cna", cna, domain=".goofish.com", path="/")
    except Exception:
        pass

    # 2) 调两次 mtop：第一次拿 _m_h5_tk，第二次拿 cookie2
    for api in (
        "mtop.taobao.idlehome.home.webpc.feed",
        "mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get",
    ):
        try:
            s.post(
                f"https://h5api.m.goofish.com/h5/{api}/1.0/",
                params={
                    "jsv": "2.7.2",
                    "appKey": "34839810",
                    "t": str(int(time.time() * 1000)),
                    "sign": "",
                    "v": "1.0",
                    "type": "originaljson",
                    "dataType": "json",
                    "timeout": "20000",
                    "api": api,
                    "sessionOption": "AutoLoginOnly",
                    "spm_cnt": "a21ybx.home.0.0",
                },
                data="data=%7B%7D",
                headers=_MTOP_HEADERS,
                timeout=10,
            )
        except Exception:
            pass

    # 3) tfstk —— node 补环境
    if with_tfstk:
        tfstk = _gen_tfstk()
        if tfstk:
            s.cookies.set("tfstk", tfstk, domain=".goofish.com", path="/")

    return s


def build_initial_cookies(*, with_tfstk: bool = True) -> Dict[str, str]:
    """扁平 dict 版本：返回闲鱼网关需要的核心 cookie 键值。"""
    s = build_initial_session(with_tfstk=with_tfstk)
    return {
        "cna": s.cookies.get("cna", domain=".goofish.com")
        or s.cookies.get("cna", domain=".mmstat.com"),
        "xlly_s": s.cookies.get("xlly_s", "1"),
        "mtop_partitioned_detect": s.cookies.get("mtop_partitioned_detect", "1"),
        "_m_h5_tk": s.cookies.get("_m_h5_tk"),
        "_m_h5_tk_enc": s.cookies.get("_m_h5_tk_enc"),
        "cookie2": s.cookies.get("cookie2"),
        "tfstk": s.cookies.get("tfstk", domain=".goofish.com"),
    }


def session_to_cookie_str(session: requests.Session) -> str:
    """Session → 闲鱼接口可直接使用的 Cookie 头字符串（同名 Cookie 保留最后一个）。"""
    ordered: Dict[str, str] = {}
    for cookie in session.cookies:
        if cookie.domain and (".goofish.com" in cookie.domain or ".mmstat.com" in cookie.domain):
            ordered[cookie.name] = cookie.value
        elif not cookie.domain:
            ordered[cookie.name] = cookie.value
    return "; ".join(f"{k}={v}" for k, v in ordered.items() if v)


if __name__ == "__main__":  # 自检：只打印初始 Cookie，不涉及登录
    import json

    print(json.dumps(build_initial_cookies(), ensure_ascii=False, indent=2))
