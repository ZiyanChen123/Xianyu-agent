# 闲鱼 Agent 服务框架

把闲鱼店铺交给一个**带工具的 AI 客服**：自动回复买家咨询，能查资料、读写文件、检索技能库，
还能**直接给买家发图片、发宝贝引导下单**。
带一个 **Web 管理后台**，配置、系统提示词、技能库、日志、会话全部可视化。

---

| 能力 | 说明 |
| --- | --- |
| **自动客服** | 买家咨询自动回复，支持多轮上下文，同一会话串行处理互不插队 |
| **7 个默认工具** | 文件读 / 写 / 改 / grep / 读技能 / **给客户发图片** / **给客户发宝贝** |
| **真正的工具调用** | 走 OpenAI function calling，模型自己决定查技能还是直接答 |
| **技能库** | 把退货流程、话术、业务规则写成 `.md`，Agent 按需读取 |
| **系统提示词可改** | 管理后台直接编辑，改完下一条消息即生效 |
| **Web 管理后台** | 状态、启停、配置、提示词、技能、日志、会话，全在网页里 |
| **后台扫码登录** | 点一下弹二维码，手机闲鱼扫一扫，Cookie 自动写入 |
| **接收买家图片** | 买家发的图片自动存进 `workspace/inbox/`，Agent 可读取处理 |
| **路径沙箱** | 所有文件操作限制在 `workspace/` 与 `skills/`，越界直接拒绝 |
| **断线自愈** | 心跳、Token 定时刷新、指数退避重连、Cookie 失效自动提示重登 |

---

## 快速开始

```bat
双击  启动.bat
```

首次启动会自动建虚拟环境、装依赖，然后：
1. 管理后台自动打开：**http://127.0.0.1:8787**
2. 在 **基础配置** 页填大模型 API Key
3. 点 **扫码登录**，用闲鱼 App 扫码
4. 回到顶栏点 **启动**

其他脚本：

| 文件 | 作用 |
| --- | --- |
| `启动.bat` | 启动服务（GUI + 机器人），会自动打开浏览器 |
| `扫码登录.bat` | 终端里扫码登录 / 换 Cookie |
| `体检.bat` | Cookie 体检 + 打印最新日志（**登录出问题先跑这个**） |
| `安装开机自启.bat` / `取消开机自启.bat` | 开机自动启动 |

命令行也支持：

```bash
python main.py                 # GUI + 机器人（默认）
python main.py --no-gui        # 只跑机器人
python main.py --port 9000     # 换后台端口
python cookie_login.py --check # Cookie 体检
```

---

## 内置工具

| 工具 | 作用 |
| --- | --- |
| `read_file` | 读 workspace / skills 里的文件（带行号），也可列目录 |
| `write_file` | 写文件（覆盖或新建），只能写 workspace |
| `edit_file` | 精确字符串替换，多处匹配会报错并要求更精确的片段 |
| `grep` | 正则搜索文件内容，返回 `文件:行号: 内容` |
| `read_skill` | 读取技能库；不传名字则列出全部技能 |
| `send_file` | **把图片发给买家**（自动上传闲鱼 CDN → 作为图片消息发出） |
| `send_item` | **把宝贝发给买家**，点开即可下单，引导成交最有效 |

每个工具都能在后台 **基础配置 → Agent** 里单独开关。

### 文件沙箱

文件操作被限制在两个根目录内，**越界一律拒绝**：

```
workspace/          Agent 的读写空间
  inbox/            买家发来的图片会自动存这里
skills/             技能库（Agent 只读，编辑走后台或直接放文件）
```

---

## 技能库怎么用

在 `skills/` 放 `.md` 文件，或在后台 **技能库** 页新建。文件名就是技能名。

```markdown
<!-- desc: 买家要求退货时的标准处理流程 -->
# 退货处理

1. 先安抚：「可以的，我帮你走一下退货流程～」
2. 问清原因
3. 引导买家在订单页点「申请退款」
4. 不要承诺「马上退」，统一回复「我提交给店主确认，稍后处理」
```

`<!-- desc: ... -->` 是可选的描述（后台会自动读写），Agent 靠它判断该不该读这个技能。

然后在 **系统提示词** 里告诉它什么时候去查：

> 遇到退货、物流、纠纷类问题，先用 `read_skill` 查对应技能再回答。

---

## 配置

**唯一事实来源是 `data/config.json`**，管理后台改的就是它。
首次运行会自动从旧的 `.env` 迁移已有密钥。

字段分组：`xianyu`（闲鱼账号）/ `llm`（大模型）/ `vision`（识图，可选）/
`agent`（提示词、工具、轮次）/ `gui`（后台地址端口）。

---

## 项目结构

```
main.py                  入口：uvicorn 后台 + 闲鱼长连接（同一进程）
config.py                配置中心（data/config.json）
cookie_login.py          扫码登录 / Cookie 体检
selftest.py              自检（--offline 不花钱）

agent/
  core.py                Agent 循环：模型 ⇄ 工具调用，直到给出回复
  tools.py               7 个工具的注册表与实现（含路径沙箱）
  sender.py              工具 ↔ 闲鱼连接之间的桥（跨线程发消息）

gui/
  server.py              管理后台 REST 接口（FastAPI）
  static/index.html      单页前端（无任何外部依赖）

utils/
  qr_login.py            纯 HTTP 扫码登录
  build_cookies.py       登录前环境 Cookie 链
  cookie_store.py        完整 Cookie Jar 存取（防跨域同名串值）
  xianyu_message_parser.py  入站报文解析（文本 + 图片）
  xianyu_send.py         文本 / 图片 / 创建会话 的帧构造
  xianyu_upload.py       图片上传到闲鱼 CDN
  xianyu_utils.py        签名、设备 ID、解密

workspace/               Agent 文件沙箱
skills/                  技能库
data/                    运行时数据（config.json / cookies.json），不入库
```
---

## 致谢

闲鱼 WebSocket 协议、扫码登录、图片上传协议参考自：

- [shaxiu/XianyuAutoAgent](https://github.com/shaxiu/XianyuAutoAgent)
- [GuDong2003/xianyu-auto-reply-fix](https://github.com/GuDong2003/xianyu-auto-reply-fix)（AGPL-3.0）
- [zhinianboke/xianyu-auto-reply](https://github.com/zhinianboke/xianyu-auto-reply)（AGPL-3.0）
