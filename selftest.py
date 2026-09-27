"""自检：不需要闲鱼账号，验证框架的核心链路。

用法:
    python selftest.py            # 全量（含真实大模型调用，会消耗少量 token）
    python selftest.py --offline  # 离线：只验证配置/工具/报文/接口，不花钱

覆盖:
    1. 配置读写 + 越界值收敛
    2. 工具层：read_file / write_file / edit_file / grep / read_skill
       —— 含**越界路径必须被拒绝**的安全回归
    3. 工具层：send_file / send_item（用假发送桥，验证目标会话与参数传递）
    4. Agent 消息组装（系统提示词 + 历史 + 商品 + 图片）
    5. 闲鱼报文解析 + 文本/图片发送帧构造
    6. 闲鱼官方提示过滤
    7. 管理后台 REST 接口冒烟（FastAPI TestClient）
    8. 真实大模型工具调用闭环（模型真的查了技能库再作答）
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

from loguru import logger

from config import ROOT, settings

PASS, FAIL = "✅ PASS", "❌ FAIL"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    print(f"{PASS if ok else FAIL}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


# --------------------------------------------------------------------------- #
def test_config() -> bool:
    cfg_path = settings.data_dir / "config.json"
    before = json.loads(cfg_path.read_text(encoding="utf-8"))
    settings.set("agent.max_iterations", 999)      # 越界 → 应收敛到 20
    settings.set("llm.temperature", 5.0)           # 越界 → 应收敛到 2.0
    settings.save()
    settings.load()
    ok = (
        cfg_path.exists()
        and settings.get("agent.max_iterations") == 20
        and settings.get("llm.temperature") == 2.0
        and isinstance(settings.agent.get("tools"), dict)
        and "send_item" in settings.agent["tools"]
    )
    settings.set("agent.max_iterations", before["agent"]["max_iterations"])
    settings.set("llm.temperature", before["llm"]["temperature"])
    settings.save()
    return check("配置读写 + 越界值自动收敛", ok,
                 f"max_iterations={settings.get('agent.max_iterations')} "
                 f"temperature={settings.get('llm.temperature')} "
                 f"工具数={len(settings.agent['tools'])}")


def test_tools() -> bool:
    from agent import tools as T

    tmp = settings.data_dir / "selftest_ws"
    tmp.mkdir(parents=True, exist_ok=True)
    old_ws = settings.get("agent.workspace_dir")
    settings.set("agent.workspace_dir", str(tmp))
    try:
        r1 = T.execute("write_file", {"path": "notes/a.txt", "content": "第一行\n第二行\n第三行\n"})
        r2 = T.execute("read_file", {"path": "notes/a.txt"})
        r3 = T.execute("edit_file", {"path": "notes/a.txt", "old_string": "第二行",
                                     "new_string": "改过的行"})
        r4 = T.execute("read_file", {"path": "notes/a.txt"})
        r5 = T.execute("edit_file", {"path": "notes/a.txt", "old_string": "不存在的文本", "new_string": "x"})
        r6 = T.execute("grep", {"pattern": "改过的", "path": "."})
        # 路径越界必须被拒绝（读系统文件 / 写到 C 盘）
        r7 = T.execute("read_file", {"path": "../../../Windows/System32/drivers/etc/hosts"})
        r8 = T.execute("write_file", {"path": "C:/Windows/Temp/hacked.txt", "content": "x"})
        r9 = T.execute("read_skill", {"name": "常见问题话术"})
        r10 = T.execute("read_skill", {"name": "不存在的技能"})

        # 第二种越界：绝对路径指向项目外
        r11 = T.execute("read_file", {"path": str(ROOT / "config.py")})

        ok = (
            "新建成功" in r1
            and "第一行" in r2 and "共 3 行" in r2
            and "修改成功" in r3 and "改过的行" in r4
            and "没有找到" in r5
            and "改过的" in r6
            and "越界" in r7 and "越界" in r8 and "越界" in r11
            and "常见问题话术" in r9 and "问价格" in r9
            and "没有名为" in r10
        )
        return check("工具层（读写改/grep/技能 + 越界拦截）", ok,
                     f"越界读={'拦截' if '越界' in r7 else '未拦截!'} "
                     f"越界写={'拦截' if '越界' in r8 else '未拦截!'} "
                     f"项目外读取={'拦截' if '越界' in r11 else '未拦截!'}")
    finally:
        settings.set("agent.workspace_dir", old_ws)
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_send_tools() -> bool:
    """send_file / send_item 必须把「当前会话」和参数正确传给发送桥。"""
    from agent import tools as T
    from agent.sender import sender

    calls: list = []

    class FakeLive:
        async def send_file_to(self, chat_id, to_id, path):
            calls.append(("file", chat_id, to_id, Path(path).name))
            return True

        async def send_item_to(self, chat_id, to_id, item_id, note=""):
            calls.append(("item", chat_id, to_id, item_id, note))
            return True

    tmp = settings.data_dir / "selftest_send"
    tmp.mkdir(parents=True, exist_ok=True)
    old_ws = settings.get("agent.workspace_dir")
    settings.set("agent.workspace_dir", str(tmp))

    import asyncio

    # 需要真实事件循环，才能跑 run_coroutine_threadsafe
    async def scenario():
        loop = asyncio.get_running_loop()
        sender.bind_loop(loop, FakeLive())
        sender.bind_session("chatX", "buyerX", "itemX")
        try:
            # 造一张真图片
            from PIL import Image

            Image.new("RGB", (64, 64), (10, 200, 120)).save(tmp / "pic.png")

            r_file = await asyncio.to_thread(T.execute, "send_file", {"path": "pic.png"})
            r_item = await asyncio.to_thread(T.execute, "send_item", {"note": "拍下马上安排～"})
            r_item_explicit = await asyncio.to_thread(
                T.execute, "send_item", {"item_id": "999888"})
            # 非图片文件应被拒绝
            (tmp / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
            r_pdf = await asyncio.to_thread(T.execute, "send_file", {"path": "doc.pdf"})
            # 不存在
            r_missing = await asyncio.to_thread(T.execute, "send_file", {"path": "nope.png"})
            return r_file, r_item, r_item_explicit, r_pdf, r_missing
        finally:
            sender.clear_session()
            sender.unbind()

    try:
        r_file, r_item, r_item_explicit, r_pdf, r_missing = asyncio.run(scenario())
    finally:
        settings.set("agent.workspace_dir", old_ws)
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    ok = (
        "已把 pic.png 发给买家" in r_file
        and ("item", "chatX", "buyerX", "itemX", "拍下马上安排～") in calls       # 默认用当前宝贝
        and ("item", "chatX", "buyerX", "999888", "") in calls                    # 显式传 ID
        and "只支持发送图片" in r_pdf
        and "文件不存在" in r_missing
    )
    return check("发送工具（发图片 / 发宝贝 / 非法类型拦截）", ok,
                 f"调用轨迹={[c[0] for c in calls]}；"
                 f"默认宝贝={'✓' if any(c[0]=='item' and c[3]=='itemX' for c in calls) else '✗'}；"
                 f"非图片={'拦截' if '只支持发送图片' in r_pdf else '未拦截!'}")


def test_agent_messages() -> bool:
    from agent import Agent

    agent = Agent()
    history = [{"role": "user", "content": "这个多少钱"},
               {"role": "assistant", "content": "9块9～"}]
    msgs = agent.build_messages(
        "能便宜点吗", history=history, item_desc='{"标题":"测试商品"}',
        images=["http://x/1.jpg"], image_notes=["inbox/1.jpg"],
    )
    system = msgs[0]["content"]
    from config import DEFAULT_SYSTEM_PROMPT

    ok = (
        msgs[0]["role"] == "system"
        and system.startswith(settings.system_prompt[:60])   # 用的就是配置里的提示词
        and "测试商品" in system          # 商品信息注入
        and "inbox/1.jpg" in system       # 图片路径注入
        and "send_item" in DEFAULT_SYSTEM_PROMPT   # 内置默认提示词里介绍了新工具
        and len(msgs) == 4
        and msgs[-1]["content"] == "能便宜点吗"
        and msgs[1]["content"] == "这个多少钱"
    )
    return check("Agent 消息组装（提示词+历史+商品+图片）", ok,
                 f"{len(msgs)} 条消息，system {len(system)} 字符")


def test_parse_and_send() -> bool:
    from utils.xianyu_message_parser import parse_chat_message
    from utils.xianyu_send import build_image_message, build_text_message, split_text

    inner = json.dumps({"contentType": 1, "text": {"text": "你好在吗"}}, ensure_ascii=False)
    parsed = parse_chat_message({
        "1": {"2": "123@goofish", "5": str(int(time.time() * 1000)),
              "6": {"3": {"5": inner}},
              "10": {"reminderTitle": "买家", "reminderContent": "你好在吗",
                     "reminderUrl": "https://www.goofish.com/item?itemId=888",
                     "senderUserId": "220011"}},
    })
    ok_parse = parsed.get("text") == "你好在吗" and parsed.get("item_id") == "888"

    img_inner = {"contentType": 2, "image": {"pics": [{"url": "https://img.alicdn.com/a.jpg"}]}}
    parsed2 = parse_chat_message({
        "1": {"2": "123@goofish",
              "6": {"3": {"5": json.dumps(img_inner, ensure_ascii=False)}},
              "10": {"reminderContent": "[图片]", "senderUserId": "220011"}},
    })
    ok_img = parsed2.get("images") == ["https://img.alicdn.com/a.jpg"]

    frame = build_text_message("c1", "b1", "s1", "你好呀")
    payload = json.loads(base64.b64decode(frame["body"][0]["content"]["custom"]["data"]).decode())
    ok_frame = (
        frame["lwp"] == "/r/MessageSend/sendByReceiverScope"
        and frame["body"][0]["cid"] == "c1@goofish"
        and frame["body"][1]["actualReceivers"] == ["b1@goofish", "s1@goofish"]
        and payload == {"contentType": 1, "text": {"text": "你好呀"}}
    )

    iframe = build_image_message("c1", "b1", "s1", "https://cdn/x.png", 1080, 1920)
    ipayload = json.loads(base64.b64decode(iframe["body"][0]["content"]["custom"]["data"]).decode())
    ok_iframe = (
        ipayload["contentType"] == 2
        and ipayload["image"]["pics"][0]["url"] == "https://cdn/x.png"
        and ipayload["image"]["pics"][0]["width"] == 1080
        and ipayload["image"]["pics"][0]["height"] == 1920
    )
    ok_split = split_text("a" * 1000, 400) == ["a" * 400, "a" * 400, "a" * 200]

    return check("闲鱼报文解析 + 文本/图片发送帧", ok_parse and ok_img and ok_frame and ok_iframe and ok_split,
                 f"文本={ok_parse} 收图={ok_img} 文本帧={ok_frame} 图片帧={ok_iframe} 分片={ok_split}")


def test_notice_filter() -> bool:
    from main import XianyuLive

    live = XianyuLive.__new__(XianyuLive)
    notices = [
        "恭喜新手卖家，您的宝贝有人来询单啦！也提醒您闲鱼客服不会以聊天的方式要求您缴纳保证金",
        "请勿脱离平台交易，谨防诈骗",
    ]
    normal = ["帮我看看这个", "能便宜点吗", "我说的是举报按钮在哪"]
    ok = all(live._is_platform_notice(n) for n in notices) and not any(
        live._is_platform_notice(n) for n in normal
    )
    return check("闲鱼官方提示过滤", ok,
                 f"官方提示全命中={all(live._is_platform_notice(n) for n in notices)}，"
                 f"误伤={sum(1 for n in normal if live._is_platform_notice(n))}")


def test_gui_api() -> bool:
    from fastapi.testclient import TestClient

    from gui.server import create_app
    from main import runtime

    client = TestClient(create_app(runtime))

    r_index = client.get("/")
    r_status = client.get("/api/status")
    r_config = client.get("/api/config")
    r_prompt = client.get("/api/prompt")
    r_tools = client.get("/api/tools")
    r_logs = client.get("/api/logs?lines=10")
    r_skills = client.get("/api/skills")
    r_skill = client.get("/api/skills/常见问题话术")

    ok_index = r_index.status_code == 200 and "闲鱼" in r_index.text
    ok_status = r_status.status_code == 200 and "running" in r_status.json()
    ok_config = r_config.status_code == 200 and "config" in r_config.json()
    ok_prompt = r_prompt.status_code == 200 and "system_prompt" in r_prompt.json()
    ok_tools = r_tools.status_code == 200 and len(r_tools.json().get("tools", [])) == 7
    ok_logs = r_logs.status_code == 200 and isinstance(r_logs.json().get("lines"), list)
    ok_skills = r_skills.status_code == 200 and any(
        s["name"] == "常见问题话术" for s in r_skills.json().get("skills", [])
    )
    ok_skill = r_skill.status_code == 200 and "问价格" in r_skill.json().get("content", "")

    # 技能 增 → 读 → 列表(含描述) → 删
    put = client.put("/api/skills/selftest_skill",
                     json={"content": "<!-- desc: 自检用 -->\n# 自检\n内容", "description": "自检用"})
    got = client.get("/api/skills/selftest_skill")
    ls = client.get("/api/skills").json()["skills"]
    listed = any(s["name"] == "selftest_skill" and s["description"] == "自检用" for s in ls)
    dele = client.delete("/api/skills/selftest_skill")
    gone = client.get("/api/skills/selftest_skill").status_code == 404
    ok_skill_rw = (put.status_code == 200 and "内容" in got.json()["content"]
                   and listed and dele.status_code == 200 and gone)

    # 配置往返：提示词不能被前端表单弄丢
    cfg = client.get("/api/config").json()["config"]
    original_prompt = cfg["agent"]["system_prompt"]      # 先存好原值，别回头把自己改了
    original_iters = cfg["agent"]["max_iterations"]
    cfg["agent"]["system_prompt"] = "自检提示词"
    cfg["agent"]["max_iterations"] = 3
    put_cfg = client.put("/api/config", json={"config": cfg})
    back = client.get("/api/config").json()["config"]
    ok_cfg_rw = (put_cfg.status_code == 200
                 and back["agent"]["system_prompt"] == "自检提示词"
                 and back["agent"]["max_iterations"] == 3)
    # 还原（用刚才存的原值，不是被改过的 cfg）
    back["agent"]["system_prompt"] = original_prompt
    back["agent"]["max_iterations"] = original_iters
    client.put("/api/config", json={"config": back})
    settings.load()
    restored = client.get("/api/config").json()["config"]["agent"]["system_prompt"]
    ok_cfg_restore = restored == original_prompt

    # 非法技能名必须被拒（路径穿越防护）
    bad = client.put("/api/skills/..%2F..%2Fevil", json={"content": "x"})
    ok_guard = bad.status_code in (400, 404)

    ok = all([ok_index, ok_status, ok_config, ok_prompt, ok_tools, ok_logs,
              ok_skills, ok_skill, ok_skill_rw, ok_cfg_rw, ok_cfg_restore, ok_guard])
    return check("管理后台 REST 接口冒烟", ok,
                 f"首页={ok_index} 状态={ok_status} 配置读={ok_config} 配置写={ok_cfg_rw} "
                 f"配置还原={ok_cfg_restore} 提示词={ok_prompt} 工具={ok_tools} 日志={ok_logs} "
                 f"技能={ok_skills} 增删改={ok_skill_rw} 路径防护={ok_guard}")


# --------------------------------------------------------------------------- #
def test_real_agent_tool_use() -> bool:
    """真实大模型工具调用闭环：必须查技能库才能答对。"""
    from agent import Agent

    if not settings.llm.get("api_key"):
        return check("真实模型工具调用闭环", False, "未配置 API Key")
    question = ("请先用 read_skill 查阅技能库里关于「退货」的技能，"
                "然后严格按技能内容回答：引导买家在订单页点哪个按钮？只回答按钮名。")
    try:
        run = Agent().run(question, history=[], item_desc="")
    except Exception as exc:  # noqa: BLE001
        return check("真实模型工具调用闭环", False, str(exc)[:200])
    used = any(t["name"] == "read_skill" for t in run.tool_calls)
    return check("真实模型工具调用闭环（查技能库后作答）", used and bool(run.reply),
                 f"工具={[t['name'] for t in run.tool_calls]}，{run.iterations} 轮，"
                 f"{run.elapsed:.1f}s → 「{run.reply[:60]}」")


def test_real_agent_plain() -> bool:
    from agent import Agent

    if not settings.llm.get("api_key"):
        return check("真实模型普通对话", False, "未配置 API Key")
    try:
        run = Agent().run("买家问：在吗？请回一句简短的客服招呼语。", [], "")
        ok = bool(run.reply) and len(run.reply) < 200
        return check("真实模型普通对话", ok, f"{run.elapsed:.1f}s → 「{run.reply[:50]}」")
    except Exception as exc:  # noqa: BLE001
        return check("真实模型普通对话", False, str(exc)[:200])


def test_cookie() -> bool:
    from cookie_login import check_cookie

    if not settings.cookies_str:
        return check("闲鱼 Cookie", True, "未配置（可在管理后台点扫码登录）")
    ok = check_cookie(settings.cookies_str)
    return check("闲鱼 Cookie 有效性", ok, "有效" if ok else "已失效，请重新扫码")


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="不调用外部 API")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level: <7}</level> | {message}")

    print("=" * 72)
    print(f"闲鱼 Agent 服务框架 自检  ({'离线模式' if args.offline else '全量模式'})")
    print("=" * 72)

    test_config()
    test_tools()
    test_send_tools()
    test_agent_messages()
    test_parse_and_send()
    test_notice_filter()
    test_gui_api()

    if not args.offline:
        test_real_agent_plain()
        test_real_agent_tool_use()
        test_cookie()

    print("-" * 72)
    failed = [n for n, ok, _ in _results if not ok]
    print(f"共 {len(_results)} 项，通过 {len(_results) - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
