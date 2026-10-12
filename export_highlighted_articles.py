#!/usr/bin/env python3
"""Find articles with personal WeRead highlights and export them from a local EPUB.

Requires weread_highlights.py, epub_to_markdown.py, download_economist.py,
article_template.py and templates beside this script.
Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import posixpath
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import epub_to_markdown as epub
import download_economist as downloads
import weread_highlights as weread
from article_template import Template, TemplateError

DEFAULT_TEMPLATE = Path(__file__).resolve().parent / "templates" / "article.md"


@dataclass
class Article:
    member: str
    fragment: str
    title: str
    aliases: list[str]
    blocks: list[epub.Block]
    searchable: str

    @property
    def source(self) -> str:
        return self.member + ("#" + self.fragment if self.fragment else "")


def book_name(value: str) -> str:
    value = value.strip()
    return value[:-5] if value.lower().endswith(".epub") else value


def resolve_href(source: str, href: str) -> tuple[str, str] | None:
    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        return None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(source), unquote(parts.path))) if parts.path else source
    if path.startswith(("../", "/")) or path == "..":
        return None
    return path, unquote(parts.fragment)


def local_tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1].lower() if isinstance(node.tag, str) else ""


def text_of(node: ET.Element | None) -> str:
    return epub.clean_text("".join(node.itertext())) if node is not None else ""


def fragment_body(body: ET.Element, start: ET.Element, stop: ET.Element | None) -> ET.Element:
    """Slice the DOM in reading order, retaining ancestors and inline markup."""
    nodes = list(body.iter())
    positions = {id(node): index for index, node in enumerate(nodes)}
    begin = positions[id(start)]
    end = positions[id(stop)] if stop is not None else len(nodes)

    def prune(node):
        pos = positions[id(node)]
        last = max(positions[id(child)] for child in node.iter())
        if last < begin or pos >= end:
            return None
        result = ET.Element(node.tag, node.attrib)
        result.text = node.text if begin <= pos < end else None
        result.tail = node.tail if begin <= last < end else None
        for child in node:
            kept = prune(child)
            if kept is not None:
                result.append(kept)
        return result

    result = prune(body)
    if result is None:
        raise weread.WeReadError("EPUB 目录锚点范围为空。")
    return result


def html_bytes(body: ET.Element) -> bytes:
    # HTMLParser expects plain tag names, not ElementTree's ns0:p prefixes.
    body = copy.deepcopy(body)
    for node in body.iter():
        node.tag = local_tag(node)
    return ET.tostring(body, encoding="utf-8", method="html")


def toc_entries(roots: dict, members: dict) -> OrderedDict:
    """Keep hierarchy; prefer EPUB 3 navigation over the legacy NCX copy."""
    navigation, ncx = OrderedDict(), OrderedDict()

    def add(entries, source, href, title, depth):
        target = resolve_href(source, href)
        if target and target[0] in members:
            entries.setdefault(target, []).append((title, depth))

    for name, root in roots.items():
        parents = {id(child): node for node in root.iter() for child in node}

        def ancestors(node):
            while id(node) in parents:
                node = parents[id(node)]
                yield node

        for node in root.iter():
            if local_tag(node) == "nav" and (
                    "toc" in node.get("{http://www.idpf.org/2007/ops}type", "").split()
                    or node.get("role") == "doc-toc"):
                for link in node.iter():
                    if local_tag(link) == "a" and link.get("href"):
                        depth = max(0, sum(local_tag(p) in ("ol", "ul") for p in ancestors(link)) - 1)
                        add(navigation, name, link.get("href"), text_of(link), depth)
            elif local_tag(node) == "navpoint":
                content = node.find("./{*}content")
                if content is not None:
                    depth = sum(local_tag(p) == "navpoint" for p in ancestors(node))
                    add(ncx, name, content.get("src", ""), text_of(node.find("./{*}navLabel")), depth)
    return navigation or ncx


def index_epub(archive: zipfile.ZipFile) -> list[Article]:
    roots, raw_html = {}, {}
    for name in archive.namelist():
        suffix = Path(name).suffix.lower()
        if suffix not in (".html", ".xhtml", ".htm", ".ncx"):
            continue
        raw = archive.read(name)
        if suffix != ".ncx":
            raw_html[name] = raw
        try:
            roots[name] = ET.fromstring(raw)
        except ET.ParseError:
            continue
    entries = toc_entries(roots, raw_html)

    articles = []
    for name, raw in raw_html.items():
        root = roots.get(name)
        targets = [(fragment, labels) for (member, fragment), labels in entries.items() if member == name]
        fragments = [(fragment, titles) for fragment, titles in targets if fragment]
        if root is None:
            # Non-XML HTML can still be handled as a whole document by the original parser.
            # Fragment slicing requires a well-formed XHTML tree.
            if fragments:
                continue
            blocks = epub.parse_html(raw)
            headings = [b.text for b in blocks if b.kind in ("h1", "title")]
            if not headings:
                continue
            aliases = headings + [title for _, labels in targets for title, _ in labels]
            articles.append(Article(name, "", headings[-1], aliases, blocks,
                                    epub.normalize_for_search(" ".join(b.text for b in blocks))))
            continue
        body = root.find(".//{*}body")
        if body is None or any(local_tag(node) == "nav" for node in body.iter()):
            continue
        doc_title = text_of(root.find("./{*}head/{*}title"))
        scopes = []
        if fragments:
            nodes = list(body.iter())
            anchors = {node.get("id"): node for node in nodes if node.get("id")}
            parents = {id(child): parent for parent in nodes for child in parent}
            positions = {id(node): index for index, node in enumerate(nodes)}
            located = []
            for fragment, labels in targets:
                node = anchors.get(fragment) if fragment else body
                if node is None:
                    continue
                # An id on a span inside a heading should include the entire heading.
                parent = parents.get(id(node))
                if parent is not None and local_tag(parent) in epub.HEADING_TAGS:
                    node = parent
                titles = [title for title, _ in labels]
                heading = next((text_of(n) for n in node.iter() if local_tag(n) in epub.HEADING_TAGS), "")
                # Section and first article may share a target. Use the article's
                # level when its heading confirms an alias, otherwise the deepest.
                confirmed = [level for title, level in labels if heading and
                             epub.normalize_for_search(title) == epub.normalize_for_search(heading)]
                depth = max(confirmed or [level for _, level in labels])
                located.append((positions[id(node)], fragment, titles, node, depth))
            located.sort(key=lambda entry: entry[0])
            for i, (position, fragment, titles, node, depth) in enumerate(located):
                stop = next((other[3] for other in located[i + 1:]
                             if other[0] > position and other[4] <= depth), None)
                scopes.append((fragment, titles, fragment_body(body, node, stop)))
        else:
            scopes.append(("", [t for _, labels in targets for t, _ in labels], body))
        for fragment, toc_titles, scope in scopes:
            headings = [text_of(node) for node in scope.iter() if local_tag(node) in epub.HEADING_TAGS]
            if not fragment and not fragments and sum(local_tag(node) == "h1" for node in scope.iter()) > 1:
                # Without TOC boundaries, exporting the whole file would mix articles.
                continue
            # A section-level NCX entry can point to its first article as well.
            # Prefer the actual heading when one of the TOC aliases confirms it.
            if headings and (not toc_titles or any(epub.normalize_for_search(headings[0]) ==
                                                  epub.normalize_for_search(t) for t in toc_titles)):
                title = headings[0]
            else:
                title = (toc_titles or headings or [doc_title or Path(name).stem])[0]
            aliases = toc_titles + headings[:1] + ([doc_title] if not fragment else [])
            blocks = epub.parse_html(html_bytes(scope))
            if not any(b.text for b in blocks):
                continue
            articles.append(Article(name, fragment, title, aliases, blocks,
                                    epub.normalize_for_search(" ".join(b.text for b in blocks))))
    return articles


def find_article(articles: list[Article], chapter_title: str, highlights: list[dict]) -> tuple[Article, str]:
    snippets = [epub.normalize_for_search(row["markText"]) for row in highlights]
    if not snippets or any(not text for text in snippets):
        raise weread.WeReadError("高亮原文为空，无法核对文章。")
    wanted = epub.normalize_for_search(chapter_title)
    titled = [article for article in articles if wanted and any(
        wanted == epub.normalize_for_search(alias) for alias in article.aliases)]
    candidates = titled or articles
    if not titled and any(len(text) < 20 for text in snippets):
        raise weread.WeReadError("章节标题未匹配，且高亮过短，无法可靠定位文章。")
    matched = [article for article in candidates if all(text in article.searchable for text in snippets)]
    if len(matched) == 1:
        return matched[0], "title+highlights" if titled else "highlights"
    if len(matched) > 1:
        raise weread.WeReadError("多个 EPUB 文章都包含这些高亮，无法唯一匹配：" +
                                ", ".join(article.source for article in matched))
    raise weread.WeReadError("未找到包含全部高亮原文的文章，请检查 EPUB 是否为同一版本及其目录结构。")


def match_highlighted_articles(bundle: dict, articles: list[Article]) -> tuple[list[dict], list[dict]]:
    chapters = {str(row["chapterUid"]): row for row in bundle["chapters"] if row.get("chapterUid") is not None}
    groups, matches, errors = OrderedDict(), OrderedDict(), []
    for index, row in enumerate(bundle["highlights"]):
        uid = str(row.get("chapterUid", ""))
        group = uid if uid else f"unknown-{index}"
        groups.setdefault(group, []).append(row)
    for group, rows in groups.items():
        chapter = chapters.get(group, {})
        title = chapter.get("title") or rows[0].get("chapterTitle") or rows[0].get("chapterName") or ""
        try:
            article, method = find_article(articles, title, rows)
            match = matches.setdefault(article.source, {"article": article, "highlights": [], "chapters": [], "method": method})
            match["highlights"].extend(rows)
            match["chapters"].append({"chapterUid": rows[0].get("chapterUid"), "title": title})
        except weread.WeReadError as exc:
            errors.append({"chapterUid": rows[0].get("chapterUid"), "title": title,
                           "highlight_count": len(rows), "error": str(exc)})
    order = {article.source: index for index, article in enumerate(articles)}
    return sorted(matches.values(), key=lambda match: order[match["article"].source]), errors


def choose_epub(book: dict, explicit: Path | None, directory: Path, *,
                download: bool = False, timeout: float = 30) -> Path:
    if explicit:
        if not explicit.is_file():
            raise weread.WeReadError(f"EPUB 不存在：{explicit}")
        return explicit
    title = epub.normalize_for_search(book_name(book.get("title", "")))
    if directory.exists() and not directory.is_dir():
        raise weread.WeReadError(f"EPUB 目录不是文件夹：{directory}")
    paths = [path for path in directory.iterdir() if path.is_file() and path.suffix.lower() == ".epub"] if directory.is_dir() else []
    matches = [path for path in paths if epub.normalize_for_search(path.stem) == title]
    if not matches and download:
        issue = downloads.issue_from_book(book.get("title", ""))
        return downloads.download_issue(issue, directory, timeout)
    if len(matches) != 1:
        raise weread.WeReadError("未找到唯一同名 EPUB，请用 --epub 指定路径；经济学人可用 --download-epub 在缺书时下载。")
    return matches[0]


def load_bundle(args) -> dict:
    if args.highlights_json:
        try:
            bundle = json.loads(args.highlights_json.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError):
            raise weread.WeReadError("高亮 JSON 格式无效。请使用 weread_highlights.py --json 生成的文件。") from None
        if not isinstance(bundle, dict) or not isinstance(bundle.get("book"), dict):
            raise weread.WeReadError("高亮 JSON 缺少 book 信息。")
        book = bundle["book"]
        if (args.book and epub.normalize_for_search(book_name(args.book)) !=
                epub.normalize_for_search(book_name(book.get("title", "")))) or (
                args.book_id and str(book.get("bookId")) != args.book_id):
            raise weread.WeReadError("高亮 JSON 中的书籍与指定书籍不一致。")
    else:
        key = weread.load_api_key(args.ask_key, args.config)
        if not key:
            raise weread.WeReadError("请先填写 weread_config.json 的 api_key，或使用 --ask-key。")
        client = weread.WeReadClient(key, args.timeout)
        book = {"bookId": args.book_id} if args.book_id else weread.select_book(client.notebooks(), book_name(args.book))
        bundle = client.fetch_book(book)
    weread.records(bundle, "chapters")
    highlights = weread.records(bundle, "highlights")
    if not bundle["book"].get("bookId"):
        raise weread.WeReadError("书籍缺少 bookId。")
    if any(not isinstance(row.get("markText"), str) for row in highlights):
        raise weread.WeReadError("高亮中缺少 markText 原文。")
    bundle["highlights"] = weread.unique([r for r in highlights if str(r.get("type", 1)) != "0"], "bookmarkId")
    return bundle


def article_context(bundle: dict, match: dict, content: str, epub_path: Path) -> dict:
    article = match["article"]
    highlights = []
    rows = sorted(match["highlights"], key=lambda row: weread.number(str(row.get("range", "")).split("-", 1)[0]))
    for row in rows:
        created_at = ""
        try:
            if row.get("createTime"):
                created_at = datetime.fromtimestamp(float(row["createTime"]), weread.LOCAL_TZ).isoformat(timespec="seconds")
        except (TypeError, ValueError, OverflowError, OSError):
            pass
        highlights.append({"text": row["markText"], "id": row.get("bookmarkId", ""),
                           "range": row.get("range", ""), "chapter_uid": row.get("chapterUid", ""),
                           "color": row.get("colorStyle", ""), "created_at": created_at})
    book = bundle["book"]
    return {"title": article.title, "content": content.rstrip(),
            "book": {"title": book.get("title", ""), "id": str(book["bookId"]), "author": book.get("author", "")},
            "epub_name": epub_path.name, "epub_source": article.source,
            "exported_at": datetime.now(weread.LOCAL_TZ).isoformat(timespec="seconds"),
            "highlights_fetched_at": bundle.get("exported_at", ""),
            "highlight_count": len(highlights), "highlights": highlights}


def article_id(book: dict, article: Article) -> str:
    identity = json.dumps([str(book["bookId"]), article.member, article.fragment], ensure_ascii=False)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def previous_article_names(target: Path, book: dict) -> dict[str, str]:
    """Reuse manifest paths, including old numbered exports, without deleting notes."""
    manifest = target / "matches.json"
    if not manifest.exists():
        return {}
    try:
        report = json.loads(manifest.read_text(encoding="utf-8"))
        if str(report["book"]["bookId"]) != str(book["bookId"]):
            raise ValueError("book identity mismatch")
        names, used = {}, set()
        for row in report["articles"]:
            source, name = row["epub_source"], row["markdown"]
            if (not isinstance(source, str) or not isinstance(name, str)
                    or not name.lower().endswith(".md") or name.lower() == "index.md"
                    or any(char in name for char in '<>:"/\\|?*')
                    or any(ord(char) < 32 for char in name)
                    or name.rstrip(". ") != name or name.casefold() in used
                    or source in names):
                raise ValueError("unsafe or duplicate filename")
            names[source] = name
            used.add(name.casefold())
        return names
    except (ValueError, KeyError, TypeError):
        raise weread.WeReadError("已有 matches.json 无效或不属于此书，请检查报告或使用新的 --output-dir。") from None


def export_articles(bundle: dict, epub_path: Path, output_dir: Path, images: bool, force: bool,
                    template: Template | None = None) -> tuple[Path, int, int]:
    template = template if template is not None else Template.from_file(DEFAULT_TEMPLATE)
    target = output_dir / Path(weread.safe_filename(bundle["book"])).stem
    if target.exists() and not force:
        raise weread.WeReadError(f"输出目录已存在：{target}。请更换 --output-dir 或用 --force 覆盖同名文件。")
    previous_names = previous_article_names(target, bundle["book"])
    with zipfile.ZipFile(epub_path) as archive:
        articles = index_epub(archive)
        matches, errors = match_highlighted_articles(bundle, articles)
        # Validate all populated template contexts before touching any output files.
        prepared = []
        filenames = set()
        for match in matches:
            article = match["article"]
            filename = previous_names.get(article.source) or weread.safe_filename(
                {"title": article.title, "bookId": "article-" + article_id(bundle["book"], article)})
            if filename.casefold() in filenames:
                raise weread.WeReadError("文章输出文件名冲突，请检查已有 matches.json 或使用新的 --output-dir。")
            filenames.add(filename.casefold())
            output = target / filename
            content = epub.render_markdown(article.blocks, article.title, archive, article.member, output,
                                           images, include_title=False, write_images=False)
            markdown = template.render(article_context(bundle, match, content, epub_path))
            prepared.append((match, output, markdown.rstrip() + "\n"))
        target.mkdir(parents=True, exist_ok=True)
        title = bundle["book"].get("title") or bundle["book"]["bookId"]
        lines = [f"# {weread.one_line(title)} · 有高亮的文章", "",
                 f"EPUB：{weread.one_line(epub_path.name)}", "",
                 f"共 {len(bundle['highlights'])} 条高亮；匹配到 {len(matches)} 篇文章；{len(errors)} 组未匹配。", ""]
        report = {"book": bundle["book"], "epub": str(epub_path.resolve()),
                  "highlights_fetched_at": bundle.get("exported_at"),
                  "highlight_count": len(bundle["highlights"]), "articles": [], "unmatched": errors}
        for match, output, markdown in prepared:
            article = match["article"]
            if images:
                epub.render_markdown(article.blocks, article.title, archive, article.member, output, True)
            weread.write_text(output, markdown, force)
            lines.append(f"- [{weread.one_line(article.title)}]({quote(output.name)}) — {len(match['highlights'])} 条高亮，原文已核对")
            report["articles"].append({"title": article.title, "epub_source": article.source,
                                       "article_id": article_id(bundle["book"], article),
                                       "markdown": output.name, "match_method": match["method"],
                                       "chapters": match["chapters"], "highlight_count": len(match["highlights"]),
                                       "highlights": match["highlights"]})
            print(f"已导出：{article.title}（{len(match['highlights'])} 条高亮）")
        if errors:
            lines.extend(["", "## 未匹配", ""])
            for error in errors:
                lines.append(f"- {weread.one_line(error['title'] or str(error['chapterUid']))}：{error['error']}")
        if not bundle["highlights"]:
            lines.append("该书暂无可导出的高亮，因此没有提取文章。")
        index = target / "index.md"
        weread.write_text(index, "\n".join(lines).rstrip() + "\n", force)
        weread.write_text(target / "matches.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n", force)
    return index, len(matches), len(errors)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="按微信读书高亮定位本地 EPUB 文章，并导出全文 Markdown")
    book = parser.add_mutually_exclusive_group(required=True)
    book.add_argument("--book", help="微信读书书名（可带 .epub 后缀）")
    book.add_argument("--book-id", help="微信读书 bookId")
    parser.add_argument("--epub", type=Path, help="对应 EPUB；默认按书名在 --epub-dir 中查找")
    parser.add_argument("--epub-dir", type=Path, default=Path("."), help="EPUB 目录，默认当前目录，不递归")
    parser.add_argument("--download-epub", action="store_true",
                        help="本地缺书时从 hehonghui/awesome-english-ebooks 下载对应日期的经济学人，保存到 --epub-dir")
    parser.add_argument("--output-dir", type=Path, default=Path("highlighted_articles"), help="输出根目录")
    parser.add_argument("--highlights-json", type=Path, help="使用已有高亮 JSON 离线导出；否则实时获取")
    parser.add_argument("--config", type=Path, help="API Key 配置文件，默认与高亮脚本共用")
    parser.add_argument("--ask-key", action="store_true", help="隐藏输入 API Key")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--no-images", action="store_true", help="不导出文章图片")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE,
                        help="文章 Markdown 模板，默认 templates/article.md（相对脚本目录）")
    parser.add_argument("--force", action="store_true", help="覆盖本次导出的同名文件，保留其他文件")
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 120:
        parser.error("--timeout 必须大于 0 且不超过 120 秒。")
    try:
        template = Template.from_file(args.template)
        bundle = load_bundle(args)
        path = choose_epub(bundle["book"], args.epub, args.epub_dir,
                           download=args.download_epub, timeout=args.timeout)
        print(f"书籍：{bundle['book'].get('title', bundle['book']['bookId'])}；高亮：{len(bundle['highlights'])} 条")
        index, count, failed = export_articles(bundle, path, args.output_dir, not args.no_images, args.force, template)
        print(f"完成：{count} 篇已导出，{failed} 组未匹配。索引：{index}")
        return 1 if failed else 0
    except (weread.WeReadError, downloads.DownloadError, TemplateError, OSError, zipfile.BadZipFile, EOFError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    for console in (sys.stdout, sys.stderr):
        if hasattr(console, "reconfigure"):
            console.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
