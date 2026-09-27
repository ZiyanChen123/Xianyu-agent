"""Agent 工具箱：文件读写改、grep、读取技能。

安全边界：所有文件操作都被限制在**两个允许的根目录**内 ——
    workspace/   Agent 自己的读写空间
    skills/      技能库（只读）
越界的路径一律拒绝，避免模型误删系统文件。

每个工具返回**字符串**（直接回灌给模型的 tool 消息），统一做长度截断，
防止一次 grep 把上下文打爆。
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from config import settings

MAX_OUTPUT_CHARS = 8000
MAX_FILE_BYTES = 1_000_000
_SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", "data", "logs", ".idea", ".vscode"}
_BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip", ".rar",
    ".7z", ".tar", ".gz", ".exe", ".dll", ".so", ".pyc", ".woff", ".woff2", ".ttf",
    ".mp3", ".mp4", ".wav", ".db", ".sqlite", ".sqlite3",
}


class ToolError(Exception):
    """工具执行失败（信息会原样返回给模型，让它自己纠正）。"""


# --------------------------------------------------------------------------- #
# 路径安全
# --------------------------------------------------------------------------- #
def allowed_roots() -> List[Path]:
    roots = []
    for p in (settings.workspace_dir, settings.skills_dir):
        try:
            roots.append(Path(p).resolve())
        except Exception:
            continue
    return roots


def resolve_path(raw: str) -> Path:
    """把模型给的相对路径解析成安全绝对路径；越界抛 ToolError。"""
    if not raw or not str(raw).strip():
        raise ToolError("路径不能为空")
    raw = str(raw).strip().strip('"').strip("'")

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = settings.workspace_dir / candidate
    try:
        resolved = candidate.resolve()
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"路径无法解析: {raw}") from exc

    for root in allowed_roots():
        if resolved == root or root in resolved.parents:
            return resolved
    allow = "、".join(str(r) for r in allowed_roots())
    raise ToolError(f"路径越界，只允许操作: {allow}（收到: {raw}）")


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[已截断，原始长度 {len(text)} 字符]"


# --------------------------------------------------------------------------- #
# 工具实现
# --------------------------------------------------------------------------- #
def tool_read_file(path: str, offset: int = 1, limit: int = 500) -> str:
    p = resolve_path(path)
    if not p.exists():
        raise ToolError(f"文件不存在: {path}")
    if p.is_dir():
        entries = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())
        return f"目录 {path} 下有 {len(entries)} 项:\n" + "\n".join(entries[:200])
    if p.stat().st_size > MAX_FILE_BYTES:
        raise ToolError(f"文件过大（{p.stat().st_size} 字节），请用 grep 或分段读取")

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"读取失败: {exc}") from exc

    lines = text.splitlines()
    start = max(1, int(offset or 1)) - 1
    count = max(1, min(int(limit or 500), 2000))
    chunk = lines[start:start + count]
    numbered = "\n".join(f"{start + i + 1:>5} | {ln}" for i, ln in enumerate(chunk))
    header = f"{path}  第 {start + 1}-{start + len(chunk)} 行 / 共 {len(lines)} 行"
    return _truncate(f"{header}\n{numbered}")


def tool_write_file(path: str, content: str) -> str:
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    try:
        p.write_text(content if content is not None else "", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"写入失败: {exc}") from exc
    action = "覆盖" if existed else "新建"
    return f"{action}成功: {p.name}（{len(content or '')} 字符）"


def tool_edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    p = resolve_path(path)
    if not p.exists():
        raise ToolError(f"文件不存在: {path}")
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"读取失败: {exc}") from exc

    if not old_string:
        raise ToolError("old_string 不能为空")
    hits = text.count(old_string)
    if hits == 0:
        raise ToolError("没有找到 old_string，请先用 read_file 看清原文再改")
    if hits > 1 and not replace_all:
        raise ToolError(f"old_string 出现了 {hits} 次，请提供更长的唯一片段，或设置 replace_all=true")

    new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    try:
        p.write_text(new_text, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"写入失败: {exc}") from exc
    return f"修改成功: {p.name}（替换 {hits if replace_all else 1} 处）"


def tool_grep(
    pattern: str,
    path: str = ".",
    include: Optional[str] = None,
    max_results: int = 60,
) -> str:
    if not pattern:
        raise ToolError("pattern 不能为空")
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"正则表达式无效: {exc}") from exc

    root = resolve_path(path)
    if not root.exists():
        raise ToolError(f"路径不存在: {path}")
    max_results = max(1, min(int(max_results or 60), 300))

    files: List[Path] = []
    if root.is_file():
        files = [root]
    else:
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            # 注意：只检查**相对搜索根目录**的路径段。
            # 用绝对路径判断的话，一旦 workspace 恰好位于名为 data/logs 的目录下，
            # 整个目录都会被跳过（踩过这个坑）。
            try:
                rel_parts = f.relative_to(root).parts
            except ValueError:
                rel_parts = f.parts
            if any(part in _SKIP_DIRS for part in rel_parts[:-1]):
                continue
            if f.suffix.lower() in _BINARY_EXT:
                continue
            if include and not fnmatch.fnmatch(f.name, include):
                continue
            files.append(f)

    results: List[str] = []
    scanned = 0
    for f in files:
        if len(results) >= max_results:
            break
        try:
            if f.stat().st_size > MAX_FILE_BYTES:
                continue
            scanned += 1
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    try:
                        rel = f.relative_to(root)
                    except ValueError:
                        rel = f
                    results.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(results) >= max_results:
                        break
        except Exception:
            continue

    if not results:
        return f"没有匹配「{pattern}」（扫了 {scanned} 个文件）"
    head = f"匹配 {len(results)} 条（扫了 {scanned} 个文件）"
    return _truncate(head + "\n" + "\n".join(results))


def _skill_files() -> List[Path]:
    d = settings.skills_dir
    if not d.exists():
        return []
    return sorted(p for p in d.glob("*.md") if p.is_file())


def tool_list_skills() -> str:
    files = _skill_files()
    if not files:
        return "技能库是空的（skills/ 目录下没有 .md 文件）"
    lines = []
    for f in files:
        desc = _skill_description(f)
        lines.append(f"- {f.stem}: {desc}" if desc else f"- {f.stem}")
    return "可用技能:\n" + "\n".join(lines)


def tool_read_skill(name: str) -> str:
    if not name:
        return tool_list_skills()
    name = str(name).strip()
    if name.lower().endswith(".md"):
        name = name[:-3]
    # 允许中文技能名，但禁止路径穿越
    if "/" in name or "\\" in name or name in (".", ".."):
        raise ToolError("技能名不能包含路径分隔符")

    target = settings.skills_dir / f"{name}.md"
    if not target.exists():
        available = "、".join(f.stem for f in _skill_files()) or "（空）"
        raise ToolError(f"没有名为「{name}」的技能。可用技能: {available}")
    try:
        content = target.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"读取技能失败: {exc}") from exc
    return _truncate(f"# 技能：{name}\n\n{content}")


def tool_send_file(path: str) -> str:
    """把本地图片发给当前买家。"""
    from agent.sender import sender

    return sender.send_file(path)


def tool_send_item(item_id: str = "", note: str = "") -> str:
    """把闲鱼商品（宝贝）发给当前买家，引导下单。"""
    from agent.sender import sender

    return sender.send_item(item_id, note)


def _skill_description(path: Path) -> str:
    """取技能文件里第一段非标题文字作为描述。"""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                return s[:80]
    except Exception:
        pass
    return ""


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]
    func: Callable[..., str]
    # 带副作用的工具（写文件）在描述里要提醒模型谨慎
    mutating: bool = False

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


TOOLS: Dict[str, ToolSpec] = {
    "read_file": ToolSpec(
        name="read_file",
        description=(
            "读取 workspace 或 skills 目录下的文本文件，返回带行号的内容。"
            "也可以传目录路径来列出目录内容。修改文件前先用它看清原文。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件或目录路径（相对 workspace 或 skills）"},
                "offset": {"type": "integer", "description": "起始行号，默认 1"},
                "limit": {"type": "integer", "description": "最多读取多少行，默认 500"},
            },
            "required": ["path"],
        },
        func=tool_read_file,
    ),
    "write_file": ToolSpec(
        name="write_file",
        description=(
            "把内容写入文件（覆盖同名文件，不存在则新建，父目录会自动创建）。"
            "只允许写 workspace 目录。有内容的旧文件请优先用 edit_file 局部修改。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（相对 workspace）"},
                "content": {"type": "string", "description": "要写入的完整内容"},
            },
            "required": ["path", "content"],
        },
        func=tool_write_file,
        mutating=True,
    ),
    "edit_file": ToolSpec(
        name="edit_file",
        description=(
            "对已有文件做精确字符串替换。old_string 必须与原文完全一致（含缩进）。"
            "若 old_string 在文件里出现多次会报错，此时请提供更长的唯一片段或设置 replace_all=true。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "old_string": {"type": "string", "description": "要被替换的原文"},
                "new_string": {"type": "string", "description": "替换成的新内容"},
                "replace_all": {"type": "boolean", "description": "是否替换所有匹配项，默认 false"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        func=tool_edit_file,
        mutating=True,
    ),
    "grep": ToolSpec(
        name="grep",
        description="用正则表达式在 workspace 或 skills 里搜索文件内容，返回 文件:行号: 内容。",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "搜索范围，默认 workspace 根目录"},
                "include": {"type": "string", "description": "只搜匹配该通配符的文件名，如 *.py"},
                "max_results": {"type": "integer", "description": "最多返回多少条，默认 60"},
            },
            "required": ["pattern"],
        },
        func=tool_grep,
    ),
    "read_skill": ToolSpec(
        name="read_skill",
        description=(
            "读取技能库里某个技能的完整内容（技能是店主预先写好的处理流程/知识）。"
            "不传 name 时列出所有可用技能。遇到具体业务问题（退货、议价、发货等）时，"
            "先读对应技能再回答，不要凭感觉答。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名（不带 .md）。留空则列出全部技能"},
            },
            "required": [],
        },
        func=tool_read_skill,
    ),
    "send_file": ToolSpec(
        name="send_file",
        description=(
            "把 workspace 里的图片直接发给买家（会自动上传到闲鱼并作为图片消息发出）。"
            "只支持 jpg/png/webp/gif/bmp。适合发截图、示意图、单据照片等。"
            "先把内容写成图片文件再调用本工具。发出后买家立即可见，不要再用文字重复描述一遍。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "workspace 里的图片路径"},
            },
            "required": ["path"],
        },
        func=tool_send_file,
        mutating=True,
    ),
    "send_item": ToolSpec(
        name="send_item",
        description=(
            "把店里的闲鱼商品（宝贝）发给买家，买家点开就能直接下单，是引导成交最有效的方式。"
            "当买家表示想买、问怎么拍、犹豫要不要买、或你需要推销店主服务时使用。"
            "不传 item_id 时默认发送当前对话关联的那个宝贝。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "description": "要发送的宝贝 ID，留空则用当前对话的宝贝"},
                "note": {"type": "string", "description": "随宝贝一起说的一句话，例如「就是这个链接，拍下我马上安排～」"},
            },
            "required": [],
        },
        func=tool_send_item,
        mutating=True,
    ),
}


def enabled_tool_names() -> List[str]:
    flags = settings.agent.get("tools", {}) or {}
    return [name for name in TOOLS if flags.get(name, True)]


def build_schemas() -> List[Dict[str, Any]]:
    return [TOOLS[name].schema() for name in enabled_tool_names()]


def execute(name: str, arguments: Dict[str, Any]) -> str:
    """执行工具，永远返回字符串（错误也返回字符串，让模型自己纠正）。"""
    spec = TOOLS.get(name)
    if spec is None:
        return f"错误：没有名为 {name} 的工具"
    if name not in enabled_tool_names():
        return f"错误：工具 {name} 已被店主禁用"
    try:
        result = spec.func(**(arguments or {}))
        logger.debug(f"[tool] {name}({_brief(arguments)}) -> {len(str(result))} 字符")
        return str(result)
    except ToolError as exc:
        logger.info(f"[tool] {name} 拒绝/失败: {exc}")
        return f"错误：{exc}"
    except TypeError as exc:
        return f"错误：参数不匹配（{exc}）。请检查必填参数。"
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[tool] {name} 异常")
        return f"错误：执行 {name} 时发生异常（{exc}）"


def _brief(args: Any) -> str:
    try:
        text = ", ".join(f"{k}={str(v)[:40]!r}" for k, v in (args or {}).items())
        return text[:150]
    except Exception:
        return ""
