from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm, PasswordResetForm
from django.core.exceptions import ValidationError
from django.utils import timezone
from datetime import timedelta
from django.utils.text import slugify

from .models import (
    Category,
    Task,
    TaskStep,
    Transaction,
    Workspace,
    UserProfile,
    WorkspaceAccessRequest,
    CategoryBudget,
    BalanceGoal,
    SubscriptionInvite,
    PricingConfig,
)

User = get_user_model()


class BaseStyledForm(forms.ModelForm):
    """
    Applies Bootstrap classes to inputs for consistent styling.
    """

    def _apply_css(self):
        for _, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                base_class = 'form-check-input'
            else:
                base_class = 'form-select' if isinstance(field.widget, forms.Select) else 'form-control'
            css = field.widget.attrs.get('class', '')
            field.widget.attrs['class'] = f'{css} {base_class}'.strip()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_css()


class TransactionForm(BaseStyledForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['date'].input_formats = ['%Y-%m-%d', '%d/%m/%Y']
        self.fields['date'].widget.format = '%Y-%m-%d'

    class Meta:
        model = Transaction
        fields = ['description', 'date', 'category', 'value', 'type']
        widgets = {
            'date': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date', 'class': 'form-control', 'lang': 'pt-BR'}),
        }


class TaskForm(BaseStyledForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['due_date'].input_formats = ['%Y-%m-%d', '%d/%m/%Y']
        self.fields['due_date'].widget.format = '%Y-%m-%d'

    class Meta:
        model = Task
        fields = ['title', 'due_date', 'status']
        widgets = {
            'due_date': forms.DateInput(
                format='%Y-%m-%d',
                attrs={'type': 'date', 'class': 'form-control', 'lang': 'pt-BR'}
            ),
        }


class TaskStepForm(BaseStyledForm):
    class Meta:
        model = TaskStep
        fields = ['title', 'responsible', 'responsible_email', 'status']
        widgets = {
            'title': forms.TextInput(attrs={'placeholder': 'Descri\u00e7\u00e3o da etapa', 'title': 'Descri\u00e7\u00e3o da etapa', 'class': 'form-control form-control-lg'}),
            'responsible': forms.TextInput(attrs={'placeholder': 'Respons\u00e1vel da etapa', 'title': 'Respons\u00e1vel da etapa', 'class': 'form-control form-control-lg'}),
            'responsible_email': forms.EmailInput(attrs={'placeholder': 'E-mail do respons\u00e1vel', 'title': 'E-mail do respons\u00e1vel', 'class': 'form-control form-control-lg'}),
            'status': forms.Select(attrs={'class': 'form-select form-select-lg'}),
        }


class CategoryForm(BaseStyledForm):
    class Meta:
        model = Category
        fields = ['name', 'color']
        widgets = {
            'color': forms.TextInput(attrs={'type': 'color', 'class': 'form-control form-control-color', 'title': 'Selecionar cor'}),
        }


class WorkspaceForm(forms.ModelForm):
    class Meta:
        model = Workspace
        fields = ['name', 'slug']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nome do workspace', 'aria-label': 'Nome do workspace'}),
            'slug': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'slug-para-url', 'aria-label': 'Slug do workspace'}),
        }

    def clean_slug(self):
        slug = self.cleaned_data.get('slug') or slugify(self.cleaned_data.get('name', ''))
        slug = slug.lower()
        base = slug
        counter = 1
        while Workspace.objects.filter(slug=slug).exists():
            slug = f"{base}-{counter}"
            counter += 1
        return slug


class WorkspaceSlugForm(forms.Form):
    workspace_slug = forms.SlugField(
        label="Slug do workspace",
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'ex.: minha-equipe', 'aria-label': 'Slug do workspace'}),
    )


class WorkspaceMemberInviteForm(forms.Form):
    name = forms.CharField(label='Nome', required=False, widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nome do respons\u00e1vel'}))
    email = forms.EmailField(label="Email", widget=forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'email@dominio.com'}))
    role = forms.ChoiceField(
        choices=(("member", "Membro"), ("owner", "Owner")),
        widget=forms.Select(attrs={'class': 'form-select'})
    )


