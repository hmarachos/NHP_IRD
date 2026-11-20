"""Build consistent document captions from their stored requisites."""
from functools import lru_cache
import re

from app.services.expiry import parse_document_date


@lru_cache(maxsize=1)
def _morph():
    from pymorphy3 import MorphAnalyzer
    return MorphAnalyzer()


@lru_cache(maxsize=512)
def organization_genitive(value):
    # Decline the leading legal form/name only. Quoted names, abbreviations and
    # dependent complements (e.g. Министерства внутренних дел) stay intact.
    head, separator, tail = re.split(r'([«"(])', value, maxsplit=1) if re.search(r'[«"(]', value) else (value, '', '')
    tokens = re.split(r'(\s+|-)', head)
    result = []
    finished = False
    for token in tokens:
        if finished or not re.fullmatch(r'[А-Яа-яЁё]+', token) or (token.isupper() and len(token) <= 5):
            result.append(token)
            continue
        choices = _morph().parse(token)
        parsed = next((word for word in choices if word.tag.case == 'nomn' and word.tag.POS in {'ADJF', 'NOUN', 'PRTF'}), choices[0])
        if parsed.tag.case == 'gent':
            finished = True
            result.append(token)
            continue
        if parsed.tag.POS not in {'ADJF', 'NOUN', 'PRTF'}:
            finished = True
            result.append(token)
            continue
        inflected = parsed.inflect({'gent'})
        word = inflected.word if inflected else token
        result.append(word.upper() if token.isupper() else (word[:1].upper() + word[1:] if token[:1].isupper() else word))
        if parsed.tag.POS == 'NOUN':
            finished = True
    return ''.join(result) + separator + tail


def clean_document_name(title, issuer='', customer=''):
    """Keep the document's own name, separating card requisites."""
    title = re.sub(r'\s+', ' ', title or '').strip()
    title = re.sub(r'\s+от\s+\d{2}[.\-/]\d{2}[.\-/]\d{4}(?:\s+№\s*.*)?$', '', title, flags=re.I)
    title = re.sub(r'\s+№\s*[^\s]+$', '', title).strip()
    for organization in (issuer, customer):
        organization = re.sub(r'\s+', ' ', organization or '').strip()
        if not organization:
            continue
        for value in (organization, organization_genitive(organization)):
            title = re.sub(r'\s+(?:(?:заказчика|от|для)\s+)?' + re.escape(value) + r'\s*$', '', title, flags=re.I).strip()
    return title.strip(' ;,—-')


def format_document_title(title, issuer='', issue_date='', document_number=''):
    title = re.sub(r'\s+', ' ', title or '').strip()
    # Rebuild a previously composed suffix when card requisites change.
    title = re.sub(r'\s+от\s+\d{2}[.\-/]\d{2}[.\-/]\d{4}(?:\s+№\s*.*)?$', '', title, flags=re.I)
    title = re.sub(r'\s+№\s*.*$', '', title).strip()
    issuer = re.sub(r'\s+', ' ', issuer or '').strip()
    if issuer:
        genitive = organization_genitive(issuer)
        if issuer.casefold() in title.casefold():
            title = re.sub(re.escape(issuer), lambda match: genitive, title, flags=re.I)
        elif genitive.casefold() not in title.casefold():
            title = f'{title} {genitive}'.strip()
    issued = parse_document_date(issue_date)
    if issued:
        title += f' от {issued.strftime("%d.%m.%Y")}'
    number = re.sub(r'^\s*№\s*', '', document_number or '').strip()
    if number:
        title += f' № {number}'
    return title.strip()


def attach_document_title(row):
    row['name_title'] = clean_document_name(
        row.get('working_title') or row.get('official_title') or row.get('original_filename'), row.get('issuer'),
    )
    row['display_title'] = format_document_title(
        row.get('working_title') or row.get('official_title') or row.get('original_filename'),
        row.get('issuer'), row.get('issue_date'), row.get('document_number'),
    )
    return row
