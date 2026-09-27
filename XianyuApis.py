import time
import os
import re
import sys

import requests
from loguru import logger
from utils.xianyu_utils import generate_sign


class CookieExpiredError(RuntimeError):
    """Cookie 已失效（或被风控拦截），需要重新扫码登录。

    主程序捕获该异常后会拉起 cookie_login 自动重新登录，而不是直接退出进程。
    """


class XianyuApis:
    def __init__(self):
        self.url = 'https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/'
        self.session = requests.Session()
        self.session.headers.update({
            'accept': 'application/json',
            'accept-language': 'zh-CN,zh;q=0.9',
            'cache-control': 'no-cache',
            'origin': 'https://www.goofish.com',
            'pragma': 'no-cache',
            'priority': 'u=1, i',
            'referer': 'https://www.goofish.com/',
            'sec-ch-ua': '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
        })
        
    def clear_duplicate_cookies(self):
        """清理重复的cookies"""
        # 创建一个新的CookieJar
        new_jar = requests.cookies.RequestsCookieJar()
        
        # 记录已经添加过的cookie名称
        added_cookies = set()
        
        # 按照cookies列表的逆序遍历（最新的通常在后面）
        cookie_list = list(self.session.cookies)
        cookie_list.reverse()
        
        for cookie in cookie_list:
            # 如果这个cookie名称还没有添加过，就添加到新jar中
            if cookie.name not in added_cookies:
                new_jar.set_cookie(cookie)
                added_cookies.add(cookie.name)
                
        # 替换session的cookies
        self.session.cookies = new_jar
        
        # 更新完cookies后，更新.env文件
        self.update_env_cookies()
        
    def update_env_cookies(self):
        """更新.env文件中的COOKIES_STR。

        安全性约束（很重要）：
        1. 只在拿到含 `unb` 的完整 Cookie 时才写回，避免用半截 Cookie 覆盖掉有效登录；
        2. 先写临时文件再原子替换，避免进程被强杀时把 .env 写成半截导致 Cookie 报废。
        """
        try:
            # 获取当前cookies的字符串形式
            cookie_str = '; '.join([f"{cookie.name}={cookie.value}" for cookie in self.session.cookies])

            if 'unb=' not in cookie_str:
                logger.debug("当前 Cookie 不含 unb，跳过写回 .env（避免覆盖有效登录）")
                return

            # 读取.env文件
            env_path = os.path.join(os.getcwd(), '.env')
            if not os.path.exists(env_path):
                logger.warning(".env文件不存在，无法更新COOKIES_STR")
                return
                
            with open(env_path, 'r', encoding='utf-8') as f:
                env_content = f.read()
                
            # 使用正则表达式替换COOKIES_STR的值
            if 'COOKIES_STR=' in env_content:
                new_env_content = re.sub(
                    r'^COOKIES_STR=.*$',
                    lambda _m: f'COOKIES_STR={cookie_str}',
                    env_content,
                    flags=re.MULTILINE,
                )
                if new_env_content == env_content:
                    return
                # 原子写入
                tmp_path = env_path + '.tmp'
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    f.write(new_env_content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, env_path)
                logger.debug("已更新.env文件中的COOKIES_STR")
            else:
                logger.warning(".env文件中未找到COOKIES_STR配置项")
        except Exception as e:
            logger.warning(f"更新.env文件失败: {str(e)}")
        
    def hasLogin(self, retry_count=0):
        """调用hasLogin.do接口进行登录状态检查"""
        if retry_count >= 2:
            logger.error("Login检查失败，重试次数过多")
            return False
            
        try:
            url = 'https://passport.goofish.com/newlogin/hasLogin.do'
            params = {
                'appName': 'xianyu',
                'fromSite': '77'
            }
            data = {
                'hid': self.session.cookies.get('unb', ''),
                'ltl': 'true',
                'appName': 'xianyu',
                'appEntrance': 'web',
                '_csrf_token': self.session.cookies.get('XSRF-TOKEN', ''),
                'umidToken': '',
                'hsiz': self.session.cookies.get('cookie2', ''),
                'bizParams': 'taobaoBizLoginFrom=web',
                'mainPage': 'false',
                'isMobile': 'false',
                'lang': 'zh_CN',
                'returnUrl': '',
                'fromSite': '77',
                'isIframe': 'true',
                'documentReferer': 'https://www.goofish.com/',
                'defaultView': 'hasLogin',
                'umidTag': 'SERVER',
                'deviceId': self.session.cookies.get('cna', '')
            }
            
            response = self.session.post(url, params=params, data=data)
            res_json = response.json()
            
            if res_json.get('content', {}).get('success'):
                logger.debug("Login成功")
                # 清理和更新cookies
                self.clear_duplicate_cookies()
                return True
            else:
                logger.warning(f"Login失败: {res_json}")
                time.sleep(0.5)
                return self.hasLogin(retry_count + 1)
                
        except Exception as e:
            logger.error(f"Login请求异常: {str(e)}")
            time.sleep(0.5)
            return self.hasLogin(retry_count + 1)

    def get_token(self, device_id, retry_count=0, relogin_count=0):
        """获取 WebSocket 用的 accessToken。

        注意 relogin_count：这里必须显式计数，否则会出现「hasLogin 成功 → 重试 get_token
        → 又失败 → 再 hasLogin」的**无限死循环**，把接口打成刷子（早期版本就踩过这个坑）。
        现在策略是：最多重试 2 次 → 允许通过 hasLogin 续一次 → 仍然失败就直接抛
        CookieExpiredError，交给主程序走扫码重登。
        """
        if retry_count >= 2:
            if relogin_count >= 1:
                logger.error("Token 接口持续失败（已尝试过 hasLogin 续期）")
                logger.error("🔴 常见原因：Cookie 不完整（缺 _m_h5_tk / cookie2）、或账号被风控")
                raise CookieExpiredError("Token 接口持续返回会话过期，需要重新扫码登录")
            logger.warning("获取token失败，尝试通过 hasLogin 续期")
            if self.hasLogin():
                logger.info("hasLogin 成功，重新尝试获取token（仅再试一轮）")
                return self.get_token(device_id, 0, relogin_count + 1)
            logger.error("hasLogin 也失败，Cookie 已失效")
            raise CookieExpiredError("Cookie 已失效，需要重新扫码登录")

        params = {
            'jsv': '2.7.2',
            'appKey': '34839810',
            't': str(int(time.time()) * 1000),
            'sign': '',
            'v': '1.0',
            'type': 'originaljson',
            'accountSite': 'xianyu',
            'dataType': 'json',
            'timeout': '20000',
            'api': 'mtop.taobao.idlemessage.pc.login.token',
            'sessionOption': 'AutoLoginOnly',
            'spm_cnt': 'a21ybx.im.0.0',
            "spm_pre": "a21ybx.item.want.1.14ad3da6ALVq3n",
            "log_id": "14ad3da6ALVq3n"
        }
        data_val = '{"appKey":"444e9908a51d1cb236a27862abc769c9","deviceId":"' + device_id + '"}'
        data = {
            'data': data_val,
        }
        headers = {
            "Host": "h5api.m.goofish.com",
            "sec-ch-ua-platform": "\"Windows\"",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "accept": "application/json",
            "sec-ch-ua": "\"Chromium\";v=\"146\", \"Not-A.Brand\";v=\"24\", \"Google Chrome\";v=\"146\"",
            "content-type": "application/x-www-form-urlencoded",
            "sec-ch-ua-mobile": "?0",
            "origin": "https://www.goofish.com",
            "sec-fetch-site": "same-site",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": "https://www.goofish.com/",
            "accept-language": "en,zh-CN;q=0.9,zh;q=0.8,zh-TW;q=0.7,ja;q=0.6",
            "priority": "u=1, i"
        }
        # 简单获取token，信任cookies已清理干净
        token = self.session.cookies.get('_m_h5_tk', '').split('_')[0]
        
        sign = generate_sign(params['t'], token, data_val)
        params['sign'] = sign
        
        try:
            response = self.session.post('https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/', headers=headers, params=params, data=data)
            res_json = response.json()
            
            if isinstance(res_json, dict):
                ret_value = res_json.get('ret', [])
                # 检查ret是否包含成功信息
                if not any('SUCCESS::调用成功' in ret for ret in ret_value):
                    # 检测风控/限流错误
                    error_msg = str(ret_value)
                    if 'RGV587_ERROR' in error_msg or '被挤爆啦' in error_msg:
                        logger.error(f"❌ 触发风控: {ret_value}")
                        logger.error("🔴 需要更新 Cookie（通常是滑块验证），交由主程序自动重新登录")
                        raise CookieExpiredError("触发风控，需要重新扫码并过滑块")

                    if 'SESSION_EXPIRED' in error_msg:
                        # 会话过期通常不是「多等一会就好」，而是 Cookie 不完整/域串了。
                        # 这里把体检结果打出来，方便一眼看出缺哪一项。
                        self._log_cookie_health(error_msg)
                    else:
                        logger.warning(f"Token API调用失败，错误信息: {ret_value}")

                    # 处理响应中的Set-Cookie（mtop 首次调用会下发新的 _m_h5_tk，必须吸收）
                    if 'Set-Cookie' in response.headers:
                        logger.debug("检测到 Set-Cookie，更新 cookie 后重试")
                        self.clear_duplicate_cookies()
                    time.sleep(0.8 * (retry_count + 1))
                    return self.get_token(device_id, retry_count + 1, relogin_count)
                else:
                    logger.info("Token获取成功")
                    return res_json
            else:
                logger.error(f"Token API返回格式异常: {res_json}")
                return self.get_token(device_id, retry_count + 1, relogin_count)

        except CookieExpiredError:
            # 不能被下面的 except Exception 吞掉，否则风控/失效会被当成网络抖动无限重试
            raise
        except Exception as e:
            logger.error(f"Token API请求异常: {str(e)}")
            time.sleep(0.5)
            return self.get_token(device_id, retry_count + 1, relogin_count)

    def _log_cookie_health(self, error_msg: str) -> None:
        """打印 Cookie 体检结果（只打名字，不打值），定位 SESSION_EXPIRED 的原因。"""
        logger.error(f"❌ mtop 返回: {error_msg}")
        try:
            from utils.cookie_store import required_missing, summarize

            entries = [
                {"name": c.name, "value": c.value, "domain": c.domain or "", "path": c.path or "/"}
                for c in self.session.cookies
            ]
            logger.error("Cookie 体检（只列名字）:")
            for line in summarize(entries).splitlines():
                logger.error(line)
            missing = required_missing(entries)
            if missing:
                logger.error(f"🔴 缺失必需 Cookie: {', '.join(missing)}")
            else:
                logger.error("必需 Cookie 齐全，仍报会话过期 → 多半是账号被风控了")
            logger.error(f"Session 内 Cookie 总数: {len(entries)}")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Cookie 体检失败: {exc}")

    def get_item_info(self, item_id, retry_count=0):
        """获取商品信息，自动处理token失效的情况"""
        if retry_count >= 3:  # 最多重试3次
            logger.error("获取商品信息失败，重试次数过多")
            return {"error": "获取商品信息失败，重试次数过多"}
            
        params = {
            'jsv': '2.7.2',
            'appKey': '34839810',
            't': str(int(time.time()) * 1000),
            'sign': '',
            'v': '1.0',
            'type': 'originaljson',
            'accountSite': 'xianyu',
            'dataType': 'json',
            'timeout': '20000',
            'api': 'mtop.taobao.idle.pc.detail',
            'sessionOption': 'AutoLoginOnly',
            'spm_cnt': 'a21ybx.im.0.0',
        }
        
        data_val = '{"itemId":"' + item_id + '"}'
        data = {
            'data': data_val,
        }
        
        # 简单获取token，信任cookies已清理干净
        token = self.session.cookies.get('_m_h5_tk', '').split('_')[0]
        
        sign = generate_sign(params['t'], token, data_val)
        params['sign'] = sign
        
        try:
            response = self.session.post(
                'https://h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail/1.0/', 
                params=params, 
                data=data
            )
            
            res_json = response.json()
            # 检查返回状态
            if isinstance(res_json, dict):
                ret_value = res_json.get('ret', [])
                # 检查ret是否包含成功信息
                if not any('SUCCESS::调用成功' in ret for ret in ret_value):
                    logger.warning(f"商品信息API调用失败，错误信息: {ret_value}")
                    # 处理响应中的Set-Cookie
                    if 'Set-Cookie' in response.headers:
                        logger.debug("检测到Set-Cookie，更新cookie")
                        self.clear_duplicate_cookies()
                    time.sleep(0.5)
                    return self.get_item_info(item_id, retry_count + 1)
                else:
                    logger.debug(f"商品信息获取成功: {item_id}")
                    return res_json
            else:
                logger.error(f"商品信息API返回格式异常: {res_json}")
                return self.get_item_info(item_id, retry_count + 1)
                
        except Exception as e:
            logger.error(f"商品信息API请求异常: {str(e)}")
            time.sleep(0.5)
            return self.get_item_info(item_id, retry_count + 1)
