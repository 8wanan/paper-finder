"""极简 YAML 子集解析器 —— 零第三方依赖。

为什么自己写：部署环境常常没有 PyYAML（本机实测就没有），而沙箱不允许往
解释器目录里装包。与其让工具在别人的机器上起不来，不如支持一个**受控的
YAML 子集**，并用测试保证它对我们自己的配置文件永远解析正确。

支持的语法（恰好是 profiles/*.yaml 与 tests/golden*.yaml 用到的部分）：
  - 注释（# ...）与空行
  - 缩进嵌套的映射（mapping）
  - 块序列（- item），含「行内映射」写法 `- key: value`
  - 块标量（| 与 >，含 |- 与 >- 去尾换行）
  - 单引号 / 双引号 / 裸标量，值尾行内注释
  - null / true / false / int / float 自动类型转换

不支持（也不需要）：锚点、别名、流式 JSON 语法、多文档、复杂键、标签。

用法：
    from papers.yamlish import load
    data = load("profiles/llm-speech-security.yaml")
"""

from __future__ import annotations

import re

__all__ = ["load", "loads", "YamlSubsetError"]


class YamlSubsetError(ValueError):
    """配置文件用了本解析器不支持的 YAML 语法。"""


_NULLS = {"null", "~", "none", ""}
_TRUE = {"true", "yes", "on"}
_FALSE = {"false", "no", "off"}


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_comment(s: str) -> str:
    """去掉值尾部的行内注释，但不动引号内部的内容。"""
    out = []
    quote = None
    for i, ch in enumerate(s):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _find_key_sep(s: str) -> int:
    """返回映射键后那个冒号的下标；不是映射则返回 -1。

    只认「冒号后跟空格或行尾」的那个冒号，并且跳过引号内部，
    这样 `title: "StyleBreak: Revealing ..."` 不会把标题里的冒号当分隔符。
    """
    quote = None
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == ":":
            if i + 1 >= len(s) or s[i + 1] in " \t":
                return i
    return -1


def _scalar(v: str):
    v = v.strip()
    if v == "":
        return None
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    low = v.lower()
    if low in _NULLS:
        return None
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    if re.fullmatch(r"[+-]?\d+", v):
        return int(v)
    if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?", v) and (
        "." in v or "e" in low
    ):
        try:
            return float(v)
        except ValueError:
            return v
    return v


