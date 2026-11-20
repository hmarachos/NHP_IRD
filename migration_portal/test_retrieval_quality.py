import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, SimpleTestCase

from app.services.document_pages import restore_page_markers
from app.services.document_preparation import run_pending_preparations
from app.services.project_qa import retrieve_project_sources
from migration_portal.models import Correspondence, Document, Project


class PageMappingTests(SimpleTestCase):
    def pdf(self, texts):
        return SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda text=text: text) for text in texts])

    def test_exact_embedded_prefix_recovers_pages_without_importing_appendices(self):
        raw = "--- letterhead ---\nRecognized header\n--- document body ---\nFirst page\nSecond page"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "letter.pdf"
            path.touch()
            with patch("pypdf.PdfReader", return_value=self.pdf(["First page", "Second page", "Unrelated appendix"])):
                mapped, mode = restore_page_markers(raw, path)
        self.assertEqual(mode, "embedded")
        self.assertIn("--- page 1 ---\n--- letterhead ---", mapped)
        self.assertIn("--- page 2 ---\nSecond page", mapped)
        self.assertNotIn("appendix", mapped)

    def test_partial_page_and_mismatched_text_never_get_guessed_page_numbers(self):
        for raw in ["First page Second", "First page altered text"]:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "letter.pdf"
                path.touch()
                with patch("pypdf.PdfReader", return_value=self.pdf(["First page", "Second page"])):
                    mapped, mode = restore_page_markers(raw, path)
            self.assertEqual((mapped, mode), (raw, "unavailable"))

    def test_single_page_scan_has_unambiguous_page_without_ocr(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "scan.pdf"
            path.touch()
            with patch("pypdf.PdfReader", return_value=self.pdf([""])):
                mapped, mode = restore_page_markers("Existing OCR", path)
        self.assertEqual((mapped, mode), ("--- page 1 ---\nExisting OCR", "single_page"))

    def test_supplemental_letterhead_stays_on_first_page(self):
        raw = "--- letterhead ---\nHeader OCR\n--- document body ---\n--- page 1 ---\nBody\n--- page 2 ---\nConclusion"
        mapped, mode = restore_page_markers(raw, "/missing.pdf")
        self.assertEqual(mode, "original")
        self.assertTrue(mapped.startswith("--- page 1 ---\n--- letterhead ---"))
        self.assertEqual(mapped.count("--- page 1 ---"), 1)
        self.assertIn("--- page 2 ---\nConclusion", mapped)

    def test_existing_mapping_and_missing_source_preserve_original_text(self):
        raw = "--- page 4 ---\nRecognized text"
        self.assertEqual(restore_page_markers(raw, "/missing.pdf"), (raw, "original"))
        self.assertEqual(restore_page_markers("Text", "/missing.pdf"), ("Text", "unavailable"))


class RetrievalQualityTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Тест", contract_number="TEST")

    def document(self, title, text):
        return Document.objects.create(project=self.project, contract_number="TEST", object_name="Тест", original_filename="test.pdf", stored_filename="test.pdf", working_title=title, raw_text=text)

    def test_requirement_continuation_is_cited_on_its_own_page(self):
        self.document("Задание на проектирование", "--- page 1 ---\nТемпература торгового зала летом не должна превышать\n1\n--- page 2 ---\n+20°С.\nСледующее требование.")
        run_pending_preparations()
        sources = retrieve_project_sources(self.project.pk, "Какую температуру торгового зала летом требует задание на проектирование?")
        self.assertTrue(any(s["page"] == 1 and "не должна превышать" in s["text"] for s in sources))
        self.assertTrue(any(s["page"] == 2 and "+20°С" in s["text"] for s in sources))
        self.assertFalse(any(s["page"] == 1 and "+20°С" in s["text"] for s in sources))

    def test_two_passages_with_unknown_page_can_both_support_a_letter_answer(self):
        letter = Correspondence.objects.create(project=self.project, direction="incoming", subject="О недостающих исходных данных", raw_text="Просим предоставить исходные данные.\n" + "Описание документа. " * 80 + "\nТУ на электроснабжение требуется заменить. Разрешенная мощность 98,8 кВт недостаточна.")
        self.document("Технические условия", "Технические условия на электроснабжение. Замена исходных данных.")
        run_pending_preparations()
        sources = retrieve_project_sources(self.project.pk, "Почему в письме о недостающих исходных данных просили заменить ТУ на электроснабжение?")
        passages = [s for s in sources if s["kind"] == "correspondence" and s["id"] == letter.pk]
        self.assertTrue(any("98,8" in s["text"] for s in passages))
        self.assertEqual(sources[0]["kind"], "correspondence")

    def test_query_wording_does_not_push_actual_power_below_generic_mentions(self):
        self.document("Технические условия на электроснабжение", "--- page 1 ---\nРазрешенная мощность электроснабжения 98,8 кВт.\n--- page 2 ---\nУказана разрешенная мощность электроснабжения при необходимости.\n--- page 3 ---\nКакая мощность электроснабжения указана в приложении, определяет проект.")
        run_pending_preparations()
        sources = retrieve_project_sources(self.project.pk, "Какая разрешённая мощность указана в ТУ на электроснабжение?")
        self.assertTrue(any("98,8" in s["text"] for s in sources))

    def test_requisite_question_prefers_first_page_of_document(self):
        document = self.document("Технические условия на электроснабжение", "--- page 1 ---\nТехнические условия на электроснабжение.\n--- page 2 ---\nНомер и дата технических условий на электроснабжение указываются при согласовании.")
        document.document_number = "TEST/42"
        document.save()
        run_pending_preparations()
        sources = retrieve_project_sources(self.project.pk, "Каковы номер и дата технических условий на электроснабжение?")
        self.assertEqual(sources[0]["page"], 1)
        self.assertIn("TEST/42", sources[0]["metadata"]["Реквизиты карточки"])

    def test_result_limit_is_respected_when_question_requests_multiple_groups(self):
        self.document("Задание на проектирование", "--- page 1 ---\nЭлектроснабжение проекта.")
        self.document("Технические условия на электроснабжение", "--- page 1 ---\nЭлектроснабжение проекта.")
        Correspondence.objects.create(project=self.project, direction="incoming", raw_text="Письмо о задании на проектирование и технических условиях электроснабжения.")
        run_pending_preparations()
        sources = retrieve_project_sources(self.project.pk, "Что в письме, задании и ТУ сказано об электроснабжении?", limit=1)
        self.assertEqual(len(sources), 1)
