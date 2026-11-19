from django.http import HttpResponseForbidden
from django.shortcuts import redirect

from migration_portal.accounts import visible_projects
from migration_portal.models import Correspondence, Document, HandoverAct, UserProfile


class ProjectAccessMiddleware:
    """Enforce account state, edit level and project scope across every route."""

    PUBLIC_PREFIXES = (
        "/accounts/login", "/accounts/register", "/accounts/password-reset",
        "/accounts/password-reset/done", "/accounts/reset/", "/health/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not request.user.is_authenticated or request.user.is_superuser:
            return None
        path = request.path
        if path.startswith(self.PUBLIC_PREFIXES):
            return None
        if path.startswith(("/manage/", "/admin/")):
            return HttpResponseForbidden("Доступ разрешён только суперадминистратору.")
        try:
            profile = request.user.profile
        except UserProfile.DoesNotExist:
            return redirect("account_profile")
        self_service = path.startswith(("/accounts/profile", "/accounts/logout", "/accounts/password-change"))
        if profile.access_level == UserProfile.AccessLevel.BLOCKED:
            if self_service:
                return None
            return redirect("account_profile")
        analytics_action = path.rstrip("/").endswith(("/analytics", "/analytics/cancel"))
        if request.method not in ("GET", "HEAD", "OPTIONS") and profile.access_level != UserProfile.AccessLevel.EDIT and not self_service and not analytics_action:
            return HttpResponseForbidden("У вашей учётной записи нет прав на редактирование.")

        project_id = view_kwargs.get("object_id")
        if project_id is None and view_kwargs.get("document_id") is not None:
            project_id = Document.objects.filter(pk=view_kwargs["document_id"]).values_list("project_id", flat=True).first()
        if project_id is None and view_kwargs.get("letter_id") is not None:
            project_id = Correspondence.objects.filter(pk=view_kwargs["letter_id"]).values_list("project_id", flat=True).first()
        if project_id is None and view_kwargs.get("act_id") is not None:
            project_id = HandoverAct.objects.filter(pk=view_kwargs["act_id"]).values_list("project_id", flat=True).first()
        if project_id is not None and not visible_projects(request.user).filter(pk=project_id).exists():
            return HttpResponseForbidden("Этот объект не входит в список разрешённых вам проектов.")
        if view_kwargs.get("document_id") is not None and project_id is None:
            return HttpResponseForbidden("Доступ к этому документу запрещён.")
        return None
