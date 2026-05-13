from dataclasses import dataclass


@dataclass
class ParsedDocument:
    working_title: str = ""
    official_title: str = ""
    issuer: str = ""
    issue_date: str = ""
    valid_until: str = ""
    validity_text: str = ""
    no_expiration: bool = False
    raw_text: str = ""
