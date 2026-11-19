"""Regressions discovered while replaying existing IRD documents."""
import unittest
from pathlib import Path
from unittest.mock import Mock
from app.models import ParsedDocument
from app.services.field_extractor import RegexFieldExtractor
from app.services.pipeline import DocumentProcessingPipeline


class DocumentRequisitesTests(unittest.TestCase):
    def test_body_certificate_reference_is_not_outgoing_number(self):
        text = 'ТЕХНИЧЕСКИЕ ТРЕБОВАНИЯ\nПредоставлены документы: свидетельство\n(удостоверение) №623/234-21353 о государственной регистрации\nземельного участка.'
        self.assertEqual(RegexFieldExtractor._extract_header_document_number(text), '')

    def test_outgoing_number_preserves_separators(self):
        text = '№08-02/4045 от 12.11.2026\nНа №42 от 24.03.2026\nО предоставлении информации'
        self.assertEqual(RegexFieldExtractor._extract_header_document_number(text), '08-02/4045')

    def test_incoming_reference_is_not_outgoing_number(self):
        self.assertEqual(RegexFieldExtractor._extract_header_document_number('На №12-10/763 от 31.03.2026'), '')

    def test_protocol_requisites_override_registration_stamp(self):
        text = '№109 от 28.09.2026\nП Р О Т О К О Л\n23.09.2026 №9\nг. Гомель\nтехнического совета'
        pipeline = DocumentProcessingPipeline(document_types=['Протокол'], tesseract_lang='rus+eng', ocr_dpi=300)
        pipeline.text_extractor.extract = Mock(return_value=text)
        pipeline.ai_extractor = Mock()
        pipeline.ai_extractor.extract.return_value = ParsedDocument(document_number='109', issue_date='2026-09-28', valid_until='2026-09-28')
        pipeline.ai_extractor.extract_document_header.return_value = {'document_number':'109', 'issue_date':'2026-09-28'}
        result = pipeline.process(Path('unused.pdf'))
        self.assertEqual((result.document_number, result.issue_date, result.valid_until, result.no_expiration), ('9', '2026-09-23', '', True))


if __name__ == '__main__':
    unittest.main()
