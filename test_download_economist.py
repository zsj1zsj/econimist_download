"""Offline GitHub discovery, safe download, caching and export integration tests."""

import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

import download_economist as dl
import export_highlighted_articles as exporter

ISSUE = date(2026, 10, 10)
FOLDER = dl.ROOT + "/te_2026.10.10"


def epub_bytes(issue=ISSUE):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="EPUB/content.opf"/></rootfiles></container>')
        archive.writestr("EPUB/content.opf", f'<package xmlns:dc="http://purl.org/dc/elements/1.1/">'
                         f'<metadata><dc:title>TheEconomist.{issue:%Y.%m.%d}</dc:title></metadata>'
                         '<manifest><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/></manifest>'
                         '<spine><itemref idref="a"/></spine></package>')
        archive.writestr("EPUB/a.xhtml", '<html><head><title>Article</title></head><body><h1>Article</h1>'
                         '<p>A highlighted sentence, followed by the full original article.</p></body></html>')
    return buffer.getvalue()


def remote_row(content, folder=FOLDER, name=None):
    name = name or dl.issue_filename(ISSUE)
    path = folder + "/" + name
    return {"type": "file", "name": name, "path": path, "download_url": dl.RAW_BASE + path,
            "size": len(content), "sha": hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()}


def folder_row(path):
    return {"type": "dir", "name": path.rsplit("/", 1)[-1], "path": path}


def fake_http(content):
    """One current issue; each call returns a fresh readable response."""
    row = remote_row(content)

    def open_url(url):
        if url == dl.API_BASE + dl.ROOT + "?ref=master":
            return io.BytesIO(json.dumps([folder_row(FOLDER)]).encode())
        if url == dl.API_BASE + FOLDER + "?ref=master":
            return io.BytesIO(json.dumps([row]).encode())
        if url == row["download_url"]:
            return io.BytesIO(content)
        raise AssertionError(f"Unexpected URL: {url}")

    return open_url


