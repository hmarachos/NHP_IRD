"""Small filename sanitizers shared by upload and document-generation code."""

import re
import unicodedata


def secure_stem(value, fallback="document"):
    normalized = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", normalized).strip("._-")
    return cleaned or fallback
