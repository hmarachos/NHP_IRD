from unittest import TestCase

from app.services.document_highlight import _matches, _select_lines, _tokens, _unit_numbers


class CitationHighlightTests(TestCase):
    def line(self, text, y):
        return {"rect": [0, y, 100, y+10], "words": _tokens(text), "text": text.split()}

    def test_short_temperature_value_beats_unrelated_matching_words(self):
        value = self.line("+20 °С.", 0)
        other = self.line("Датчик температуры воздуха установить в торговом зале", 100)
        paragraphs = {1: {"lines": {(1,1,1): value}}, 2: {"lines": {(2,1,1): other}}}
        answer = "Летом температура воздуха в торговом зале не должна превышать +20 °C. [1][2]"
        query = _tokens(answer) | _unit_numbers(answer)
        self.assertEqual(_select_lines(paragraphs, query), [value])

    def test_numeric_anchors_require_exact_value_not_prefix(self):
        self.assertFalse(_matches("@number:20", {"@number:200"}))
        self.assertTrue(_matches("@number:98.8", {"@number:98.8"}))
        self.assertEqual(_unit_numbers("+20 °C, 98,8 кВт"), {"@number:20", "@number:98.8"})

    def test_page_number_is_not_treated_as_temperature(self):
        paragraphs = {1: {"lines": {(1,1,1): self.line("20", 900)}}}
        answer = "Температура не выше +20 °C"
        self.assertEqual(_select_lines(paragraphs, _tokens(answer) | _unit_numbers(answer)), [])

    def test_missing_quantity_does_not_invent_highlight(self):
        paragraphs = {1: {"lines": {(1,1,1): self.line("Входные двери здания", 100)}}}
        query = _tokens("Температура не выше +20 °C") | _unit_numbers("Температура не выше +20 °C")
        self.assertEqual(_select_lines(paragraphs, query), [])


from django.test import SimpleTestCase, RequestFactory, override_settings


class HighlightCacheVersionTests(SimpleTestCase):
    def test_legacy_wrong_highlight_is_not_served_after_algorithm_change(self):
        import hashlib
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from migration_portal.views import _viewer_highlight_response

        with tempfile.TemporaryDirectory() as folder, override_settings(BASE_DIR=Path(folder)):
            path = Path(folder) / "source.pdf"
            path.write_bytes(b"fixture")
            stat = path.stat()
            cache = Path(folder) / "instance" / "preview-cache" / f"fixture-{stat.st_mtime_ns}-{stat.st_size}"
            cache.mkdir(parents=True)
            query = "Температура +20 °C"
            legacy_key = hashlib.sha256(f"0\0{query}".encode()).hexdigest()[:24]
            (cache / f"highlight-{legacy_key}.json").write_text(json.dumps({"rects": [{"y": 0.9}]}))
            request = RequestFactory().post("/", {"q": query})
            expected = [{"x": 0.4, "y": 0.02, "width": 0.1, "height": 0.02}]
            with patch("app.services.document_highlight.highlight_paragraph", return_value=expected) as highlight:
                first = _viewer_highlight_response(request, path, "fixture", 0)
                second = _viewer_highlight_response(request, path, "fixture", 0)
            self.assertEqual(json.loads(first.content)["rects"], expected)
            self.assertEqual(json.loads(second.content)["rects"], expected)
            self.assertEqual(highlight.call_count, 1)
