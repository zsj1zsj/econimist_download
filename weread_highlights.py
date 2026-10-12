#!/usr/bin/env python3
"""Export personal WeRead highlights using the official Agent Gateway (Python 3.10+).

API contract: https://github.com/Tencent/WeChatReading/blob/main/skills/notes.md
No third-party dependencies. Credentials come from JSON, WEREAD_API_KEY or a hidden prompt.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_URL = "https://i.weread.qq.com/api/agent/gateway"
API_VERSION = "1.0.4"
LOCAL_TZ = timezone(timedelta(hours=8))
DEFAULT_CONFIG = Path(__file__).resolve().with_name("weread_config.json")


class WeReadError(Exception):
    """An actionable error that is safe to display without exposing credentials."""


def load_api_key(ask_key: bool = False, config: Path | None = None) -> str:
    """Priority: explicit hidden prompt, nonempty environment variable, JSON file."""
    if ask_key:
        return getpass.getpass("微信读书 API Key（隐藏输入）：").strip()
    env_key = os.environ.get("WEREAD_API_KEY", "").strip()
    if env_key:
        return env_key
    path = config if config is not None else DEFAULT_CONFIG
    try:
        # Windows editors may add a UTF-8 BOM.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        if config is not None:
            raise WeReadError(f"配置文件不存在：{path}") from None
        return ""
    except (ValueError, UnicodeError):
        raise WeReadError(f"配置文件不是有效的 UTF-8 JSON：{path}。请检查双引号和逗号。") from None
    except OSError:
        raise WeReadError(f"无法读取配置文件：{path}。请检查文件权限。") from None
    if not isinstance(data, dict) or not isinstance(data.get("api_key", ""), str):
        raise WeReadError(f"配置文件 {path} 应为 JSON 对象，api_key 必须是字符串。")
    return data.get("api_key", "").strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Authorization header to a different destination.
        return None


def records(data: dict, field: str) -> list[dict]:
    value = data.get(field)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise WeReadError(f"接口数据格式异常：{field} 应为对象数组，已停止导出。")
    return value


def unique(items: list[dict], field: str) -> list[dict]:
    result, seen = [], set()
    for item in items:
        identity = ("id", str(item[field])) if item.get(field) else (
            "data", json.dumps(item, sort_keys=True, ensure_ascii=False))
        if identity not in seen:
            seen.add(identity)
            result.append(item)
    return result


def has_more(data: dict) -> bool:
    value = data.get("hasMore", 0)
    if value not in (0, 1, "0", "1", False, True):
        raise WeReadError("接口返回了无法识别的 hasMore。")
    return value in (1, "1", True)


class WeReadClient:
    def __init__(self, api_key: str, timeout: float = 30):
        if not re.fullmatch(r"wrk-[A-Za-z0-9_-]+", api_key):
            raise WeReadError("API Key 格式无效，请从微信读书官方页面获取 wrk- 开头的 Key。")
        self.api_key = api_key
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def call(self, api_name: str, **params) -> dict:
        payload = {"api_name": api_name, "skill_version": API_VERSION, **params}
        request = urllib.request.Request(
            API_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json", "Accept": "application/json"})
        for attempt in range(3):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                code = exc.code
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                exc.close()
                if code in (429, 500, 502, 503, 504) and attempt < 2:
                    delay = min(int(retry_after), 30) if retry_after.isdigit() else 2 ** attempt
                    time.sleep(delay)
                    continue
                if code in (401, 403):
                    raise WeReadError("认证失败，请检查 API Key 或重新获取。") from None
                raise WeReadError(f"{api_name} 请求失败（HTTP {code}），请稍后重试。") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                raise WeReadError("连接微信读书失败，请检查网络、代理或证书配置后重试。") from None
            except (ValueError, UnicodeError):
                raise WeReadError("微信读书返回了非 JSON 数据，导出未完成。") from None
            if not isinstance(data, dict):
                raise WeReadError("微信读书响应格式异常，导出未完成。")
            if data.get("errcode", 0) not in (0, "0", None):
                # Do not echo a server-provided message that might contain a token.
                raw_code = str(data["errcode"])
                code = raw_code if re.fullmatch(r"-?\d{1,10}", raw_code) else "unknown"
                raise WeReadError(f"{api_name} 返回错误码 {code}，请检查 Key 和官方接口文档。")
            return data
        raise WeReadError("请求重试失败。")

    def notebooks(self) -> list[dict]:
        result, seen_cursors = [], set()
        params = {"count": 20}
        while True:
            data = self.call("/user/notebooks", **params)
            page = records(data, "books")
            for row in page:
                book = row.get("book") or {}
                if not isinstance(book, dict):
                    raise WeReadError("笔记本中的书籍信息格式异常。")
                book_id = str(row.get("bookId") or book.get("bookId") or "")
                if not book_id:
                    raise WeReadError("笔记本缺少 bookId，无法确保完整导出。")
                result.append({**row, "bookId": book_id, "book": {**book, "bookId": book_id}})
            if not has_more(data):
                return unique(result, "bookId")
            cursor = page[-1].get("sort") if page else None
            if not isinstance(cursor, (int, str)) or str(cursor) in seen_cursors:
                raise WeReadError("笔记本分页游标缺失或重复，已停止以避免漏导出。")
            seen_cursors.add(str(cursor))
            params["lastSort"] = cursor

    def reviews(self, book_id: str) -> list[dict]:
        result, seen_cursors = [], {"0"}
        cursor = 0
        while True:
            data = self.call("/review/list/mine", bookid=book_id, count=20, synckey=cursor)
            page = records(data, "reviews")
            for row in page:
                review = row.get("review")
                if not isinstance(review, dict):
                    raise WeReadError("想法数据缺少 review 对象。")
                result.append({**review, "reviewId": review.get("reviewId") or row.get("reviewId", "")})
            if not has_more(data):
                return unique(result, "reviewId")
            cursor = data.get("synckey")
            if not page or not isinstance(cursor, (int, str)) or str(cursor) in seen_cursors:
                raise WeReadError("想法分页游标缺失或重复，已停止以避免漏导出。")
            seen_cursors.add(str(cursor))

    def fetch_book(self, book: dict, include_notes: bool = False) -> dict:
        book_id = str(book["bookId"])
        data = self.call("/book/bookmarklist", bookId=book_id)
        highlights = unique(records(data, "updated"), "bookmarkId")
        highlights = [row for row in highlights if str(row.get("type", 1)) != "0"]
        if any(not isinstance(row.get("markText"), str) for row in highlights):
            raise WeReadError("划线缺少 markText，已停止以避免生成不完整文件。")
        chapters = records(data, "chapters") if "chapters" in data else []
        info = data.get("book") or {}
        if not isinstance(info, dict):
            raise WeReadError("划线响应中的书籍信息格式异常。")
        info = {**book, **info, "bookId": book_id}
        if not info.get("title"):
            info = {**info, **self.call("/book/info", bookId=book_id), "bookId": book_id}
        reviews = self.reviews(book_id) if include_notes else []
        return {"book": info, "highlights": highlights, "chapters": chapters,
                "reviews": reviews, "notes_included": include_notes,
                "exported_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds")}


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def select_book(notebooks: list[dict], query: str) -> dict:
    wanted = normalized(query)
    if not wanted:
        raise WeReadError("书名不能为空。")
    exact = [row["book"] for row in notebooks
             if wanted == normalized(str(row["book"].get("title", "")))]
    matches = exact or [row["book"] for row in notebooks
                        if wanted in normalized(str(row["book"].get("title", "")))]
    if not matches:
        raise WeReadError("未找到有笔记的对应书籍。请先运行 --list，或用 --book-id 指定 ID。")
    if len(matches) > 1:
        choices = "\n".join(f"  {b['bookId']}  {b.get('title', '')}  {b.get('author', '')}" for b in matches)
        raise WeReadError(f"匹配到多本书，请用 --book-id 明确选择：\n{choices}")
    return matches[0]


def number(value, default=10**18) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def escape(text) -> str:
    text = html.escape(str(text or ""), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)


def one_line(text) -> str:
    return escape(" ".join(str(text or "").split()))


def quote_lines(text) -> list[str]:
    return ["> " + escape(line) if line else ">" for line in str(text or "").splitlines()]


def render_markdown(bundle: dict) -> str:
    book = bundle["book"]
    highlights, reviews = bundle["highlights"], bundle["reviews"]
    lines = [f"# {one_line(book.get('title') or book['bookId'])} · 微信读书高亮", "",
             f"作者：{one_line(book.get('author'))}", "",
             f"Book ID：{one_line(book['bookId'])}", "",
             f"导出时间：{bundle['exported_at']}", "",
             f"高亮：{len(highlights)} 条" + (f"；想法/点评：{len(reviews)} 条" if bundle["notes_included"] else ""), ""]
    chapters = {str(ch["chapterUid"]): ch for ch in bundle["chapters"] if ch.get("chapterUid") is not None}
    groups: dict[str, list[tuple[str, dict]]] = OrderedDict()
    for kind, items in (("highlight", highlights), ("review", reviews)):
        for item in items:
            uid = str(item.get("chapterUid", ""))
            groups.setdefault(uid, []).append((kind, item))
            if uid not in chapters:
                chapters[uid] = {"title": item.get("chapterName") or item.get("chapterTitle") or
                                (f"章节 {uid}" if uid else "未标注章节 / 整本书"),
                                "chapterIdx": item.get("chapterIdx")}
    for uid in sorted(groups, key=lambda key: number(chapters[key].get("chapterIdx"))):
        lines.extend([f"## {one_line(chapters[uid].get('title') or '未命名章节')}", ""])
        items = sorted(groups[uid], key=lambda pair: (
            number(str(pair[1].get("range", "")).split("-", 1)[0]),
            0 if pair[0] == "highlight" else 1, number(pair[1].get("createTime"))))
        for kind, item in items:
            if kind == "highlight":
                lines.extend(quote_lines(item["markText"]))
            else:
                lines.extend(["**想法 / 点评**", ""])
                if item.get("abstract"):
                    lines.extend(quote_lines(item["abstract"]) + [""])
                lines.extend(quote_lines(item.get("content", "")))
            lines.extend(["", "---", ""])
    if not groups:
        lines.extend(["未找到可导出的高亮" + ("或想法。" if bundle["notes_included"] else "。"), ""])
    return "\n".join(lines).rstrip() + "\n"


def safe_filename(book: dict) -> str:
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(book.get("title") or "未命名"))
    title = title.strip(" .")[:60].rstrip(" .") or "未命名"
    book_id = str(book["bookId"])
    suffix = re.sub(r"[^A-Za-z0-9_-]", "_", book_id)[:40]
    if suffix != book_id:
        suffix += "-" + hashlib.sha256(book_id.encode()).hexdigest()[:10]
    return f"{title}--{suffix}.md"


def write_text(path: Path, content: str, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not force:
        # Exclusive create also avoids an exists()/write() race.
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        return
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, delete=False) as stream:
            temp_name = stream.name
            stream.write(content)
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def save_bundle(bundle: dict, output: Path, include_json: bool, force: bool) -> None:
    targets = [(output, render_markdown(bundle))]
    if include_json:
        targets.append((output.with_suffix(".json"), json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"))
    for path, _ in targets:
        if path.exists() and not force:
            raise WeReadError(f"输出已存在：{path}。更换输出路径，或用 --force 覆盖。")
    for path, content in targets:
        write_text(path, content, force)


def demo_bundle() -> dict:
    return {"book": {"bookId": "demo", "title": "示例书（模拟数据）", "author": "示例作者"},
            "exported_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
            "notes_included": False, "reviews": [],
            "chapters": [{"chapterUid": 10, "chapterIdx": 1, "title": "第一章 观察"},
                         {"chapterUid": 20, "chapterIdx": 2, "title": "第二章 行动"}],
            "highlights": [{"bookmarkId": "demo-1", "chapterUid": 10, "range": "10-20",
                            "markText": "这是一条用于检查导出格式的模拟高亮。", "type": 1},
                           {"bookmarkId": "demo-2", "chapterUid": 10, "range": "30-40",
                            "markText": "多行高亮第一行。\n第二行也保留。", "type": 1},
                           {"bookmarkId": "demo-3", "chapterUid": 20, "range": "0-10",
                            "markText": "这里显示另一个章节的模拟内容。", "type": 1}]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="获取自己的微信读书高亮并导出 Markdown（标准库，无需 pip）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="列出有笔记的书和 bookId")
    mode.add_argument("--book", help="按书名选择（支持唯一部分匹配）")
    mode.add_argument("--book-id", help="按 bookId 直接导出，包括上传书籍的 CB_ ID")
    mode.add_argument("--all", action="store_true", help="每本有笔记的书分别导出")
    mode.add_argument("--demo", action="store_true", help="离线导出模拟数据，无需 Key")
    parser.add_argument("-o", "--output", type=Path, help="单本输出 .md 文件路径")
    parser.add_argument("--output-dir", type=Path, default=Path("weread_exports"), help="输出目录，默认 ./weread_exports")
    parser.add_argument("--include-notes", action="store_true", help="同时获取自己的想法和点评")
    parser.add_argument("--json", action="store_true", help="同时保存结构化 JSON")
    parser.add_argument("--force", action="store_true", help="覆盖已有导出文件")
    parser.add_argument("--ask-key", action="store_true", help="在本机隐藏输入 API Key（不保存）")
    parser.add_argument("--config", type=Path, help="配置文件路径，默认读取脚本旁的 weread_config.json")
    parser.add_argument("--timeout", type=float, default=30, help="单次请求超时秒数，默认 30")
    args = parser.parse_args(argv)
    if args.output and (args.all or args.list):
        parser.error("-o 只用于单本导出；批量导出请使用 --output-dir。")
    if args.output and args.output.suffix.lower() != ".md":
        parser.error("输出文件必须使用 .md 后缀。")
    if not 0 < args.timeout <= 120:
        parser.error("--timeout 必须大于 0 且不超过 120 秒。")
    try:
        if args.demo:
            bundle = demo_bundle()
            output = args.output or args.output_dir / safe_filename(bundle["book"])
            save_bundle(bundle, output, args.json, args.force)
            print(f"模拟数据导出成功（不是账号数据）：{output}")
            return 0
        key = load_api_key(args.ask_key, args.config)
        if not key:
            raise WeReadError("未配置 API Key。请填写 weread_config.json 的 api_key，"
                              "设置 WEREAD_API_KEY，或加 --ask-key 在本机输入。\n"
                              "获取 Key：https://weread.qq.com/r/weread-skills")
        client = WeReadClient(key, args.timeout)
        if args.book_id:
            books = [{"bookId": args.book_id}]
        else:
            notebooks = client.notebooks()
            if args.list:
                for row in notebooks:
                    book = row["book"]
                    print(f"{book['bookId']}\t{book.get('title', '')}\t{book.get('author', '')}"
                          f"\t高亮 {row.get('noteCount', 0)}\t想法 {row.get('reviewCount', 0)}")
                print(f"共 {len(notebooks)} 本有笔记的书。")
                return 0
            books = [select_book(notebooks, args.book)] if args.book else [row["book"] for row in notebooks]
        failures = 0
        for book in books:
            try:
                bundle = client.fetch_book(book, args.include_notes)
                output = args.output or args.output_dir / safe_filename(bundle["book"])
                save_bundle(bundle, output, args.json, args.force)
                print(f"已导出 {len(bundle['highlights'])} 条高亮：{output}")
            except (WeReadError, OSError) as exc:
                failures += 1
                print(f"导出失败 [{book['bookId']}]：{exc}", file=sys.stderr)
                if not args.all:
                    return 1
        print(f"完成：{len(books) - failures} 本成功，{failures} 本失败。")
        return 1 if failures else 0
    except (WeReadError, OSError, EOFError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    for console in (sys.stdout, sys.stderr):
        if hasattr(console, "reconfigure"):
            console.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)
