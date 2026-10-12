"""Template syntax, scope and output contract tests; no network requests."""

import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from article_template import Template, TemplateError
import export_highlighted_articles as tool


class TemplateTests(unittest.TestCase):
    def test_inline_and_standalone_loops_keep_intended_spacing(self):
        context = {"highlights": [{"text": "A"}, {"text": "B"}]}
        inline = Template("{% for h in highlights %}{{loop.index}}:{{h.text}};{% endfor %}")
        self.assertEqual(inline.render(context), "1:A;2:B;")
        block = Template("# Notes\n\n{% for h in highlights %}\n{{h.text}}\n\n{% endfor %}\nEnd\n")
        self.assertEqual(block.render(context), "# Notes\n\nA\n\nB\n\nEnd\n")
        self.assertEqual(block.render({"highlights": []}), "# Notes\n\nEnd\n")

    def test_nested_loops_restore_parent_scope(self):
        template = Template("{% for group in groups %}{{loop.index}}:{% for h in group.items %}{{loop.index}}-{{h}};{% endfor %}{{loop.index}}/{{loop.length}} {% endfor %}")
        self.assertEqual(template.render({"groups": [{"items": ["A", "B"]}, {"items": ["C"]}]}),
                         "1:1-A;2-B;1/2 2:1-C;2/2 ")

    def test_body_and_highlights_are_not_interpreted_as_template_code(self):
        text = "Original {{missing}} and {% endfor %}."
        self.assertEqual(Template("{{content}}\n{% for h in highlights %}{{h.text}}{% endfor %}").render(
            {"content": text, "highlights": [{"text": text}]}), text + "\n" + text)

    def test_filters_preserve_multiline_quotes_and_json_strings(self):
        template = Template("{{text | blockquote}}\n{{title | json}}\n{{zero | markdown}}")
        result = template.render({"text": "A *word*\n\nB <tag>", "title": '书: "标题"', "zero": 0})
        self.assertTrue(result.startswith("> A \\*word\\*\n>\n> B &lt;tag&gt;\n"))
        self.assertIn(json.dumps('书: "标题"', ensure_ascii=False), result)
        self.assertTrue(result.endswith("\n0"))

    def test_invalid_syntax_is_rejected_with_line_number(self):
        for source in ("{{content", "{% for h in highlights %}\ntext", "{% endfor %}",
                       "{{book.title()}}", "{% if highlights %}", "{{title | unknown}}"):
            with self.subTest(source=source), self.assertRaisesRegex(TemplateError, "行"):
                Template(source)

    def test_unknown_variables_and_wrong_loop_types(self):
        with self.assertRaisesRegex(TemplateError, "book.typo"):
            Template("{{book.typo}}").render({"book": {"title": "x"}})
        with self.assertRaisesRegex(TemplateError, "不是列表"):
            Template("{% for h in highlights %}{{h}}{% endfor %}").render({"highlights": "text"})
        with self.assertRaisesRegex(TemplateError, "集合"):
            Template("{{highlights}}").render({"highlights": []})

    def test_template_context_uses_body_only_and_sorted_highlights(self):
        article = tool.Article("a.xhtml", "", "Article", [], [], "")
        match = {"article": article, "highlights": [
            {"markText": "Later", "range": "100-110"},
            {"markText": "First", "range": "10-20", "createTime": 0}]}
        context = tool.article_context({"book": {"bookId": "1", "title": "Book"}}, match, "BODY\n", Path("b.epub"))
        self.assertEqual(context["content"], "BODY")
        self.assertEqual(context["book"]["id"], "1")
        self.assertEqual(context["highlight_count"], 2)
        self.assertEqual([h["text"] for h in context["highlights"]], ["First", "Later"])
        self.assertEqual(context["highlights"][0]["created_at"], "")

    def test_custom_template_integration_and_invalid_template_no_output(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            root = Path(temp)
            epub_path = root / "b.epub"
            with zipfile.ZipFile(epub_path, "w") as archive:
                archive.writestr("a.xhtml", '<html><head><title>Title</title></head><body><h1>Title</h1><p>Body and highlighted words.</p><img src="image.png"/></body></html>')
                archive.writestr("image.png", b"fixture")
            bundle = {"book": {"bookId": "1", "title": "Book"},
                      "chapters": [{"chapterUid": 1, "title": "Title"}],
                      "highlights": [{"chapterUid": 1, "markText": "highlighted words", "range": "10-20"}]}
            bad_output = root / "bad"
            with self.assertRaises(TemplateError):
                tool.export_articles(bundle, epub_path, bad_output, True, False, Template("{{typo}}"))
            self.assertFalse(bad_output.exists())
            template = Template("# Custom: {{title}}\n\n{{content}}\n{% for h in highlights %}Item {{loop.index}}: {{h.text}}\n{% endfor %}")
            index, _, _ = tool.export_articles(bundle, epub_path, root / "good", True, False, template)
            report = json.loads(index.with_name("matches.json").read_text(encoding="utf-8"))
            result = index.with_name(report["articles"][0]["markdown"]).read_text(encoding="utf-8")
            self.assertTrue(result.startswith("# Custom: Title"))
            self.assertNotIn("# Title", result)
            self.assertIn("Body and highlighted words.", result)
            self.assertIn("Item 1: highlighted words", result)
            self.assertEqual(len(list(index.parent.rglob("*.png"))), 1)
            before = {p: p.read_bytes() for p in index.parent.rglob("*") if p.is_file()}
            with self.assertRaises(TemplateError):
                tool.export_articles(bundle, epub_path, root / "good", True, True, Template("{{wrong}}"))
            self.assertEqual(before, {p: p.read_bytes() for p in index.parent.rglob("*") if p.is_file()})

    def test_invalid_template_checked_before_loading_account(self):
        with patch.object(Path, "read_text", return_value="{% for h in highlights %}"), \
                patch.object(tool, "load_bundle") as load, redirect_stderr(io.StringIO()):
            self.assertEqual(tool.main(["--book", "Book", "--template", "custom.md"]), 1)
            load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