class DiscoveryTests(unittest.TestCase):
    def test_book_names_and_dates(self):
        for title in ("TheEconomist.2026.10.10.epub", "The Economist 2026-10-10", "te_2026.10.10",
                      "经济学人 2026年10月10日", "Economist_2026_10_10"):
            with self.subTest(title=title):
                self.assertEqual(dl.issue_from_book(title), ISSUE)
        for value in ("2026.02.30", "2026-10.10", "20261010", "../../2026-10-10", "2026-13-01"):
            with self.subTest(value=value), self.assertRaises(dl.DownloadError):
                dl.parse_date(value)
        for title in ("New Yorker 2026-10-10", "2026-10-10", "TheEconomist", "TheEconomist 2026.10.10 backup"):
            with self.assertRaises(dl.DownloadError):
                dl.issue_from_book(title)

    def test_current_and_archived_directories(self):
        for folder in (FOLDER, dl.ROOT + "/2026/te_2026.10.10"):
            with self.subTest(folder=folder):
                client = dl.GitHubSource()
                content = epub_bytes()
                row = remote_row(content, folder)
                listings = {dl.ROOT: [folder_row(FOLDER)]} if folder == FOLDER else {
                    dl.ROOT: [folder_row(dl.ROOT + "/2026")], dl.ROOT + "/2026": [folder_row(folder)]}
                listings[folder] = [row, {**row, "type": "file", "name": "other.mobi"}]
                with patch.object(client, "list_directory", side_effect=listings.__getitem__):
                    found = client.find_issue(ISSUE)
                self.assertEqual(found.url, row["download_url"])
                self.assertEqual(found.sha, row["sha"])

    def test_missing_issue_does_not_use_nearest_date(self):
        client = dl.GitHubSource()
        with patch.object(client, "list_directory", return_value=[folder_row(dl.ROOT + "/te_2026.10.03")]):
            with self.assertRaisesRegex(dl.DownloadError, "不会用其他日期"):
                client.find_issue(ISSUE)

    def test_duplicate_issue_directories_are_rejected(self):
        client = dl.GitHubSource()
        with patch.object(client, "list_directory", side_effect=[
                [folder_row(FOLDER), folder_row(dl.ROOT + "/2026")],
                [folder_row(dl.ROOT + "/2026/te_2026.10.10")]]):
            with self.assertRaisesRegex(dl.DownloadError, "多个目录"):
                client.find_issue(ISSUE)

    def test_multiple_epubs_or_wrong_date_are_rejected(self):
        content = epub_bytes()
        for rows in ([remote_row(content), remote_row(content, name="The Economist 2026-10-10.epub")],
                     [remote_row(content, name="TheEconomist.2026.10.03.epub")], []):
            with self.subTest(rows=len(rows)):
                client = dl.GitHubSource()
                with patch.object(client, "list_directory", side_effect=[[folder_row(FOLDER)], rows]):
                    with self.assertRaisesRegex(dl.DownloadError, "唯一"):
                        client.find_issue(ISSUE)

    def test_untrusted_url_size_and_hash_are_rejected(self):
        for change in ({"download_url": "https://example.com/file.epub"},
                       {"download_url": dl.RAW_BASE + "../another/file.epub"},
                       {"size": dl.MAX_DOWNLOAD + 1}, {"size": True}, {"sha": "invalid"}):
            client = dl.GitHubSource()
            row = {**remote_row(epub_bytes()), **change}
            with patch.object(client, "list_directory", side_effect=[[folder_row(FOLDER)], [row]]):
                with self.assertRaises(dl.DownloadError):
                    client.find_issue(ISSUE)

    def test_invalid_json_paths_or_truncated_listing_are_rejected(self):
        for content in (b"not json", b"{}", json.dumps([{"name": "../bad", "path": "../bad"}]).encode(),
                        json.dumps([folder_row(FOLDER)] * 1000).encode()):
            client = dl.GitHubSource()
            with patch.object(client, "open", return_value=io.BytesIO(content)):
                with self.assertRaises(dl.DownloadError):
                    client.list_directory(dl.ROOT)

    def test_request_does_not_send_weread_credentials(self):
        client = dl.GitHubSource()
        with patch.dict("os.environ", {"WEREAD_API_KEY": "wrk-private", "GITHUB_TOKEN": "private"}), \
                patch.object(client.opener, "open", return_value=io.BytesIO(b"[]")) as opening:
            client.list_directory(dl.ROOT)
        request = opening.call_args.args[0]
        self.assertIsNone(request.get_header("Authorization"))
        self.assertNotIn("private", str(request.headers))
        with self.assertRaises(dl.DownloadError):
            client.open("https://example.com/")
        self.assertIsNone(dl.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_rate_limits_timeouts_and_bounded_retries(self):
        client = dl.GitHubSource()
        errors = [urllib.error.HTTPError(dl.API_BASE, 503, "", {}, None), io.BytesIO(b"[]")]
        with patch.object(client.opener, "open", side_effect=errors), patch.object(dl.time, "sleep") as sleep:
            self.assertEqual(client.list_directory(dl.ROOT), [])
            sleep.assert_called_once_with(1)
        for failure in (urllib.error.HTTPError(dl.API_BASE, 403, "SECRET", {}, None),
                        urllib.error.HTTPError(dl.API_BASE, 429, "SECRET", {"Retry-After": "600"}, None),
                        urllib.error.HTTPError(dl.API_BASE, 404, "SECRET", {}, None),
                        urllib.error.URLError("SECRET"), TimeoutError("SECRET")):
            with patch.object(client.opener, "open", side_effect=failure), patch.object(dl.time, "sleep") as sleep:
                with self.assertRaises(dl.DownloadError) as caught:
                    client.list_directory(dl.ROOT)
                self.assertNotIn("SECRET", str(caught.exception))
                sleep.assert_not_called()


class DownloadTests(unittest.TestCase):
    def test_download_and_cached_reuse(self):
        content = epub_bytes()
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            directory = Path(temp) / "epubs"
            with patch.object(dl.GitHubSource, "open", side_effect=fake_http(content)) as http:
                path = dl.download_issue(ISSUE, directory)
                self.assertEqual(path.read_bytes(), content)
                self.assertEqual(http.call_count, 3)
            with patch.object(dl.GitHubSource, "open", side_effect=AssertionError("No network")):
                self.assertEqual(dl.download_issue(ISSUE, directory), path)
            self.assertEqual(list(directory.iterdir()), [path])

    def test_bad_downloads_leave_no_final_or_partial_file(self):
        content = epub_bytes()
        row = remote_row(content)
        variants = ((row, content[:-5]), (row, content + b"extra"),
                    ({**row, "sha": "0" * 40}, content),
                    (remote_row(b"<html>error</html>"), b"<html>error</html>"),
                    (remote_row(epub_bytes(date(2026, 10, 3))), epub_bytes(date(2026, 10, 3))))
        for metadata, received in variants:
            with self.subTest(size=len(received)), tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
                directory = Path(temp)
                remote = dl.RemoteEpub(metadata["path"], metadata["download_url"], metadata["size"], metadata["sha"])
                with patch.object(dl.GitHubSource, "find_issue", return_value=remote), \
                        patch.object(dl.GitHubSource, "open", return_value=io.BytesIO(received)):
                    with self.assertRaises(dl.DownloadError):
                        dl.download_issue(ISSUE, directory)
                self.assertEqual(list(directory.iterdir()), [])

    def test_interrupted_download_cleans_up(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            row = remote_row(epub_bytes())
            remote = dl.RemoteEpub(row["path"], row["download_url"], row["size"], row["sha"])
            with patch.object(dl.GitHubSource, "find_issue", return_value=remote), \
                    patch.object(dl.GitHubSource, "open", side_effect=dl.DownloadError("Connection lost")):
                with self.assertRaises(dl.DownloadError):
                    dl.download_issue(ISSUE, Path(temp))
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_existing_invalid_file_is_preserved_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / dl.issue_filename(ISSUE)
            with target.open("xb") as stream:
                stream.write(b"my original file")
            with patch.object(dl.GitHubSource, "open", side_effect=AssertionError("No network")):
                with self.assertRaises(dl.DownloadError):
                    dl.download_issue(ISSUE, Path(temp))
            self.assertEqual(target.read_bytes(), b"my original file")

    def test_concurrent_target_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            directory = Path(temp)
            publish = dl._publish

            def race(temporary, target):
                with target.open("xb") as stream:
                    stream.write(b"other writer")
                publish(temporary, target)

            with patch.object(dl.GitHubSource, "open", side_effect=fake_http(epub_bytes())), \
                    patch.object(dl, "_publish", side_effect=race):
                with self.assertRaisesRegex(dl.DownloadError, "未覆盖"):
                    dl.download_issue(ISSUE, directory)
            self.assertEqual((directory / dl.issue_filename(ISSUE)).read_bytes(), b"other writer")
            self.assertEqual(len(list(directory.iterdir())), 1)

    def test_download_cli_and_invalid_dates(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with patch.object(dl.GitHubSource, "open", side_effect=fake_http(epub_bytes())):
                self.assertEqual(dl.main(["--date", "2026-10-10", "--output-dir", temp]), 0)
            with patch.object(dl.GitHubSource, "open") as http:
                self.assertEqual(dl.main(["--date", "2026-02-30"]), 1)
                http.assert_not_called()

    def test_export_integration_download_then_full_article_and_template(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            root = Path(temp)
            bundle = {"book": {"title": "TheEconomist.2026.10.10", "bookId": "test"},
                      "chapters": [{"chapterUid": 1, "title": "Article"}],
                      "highlights": [{"chapterUid": 1, "markText": "A highlighted sentence"}]}
            json_path = root / "highlights.json"
            exporter.weread.write_text(json_path, json.dumps(bundle), False)
            with patch.object(dl.GitHubSource, "open", side_effect=fake_http(epub_bytes())), \
                    patch.object(exporter.weread, "load_api_key", side_effect=AssertionError("No key")):
                result = exporter.main(["--book", bundle["book"]["title"], "--highlights-json", str(json_path),
                                        "--download-epub", "--epub-dir", str(root / "epubs"),
                                        "--output-dir", str(root / "articles")])
            self.assertEqual(result, 0)
            manifest = next((root / "articles").rglob("matches.json"))
            report = json.loads(manifest.read_text(encoding="utf-8"))
            markdown = manifest.with_name(report["articles"][0]["markdown"]).read_text(encoding="utf-8")
            self.assertIn("followed by the full original article", markdown)
            self.assertIn("我的高亮", markdown)

    def test_local_first_and_explicit_path_and_opt_in(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            local = directory / dl.issue_filename(ISSUE)
            with local.open("xb") as stream:
                stream.write(epub_bytes())
            book = {"title": "TheEconomist.2026.10.10"}
            with patch.object(dl, "download_issue", side_effect=AssertionError("Must not download")):
                self.assertEqual(exporter.choose_epub(book, None, directory, download=True), local)
                self.assertEqual(exporter.choose_epub(book, local, directory / "other", download=True), local)
                with self.assertRaises(exporter.weread.WeReadError):
                    exporter.choose_epub(book, directory / "missing.epub", directory, download=True)
                with self.assertRaises(exporter.weread.WeReadError):
                    exporter.choose_epub(book, None, directory / "missing")

    def test_local_ambiguity_and_unsupported_book_do_not_download(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with patch.object(dl, "download_issue", side_effect=AssertionError("Must not download")):
                with self.assertRaises(dl.DownloadError):
                    exporter.choose_epub({"title": "Different Book"}, None, directory, download=True)
                for name in (dl.issue_filename(ISSUE), "TheEconomist-2026-10-10.epub"):
                    with (directory / name).open("xb") as stream:
                        stream.write(epub_bytes())
                with self.assertRaises(exporter.weread.WeReadError):
                    exporter.choose_epub({"title": "TheEconomist.2026.10.10"}, None, directory, download=True)


if __name__ == "__main__":
    unittest.main()
