import copy
import json
import logging

from flask import current_app

from app.models import ParsedDocument
from app.prompts import EXTRACTION_PROMPT, EXTRACTION_SCHEMA


logger = logging.getLogger(__name__)
OPENAI_INPUT_CHUNK_CHARS = 12_000


class AiFieldExtractor:
    def __init__(self, fallback_extractor):
        self.fallback_extractor = fallback_extractor

    def extract(self, text: str, document_types: list[str]) -> ParsedDocument:
        if not current_app.config["USE_AI"]:
            return self.fallback_extractor.extract(text)

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

        parsed_chunks = []
        for chunk in self._split_text(text, OPENAI_INPUT_CHUNK_CHARS):
            try:
                response = client.responses.create(
                    model=current_app.config["OPENAI_MODEL"],
                    instructions=EXTRACTION_PROMPT,
                    input=(
                        f"Возможные рабочие названия: {', '.join(document_types)}\n\n"
                        f"OCR-текст документа (часть):\n{chunk}"
                    ),
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
                parsed_chunks.append(ParsedDocument(raw_text=chunk, **data))
            except Exception:
                logger.exception("OpenAI document extraction failed; using local extraction")
                return self.fallback_extractor.extract(text)

        if not parsed_chunks:
            return self.fallback_extractor.extract(text)
        return self._merge_chunks(text, parsed_chunks)

    @staticmethod
    def _split_text(text: str, max_chars: int) -> list[str]:
        """Split OCR text without dropping pages or exceeding the request size."""
        if len(text) <= max_chars:
            return [text]

        chunks = []
        current = ""
        for line in text.splitlines(keepends=True):
            remaining = line
            while remaining:
                room = max_chars - len(current)
                if room == 0:
                    chunks.append(current)
                    current = ""
                    room = max_chars

                if len(remaining) <= room:
                    current += remaining
                    remaining = ""
                    continue

                split_at = remaining.rfind(" ", 0, room + 1)
                if split_at < room // 2:
                    split_at = room
                current += remaining[:split_at]
                remaining = remaining[split_at:].lstrip()
                chunks.append(current)
                current = ""

        if current:
            chunks.append(current)
        return chunks

    def _merge_chunks(self, full_text: str, chunks: list[ParsedDocument]) -> ParsedDocument:
        fallback = self.fallback_extractor.extract(full_text)

        def first_value(field: str) -> str:
            return next((getattr(item, field) for item in chunks if getattr(item, field)), "")

        issue_date = first_value("issue_date") or fallback.issue_date
        valid_until = first_value("valid_until") or fallback.valid_until
        validity_text = max(
            (item.validity_text for item in chunks if item.validity_text),
            key=len,
            default=fallback.validity_text,
        )
        working_title = next(
            (
                item.working_title
                for item in chunks
                if item.working_title and item.working_title != "Неопределенный документ"
            ),
            first_value("working_title") or fallback.working_title,
        )

        return ParsedDocument(
            working_title=working_title,
            official_title=first_value("official_title") or fallback.official_title,
            issuer=first_value("issuer") or fallback.issuer,
            issue_date=issue_date,
            valid_until=valid_until,
            validity_text=validity_text,
            no_expiration=not valid_until and (
                fallback.no_expiration
                or any(item.no_expiration and item.validity_text for item in chunks)
            ),
            raw_text=full_text,
        )
