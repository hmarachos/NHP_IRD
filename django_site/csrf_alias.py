from django.conf import settings
from django.http.request import validate_host
from django.middleware.csrf import CsrfViewMiddleware


class CsrfAliasMiddleware:
    """Accept the established form field and AJAX header names on Django views."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        supplied = request.headers.get("X-CSRF-Token")
        if supplied:
            request.META.setdefault("HTTP_X_CSRFTOKEN", supplied)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            form = request.POST
            if "csrfmiddlewaretoken" not in form and form.get("csrf_token"):
                form._mutable = True
                form["csrfmiddlewaretoken"] = form["csrf_token"]
                form._mutable = False
        return self.get_response(request)


class ProjectCsrfViewMiddleware(CsrfViewMiddleware):
    """Allow opaque-origin browser forms only on configured hosts.

    Django still validates the CSRF cookie and submitted token after this
    origin check. This handles embedded browsers that send Origin: null.
    """

    def _origin_verified(self, request):
        if request.META.get("HTTP_ORIGIN") == "null":
            return validate_host(request.get_host(), settings.ALLOWED_HOSTS)
        return super()._origin_verified(request)