class _Parser:
    def __init__(self, text: str):
        # 保留原始行：块标量的内容不能再被当成注释剥掉。
        self.lines = text.splitlines()
        self.pos = 0

    # ── 游标 ────────────────────────────────────────────────
    def _peek(self):
        """跳过空行与整行注释，返回下一个有效行。

        **必须永久前进游标**（而不是只读预读）。早期版本只做只读预读，
        于是 `self.pos` 与真正被返回的行错位：调用方 `self.pos += 1` 或
        `self.lines[self.pos] = ...` 都会作用到错误的行上。触发条件是
        "序列项内部夹了注释行，后面跟着下一个序列项"，此时同一行会被解析
        多次 —— 实测表现为金标集里 1 个条目被读成 5 个。

        空行与整行注释在语义上永远是噪声，所以直接消费掉是安全的。
        注意块标量（_block_scalar）不走这里：它的内容可以以 # 开头。
        """
        while self.pos < len(self.lines):
            raw = self.lines[self.pos]
            if raw.strip() == "" or raw.lstrip().startswith("#"):
                self.pos += 1
                continue
            return raw
        return None

    def _skip_blanks(self):
        self._peek()

    # ── 入口 ────────────────────────────────────────────────
    def parse(self):
        self._skip_blanks()
        line = self._peek()
        if line is None:
            return None
        return self.parse_block(_indent_of(line))

    def parse_block(self, indent: int):
        line = self._peek()
        if line is None:
            return None
        if _indent_of(line) < indent:
            return None
        content = line.strip()
        if content == "-" or content.startswith("- "):
            return self.parse_sequence(indent)
        return self.parse_mapping(indent)

    # ── 映射 ────────────────────────────────────────────────
    def parse_mapping(self, indent: int) -> dict:
        result: dict = {}
        while True:
            line = self._peek()
            if line is None:
                break
            ind = _indent_of(line)
            if ind < indent:
                break
            if ind > indent:
                raise YamlSubsetError(
                    f"第 {self._line_no()} 行缩进异常（期望 {indent}，实际 {ind}）：{line!r}"
                )
            content = line.strip()
            if content == "-" or content.startswith("- "):
                break
            sep = _find_key_sep(content)
            if sep < 0:
                raise YamlSubsetError(f"第 {self._line_no()} 行不是合法的 key: value：{line!r}")
            key = content[:sep].strip()
            if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
                key = key[1:-1]
            rest = _strip_comment(content[sep + 1:]).strip()
            self.pos += 1

            if rest in ("|", "|-", "|+", ">", ">-", ">+"):
                result[key] = self._block_scalar(ind, rest)
            elif rest == "":
                nxt = self._peek()
                if nxt is not None and _indent_of(nxt) > ind:
                    result[key] = self.parse_block(_indent_of(nxt))
                elif nxt is not None and _indent_of(nxt) == ind and nxt.strip().startswith("- "):
                    # 序列与键同缩进（YAML 允许）：key:\n- a\n- b
                    result[key] = self.parse_sequence(ind)
                else:
                    result[key] = None
            else:
                result[key] = _scalar(rest)
        return result

    # ── 序列 ────────────────────────────────────────────────
    def parse_sequence(self, indent: int) -> list:
        result: list = []
        while True:
            line = self._peek()
            if line is None:
                break
            ind = _indent_of(line)
            if ind != indent:
                break
            content = line.strip()
            if not (content == "-" or content.startswith("- ")):
                break

            if content == "-":
                self.pos += 1
                nxt = self._peek()
                if nxt is not None and _indent_of(nxt) > ind:
                    result.append(self.parse_block(_indent_of(nxt)))
                else:
                    result.append(None)
                continue

            item = content[1:].lstrip()
            item_indent = ind + (len(content) - len(item))
            if _find_key_sep(item) >= 0:
                # 行内映射：把当前行改写为「缩进后的 item」，复用 mapping 解析逻辑，
                # 后续同缩进的行会被 parse_mapping 自然吃掉。
                self.lines[self.pos] = " " * item_indent + item
                result.append(self.parse_mapping(item_indent))
            else:
                self.pos += 1
                result.append(_scalar(_strip_comment(item)))
        return result

    # ── 块标量 ──────────────────────────────────────────────
    def _block_scalar(self, parent_indent: int, marker: str):
        collected = []
        while self.pos < len(self.lines):
            raw = self.lines[self.pos]
            if raw.strip() == "":
                collected.append("")
                self.pos += 1
                continue
            if _indent_of(raw) <= parent_indent:
                break
            collected.append(raw)
            self.pos += 1

        while collected and collected[-1] == "":
            collected.pop()

        if not collected:
            return ""

        base = min(_indent_of(l) for l in collected if l.strip())
        stripped = [(l[base:] if l.strip() else "") for l in collected]

        if marker.startswith(">"):
            text = " ".join(s.strip() for s in stripped)
            text = re.sub(r"\s{2,}", " ", text).strip()
        else:
            text = "\n".join(stripped)

        if marker.endswith("-"):
            return text.rstrip("\n")
        return text + "\n"

    def _line_no(self) -> int:
        return self.pos + 1


def loads(text: str):
    """把 YAML 子集文本解析成 dict/list。"""
    return _Parser(text).parse()


def load(path):
    """读取并解析一个 YAML 子集文件。"""
    with open(path, "r", encoding="utf-8") as fh:
        return loads(fh.read())