class StatementUploadForm(forms.Form):
    file = forms.FileField(
        label="Arquivo de extrato (CSV ou PDF)",
        help_text='Use CSV (descri\u00e7\u00e3o, data, valor, tipo opcional, categoria opcional) ou PDF simples do extrato.',
    )

    def clean_file(self):
        f = self.cleaned_data['file']
        if f.size > 5 * 1024 * 1024:
            raise forms.ValidationError("Arquivo acima de 5MB.")
        return f


class SignupForm(UserCreationForm):
    first_name = forms.CharField(
        label="Nome",
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nome'})
    )
    last_name = forms.CharField(
        label="Sobrenome",
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Sobrenome'})
    )
    workspace_name = forms.CharField(
        label="Workspace",
        required=False,
        help_text='Cria um workspace inicial para voc\u00ea.',
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Financeiro pessoal'})
    )
    billing_cycle = forms.ChoiceField(
        label="Plano de cobran\u00e7a",
        choices=UserProfile.BILLING_CHOICES,
        required=True,
        widget=forms.Select(attrs={'class': 'form-select'}),
        help_text='Escolha entre mensal ou anual.'
    )
    avatar = forms.ImageField(label="Foto", required=False)
    invite_code = forms.CharField(
        label="Tenho convite",
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Código do convite'}),
        help_text='Opcional. Use um convite para ativar o plano sem cobrança.',
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email", "password1", "password2", "workspace_name", "billing_cycle", "invite_code", "avatar")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault('class', 'form-select')
            else:
                field.widget.attrs.setdefault('class', 'form-control')
            field.widget.attrs.setdefault('aria-label', field.label)
        self.fields['email'].required = True

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data.get('first_name', '')
        user.last_name = self.cleaned_data.get('last_name', '')
        if commit:
            user.save()
        avatar = self.cleaned_data.get('avatar')
        billing_cycle = self.cleaned_data.get('billing_cycle') or 'monthly'
        if commit:
            from .models import UserProfile
            profile, _ = UserProfile.objects.get_or_create(user=user)
            if avatar:
                profile.avatar = avatar
            profile.billing_cycle = billing_cycle
            profile.save(update_fields=['avatar', 'billing_cycle', 'created_at'])
        return user


class GuestSignupForm(UserCreationForm):
    first_name = forms.CharField(
        label="Nome",
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nome'})
    )
    last_name = forms.CharField(
        label="Sobrenome",
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Sobrenome'})
    )
    avatar = forms.ImageField(label="Foto", required=False)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email", "password1", "password2", "avatar")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault('class', 'form-select')
            else:
                field.widget.attrs.setdefault('class', 'form-control')
            field.widget.attrs.setdefault('aria-label', field.label)
        self.fields['email'].required = True

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data.get('first_name', '')
        user.last_name = self.cleaned_data.get('last_name', '')
        if commit:
            user.save()
        avatar = self.cleaned_data.get('avatar')
        if commit:
            from .models import UserProfile
            profile, _ = UserProfile.objects.get_or_create(user=user)
            if avatar:
                profile.avatar = avatar
                profile.save(update_fields=['avatar', 'created_at'])
        return user


class LoginForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')
            field.widget.attrs.setdefault('aria-label', field.label)

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if user.is_superuser:
            return
        profile = getattr(user, "profile", None)
        if not profile or not profile.is_approved:
            raise ValidationError('Cadastro pendente de aprova\u00e7\u00e3o.', code='not_approved')
        if profile.subscription_expires and profile.subscription_expires < timezone.localdate():
            grace_days = int(getattr(settings, 'SUBSCRIPTION_GRACE_DAYS', 7))
            if timezone.localdate() > (profile.subscription_expires + timedelta(days=grace_days)):
                raise ValidationError("Assinatura expirada. Fale com o administrador.", code="expired")


class PasswordResetRequestForm(PasswordResetForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].widget.attrs.setdefault('class', 'form-control')
        self.fields['email'].widget.attrs.setdefault('aria-label', 'E-mail')


class UserAdminForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['username', 'email', 'first_name', 'last_name', 'is_active', 'is_staff']
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
        }


class UserProfileAdminForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ['plan', 'billing_cycle', 'payment_confirmed', 'subscription_expires', 'payment_notes', 'is_approved', 'is_guest']
        widgets = {
            'plan': forms.Select(attrs={'class': 'form-select'}),
            'billing_cycle': forms.Select(attrs={'class': 'form-select'}),
            'payment_confirmed': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'subscription_expires': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'payment_notes': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Observa\u00e7\u00f5es do pagamento'}),
            'is_approved': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_guest': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'plan': 'Plano',
            'billing_cycle': 'Ciclo de cobran\u00e7a',
            'payment_confirmed': 'Pagamento confirmado',
            'subscription_expires': 'Expira em',
            'payment_notes': 'Observa\u00e7\u00f5es',
            'is_approved': 'Conta aprovada',
            'is_guest': 'Conta convidado',
        }


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['username', 'email', 'first_name', 'last_name']
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
        }


class ProfileAvatarForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ['avatar']
        widgets = {
            'avatar': forms.ClearableFileInput(attrs={'class': 'form-control'}),
        }


class NotificationPreferencesForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = [
            'notify_balance_threshold',
            'balance_threshold',
            'notify_task_completed',
            'notify_step_completed',
            'notify_task_reminder',
            'notify_task_overdue',
            'digest_weekly',
            'digest_monthly',
            'notify_budget_alerts',
            'notify_goal_alerts',
            'notify_admin_new_account',
            'notify_admin_payment_request',
            'notify_admin_contact',
        ]
        labels = {
            'notify_balance_threshold': 'Alertar quando o saldo atingir um valor',
            'balance_threshold': 'Limite de saldo (R$)',
            'notify_task_completed': 'Notificar quando uma tarefa for conclu\u00edda',
            'notify_step_completed': 'Notificar quando uma etapa for conclu\u00edda',
            'notify_task_reminder': 'Receber lembrete de tarefas pr\u00f3ximas do prazo',
            'notify_task_overdue': 'Receber aviso de tarefas atrasadas',
            'digest_weekly': 'Receber resumo semanal com insights',
            'digest_monthly': 'Receber resumo mensal com insights',
            'notify_budget_alerts': 'Notificar quando or\u00e7amentos por categoria forem atingidos',
            'notify_goal_alerts': 'Notificar quando metas de saldo forem atingidas',
            'notify_admin_new_account': 'Receber aviso de novos cadastros pendentes',
            'notify_admin_payment_request': 'Receber aviso de solicita\u00e7\u00f5es de pagamento',
            'notify_admin_contact': 'Receber mensagens enviadas pelo contato',
        }
        widgets = {
            'balance_threshold': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0', 'placeholder': 'Ex.: 5000'}),
            'notify_balance_threshold': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_task_completed': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_step_completed': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_task_reminder': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_task_overdue': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'digest_weekly': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'digest_monthly': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_budget_alerts': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_goal_alerts': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_admin_new_account': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_admin_payment_request': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_admin_contact': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        help_texts = {
            'balance_threshold': 'Considera o saldo acumulado do workspace.',
            'notify_task_reminder': 'Envia alertas de tarefas com prazo pr\u00f3ximo.',
            'notify_task_overdue': 'Envia alertas quando tarefas ficam atrasadas.',
            'digest_weekly': 'Envia um resumo semanal do desempenho.',
            'digest_monthly': 'Envia um resumo mensal com varia\u00e7\u00f5es.',
            'notify_budget_alerts': 'Usa os or\u00e7amentos configurados por categoria.',
            'notify_goal_alerts': 'Usa as metas de saldo configuradas no workspace.',
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not (user and user.is_superuser):
            for field in ('notify_admin_new_account', 'notify_admin_payment_request', 'notify_admin_contact'):
                self.fields.pop(field, None)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('notify_balance_threshold') and not cleaned.get('balance_threshold'):
            self.add_error('balance_threshold', 'Informe um limite para ativar o alerta de saldo.')
        return cleaned


class CategoryBudgetForm(BaseStyledForm):
    class Meta:
        model = CategoryBudget
        fields = ['category', 'period', 'limit_value', 'notify_owner']
        widgets = {
            'category': forms.Select(attrs={'class': 'form-select'}),
            'period': forms.Select(attrs={'class': 'form-select'}),
            'limit_value': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0'}),
            'notify_owner': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'category': 'Categoria',
            'period': 'Per\u00edodo',
            'limit_value': 'Limite',
            'notify_owner': 'Notificar owner quando atingir o limite',
        }


class BalanceGoalForm(BaseStyledForm):
    class Meta:
        model = BalanceGoal
        fields = ['period', 'direction', 'target_value', 'notify_owner']
        widgets = {
            'period': forms.Select(attrs={'class': 'form-select'}),
            'direction': forms.Select(attrs={'class': 'form-select'}),
            'target_value': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0'}),
            'notify_owner': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'period': 'Per\u00edodo',
            'direction': 'Dire\u00e7\u00e3o',
            'target_value': 'Meta de saldo',
            'notify_owner': 'Notificar owner quando atingir',
        }


class ContactAdminForm(forms.Form):
    TOPIC_CHOICES = (
        ('general', 'D\u00favida geral'),
        ('payment', 'Pagamento / assinatura'),
        ('support', 'Suporte t\u00e9cnico'),
        ('improvement', 'Sugest\u00e3o de melhoria'),
    )
    topic = forms.ChoiceField(label='Assunto', choices=TOPIC_CHOICES, widget=forms.Select(attrs={'class': 'form-select'}))
    subject = forms.CharField(label='T\u00edtulo', widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Resumo do contato'}))
    message = forms.CharField(label='Mensagem', widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 5, 'placeholder': 'Escreva sua mensagem'}))


