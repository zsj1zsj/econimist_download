#!/usr/bin/env python3
"""Download one exact Economist issue from the user-selected public GitHub repo.

Python 3.10+, standard library only. No WeRead credentials are read or sent.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

REPOSITORY = "hehonghui/awesome-english-ebooks"
BRANCH = "master"
ROOT = "01_economist"
SOURCE_URL = f"https://github.com/{REPOSITORY}/tree/{BRANCH}/{ROOT}"
API_BASE = f"https://api.github.com/repos/{REPOSITORY}/contents/"
RAW_BASE = f"https://raw.githubusercontent.com/{REPOSITORY}/{BRANCH}/"
MAX_DOWNLOAD = 100 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024
MAX_UNCOMPRESSED = 512 * 1024 * 1024


class DownloadError(Exception):
    """A safe, actionable error without remote response bodies or credentials."""


def parse_date(value: str) -> date:
    value = value.strip()
    match = re.fullmatch(r"(\d{4})([.\-_/])(\d{1,2})\2(\d{1,2})", value)
    chinese = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日?", value)
    parts = (match[1], match[3], match[4]) if match else chinese.groups() if chinese else None
    try:
        if parts:
            return date(*(int(part) for part in parts))
    except ValueError:
        pass
    raise DownloadError("期刊日期无效，请使用 YYYY-MM-DD 或 YYYY.MM.DD。")


def issue_from_book(title: str) -> date:
    title = re.sub(r"\.epub$", "", title.strip(), flags=re.I)
    match = re.fullmatch(r"(?:the[\s._-]*economist|economist|te|经济学人)[\s._-]*(.+)", title, re.I)
    if not match:
        raise DownloadError("自动下载仅支持带日期的经济学人书名，例如 TheEconomist.2026.10.10；其他书请用 --epub。")
    return parse_date(match[1])


def issue_filename(issue: date) -> str:
    return f"TheEconomist.{issue:%Y.%m.%d}.epub"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class RemoteEpub:
    path: str
    url: str
    size: int
    sha: str


class GitHubSource:
    def __init__(self, timeout: float = 30):
        if not 0 < timeout <= 120:
            raise DownloadError("--timeout 必须大于 0 且不超过 120 秒。")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def open(self, url: str):
        # Callers only use our fixed API paths or an exactly validated raw URL.
        if not url.startswith((API_BASE, RAW_BASE)) or urlsplit(url).scheme != "https":
            raise DownloadError("拒绝访问下载源以外的地址。")
        request = urllib.request.Request(url, headers={
            "User-Agent": "economist-epub-exporter/1.0",
            "Accept": "application/vnd.github+json" if url.startswith(API_BASE) else "application/octet-stream"})
        for attempt in range(3):
            try:
                return self.opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                code = exc.code
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                exc.close()
                delay = int(retry_after) if retry_after.isdigit() else 2 ** attempt
                if code in (429, 500, 502, 503, 504) and attempt < 2 and delay <= 5:
                    time.sleep(delay)
                    continue
                if code in (403, 429):
                    raise DownloadError("GitHub 访问受限或达到请求限额，请稍后再试，或手动下载后用 --epub。") from None
                if code == 404:
                    raise DownloadError("GitHub 目录或文件不存在，仓库可能已更新；请稍后重试或手动指定 --epub。") from None
                raise DownloadError(f"GitHub 请求失败（HTTP {code}），请稍后重试。") from None
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
                raise DownloadError("无法连接 GitHub，请检查网络、代理或证书后重试。") from None

    def list_directory(self, path: str) -> list[dict]:
        url = API_BASE + quote(path, safe="/") + "?ref=" + quote(BRANCH, safe="")
        try:
            with self.open(url) as response:
                data = response.read(MAX_JSON + 1)
            if len(data) > MAX_JSON:
                raise DownloadError("GitHub 目录响应过大，已停止，未尝试选择部分结果。")
            rows = json.loads(data)
        except (ValueError, UnicodeError):
            raise DownloadError("GitHub 返回了无效的目录 JSON。") from None
        except (OSError, http.client.HTTPException):
            raise DownloadError("读取 GitHub 目录时连接中断，请重试。") from None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DownloadError("GitHub 目录数据格式异常。")
        if len(rows) >= 1000:
            raise DownloadError("GitHub 目录可能被截断，无法可靠定位期刊，请手动指定 --epub。")
        for row in rows:
            name = row.get("name")
            if (not isinstance(name, str) or name in ("", ".", "..")
                    or "/" in name or "\\" in name or row.get("path") != path + "/" + name):
                raise DownloadError("GitHub 目录包含无效的文件路径。")
        return rows

    def find_issue(self, issue: date) -> RemoteEpub:
        folder_name = f"te_{issue:%Y.%m.%d}"
        rows = self.list_directory(ROOT)
        folders = [r for r in rows if r.get("type") == "dir" and r["name"] == folder_name]
        year = next((r for r in rows if r.get("type") == "dir" and r["name"] == str(issue.year)), None)
        if year is not None:
            archived = self.list_directory(year["path"])
            folders.extend(r for r in archived if r.get("type") == "dir" and r["name"] == folder_name)
        if not folders:
            raise DownloadError(f"仓库中没有 {issue:%Y.%m.%d} 这一期；不会用其他日期替代。请检查日期或用 --epub。")
        if len(folders) != 1:
            raise DownloadError("同一期存在多个目录，无法唯一选择，请手动下载并用 --epub。")
        matches = []
        for row in self.list_directory(folders[0]["path"]):
            if row.get("type") != "file" or not row["name"].lower().endswith(".epub"):
                continue
            try:
                candidate = issue_from_book(row["name"])
            except DownloadError:
                continue
            if candidate == issue:
                matches.append(row)
        if len(matches) != 1:
            raise DownloadError("该期目录没有唯一的同日期 EPUB；不会选用 PDF、MOBI 或其他期刊。")
        row = matches[0]
        expected_url = RAW_BASE + quote(row["path"], safe="/")
        size, sha = row.get("size"), row.get("sha")
        if row.get("download_url") != expected_url:
            raise DownloadError("GitHub 返回的 EPUB 下载地址不属于指定仓库路径。")
        if type(size) is not int or not 0 < size <= MAX_DOWNLOAD:
            raise DownloadError("EPUB 大小无效或超过 100 MiB 下载上限。")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise DownloadError("GitHub 缺少有效的文件校验值。")
        return RemoteEpub(row["path"], expected_url, size, sha)


def validate_epub(path: Path, issue: date) -> None:
    """Inspect structure without extracting files or trusting ZIP paths."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if (len(entries) > 20000 or sum(info.file_size for info in entries) > MAX_UNCOMPRESSED
                    or len({info.filename for info in entries}) != len(entries)
                    or any(info.flag_bits & 1 for info in entries)):
                raise DownloadError("EPUB 包含重复、加密或过大的内容，已停止。")

            def read_small(member):
                if archive.getinfo(member).file_size > MAX_JSON:
                    raise DownloadError("EPUB 元数据过大，已停止。")
                return archive.read(member)

            if read_small("mimetype").strip() != b"application/epub+zip":
                raise DownloadError("文件不是有效的 EPUB（mimetype 不匹配）。")
            container = ET.fromstring(read_small("META-INF/container.xml"))
            package = next((n.get("full-path", "") for n in container.iter()
                            if n.tag.rsplit("}", 1)[-1] == "rootfile"), "")
            if (not package or package.startswith("/") or "\\" in package or ":" in package
                    or ".." in PurePosixPath(package).parts):
                raise DownloadError("EPUB 主文档路径无效。")
            root = ET.fromstring(read_small(package))
            tags = {n.tag.rsplit("}", 1)[-1] for n in root}
            if root.tag.rsplit("}", 1)[-1] != "package" or not {"metadata", "manifest", "spine"} <= tags:
                raise DownloadError("EPUB 缺少有效的 OPF 结构。")
            for title in root.findall(".//{*}title"):
                try:
                    metadata_issue = issue_from_book("".join(title.itertext()))
                except DownloadError:
                    continue
                if metadata_issue != issue:
                    raise DownloadError("EPUB 内部书名日期与所选期刊不一致，已停止。")
    except (zipfile.BadZipFile, KeyError, ET.ParseError, RuntimeError, NotImplementedError):
        raise DownloadError("文件损坏或不是有效 EPUB；请保留原文件并检查，或使用另一个下载目录。") from None


