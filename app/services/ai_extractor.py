import json

from flask import current_app

from app.models import ParsedDocument


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
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "working_title": {"type": "string", "enum": document_types + ["Неопределенный документ"]},
                "official_title": {"type": "string"},
                "issuer": {"type": "string"},
                "issue_date": {"type": "string"},
                "valid_until": {"type": "string"},
                "validity_text": {"type": "string"},
                "no_expiration": {"type": "boolean"},
            },
            "required": [
                "working_title",
                "official_title",
                "issuer",
                "issue_date",
                "valid_until",
                "validity_text",
                "no_expiration",
            ],
        }

        try:
            response = client.responses.create(
                model=current_app.config["OPENAI_MODEL"],
                instructions=(
                    "Ты извлекаешь реквизиты из OCR-текста проектной исходно-разрешительной "
                    "документации на русском языке. Верни только достоверные значения. "
                    "В поле working_title выбери наиболее подходящее краткое/каноническое "
                    "название строго из списка возможных рабочих названий. "
                    "В поле official_title верни полное наименование документа; если в тексте "
                    "видно только общее название, используй наиболее подходящее полное "
                    "наименование из списка возможных рабочих названий. "
                    "В поле issuer верни организацию, указанную в самом документе как выдавшая "
                    "организация. Если шапка или реквизиты указаны на белорусском и русском "
                    "языке, верни только русский вариант названия организации, без белорусского "
                    "дубля и без адреса, телефона, УНП, банковских реквизитов. "
                    "Если срок действия не указан, поставь no_expiration=true и valid_until=''. "
                    "Даты возвращай в формате DD-MM-YYYY, если дата присутствует."
                ),
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
