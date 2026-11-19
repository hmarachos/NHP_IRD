import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_not_required, login_required, user_passes_test
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, PasswordResetForm, SetPasswordForm
from django.contrib.auth.models import User
from django.contrib.auth.views import PasswordResetCompleteView, PasswordResetConfirmView, PasswordResetDoneView, PasswordResetView
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST
from django.contrib.auth.views import PasswordChangeDoneView, PasswordChangeView
from django.utils.http import url_has_allowed_host_and_scheme
from django.template.loader import render_to_string

from migration_portal.accounts import user_profile
from migration_portal.forms import CustomerForm, OrganizationForm, ProfileForm, RegistrationForm, UserAccessForm
from migration_portal.models import Customer, Organization, Project, UserProfile

logger = logging.getLogger(__name__)


def admin_only(user):
    return user.is_superuser


@login_not_required
def account_login(request):
    if request.user.is_authenticated:
        return redirect("account_profile")
    login_data = request.POST.copy() if request.method == "POST" else None
    if login_data is not None:
        login_data["username"] = login_data.get("username", "").strip().casefold()
    form = AuthenticationForm(request, data=login_data)
    form.fields["username"].label = "Электронная почта"
    form.fields["username"].widget.attrs["autocomplete"] = "email"
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        target = request.GET.get("next", "")
        if target and url_has_allowed_host_and_scheme(target, {request.get_host()}, require_https=request.is_secure()):
            return HttpResponseRedirect(target)
        return redirect("account_profile")
    return render(request, "accounts/login.html", {"form": form})


@login_not_required
def account_register(request):
    if request.user.is_authenticated:
        return redirect("account_profile")
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            user = User.objects.create_user(
                username=data["email"], email=data["email"],
                first_name=data["first_name"].strip(), last_name=data["last_name"].strip(),
                password=data["password1"],
            )
            UserProfile.objects.create(
                user=user, middle_name=data["middle_name"].strip(), is_gip=data["is_gip"] == "1",
                access_level=UserProfile.AccessLevel.BLOCKED,
            )
        settings_url = request.build_absolute_uri(reverse("admin_user_edit", kwargs={"user_id": user.pk}))
        email_context = {
            "full_name": " ".join(filter(None, (user.last_name, user.first_name, data["middle_name"].strip()))),
            "email": user.email,
            "is_gip": data["is_gip"] == "1",
            "settings_url": settings_url,
        }
        try:
            notification = EmailMultiAlternatives(
                subject=f"Новая регистрация в Архиве ИРД: {email_context['full_name']}",
                body=render_to_string("accounts/emails/new_user_registration.txt", email_context),
                from_email=settings.DEFAULT_FROM_EMAIL or None,
                to=[settings.REGISTRATION_NOTIFICATION_EMAIL],
            )
            notification.attach_alternative(
                render_to_string("accounts/emails/new_user_registration.html", email_context),
                "text/html",
            )
            notification.send(fail_silently=False)
        except Exception:
            logger.exception("Unable to notify administrator about new registration for user id %s", user.pk)
        messages.success(request, "Регистрация завершена. Доступ откроет администратор после проверки данных.")
        return redirect("account_login")
    return render(request, "accounts/register.html", {"form": form})


@login_required
def account_profile(request):
    profile = user_profile(request.user)
    initial = {
        "last_name": request.user.last_name, "first_name": request.user.first_name,
        "middle_name": profile.middle_name, "email": request.user.email,
        "is_gip": "1" if profile.is_gip else "0",
    }
    form = ProfileForm(request.POST or None, user=request.user, initial=initial)
    if request.method == "POST" and form.is_valid():
        form.save(profile)
        messages.success(request, "Личные данные сохранены.")
        return redirect("account_profile")
    return render(request, "accounts/profile.html", {"form": form, "profile": profile})


@login_required
@require_POST
def account_logout(request):
    logout(request)
    return redirect("account_login")


class AppPasswordResetView(PasswordResetView):
    template_name = "accounts/password_reset_form.html"
    email_template_name = "accounts/password_reset_email.txt"
    subject_template_name = "accounts/password_reset_subject.txt"
    success_url = reverse_lazy("account_password_reset_done")
    form_class = PasswordResetForm


class AppPasswordResetDoneView(PasswordResetDoneView):
    template_name = "accounts/password_reset_done.html"


class AppPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "accounts/password_reset_confirm.html"
    success_url = reverse_lazy("account_password_reset_complete")
    form_class = SetPasswordForm


class AppPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = "accounts/password_reset_complete.html"


class AppPasswordChangeView(PasswordChangeView):
    template_name = "accounts/password_change_form.html"
    success_url = reverse_lazy("account_password_change_done")
    form_class = PasswordChangeForm


class AppPasswordChangeDoneView(PasswordChangeDoneView):
    template_name = "accounts/password_change_done.html"


@login_required
@user_passes_test(admin_only)
def user_list(request):
    users = User.objects.filter(is_superuser=False).select_related("profile", "profile__organization").order_by("last_name", "first_name", "email")
    return render(request, "accounts/user_list.html", {"users": users})