class InviteByUsernameForm(forms.Form):
    username = forms.CharField(label="Username", widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'username'}))
    role = forms.ChoiceField(choices=(('member', 'Membro'), ('owner', 'Owner')), widget=forms.Select(attrs={'class': 'form-select'}))


class AccessRequestForm(forms.ModelForm):
    slug = forms.SlugField(label="Slug do workspace", widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'ex.: principal'}))

    class Meta:
        model = WorkspaceAccessRequest
        fields = ['slug']


class SubscriptionInviteForm(forms.ModelForm):
    code = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Deixe vazio para gerar'}))

    class Meta:
        model = SubscriptionInvite
        fields = ['code', 'plan_cycle', 'max_uses', 'expires_at', 'is_active']
        widgets = {
            'code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Deixe vazio para gerar'}),
            'plan_cycle': forms.Select(attrs={'class': 'form-select'}),
            'max_uses': forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
            'expires_at': forms.DateTimeInput(attrs={'class': 'form-control', 'type': 'datetime-local'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class PricingConfigForm(BaseStyledForm):
    class Meta:
        model = PricingConfig
        fields = [
            'promo_active',
            'promo_limit',
            'promo_label',
            'promo_monthly_price',
            'promo_annual_price',
            'regular_monthly_price',
            'regular_annual_price',
        ]
        labels = {
            'promo_active': 'Promo\u00e7\u00e3o ativa',
            'promo_limit': 'Limite de usu\u00e1rios da promo\u00e7\u00e3o',
            'promo_label': 'R\u00f3tulo da promo\u00e7\u00e3o',
            'promo_monthly_price': 'Pre\u00e7o mensal (promo)',
            'promo_annual_price': 'Pre\u00e7o anual/m\u00eas (promo)',
            'regular_monthly_price': 'Pre\u00e7o mensal padr\u00e3o',
            'regular_annual_price': 'Pre\u00e7o anual/m\u00eas padr\u00e3o',
        }


