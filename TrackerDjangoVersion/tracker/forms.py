from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.utils.text import slugify

from .models import Category, Task, TaskStep, Transaction, Workspace, UserProfile, WorkspaceAccessRequest

User = get_user_model()


class BaseStyledForm(forms.ModelForm):
    """
    Applies Bootstrap classes to inputs for consistent styling.
    """

    def _apply_css(self):
        for _, field in self.fields.items():
            base_class = 'form-select' if isinstance(field.widget, forms.Select) else 'form-control'
            css = field.widget.attrs.get('class', '')
            field.widget.attrs['class'] = f'{css} {base_class}'.strip()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_css()


class TransactionForm(BaseStyledForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['date'].input_formats = ['%d/%m/%Y', '%Y-%m-%d']
        self.fields['date'].widget.format = '%d/%m/%Y'

    class Meta:
        model = Transaction
        fields = ['description', 'date', 'category', 'value', 'type']
        widgets = {
            'date': forms.DateInput(format='%d/%m/%Y', attrs={'type': 'text', 'class': 'form-control', 'placeholder': 'dd/mm/aaaa', 'inputmode': 'numeric'}),
        }


class TaskForm(BaseStyledForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['due_date'].input_formats = ['%d/%m/%Y', '%Y-%m-%d']
        self.fields['due_date'].widget.format = '%d/%m/%Y'

    class Meta:
        model = Task
        fields = ['title', 'due_date', 'status']
        widgets = {
            'due_date': forms.DateInput(format='%d/%m/%Y', attrs={'type': 'text', 'class': 'form-control', 'placeholder': 'dd/mm/aaaa', 'inputmode': 'numeric'}),
        }


class TaskStepForm(BaseStyledForm):
    class Meta:
        model = TaskStep
        fields = ['title', 'responsible', 'responsible_email']
        widgets = {
            'title': forms.TextInput(attrs={'placeholder': 'Descrição da etapa', 'title': 'Descrição da etapa', 'class': 'form-control form-control-lg'}),
            'responsible': forms.TextInput(attrs={'placeholder': 'Responsável da etapa', 'title': 'Responsável da etapa', 'class': 'form-control form-control-lg'}),
            'responsible_email': forms.EmailInput(attrs={'placeholder': 'Email do responsável', 'title': 'Email do responsável', 'class': 'form-control form-control-lg'}),
        }


class CategoryForm(BaseStyledForm):
    class Meta:
        model = Category
        fields = ['name', 'color']
        widgets = {
            'color': forms.TextInput(attrs={'placeholder': '#00ff99'}),
        }


class WorkspaceForm(forms.ModelForm):
    class Meta:
        model = Workspace
        fields = ['name', 'slug']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nome do workspace'}),
            'slug': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'slug-para-url'}),
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
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'ex.: minha-equipe'}),
    )


class WorkspaceMemberInviteForm(forms.Form):
    name = forms.CharField(label="Nome", required=False, widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nome do responsável'}))
    email = forms.EmailField(label="Email", widget=forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'email@dominio.com'}))
    role = forms.ChoiceField(
        choices=(('member', 'Membro'), ('owner', 'Owner')),
        widget=forms.Select(attrs={'class': 'form-select'})
    )


class SignupForm(UserCreationForm):
    workspace_name = forms.CharField(
        label="Workspace",
        required=False,
        help_text="Cria um workspace inicial para vocÇˆ.",
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Financeiro pessoal'})
    )
    avatar = forms.ImageField(label="Foto", required=False)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email", "password1", "password2", "workspace_name", "avatar")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault('class', 'form-select')
            else:
                field.widget.attrs.setdefault('class', 'form-control')
        self.fields['email'].required = True

    def save(self, commit=True):
        user = super().save(commit)
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


class InviteByUsernameForm(forms.Form):
    username = forms.CharField(label="Username", widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'username'}))
    role = forms.ChoiceField(choices=(('member', 'Membro'), ('owner', 'Owner')), widget=forms.Select(attrs={'class': 'form-select'}))


class AccessRequestForm(forms.ModelForm):
    slug = forms.SlugField(label="Slug do workspace", widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'ex.: principal'}))

    class Meta:
        model = WorkspaceAccessRequest
        fields = ['slug']
