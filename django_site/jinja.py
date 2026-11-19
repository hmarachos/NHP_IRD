from datetime import date, datetime

from django.contrib.messages import get_messages
from django.middleware.csrf import get_token
from django.urls import reverse
from django.urls.resolvers import get_resolver
from django.templatetags.static import static
from jinja2 import Environment
from jinja2 import select_autoescape
from urllib.parse import urlencode


class RequestProxy:
    def __init__(self, request):
        self._request = request

    def __getattr__(self, name):
        if name == "endpoint":
            url_name = getattr(self._request.resolver_match, "url_name", "") or ""
            return "web." + url_name.removeprefix("web_") if url_name.startswith("web_") else url_name
        if name == "args":
            return self._request.GET
        return getattr(self._request, name)


def _ru_date(value):
    if not value:
        return ""
    raw = str(value).strip()
    try:
        return date.fromisoformat(raw[:10]).strftime("%d.%m.%Y")
    except ValueError:
        for fmt in ("%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(raw, fmt).strftime("%d.%m.%Y")
            except ValueError:
                pass
    return raw


def environment(**options):
    options.setdefault("autoescape", select_autoescape(["html", "htm", "xml"]))
    env = Environment(**options)
    env.filters["ru_date"] = _ru_date
    return env


def context_processor(request):
    def url_for(endpoint, **values):
        if endpoint == "static":
            filename = values.pop("filename", "")
            version = values.pop("v", None)
            url = static(filename)
            if version is not None:
                values["v"] = version
            return f"{url}?{urlencode(values)}" if values else url
        name = endpoint.replace(".", "_")
        patterns = get_resolver().reverse_dict.getlist(name)
        converters = patterns[0][3] if patterns else {}
        path_values = {key: value for key, value in values.items() if key in converters}
        query_values = {key: value for key, value in values.items() if key not in converters}
        url = reverse(name, kwargs=path_values or None)
        return f"{url}?{urlencode(query_values, doseq=True)}" if query_values else url

    def get_flashed_messages(with_categories=False, category_filter=()):
        messages = list(get_messages(request))
        if category_filter:
            messages = [message for message in messages if message.level_tag in category_filter]
        if with_categories:
            return [(message.level_tag, str(message)) for message in messages]
        return [str(message) for message in messages]

    def shared_csrf_token():
        token = getattr(request, "_shared_csrf_form_token", None)
        if token is None:
            token = get_token(request)
            request._shared_csrf_form_token = token
        return token

    return {
        "request": RequestProxy(request),
        "url_for": url_for,
        "csrf_token": shared_csrf_token,
        "get_flashed_messages": get_flashed_messages,
    }