@login_required
@user_passes_test(admin_only)
def user_edit(request, user_id):
    user = get_object_or_404(User.objects.filter(is_superuser=False), pk=user_id)
    profile = user_profile(user)
    initial = {
        "last_name": user.last_name, "first_name": user.first_name, "middle_name": profile.middle_name,
        "email": user.email, "is_gip": profile.is_gip, "organization": profile.organization_id,
        "show_ai_analytics": profile.show_ai_analytics,
        "access_level": profile.access_level, "allowed_customers": profile.allowed_customers.all(),
        "allowed_projects": profile.allowed_projects.all(), "is_active": user.is_active,
    }
    form = UserAccessForm(request.POST or None, user_record=user, profile=profile, initial=initial)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Права и настройки пользователя сохранены.")
        return redirect("admin_users")
    return render(request, "accounts/user_edit.html", {
        "form": form, "managed_user": user, "profile": profile,
        "organizations": Organization.objects.order_by("name"),
    })


@login_required
@user_passes_test(admin_only)
@require_POST
def organization_api(request):
    action = request.POST.get("action")
    if action == "add":
        form = OrganizationForm({"name": request.POST.get("name", ""), "is_active": "on"})
        if not form.is_valid():
            return JsonResponse({"error": form.errors.get("name", ["Не удалось добавить организацию."])[0]}, status=400)
        organization = form.save()
    elif action == "delete":
        try:
            organization_id = int(request.POST.get("id", ""))
        except (TypeError, ValueError):
            return JsonResponse({"error": "Не выбрана организация для удаления."}, status=400)
        organization = get_object_or_404(Organization, pk=organization_id)
        try:
            organization.delete()
        except ProtectedError:
            return JsonResponse({"error": "Организацию нельзя удалить: она назначена одному или нескольким пользователям."}, status=409)
        return JsonResponse({"id": organization_id, "deleted": True})
    else:
        return JsonResponse({"error": "Неизвестное действие."}, status=400)
    return JsonResponse({"id": organization.pk, "name": organization.name, "is_active": organization.is_active}, status=201)


@login_required
@user_passes_test(admin_only)
def organization_list(request):
    organization_id = request.GET.get("edit_org")
    customer_id = request.GET.get("edit_customer")
    editing_organization = get_object_or_404(Organization, pk=organization_id) if organization_id else None
    editing_customer = get_object_or_404(Customer, pk=customer_id) if customer_id else None
    organization_form = OrganizationForm()
    customer_form = CustomerForm()
    organization_edit_form = OrganizationForm(instance=editing_organization) if editing_organization else None
    customer_edit_form = CustomerForm(instance=editing_customer) if editing_customer else None
    if request.method == "POST":
        kind = request.POST.get("kind")
        if kind == "delete_organization":
            organization = get_object_or_404(Organization, pk=request.POST.get("id"))
            try:
                organization.delete()
            except ProtectedError:
                messages.error(request, "Организацию нельзя удалить: она назначена пользователям. Сначала переназначьте этих пользователей.")
            else:
                messages.success(request, "Организация удалена.")
            return redirect("admin_organizations")
        if kind == "delete_customer":
            customer = get_object_or_404(Customer, pk=request.POST.get("id"))
            if Project.objects.filter(customer__iexact=customer.name).exists():
                messages.error(request, "Заказчика нельзя удалить: с ним связаны проекты. Отключите его, если он больше не используется.")
            elif customer.permitted_users.exists():
                messages.error(request, "Заказчика нельзя удалить: он назначен пользователям. Сначала измените их разрешения.")
            else:
                customer.delete()
                messages.success(request, "Заказчик удалён.")
            return redirect("admin_organizations")
        if kind == "organization":
            organization = get_object_or_404(Organization, pk=request.POST.get("id")) if request.POST.get("id") else None
            if organization:
                editing_organization = organization
                organization_edit_form = OrganizationForm(request.POST, instance=organization)
                valid_form = organization_edit_form
            else:
                organization_form = OrganizationForm(request.POST)
                valid_form = organization_form
            if valid_form.is_valid():
                valid_form.save()
                messages.success(request, "Организация сохранена.")
                return redirect("admin_organizations")
        elif kind == "customer":
            customer = get_object_or_404(Customer, pk=request.POST.get("id")) if request.POST.get("id") else None
            if customer:
                editing_customer = customer
                customer_edit_form = CustomerForm(request.POST, instance=customer)
                valid_form = customer_edit_form
            else:
                customer_form = CustomerForm(request.POST)
                valid_form = customer_form
            if valid_form.is_valid():
                valid_form.save()
                messages.success(request, "Заказчик сохранён.")
                return redirect("admin_organizations")
    return render(request, "accounts/organization_list.html", {
        "organizations": Organization.objects.order_by("name"),
        "customers": Customer.objects.order_by("name"),
        "organization_form": organization_form,
        "customer_form": customer_form,
        "editing_organization": editing_organization,
        "editing_customer": editing_customer,
        "organization_edit_form": organization_edit_form,
        "customer_edit_form": customer_edit_form,
    })


@login_required
@user_passes_test(admin_only)
def admin_dashboard(request):
    return render(request, "accounts/admin_dashboard.html", {
        "pending_count": UserProfile.objects.filter(access_level=UserProfile.AccessLevel.BLOCKED, user__is_superuser=False).count(),
        "user_count": User.objects.filter(is_superuser=False).count(),
        "organization_count": Organization.objects.count(),
    })