def _publish(temporary: Path, target: Path) -> None:
    # Windows rename and POSIX hard-link creation both refuse an existing target.
    if os.name == "nt":
        temporary.rename(target)
    else:
        os.link(temporary, target)


def download_issue(issue: date, directory: Path, timeout: float = 30) -> Path:
    target = directory / issue_filename(issue)
    if target.exists() or target.is_symlink():
        if not target.is_file() or target.is_symlink():
            raise DownloadError(f"下载目标不是普通文件，未覆盖：{target}")
        validate_epub(target, issue)
        print(f"使用已有 EPUB：{target}")
        return target
    client = GitHubSource(timeout)
    remote = client.find_issue(issue)
    print(f"下载 EPUB：{remote.path}（{remote.size / 1024 / 1024:.1f} MiB）")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".economist-", suffix=".part", dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            digest = hashlib.sha1(f"blob {remote.size}\0".encode("ascii"))
            total = 0
            try:
                with client.open(remote.url) as response:
                    while chunk := response.read(128 * 1024):
                        total += len(chunk)
                        if total > remote.size or total > MAX_DOWNLOAD:
                            raise DownloadError("下载内容超过预期大小，未保存为 EPUB。")
                        digest.update(chunk)
                        stream.write(chunk)
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                raise DownloadError("下载中断或无法写入文件，未保存不完整的 EPUB；请检查网络和磁盘后重试。") from None
        if total != remote.size or digest.hexdigest() != remote.sha:
            raise DownloadError("下载大小或 Git 文件校验值不匹配，未保存为 EPUB；仓库可能已更新，请重试。")
        validate_epub(temporary, issue)
        try:
            _publish(temporary, target)
        except FileExistsError:
            raise DownloadError(f"下载期间目标文件已出现，已保留该文件，未覆盖：{target}") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"下载完成，校验通过：{target}")
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="从指定 GitHub 仓库下载某一期经济学人 EPUB；不覆盖已有文件")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--book", help="带日期的经济学人书名，例如 TheEconomist.2026.10.10")
    selection.add_argument("--date", help="期刊日期，例如 2026-10-10")
    parser.add_argument("--output-dir", type=Path, default=Path("epubs"), help="下载目录，默认 epubs")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 120:
        parser.error("--timeout 必须大于 0 且不超过 120 秒。")
    try:
        issue = issue_from_book(args.book) if args.book else parse_date(args.date)
        download_issue(issue, args.output_dir, args.timeout)
        return 0
    except (DownloadError, OSError) as exc:
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
