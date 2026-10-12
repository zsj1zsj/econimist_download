"""Offline contract tests; no account credentials or network are used."""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import weread_highlights as wr


def response(data):
    return io.BytesIO(json.dumps(data).encode())


class WeReadTests(unittest.TestCase):
    def client(self):
        client = wr.WeReadClient("wrk-test-only-not-real")
        client.opener = Mock()
        return client

    def test_gateway_payload_and_error_redaction(self):
        client = self.client()
        client.opener.open.return_value = response({"books": []})
        client.call("/user/notebooks", count=20, lastSort=123)
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, wr.API_URL)
        self.assertEqual(json.loads(request.data), {"api_name": "/user/notebooks",
                         "skill_version": "1.0.4", "count": 20, "lastSort": 123})
        self.assertEqual(request.get_header("Authorization"), "Bearer wrk-test-only-not-real")
        client.opener.open.return_value = response({"errcode": -1, "errmsg": client.api_key})
        with self.assertRaises(wr.WeReadError) as error:
            client.call("/user/notebooks")
        self.assertNotIn(client.api_key, str(error.exception))

    def test_notebooks_all_pages_and_id_in_outer_record(self):
        client = self.client()
        client.call = Mock(side_effect=[
            {"hasMore": 1, "books": [{"bookId": "a", "book": {"title": "甲"}, "sort": 9}]},
            {"hasMore": "0", "books": [{"bookId": "a", "book": {"title": "甲"}, "sort": 9},
                                         {"bookId": "CB_b", "book": {"title": "乙"}, "sort": 8}]}])
        books = client.notebooks()
        self.assertEqual([row["book"]["bookId"] for row in books], ["a", "CB_b"])
        self.assertEqual(client.call.call_args.kwargs, {"count": 20, "lastSort": 9})

    def test_stalled_notebook_pagination_fails(self):
        client = self.client()
        client.call = Mock(return_value={"hasMore": 1, "books": [
            {"bookId": "a", "book": {}, "sort": 9}]})
        with self.assertRaisesRegex(wr.WeReadError, "分页"):
            client.notebooks()
        self.assertEqual(client.call.call_count, 2)

    def test_review_pagination_preserves_distinct_notes(self):
        client = self.client()
        r1 = {"review": {"reviewId": "r1", "content": "一"}}
        r2 = {"reviewId": "r2", "review": {"content": "二"}}
        client.call = Mock(side_effect=[{"hasMore": 1, "synckey": 77, "reviews": [r1]},
                                       {"hasMore": 0, "reviews": [r1, r2]}])
        reviews = client.reviews("CB_test")
        self.assertEqual([r["reviewId"] for r in reviews], ["r1", "r2"])
        self.assertEqual(client.call.call_args.kwargs, {"bookid": "CB_test", "count": 20, "synckey": 77})

    def test_fetch_filters_bookmarks_but_retains_distinct_highlights(self):
        client = self.client()
        client.call = Mock(return_value={"updated": [
            {"bookmarkId": "h1", "type": 1, "markText": "重复原文", "range": "0-4"},
            {"bookmarkId": "h1", "type": 1, "markText": "重复原文", "range": "0-4"},
            {"bookmarkId": "h2", "type": 1, "markText": "重复原文", "range": "10-14"},
            {"bookmarkId": "position", "type": 0}], "chapters": []})
        bundle = client.fetch_book({"bookId": "b", "title": "书"})
        self.assertEqual(len(bundle["highlights"]), 2)
        self.assertEqual(client.call.call_count, 1)

    def test_bad_response_is_not_treated_as_no_highlights(self):
        client = self.client()
        client.call = Mock(return_value={"unexpected": []})
        with self.assertRaisesRegex(wr.WeReadError, "updated"):
            client.fetch_book({"bookId": "b", "title": "书"})

    def test_same_title_requires_id_and_exact_precedes_partial(self):
        books = [{"book": {"bookId": "a", "title": "Book"}},
                 {"book": {"bookId": "b", "title": "Book Two"}}]
        self.assertEqual(wr.select_book(books, "  BOOK ")["bookId"], "a")
        with self.assertRaisesRegex(wr.WeReadError, "--book-id"):
            wr.select_book(books, "Boo")
        books[1]["book"]["title"] = "Book"
        with self.assertRaisesRegex(wr.WeReadError, "--book-id"):
            wr.select_book(books, "Book")

    def test_chapter_order_numeric_range_and_multiline(self):
        bundle = wr.demo_bundle()
        bundle["chapters"] = [{"chapterUid": 20, "chapterIdx": 1, "title": "先"},
                              {"chapterUid": 10, "chapterIdx": 2, "title": "后"}]
        bundle["highlights"] = [
            {"chapterUid": 10, "range": "100-110", "markText": "晚"},
            {"chapterUid": "10", "range": "20-30", "markText": "早\n\n第二行 <script>"},
            {"chapterUid": 20, "range": "0-1", "markText": "第一章内容"}]
        markdown = wr.render_markdown(bundle)
        self.assertLess(markdown.index("## 先"), markdown.index("## 后"))
        self.assertLess(markdown.index("> 早"), markdown.index("> 晚"))
        self.assertIn("> 早\n>\n> 第二行 &lt;script&gt;", markdown)

    def test_safe_filename_and_different_editions(self):
        book = {"title": '../CON: * " | ? / \\ <>', "bookId": "../../bad"}
        filename = wr.safe_filename(book)
        self.assertEqual(Path(filename).name, filename)
        self.assertFalse(any(char in filename for char in '<>:"/\\|?*'))
        self.assertNotEqual(wr.safe_filename({"title": "Same", "bookId": "1"}),
                            wr.safe_filename({"title": "Same", "bookId": "2"}))

    def test_existing_files_preserved_and_force_updates(self):
        bundle = wr.demo_bundle()
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "export.md"
            wr.save_bundle(bundle, output, True, False)
            first = output.read_bytes()
            bundle["book"]["title"] = "Changed"
            with self.assertRaises(wr.WeReadError):
                wr.save_bundle(bundle, output, True, False)
            self.assertEqual(output.read_bytes(), first)
            wr.save_bundle(bundle, output, True, True)
            self.assertIn("Changed", output.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))["book"]["title"], "Changed")

    def test_auth_failure_is_not_retried_and_rate_limit_is(self):
        client = self.client()
        client.opener.open.side_effect = urllib.error.HTTPError(wr.API_URL, 401, "auth", {}, None)
        with self.assertRaisesRegex(wr.WeReadError, "认证失败"):
            client.call("/user/notebooks")
        self.assertEqual(client.opener.open.call_count, 1)
        client.opener.open.reset_mock()
        client.opener.open.side_effect = [urllib.error.HTTPError(wr.API_URL, 429, "rate", {"Retry-After": "1"}, None),
                                          response({"books": []})]
        with patch.object(wr.time, "sleep") as sleep:
            self.assertEqual(client.call("/user/notebooks"), {"books": []})
            sleep.assert_called_once_with(1)

    def test_cli_demo_and_missing_key(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            self.assertEqual(wr.main(["--demo", "--json", "--output-dir", temp]), 0)
            self.assertEqual(len(list(Path(temp).glob("*.md"))), 1)
        with patch.object(wr, "load_api_key", return_value=""), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(wr.main(["--list"]), 1)
            self.assertIn("--ask-key", error.getvalue())

    def test_config_key_priority(self):
        with patch.dict(os.environ, {"WEREAD_API_KEY": ""}), \
                patch.object(Path, "read_text", return_value='{"api_key": " wrk-config "}') as read:
            self.assertEqual(wr.load_api_key(), "wrk-config")
            read.assert_called_once_with(encoding="utf-8-sig")
        with patch.dict(os.environ, {"WEREAD_API_KEY": " wrk-env "}), \
                patch.object(Path, "read_text") as read, \
                patch.object(wr.getpass, "getpass", return_value=" wrk-prompt "):
            self.assertEqual(wr.load_api_key(), "wrk-env")
            self.assertEqual(wr.load_api_key(ask_key=True), "wrk-prompt")
            read.assert_not_called()

    def test_missing_and_invalid_config_errors_hide_contents(self):
        with patch.dict(os.environ, {"WEREAD_API_KEY": ""}):
            with patch.object(Path, "read_text", side_effect=FileNotFoundError):
                self.assertEqual(wr.load_api_key(), "")
                with self.assertRaisesRegex(wr.WeReadError, "不存在"):
                    wr.load_api_key(config=Path("missing.json"))
            for content in ['{"api_key": "wrk-sensitive",}', '[]', '{"api_key": 123}']:
                with self.subTest(content=content), patch.object(Path, "read_text", return_value=content):
                    with self.assertRaises(wr.WeReadError) as error:
                        wr.load_api_key()
                    self.assertNotIn("wrk-sensitive", str(error.exception))

    def test_cli_uses_custom_config(self):
        with patch.dict(os.environ, {"WEREAD_API_KEY": ""}), \
                patch.object(Path, "read_text", return_value='{"api_key": "wrk-test-only"}') as read, \
                patch.object(wr, "WeReadClient") as client, redirect_stdout(io.StringIO()):
            client.return_value.notebooks.return_value = []
            self.assertEqual(wr.main(["--list", "--config", "custom.json"]), 0)
            client.assert_called_once_with("wrk-test-only", 30)
            read.assert_called_once_with(encoding="utf-8-sig")


if __name__ == "__main__":
    unittest.main()
