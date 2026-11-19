from django.db.models import Q
from django.db.models.functions import Lower, Trim

from migration_portal.models import Customer, Project, UserProfile


def user_profile(user):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


def visible_projects(user):
    if user.is_superuser:
        return Project.objects.all()
    try:
        profile = user.profile
    except UserProfile.DoesNotExist:
        return Project.objects.none()
    if profile.access_level == UserProfile.AccessLevel.BLOCKED:
        return Project.objects.none()
    customer_names = [name.strip().casefold() for name in profile.allowed_customers.filter(is_active=True).values_list("name", flat=True)]
    project_ids = profile.allowed_projects.values_list("pk", flat=True)
    return Project.objects.annotate(customer_normalized=Lower(Trim("customer"))).filter(
        Q(customer_normalized__in=customer_names) | Q(pk__in=project_ids)
    ).distinct()


def visible_customers(user):
    if user.is_superuser:
        return Customer.objects.filter(is_active=True)
    try:
        profile = user.profile
    except UserProfile.DoesNotExist:
        return Customer.objects.none()
    return profile.allowed_customers.filter(is_active=True)
