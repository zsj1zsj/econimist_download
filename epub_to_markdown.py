#!/usr/bin/env python3
"""Export an article selected by title from an EPUB file to Markdown.

Usage:
    python epub_to_markdown.py book.epub "Article title"
"""

from __future__ import annotations

import argparse
import hashlib
import html
import posixpath
import re
import sys
import unicodedata
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import quote, unquote, urlsplit


BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt",
    "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "li", "main", "ol", "p", "pre", "section", "table", "td", "th", "title", "tr", "ul",
}
SKIP_TAGS = {"script", "style", "svg", "canvas", "video", "audio", "nav", "footer"}
HEADING_TAGS = {f"h{i}" for i in range(1, 7)}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def clean_text(value: str) -> str:
    value = html.unescape(value).replace("\xa0", " ")
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_for_search(value: str) -> str:
    value = clean_text(value).casefold()
    return re.sub(r"[^\w]+", "", value, flags=re.UNICODE)


@dataclass
class Block:
    kind: str
    text: str = ""
    src: str = ""
    alt: str = ""
    markdown: str | None = None
    prefix: str = ""
    continuation: str = ""
    quote_depth: int = 0


@dataclass
class _Frame:
    tag: str
    opening: str = ""
    closing: str = ""
    counter: int = 1
    marker: str = ""
    emitted: bool = False
    inline_start: int = 0


