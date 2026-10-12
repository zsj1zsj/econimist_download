"""Article matching tests, entirely offline."""

import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import export_highlighted_articles as tool


def epub_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    buffer.seek(0)
    return zipfile.ZipFile(buffer)


def xhtml(title, body):
    return f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{title}</title></head><body>{body}</body></html>'


class ArticleTests(unittest.TestCase):
    def test_title_and_all_highlights_select_body_not_contents_page(self):
        files = {
            "article.xhtml": xhtml("China’s oil", "<h1>China’s oil</h1><p>This is a long highlighted sentence.</p>"),
            "toc.xhtml": xhtml("Contents", '<h1>Contents</h1><p><a href="article.xhtml">China’s oil</a></p>')}
        with epub_zip(files) as archive:
            articles = tool.index_epub(archive)
        article, method = tool.find_article(articles, "China's oil", [{"markText": "This is a long highlighted sentence."}])
        self.assertEqual(article.member, "article.xhtml")
        self.assertEqual(method, "title+highlights")
        with self.assertRaises(tool.weread.WeReadError):
            tool.find_article(articles, "China's oil", [{"markText": "Not in this edition"}])

    def test_duplicate_titles_disambiguated_by_content(self):
        with epub_zip({"a.xhtml": xhtml("Title", "<h1>Title</h1><p>First article text.</p>"),
                       "b.xhtml": xhtml("Title", "<h1>Title</h1><p>Second article text.</p>")}) as archive:
            articles = tool.index_epub(archive)
        self.assertEqual(tool.find_article(articles, "Title", [{"markText": "Second article text."}])[0].member, "b.xhtml")
        with self.assertRaisesRegex(tool.weread.WeReadError, "多个"):
            tool.find_article(articles, "Title", [{"markText": "article text"}])

    def test_fallback_to_long_text_and_reject_short_text(self):
        with epub_zip({"a.xhtml": xhtml("Renamed", "<h1>Renamed</h1><p>Long original highlight identifying one article.</p>")}) as archive:
            articles = tool.index_epub(archive)
        self.assertEqual(tool.find_article(articles, "Old title", [{"markText": "Long original highlight identifying one article."}])[1], "highlights")
        with self.assertRaisesRegex(tool.weread.WeReadError, "过短"):
            tool.find_article(articles, "Old title", [{"markText": "one"}])

    def test_nav_fragments_do_not_export_adjacent_article(self):
        nav = '<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><a href="chapter.xhtml#a">Alpha</a><a href="chapter.xhtml#b">Beta</a></nav>'
        with epub_zip({"nav.xhtml": xhtml("TOC", nav), "chapter.xhtml": xhtml("Book", '<section><h1 id="a">Alpha</h1><p>First highlighted paragraph.</p></section><section><h1 id="b">Beta</h1><p>Second highlighted paragraph.</p></section>')}) as archive:
            articles = tool.index_epub(archive)
        self.assertEqual(len(articles), 2)
        article, _ = tool.find_article(articles, "Alpha", [{"markText": "First highlighted paragraph."}])
        text = " ".join(block.text for block in article.blocks)
        self.assertNotIn("Second", text)
        self.assertNotIn("Beta", text)

    def test_ncx_and_relative_encoded_targets(self):
        ncx = '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap><navPoint><navLabel><text>Real title</text></navLabel><content src="../Text/a%20b.xhtml"/></navPoint></navMap></ncx>'
        with epub_zip({"Nav/toc.ncx": ncx, "Text/a b.xhtml": xhtml("Other", "<h1>Other</h1><p>Unique contents</p>")}) as archive:
            articles = tool.index_epub(archive)
        self.assertEqual(articles[0].title, "Real title")
        self.assertEqual(tool.find_article(articles, "Real title", [{"markText": "Unique contents"}])[1], "title+highlights")

    def test_ncx_section_and_article_share_href(self):
        ncx = '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap><navPoint><navLabel><text>China</text></navLabel><content src="article.xhtml"/><navPoint><navLabel><text>China’s oil tactics</text></navLabel><content src="article.xhtml"/></navPoint></navPoint></navMap></ncx>'
        with epub_zip({"toc.ncx": ncx, "article.xhtml": xhtml("China’s oil tactics", "<h1>China’s oil tactics</h1><p>Article contents.</p>")}) as archive:
            articles = tool.index_epub(archive)
        self.assertEqual(articles[0].title, "China’s oil tactics")

    def test_nested_navigation_keeps_article_subsections(self):
        body = ('<h1 id="a">Article</h1><p>Intro highlighted paragraph.</p>'
                '<h2 id="detail">Details</h2><p>The second half still belongs to Article.</p>'
                '<h3 id="deep">Deep</h3><p>Nested detail.</p>'
                '<h1 id="b">Next</h1><p>Another article.</p>')
        nav = ('<nav role="doc-toc"><ol><li><a href="chapter.xhtml#a">Article</a>'
               '<ol><li><a href="chapter.xhtml#detail">Details</a><ol><li>'
               '<a href="chapter.xhtml#deep">Deep</a></li></ol></li></ol></li>'
               '<li><a href="chapter.xhtml#b">Next</a></li></ol></nav>')
        ncx = ('<ncx><navMap><navPoint><navLabel><text>Article</text></navLabel><content src="chapter.xhtml#a"/>'
               '<navPoint><navLabel><text>Details</text></navLabel><content src="chapter.xhtml#detail"/>'
               '<navPoint><navLabel><text>Deep</text></navLabel><content src="chapter.xhtml#deep"/>'
               '</navPoint></navPoint></navPoint><navPoint><navLabel><text>Next</text></navLabel>'
               '<content src="chapter.xhtml#b"/></navPoint></navMap></ncx>')
        for toc_name, toc in (("nav.xhtml", xhtml("TOC", nav)), ("toc.ncx", ncx)):
            with self.subTest(toc=toc_name), epub_zip({toc_name: toc, "chapter.xhtml": xhtml("Book", body)}) as archive:
                articles = tool.index_epub(archive)
                article, _ = tool.find_article(articles, "Article", [{"markText": "Intro highlighted paragraph."}])
                text = " ".join(b.text for b in article.blocks)
                self.assertIn("The second half still belongs to Article.", text)
                self.assertIn("Nested detail.", text)
                self.assertNotIn("Another article.", text)
                detail, _ = tool.find_article(articles, "Details", [{"markText": "Nested detail."}])
                self.assertNotIn("Intro", " ".join(b.text for b in detail.blocks))

    def test_parent_target_without_fragment_and_heading_span(self):
        nav = ('<nav role="doc-toc"><ol><li><a href="chapter.xhtml">Article</a><ol>'
               '<li><a href="chapter.xhtml#detail">Details</a></li></ol></li>'
               '<li><a href="chapter.xhtml#b">Next</a></li></ol></nav>')
        body = ('<h1>Article</h1><p>Intro.</p><h2><span id="detail">Details</span></h2>'
                '<p>Detail text.</p><h1 id="b">Next</h1><p>Not ours.</p>')
        with epub_zip({"nav.xhtml": xhtml("TOC", nav), "chapter.xhtml": xhtml("Book", body)}) as archive:
            articles = tool.index_epub(archive)
        article, _ = tool.find_article(articles, "Article", [{"markText": "Intro."}])
        self.assertIn("Detail text.", " ".join(b.text for b in article.blocks))
        self.assertNotIn("Not ours.", " ".join(b.text for b in article.blocks))
        detail, _ = tool.find_article(articles, "Details", [{"markText": "Detail text."}])
        self.assertEqual(detail.blocks[0].kind, "h2")

    def test_shared_parent_and_first_article_target_does_not_include_sibling(self):
        nav = ('<nav role="doc-toc"><ol><li><a href="chapter.xhtml#a">Section</a><ol>'
               '<li><a href="chapter.xhtml#a">Alpha</a></li><li><a href="chapter.xhtml#b">Beta</a>'
               '</li></ol></li></ol></nav>')
        with epub_zip({"nav.xhtml": xhtml("TOC", nav), "chapter.xhtml": xhtml("Book",
                       '<h1 id="a">Alpha</h1><p>First.</p><h1 id="b">Beta</h1><p>Second.</p>')}) as archive:
            articles = tool.index_epub(archive)
        article, _ = tool.find_article(articles, "Alpha", [{"markText": "First."}])
        self.assertNotIn("Second.", " ".join(b.text for b in article.blocks))

    def test_navigation_takes_precedence_over_flat_ncx(self):
        nav = ('<nav role="doc-toc"><ol><li><a href="chapter.xhtml#a">Alpha</a><ol><li>'
               '<a href="chapter.xhtml#detail">Detail</a></li></ol></li></ol></nav>')
        ncx = ('<ncx><navMap><navPoint><navLabel><text>Alpha</text></navLabel><content src="chapter.xhtml#a"/>'
               '</navPoint><navPoint><navLabel><text>Detail</text></navLabel><content src="chapter.xhtml#detail"/>'
               '</navPoint></navMap></ncx>')
        with epub_zip({"toc.ncx": ncx, "nav.xhtml": xhtml("TOC", nav), "chapter.xhtml": xhtml("Book",
                       '<h1 id="a">Alpha</h1><p>First.</p><h2 id="detail">Detail</h2><p>Second.</p>')}) as archive:
            articles = tool.index_epub(archive)
        article, _ = tool.find_article(articles, "Alpha", [{"markText": "First."}])
        self.assertIn("Second.", " ".join(b.text for b in article.blocks))

    def test_grouping_only_exports_highlighted_articles_and_reports_unmatched(self):
        with epub_zip({"a.xhtml": xhtml("Alpha", "<h1>Alpha</h1><p>First text. Second text.</p>"),
                       "b.xhtml": xhtml("Beta", "<h1>Beta</h1><p>No highlights.</p>")}) as archive:
            articles = tool.index_epub(archive)
        bundle = {"chapters": [{"chapterUid": 1, "title": "Alpha"}, {"chapterUid": 2, "title": "Missing"}],
                  "highlights": [{"chapterUid": "1", "markText": "First text."},
                                 {"chapterUid": 1, "markText": "Second text."},
                                 {"chapterUid": 2, "markText": "Absent text"}]}
        matches, errors = tool.match_highlighted_articles(bundle, articles)
        self.assertEqual(len(matches), 1)
        self.assertEqual(len(matches[0]["highlights"]), 2)
        self.assertEqual(len(errors), 1)

    def test_offline_book_identity_must_match(self):
        args = tool.argparse.Namespace(highlights_json=Path("test.json"), book="Different book", book_id=None)
        with patch.object(Path, "read_text", return_value=json.dumps({"book": {"title": "Original", "bookId": "1"}})):
            with self.assertRaisesRegex(tool.weread.WeReadError, "不一致"):
                tool.load_bundle(args)

    def test_live_book_selection_reuses_credentials_and_client(self):
        args = tool.argparse.Namespace(highlights_json=None, book="Example.epub", book_id=None,
                                      config=None, ask_key=False, timeout=30)
        book = {"title": "Example", "bookId": "CB_x"}
        with patch.object(tool.weread, "load_api_key", return_value="wrk-fake"), \
                patch.object(tool.weread, "WeReadClient") as client:
            client.return_value.notebooks.return_value = [{"book": book}]
            client.return_value.fetch_book.return_value = {"book": book, "chapters": [], "highlights": []}
            tool.load_bundle(args)
            client.return_value.fetch_book.assert_called_once_with(book)

    def test_export_images_full_article_and_overwrite_protection(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            root = Path(temp)
            path = root / "Example.epub"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Text/a.xhtml", xhtml("Article title", '<h1>Article title</h1><p>Paragraph with highlight.</p><p>Unhighlighted paragraph is also exported.</p><img src="../Images/pic%20one.png" alt="Sample"/>'))
                archive.writestr("Images/pic one.png", b"test-image")
            bundle = {"book": {"title": "Example", "bookId": "1"},
                      "chapters": [{"chapterUid": 1, "title": "Article title"}],
                      "highlights": [{"chapterUid": 1, "markText": "highlight", "range": "5-10"}]}
            index, count, failed = tool.export_articles(bundle, path, root / "out", True, False)
            self.assertEqual((count, failed), (1, 0))
            report = json.loads(index.with_name("matches.json").read_text(encoding="utf-8"))
            markdown = index.with_name(report["articles"][0]["markdown"]).read_text(encoding="utf-8")
            self.assertIn("Unhighlighted paragraph", markdown)
            self.assertIn("我的高亮", markdown)
            self.assertIn(tool.epub.safe_image_name("Images/pic one.png", 1), markdown)
            self.assertEqual(len(list(index.parent.rglob("*.png"))), 1)
            with self.assertRaisesRegex(tool.weread.WeReadError, "已存在"):
                tool.export_articles(bundle, path, root / "out", True, False)

    def test_stable_names_when_highlights_reorder_or_articles_are_added(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            root = Path(temp)
            path = root / "Book.epub"
            with zipfile.ZipFile(path, "w") as archive:
                for title in ("Alpha", "Beta"):
                    archive.writestr(title + ".xhtml", xhtml(title, f"<h1>{title}</h1><p>{title} contents.</p>"))
            bundle = {"book": {"title": "Book", "bookId": "1"},
                      "chapters": [{"chapterUid": i, "title": title} for i, title in enumerate(("Alpha", "Beta"))],
                      "highlights": [{"chapterUid": 1, "markText": "Beta contents."}]}

            def export(directory, force=False):
                index, _, _ = tool.export_articles(bundle, path, root / directory, False, force)
                report = json.loads(index.with_name("matches.json").read_text(encoding="utf-8"))
                return index, {a["epub_source"]: a["markdown"] for a in report["articles"]}

            index, first = export("out")
            bundle["highlights"].insert(0, {"chapterUid": 0, "markText": "Alpha contents."})
            _, both = export("out", True)
            self.assertEqual(first["Beta.xhtml"], both["Beta.xhtml"])
            bundle["highlights"].reverse()
            _, reversed_names = export("out", True)
            _, fresh = export("fresh")
            self.assertEqual(both, reversed_names)
            self.assertEqual(both, fresh)
            self.assertEqual(list(both), ["Alpha.xhtml", "Beta.xhtml"])
            self.assertEqual(len(list(index.parent.glob("*.md"))), 3)

    def test_legacy_manifest_names_are_reused_and_unsafe_names_rejected(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            root = Path(temp)
            path = root / "Book.epub"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("a.xhtml", xhtml("Alpha", "<h1>Alpha</h1><p>Contents.</p>"))
            bundle = {"book": {"title": "Book", "bookId": "1"},
                      "chapters": [{"chapterUid": 1, "title": "Alpha"}],
                      "highlights": [{"chapterUid": 1, "markText": "Contents."}]}
            index, _, _ = tool.export_articles(bundle, path, root / "out", False, False)
            manifest = index.with_name("matches.json")
            report = json.loads(manifest.read_text(encoding="utf-8"))
            original = index.with_name(report["articles"][0]["markdown"])
            legacy_name = "001-Alpha--article-1.md"
            original.rename(index.with_name(legacy_name))
            report["articles"][0]["markdown"] = legacy_name
            tool.weread.write_text(manifest, json.dumps(report), True)
            tool.export_articles(bundle, path, root / "out", False, True)
            current = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(current["articles"][0]["markdown"], legacy_name)
            self.assertEqual(len(list(index.parent.glob("*.md"))), 2)
            for unsafe in ("../outside.md", "..\\outside.md", "index.md", "C:outside.md"):
                report["articles"][0]["markdown"] = unsafe
                tool.weread.write_text(manifest, json.dumps(report), True)
                before = index.with_name(legacy_name).read_bytes()
                with self.assertRaisesRegex(tool.weread.WeReadError, "matches.json"):
                    tool.export_articles(bundle, path, root / "out", False, True)
                self.assertEqual(before, index.with_name(legacy_name).read_bytes())


if __name__ == "__main__":
    unittest.main()
