"""Small Markdown template renderer: values, filters and for/endfor; no eval."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import weread_highlights as weread


class TemplateError(ValueError):
    pass


PATH = r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*"
TOKENS = re.compile(r"^[ \t]*({%(?:(?!%})[^\n])*%})[ \t]*(?:\n|$)|({{.*?}}|{%.*?%})", re.MULTILINE | re.DOTALL)


@dataclass
class Token:
    text: str
    line: int
    tag: bool = False


@dataclass
class Output:
    path: str
    filters: list[str]
    line: int


@dataclass
class Loop:
    alias: str
    path: str
    body: list
    line: int


def tokenize(source: str) -> list[Token]:
    tokens, previous = [], 0
    for match in TOKENS.finditer(source):
        literal = source[previous:match.start()]
        if "{{" in literal or "{%" in literal:
            raise TemplateError(f"第 {source.count(chr(10), 0, previous) + 1} 行附近：模板标签未闭合。")
        if literal:
            tokens.append(Token(literal, source.count("\n", 0, previous) + 1))
        tokens.append(Token(match.group(1) or match.group(2), source.count("\n", 0, match.start()) + 1, True))
        previous = match.end()
    literal = source[previous:]
    if "{{" in literal or "{%" in literal:
        raise TemplateError(f"第 {source.count(chr(10), 0, previous) + 1} 行附近：模板标签未闭合。")
    if literal:
        tokens.append(Token(literal, source.count("\n", 0, previous) + 1))
    return tokens


class Template:
    """A deliberately limited syntax, not a complete Jinja implementation.

    Values are dictionary fields, not Python attributes or expressions.
    Inserted content is never parsed again as a template.
    """

    def __init__(self, source: str):
        self.tokens = tokenize(source.replace("\r\n", "\n"))
        self.nodes, _ = self._parse(0)

    @classmethod
    def from_file(cls, path: Path) -> "Template":
        try:
            return cls(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError):
            raise TemplateError(f"无法读取 UTF-8 模板：{path}") from None
        except TemplateError as exc:
            raise TemplateError(f"模板 {path}：{exc}") from None

    def _parse(self, start: int, depth: int = 0, opening_line: int = 0) -> tuple[list, int]:
        if depth > 20:
            raise TemplateError("循环嵌套超过 20 层。")
        nodes = []
        index = start
        while index < len(self.tokens):
            token = self.tokens[index]
            index += 1
            if not token.tag:
                nodes.append(token.text)
                continue
            if token.text.startswith("{{"):
                parts = [part.strip() for part in token.text[2:-2].split("|")]
                if not re.fullmatch(PATH, parts[0]):
                    raise TemplateError(f"第 {token.line} 行：变量只支持名称或点号字段路径。")
                if any(part not in ("markdown", "blockquote", "json") for part in parts[1:]):
                    raise TemplateError(f"第 {token.line} 行：支持的过滤器为 markdown、blockquote、json。")
                nodes.append(Output(parts[0], parts[1:], token.line))
                continue
            tag = token.text[2:-2].strip()
            if tag == "endfor":
                if not depth:
                    raise TemplateError(f"第 {token.line} 行：endfor 没有对应的 for。")
                return nodes, index
            match = re.fullmatch(rf"for\s+([A-Za-z][A-Za-z0-9_]*)\s+in\s+({PATH})", tag)
            if not match or match[1] == "loop":
                raise TemplateError(f"第 {token.line} 行：使用 for 变量 in 列表 / endfor，循环变量不能命名为 loop。")
            children, index = self._parse(index, depth + 1, token.line)
            nodes.append(Loop(match[1], match[2], children, token.line))
        if depth:
            raise TemplateError(f"第 {opening_line} 行：for 缺少 endfor。")
        return nodes, index

    def _lookup(self, path: str, context: dict, line: int):
        value = context
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                raise TemplateError(f"第 {line} 行：未知变量或字段 {path}。")
            value = value[part]
        return value

    def _render(self, nodes: list, context: dict) -> str:
        chunks = []
        for node in nodes:
            if isinstance(node, str):
                chunks.append(node)
            elif isinstance(node, Output):
                value = self._lookup(node.path, context, node.line)
                for name in node.filters:
                    if name == "json":
                        value = json.dumps(value, ensure_ascii=False)
                    elif isinstance(value, (dict, list, tuple)):
                        raise TemplateError(f"第 {node.line} 行：{node.path} 是集合，请用循环或 json 过滤器。")
                    elif name == "markdown":
                        value = weread.escape("" if value is None else str(value))
                    else:
                        value = "\n".join(weread.quote_lines("" if value is None else str(value)))
                if isinstance(value, (dict, list, tuple)):
                    raise TemplateError(f"第 {node.line} 行：{node.path} 是集合，请用循环或 json 过滤器。")
                chunks.append("" if value is None else str(value))
            else:
                items = self._lookup(node.path, context, node.line)
                if not isinstance(items, (list, tuple)):
                    raise TemplateError(f"第 {node.line} 行：for 需要列表，{node.path} 不是列表。")
                for index, item in enumerate(items):
                    scope = {**context, node.alias: item,
                             "loop": {"index": index + 1, "index0": index, "first": index == 0,
                                      "last": index == len(items) - 1, "length": len(items)}}
                    chunks.append(self._render(node.body, scope))
        return "".join(chunks)

    def render(self, context: dict) -> str:
        return self._render(self.nodes, context)
