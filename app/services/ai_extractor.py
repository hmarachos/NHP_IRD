import json

from flask import current_app

from app.models import ParsedDocument
from app.prompts import EXTRACTION_PROMPT, EXTRACTION_SCHEMA


class AiFieldExtractor:
    def __init__(self, fallback_extractor):
        self.fallback_extractor = fallback_extractor

    def extract(self, text: str, document_types: list[str]) -> ParsedDocument:
        if not current_app.config["USE_AI"] or not current_app.config["OPENAI_API_KEY"]:
            return self.fallback_extractor.extract(text)

        try:
            from openai import OpenAI
        except ImportError:
            return self.fallback_extractor.extract(text)

        client = OpenAI(api_key=current_app.config["OPENAI_API_KEY"])
        
        # Создаем копию схемы с динамическим enum
        schema = EXTRACTION_SCHEMA.copy()
        schema["properties"]["working_title"]["enum"] = document_types + ["Неопределенный документ"]

        try:
            response = client.responses.create(
                model=current_app.config["OPENAI_MODEL"],
                instructions=EXTRACTION_PROMPT,
                input=f"Возможные рабочие названия: {', '.join(document_types)}\n\nOCR-текст:\n{text[:18000]}",
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "document_fields",
                        "schema": schema,
                        "strict": True,
                    }
                },
            )
            data = json.loads(response.output_text)
            if data.get("issuer"):
                data["issuer"] = self.fallback_extractor._clean_issuer(
                    self.fallback_extractor._prefer_russian_text(data["issuer"])
                )
            return ParsedDocument(raw_text=text, **data)
        except Exception:
            return self.fallback_extractor.extract(text)
