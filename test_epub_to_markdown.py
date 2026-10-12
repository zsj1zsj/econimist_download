"""Offline conversion and asset naming regressions."""

import io
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from urllib.parse import unquote

import epub_to_markdown as tool


def render(source):
    blocks = tool.parse_html(source.encode("utf-8"))
    with zipfile.ZipFile(io.BytesIO(), "w") as archive:
        result = tool.render_markdown(blocks, "Unrelated title", archive,
                                      "a.xhtml", Path("article.md"), False, include_title=False)
    return blocks, result


class ConversionTests(unittest.TestCase):
    def test_breaks_and_inline_formatting_preserve_search_text(self):
        blocks, markdown = render('<p>First<br/>Second <strong>bold</strong> '
                                  '<em>italic</em> <a href="https://example.com/a b?q=(x)&amp;n=1">link</a></p>')
        self.assertEqual(blocks[0].text, "First Second bold italic link")
        self.assertIn("First  \nSecond **bold** *italic*", markdown)
        self.assertIn("[link](https://example.com/a%20b?q=%28x%29&n=1)", markdown)
        self.assertNotIn("example", blocks[0].text)

    def test_br_forms_void_tags_and_literal_markdown(self):
        blocks, markdown = render('<p>A<br>B<br />C<img/>D<input/>E &lt;tag&gt; *literal* snake_case</p><hr/><p>After</p>')
        self.assertIn("A  \nB  \nC", markdown)
        self.assertIn("&lt;tag&gt; \\*literal\\* snake\\_case", markdown)
        self.assertIn("\n\n---\n\nAfter", markdown)
        self.assertEqual(blocks[-1].kind, "p")

    def test_inline_spans_do_not_change_heading_kind(self):
        blocks, markdown = render('<h2><span>Heading <b>bold</b></span></h2>')
        self.assertEqual(blocks[0].kind, "h2")
        self.assertEqual(markdown, "### Heading **bold**\n")

    def test_whitespace_inside_emphasis_and_empty_inline_tags(self):
        _, markdown = render('<p>A<strong> bold <em> inner </em> </strong>B '
                             '<em> </em><a href="https://example.com"> link </a>C</p>')
        self.assertIn("A **bold *inner*** B", markdown)
        self.assertIn("[link](https://example.com) C", markdown)
        self.assertNotIn("** **", markdown)

    def test_inline_formatting_survives_image_split(self):
        blocks, markdown = render('<p><strong>Before<img src="a.png"/>After</strong> end</p>')
        self.assertEqual([b.text for b in blocks if b.text], ["Before", "After end"])
        self.assertEqual(markdown, "**Before**\n\n**After** end\n")

    def test_unsafe_link_retains_text_not_destination(self):
        blocks, markdown = render('<p><a href="javascript:alert(1)">Text</a> '
                                  '<a href="#footnote">Note</a></p>')
        self.assertEqual(blocks[0].text, "Text Note")
        self.assertEqual(markdown, "Text [Note](#footnote)\n")

    def test_ordered_nested_lists_start_value_and_continuations(self):
        _, markdown = render('<ol start="3"><li><p>First</p><p>Continued</p>'
                             '<ul><li>Nested</li></ul></li><li value="7">Second</li>'
                             '<li>Third</li></ol><p>Outside</p>')
        self.assertIn("3. First", markdown)
        self.assertIn("\n   Continued", markdown)
        self.assertIn("\n   - Nested", markdown)
        self.assertIn("\n7. Second", markdown)
        self.assertIn("\n8. Third\n\nOutside", markdown)

    def test_quotes_with_paragraphs_and_breaks(self):
        _, markdown = render('<blockquote><p>A<br/>B <em>word</em></p></blockquote>')
        self.assertEqual(markdown, "> A  \n> B *word*\n")

    def test_skipped_content_does_not_leak(self):
        _, markdown = render('<p>Before</p><nav><p>Hidden</p><script>Hidden too</script></nav><p>After</p>')
        self.assertEqual(markdown, "Before\n\nAfter\n")

    def test_images_with_same_basename_are_distinct_and_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "article.md"
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("one/chart.png", b"first")
                archive.writestr("two/chart.png", b"second")
            blocks = tool.parse_html(b'<img src="one/chart.png"/><img src="two/chart.png"/><img src="one/chart.png"/>')
            with zipfile.ZipFile(buffer) as archive:
                preview = tool.render_markdown(blocks, "Title", archive, "a.xhtml", output,
                                               True, write_images=False)
                self.assertFalse(output.with_name("article_assets").exists())
                actual = tool.render_markdown(blocks, "Title", archive, "a.xhtml", output, True)
                reversed_result = tool.render_markdown(list(reversed(blocks[:2])), "Title", archive,
                                                       "a.xhtml", output, True, write_images=False)
            self.assertEqual(preview, actual)
            links = re.findall(r'!\[[^\]]*\]\(([^)]+)\)', actual)
            self.assertNotEqual(links[0], links[1])
            self.assertEqual(links[0], links[2])
            self.assertEqual([(output.parent / unquote(link)).read_bytes() for link in links],
                             [b"first", b"second", b"first"])
            self.assertEqual(re.findall(r'!\[[^\]]*\]\(([^)]+)\)', reversed_result), list(reversed(links[:2])))
            self.assertEqual(len(list(output.parent.rglob("*.png"))), 2)

    def test_asset_names_are_safe_on_windows_and_case_sensitive(self):
        for member in ("foo/CON.png", "bar/bad:name?.png", "long/" + "a" * 240 + ".png"):
            name = tool.safe_image_name(member, 1)
            self.assertNotRegex(name, r'[<>:"/\\|?*]')
            self.assertLess(len(name), 150)
        self.assertNotEqual(tool.safe_image_name("A/chart.png", 1), tool.safe_image_name("a/chart.png", 1))


if __name__ == "__main__":
    unittest.main()
