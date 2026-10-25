import copy
import json
from urllib.request import Request, urlopen

from flask import current_app

from app.models import ParsedDocument
from app.prompts import EXTRACTION_PROMPT, EXTRACTION_SCHEMA


class AiFieldExtractor:
    def __init__(self, fallback_extractor):
        self.fallback_extractor = fallback_extractor

    def extract(self, text: str, document_types: list[str]) -> ParsedDocument:
        if not current_app.config["USE_AI"]:
            return self.fallback_extractor.extract(text)

        if current_app.config["AI_PROVIDER"] == "ollama":
            return self._extract_with_ollama(text, document_types)

        if not current_app.config["OPENAI_API_KEY"]:
            return self.fallback_extractor.extract(text)

        try:
            from openai import OpenAI
        except ImportError:
            return self.fallback_extractor.extract(text)

        client = OpenAI(api_key=current_app.config["OPENAI_API_KEY"])
        
        # Создаем копию схемы с динамическим enum
        schema = copy.deepcopy(EXTRACTION_SCHEMA)
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

    def _extract_with_ollama(self, text: str, document_types: list[str]) -> ParsedDocument:
        schema = copy.deepcopy(EXTRACTION_SCHEMA)
        schema["properties"]["working_title"]["enum"] = document_types + ["Неопределенный документ"]
        prompt = (
            f"Возможные рабочие названия: {', '.join(document_types)}\n\n"
            f"OCR-текст документа:\n{text[:18000]}"
        )
        payload = {
            "model": current_app.config["OLLAMA_MODEL"],
            "messages": [
                {"role": "system", "content": EXTRACTION_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "format": schema,
            "stream": False,
            "think": False,
            "keep_alive": "5m",
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 700},
        }
        request = Request(
            f"{current_app.config['OLLAMA_BASE_URL']}/api/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(request, timeout=current_app.config["OLLAMA_TIMEOUT"]) as response:
                result = json.loads(response.read().decode("utf-8"))
            data = json.loads(result["message"]["content"])
            if data.get("issuer"):
                data["issuer"] = self.fallback_extractor._clean_issuer(
                    self.fallback_extractor._prefer_russian_text(data["issuer"])
                )
            return ParsedDocument(raw_text=text, **data)
        except Exception:
            return self.fallback_extractor.extract(text)
