from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from migration_portal.models import Customer, Organization, Project, UserProfile


class RegistrationForm(forms.Form):
    last_name = forms.CharField(label="Фамилия", max_length=150)
    first_name = forms.CharField(label="Имя", max_length=150)
    middle_name = forms.CharField(label="Отчество", max_length=150, required=False)
    email = forms.EmailField(label="Электронная почта", max_length=150)
    is_gip = forms.ChoiceField(label="Главный инженер проекта", choices=(("", "Выберите"), ("0", "Нет"), ("1", "Да")))
    password1 = forms.CharField(label="Пароль", widget=forms.PasswordInput, min_length=8)
    password2 = forms.CharField(label="Подтверждение пароля", widget=forms.PasswordInput)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().casefold()
        if User.objects.filter(email__iexact=email).exists() or User.objects.filter(username__iexact=email).exists():
            raise ValidationError("Учётная запись с таким email уже зарегистрирована.")
        return email

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password1") and cleaned.get("password2") and cleaned["password1"] != cleaned["password2"]:
            self.add_error("password2", "Пароли не совпадают.")
        elif cleaned.get("password1"):
            try:
                validate_password(cleaned["password1"])
            except ValidationError as error:
                self.add_error("password1", error)
        return cleaned


class ProjectPermissionField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, project):
        archived = " · АРХИВ" if project.archived_at else ""
        return f"{project.contract_number} · {project.name} · {project.customer or 'Заказчик не указан'}{archived}"


class ProfileForm(forms.Form):
    last_name = forms.CharField(label="Фамилия", max_length=150)
    first_name = forms.CharField(label="Имя", max_length=150)
    middle_name = forms.CharField(label="Отчество", max_length=150, required=False)
    email = forms.EmailField(label="Электронная почта", max_length=150)
    is_gip = forms.ChoiceField(label="Главный инженер проекта", choices=(("", "Выберите"), ("0", "Нет"), ("1", "Да")))

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().casefold()
        if User.objects.exclude(pk=self.user.pk).filter(email__iexact=email).exists():
            raise ValidationError("Этот email уже используется.")
        if User.objects.exclude(pk=self.user.pk).filter(username__iexact=email).exists():
            raise ValidationError("Этот email уже используется как имя входа.")
        return email

    def save(self, profile):
        self.user.last_name = self.cleaned_data["last_name"].strip()
        self.user.first_name = self.cleaned_data["first_name"].strip()
        self.user.email = self.cleaned_data["email"]
        self.user.username = self.cleaned_data["email"]
        self.user.save(update_fields=["last_name", "first_name", "email", "username"])
        profile.middle_name = self.cleaned_data["middle_name"].strip()
        profile.is_gip = self.cleaned_data["is_gip"] == "1"
        profile.save(update_fields=["middle_name", "is_gip"])


class UserAccessForm(forms.Form):
    last_name = forms.CharField(label="Фамилия", max_length=150)
    first_name = forms.CharField(label="Имя", max_length=150)
    middle_name = forms.CharField(label="Отчество", max_length=150, required=False)
    email = forms.EmailField(label="Email", max_length=150)
    is_gip = forms.BooleanField(label="Главный инженер проекта", required=False, widget=forms.CheckboxInput(attrs={"class": "account-switch-input"}))
    organization = forms.ModelChoiceField(label="Место работы", queryset=Organization.objects.none())
    access_level = forms.ChoiceField(label="Уровень доступа", choices=UserProfile.AccessLevel.choices)
    allowed_customers = forms.ModelMultipleChoiceField(
        label="Разрешённые заказчики", queryset=Customer.objects.none(), required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    allowed_projects = ProjectPermissionField(
        label="Разрешённые объекты (для гостевого доступа)", queryset=Project.objects.none(), required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    is_active = forms.BooleanField(label="Учётная запись активна", required=False, initial=True, widget=forms.CheckboxInput(attrs={"class": "account-switch-input"}))

    def __init__(self, *args, user_record, profile, **kwargs):
        self.user_record = user_record
        self.profile_record = profile
        super().__init__(*args, **kwargs)
        self.fields["organization"].queryset = Organization.objects.filter(is_active=True)
        if profile.organization_id:
            self.fields["organization"].queryset = Organization.objects.filter(is_active=True) | Organization.objects.filter(pk=profile.organization_id)
        self.fields["allowed_customers"].queryset = Customer.objects.filter(is_active=True)
        self.fields["allowed_projects"].queryset = Project.objects.order_by("customer", "name", "id")

    def clean_email(self):
        email = self.cleaned_data["email"].strip().casefold()
        if User.objects.exclude(pk=self.user_record.pk).filter(email__iexact=email).exists():
            raise ValidationError("Этот email уже используется.")
        if User.objects.exclude(pk=self.user_record.pk).filter(username__iexact=email).exists():
            raise ValidationError("Этот email уже используется как имя входа.")
        return email

    def save(self):
        user = self.user_record
        user.first_name = self.cleaned_data["first_name"].strip()
        user.last_name = self.cleaned_data["last_name"].strip()
        user.username = self.cleaned_data["email"]
        user.email = self.cleaned_data["email"]
        user.is_active = self.cleaned_data["is_active"]
        user.save()
        profile = self.profile_record
        profile.middle_name = self.cleaned_data["middle_name"].strip()
        profile.is_gip = self.cleaned_data["is_gip"]
        profile.organization = self.cleaned_data["organization"]
        profile.access_level = self.cleaned_data["access_level"]
        profile.save()
        profile.allowed_customers.set(self.cleaned_data["allowed_customers"])
        profile.allowed_projects.set(self.cleaned_data["allowed_projects"])
        return user


class OrganizationForm(forms.ModelForm):
    is_active = forms.BooleanField(
        label="Активна",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "account-switch-input"}),
    )

    class Meta:
        model = Organization
        fields = ("name", "is_active")
        labels = {
            "name": "Название организации", "is_active": "Активна",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        duplicates = Organization.objects.filter(name__iexact=name)
        if self.instance.pk:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise ValidationError("Организация с таким названием уже есть.")
        return name

    def clean_is_active(self):
        # New directory entries are active by default. The switch is shown only
        # while editing an existing record.
        if not self.instance.pk:
            return True
        return self.cleaned_data["is_active"]


class CustomerForm(forms.ModelForm):
    is_active = forms.BooleanField(
        label="Активен",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "account-switch-input"}),
    )

    class Meta:
        model = Customer
        fields = ("name", "is_active")
        labels = {"name": "Название заказчика", "is_active": "Активен"}

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        duplicates = Customer.objects.filter(name__iexact=name)
        if self.instance.pk:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise ValidationError("Заказчик с таким названием уже есть.")
        return name

    def clean_is_active(self):
        if not self.instance.pk:
            return True
        return self.cleaned_data["is_active"]
