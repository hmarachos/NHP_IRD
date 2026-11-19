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