def _integer(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _link_destination(value: str) -> str:
    try:
        scheme = urlsplit(value).scheme.lower()
    except ValueError:
        return ""
    if scheme not in ("", "http", "https", "mailto", "tel"):
        return ""
    return quote(value, safe="/:#?=&%+@~;,$-_.!")


class ArticleParser(HTMLParser):
    """Small, dependency-free HTML-to-block parser for typical EPUB XHTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self._text: list[str] = []
        self._markdown: list[str] = []
        self._tag_stack: list[_Frame] = []
        self._skip_depth = 0

    def _reopen_inline(self) -> None:
        self._markdown = []
        for frame in self._tag_stack:
            if frame.opening:
                frame.inline_start = len(self._markdown)
                self._markdown.append(frame.opening)

    def _close_inline(self, frames: Iterable[_Frame]) -> None:
        for frame in frames:
            if not frame.opening:
                continue
            content = "".join(self._markdown[frame.inline_start + 1:])
            value = content.strip()
            if value:
                leading = content[:len(content) - len(content.lstrip())]
                trailing = content[len(content.rstrip()):]
                replacement = leading + frame.opening + value + frame.closing + trailing
            else:
                replacement = content
            self._markdown[frame.inline_start:] = [replacement]

    def _append(self, block: Block) -> None:
        items = [frame for frame in self._tag_stack if frame.tag == "li"]
        if items:
            item = items[-1]
            indent = " " * sum(len(frame.marker) + 1 for frame in items[:-1])
            block.continuation = indent + " " * (len(item.marker) + 1)
            block.prefix = block.continuation if item.emitted else indent + item.marker + " "
            item.emitted = True
        block.quote_depth = sum(frame.tag == "blockquote" for frame in self._tag_stack)
        self.blocks.append(block)

    def _flush(self) -> None:
        text = clean_text("".join(self._text))
        if text:
            kind = next((frame.tag for frame in reversed(self._tag_stack)
                         if frame.tag in BLOCK_TAGS or frame.tag == "title"), "p")
            self._close_inline(reversed(self._tag_stack))
            markdown = re.sub(r"[^\S\n]+", " ", "".join(self._markdown))
            # Source whitespace is soft; only explicit <br> tags create hard breaks.
            markdown = "  \n".join(line.strip() for line in markdown.split("\n")).strip()
            self._append(Block(kind=kind, text=text, markdown=markdown))
        self._text.clear()
        self._reopen_inline()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = dict(attrs)
        if tag in SKIP_TAGS:
            self._flush()
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "br":
            self._text.append("\n")
            self._markdown.append("\n")
            return
        if tag == "hr":
            self._flush()
            self._append(Block(kind="hr", markdown="---"))
            return
        if tag == "img":
            self._flush()
            src = attrs_dict.get("src") or attrs_dict.get("data-src") or ""
            alt = attrs_dict.get("alt") or ""
            if src:
                self._append(Block(kind="img", src=src, alt=clean_text(alt)))
            return
        if tag in VOID_TAGS:
            return
        if tag in BLOCK_TAGS:
            self._flush()
        frame = _Frame(tag)
        if tag in ("strong", "b"):
            frame.opening = frame.closing = "**"
        elif tag in ("em", "i"):
            frame.opening = frame.closing = "*"
        elif tag in ("del", "s", "strike"):
            frame.opening = frame.closing = "~~"
        elif tag == "a":
            destination = _link_destination(attrs_dict.get("href") or "")
            if destination:
                frame.opening, frame.closing = "[", f"]({destination})"
        elif tag == "ol":
            frame.counter = _integer(attrs_dict.get("start"), 1)
        elif tag == "li":
            parent = next((p for p in reversed(self._tag_stack) if p.tag in ("ol", "ul")), None)
            frame.marker = "-"
            if parent is not None and parent.tag == "ol":
                number = _integer(attrs_dict.get("value"), parent.counter)
                frame.marker, parent.counter = f"{number}.", number + 1
        self._tag_stack.append(frame)
        frame.inline_start = len(self._markdown)
        self._markdown.append(frame.opening)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in VOID_TAGS:
            return
        if tag in BLOCK_TAGS:
            self._flush()
        if self._tag_stack:
            index = next((i for i in range(len(self._tag_stack) - 1, -1, -1)
                          if self._tag_stack[i].tag == tag), None)
            if index is not None:
                if tag not in BLOCK_TAGS:
                    self._close_inline(reversed(self._tag_stack[index:]))
                del self._tag_stack[index:]
                if tag in BLOCK_TAGS:
                    self._reopen_inline()

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            data = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", data.replace("\xa0", " ")))
            self._text.append(data)
            self._markdown.append(markdown_escape(data))

    def finish(self) -> list[Block]:
        self._flush()
        return self.blocks


def parse_html(data: bytes) -> list[Block]:
    parser = ArticleParser()
    parser.feed(data.decode("utf-8", errors="replace"))
    return parser.finish()


def html_path(value: str, current_file: str) -> str:
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return value
    path = posixpath.normpath(posixpath.join(posixpath.dirname(current_file), unquote(parts.path)))
    return path if not path.startswith(("../", "/")) else ""


def safe_image_name(path: str, index: int) -> str:
    # Hash the complete, case-sensitive ZIP path, not the basename or encounter order.
    # A short name also keeps deeply nested exports under Windows path limits.
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    suffix = PurePosixPath(path).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ".bin"
    return digest + suffix


def markdown_escape(text: str) -> str:
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_\[\]])", r"\\\1", text)


def score_document(title: str, blocks: Iterable[Block]) -> tuple[int, int]:
    wanted = normalize_for_search(title)
    texts = [normalize_for_search(block.text) for block in blocks if block.text]
    if not wanted:
        return (0, 0)
    exact_heading = any(wanted == text for text in texts[:8])
    exact_any = any(wanted == text for text in texts)
    contained = any(wanted in text for text in texts)
    return ((100 if exact_heading else 80 if exact_any else 50 if contained else 0),
            max((len(text) for text in texts if wanted in text), default=0))


def render_markdown(blocks: list[Block], title: str, zf: zipfile.ZipFile,
                    source_file: str, output_file: Path, extract_images: bool,
                    *, include_title: bool = True, write_images: bool = True) -> str:
    lines = [f"# {title.strip()}", ""] if include_title else []
    assets_dir = output_file.with_name(output_file.stem + "_assets")
    image_index = 0
    seen_title = False
    for block in blocks:
        text = block.text.strip()
        markdown = block.markdown if block.markdown is not None else markdown_escape(text)
        if block.kind in HEADING_TAGS:
            if normalize_for_search(text) == normalize_for_search(title):
                seen_title = True
                continue
            level = int(block.kind[1])
            markdown = f"{'#' * min(level + 1, 6)} {markdown}"
        elif block.kind == "img":
            image_index += 1
            target = html_path(block.src, source_file)
            if extract_images and target in zf.namelist():
                filename = safe_image_name(target, image_index)
                if write_images:
                    assets_dir.mkdir(parents=True, exist_ok=True)
                    (assets_dir / filename).write_bytes(zf.read(target))
                markdown = f"![{markdown_escape(block.alt)}]({quote(assets_dir.name)}/{quote(filename)})"
            else:
                continue
        elif block.kind == "hr":
            markdown = "---"
        elif text:
            if normalize_for_search(text) == normalize_for_search(title) and not seen_title:
                seen_title = True
                continue
            if block.kind == "li" and not block.prefix:
                markdown = "- " + markdown
        else:
            continue
        paragraph = markdown.splitlines()
        paragraph = [(block.prefix if i == 0 else block.continuation) + line
                     for i, line in enumerate(paragraph)]
        depth = block.quote_depth or (1 if block.kind == "blockquote" else 0)
        lines.extend(["> " * depth + line for line in paragraph] + [""])
    return "\n".join(lines).rstrip() + "\n"


def choose_document(zf: zipfile.ZipFile, title: str) -> tuple[str, list[Block], tuple[int, int]]:
    candidates: list[tuple[tuple[int, int], str, list[Block]]] = []
    for name in zf.namelist():
        if not name.lower().endswith((".html", ".xhtml")):
            continue
        blocks = parse_html(zf.read(name))
        score = score_document(title, blocks)
        if score[0]:
            candidates.append((score, name, blocks))
    if not candidates:
        raise LookupError(f"找不到标题：{title}")
    score, name, blocks = max(candidates, key=lambda item: item[0])
    return name, blocks, score


def main() -> int:
    parser = argparse.ArgumentParser(description="按文章标题从 EPUB 导出 Markdown")
    parser.add_argument("epub", type=Path, help="EPUB 文件路径")
    parser.add_argument("title", help="文章标题")
    parser.add_argument("-o", "--output", type=Path, help="输出 Markdown 路径")
    parser.add_argument("--no-images", action="store_true", help="不提取文章中的图片")
    args = parser.parse_args()
    if not args.epub.is_file():
        print(f"错误：文件不存在：{args.epub}", file=sys.stderr)
        return 2
    output = args.output or args.epub.with_name(f"{args.epub.stem} - {args.title.strip()}.md")
    try:
        with zipfile.ZipFile(args.epub) as zf:
            source_file, blocks, score = choose_document(zf, args.title)
            markdown = render_markdown(blocks, args.title, zf, source_file, output, not args.no_images)
        output.write_text(markdown, encoding="utf-8")
    except (zipfile.BadZipFile, LookupError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    # Keep CLI status messages ASCII-only so the tool also works in legacy
    # Windows consoles whose code page cannot encode Chinese characters.
    print(f"Exported: {output}")
    print(f"Source: {source_file} (match score {score[0]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
