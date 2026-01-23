from django import forms
from django.core.exceptions import ValidationError

from tracker.models import Workspace, WorkspaceMembership
from .models import WhatsAppProfile
from .whatsapp import normalize_phone


class WhatsAppProfileForm(forms.ModelForm):
    phone_number = forms.CharField(
        label="Seu n\u00famero do WhatsApp",
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "+55DDDNUMERO",
                "inputmode": "tel",
            }
        ),
    )
    workspace = forms.ModelChoiceField(
        label="Workspace",
        queryset=Workspace.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    is_active = forms.BooleanField(
        label="Ativar agente no WhatsApp",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    class Meta:
        model = WhatsAppProfile
        fields = ["phone_number", "workspace", "is_active"]

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user:
            memberships = WorkspaceMembership.objects.filter(user=user).values_list("workspace_id", flat=True)
            owned = Workspace.objects.filter(owner=user).values_list("id", flat=True)
            allowed_ids = set(memberships) | set(owned)
            self.fields["workspace"].queryset = Workspace.objects.filter(id__in=allowed_ids).order_by("name")
        else:
            self.fields["workspace"].queryset = Workspace.objects.none()

    def clean(self):
        cleaned = super().clean()
        raw_phone = cleaned.get("phone_number", "") or ""
        workspace = cleaned.get("workspace")

        if raw_phone:
            normalized = normalize_phone(raw_phone)
            if len(normalized) < 8:
                raise ValidationError("Informe um n\u00famero v\u00e1lido com DDD.")
            cleaned["phone_number"] = normalized
            if not workspace:
                self.add_error("workspace", "Selecione o workspace que vai receber os lan\u00e7amentos.")
        elif workspace:
            self.add_error("phone_number", "Informe o seu n\u00famero do WhatsApp para ativar.")

        return cleaned
