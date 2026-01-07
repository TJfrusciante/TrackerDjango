from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()


class Category(models.Model):
    name = models.CharField(max_length=80)
    color = models.CharField(max_length=20, blank=True, default='')
    workspace = models.ForeignKey('Workspace', null=True, blank=True, on_delete=models.SET_NULL, related_name='categories')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        unique_together = ('workspace', 'name')
        indexes = [models.Index(fields=['workspace', 'name'])]

    def __str__(self):
        return self.name


class Task(models.Model):
    STATUS_CHOICES = [
        ('ongoing', 'Em andamento'),
        ('done', 'Finalizada'),
    ]

    title = models.CharField(max_length=180)
    due_date = models.DateField()
    selected = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ongoing')
    responsible = models.CharField(max_length=120, blank=True, default='')
    responsible_email = models.EmailField(blank=True, default='')
    progress = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    workspace = models.ForeignKey('Workspace', null=True, blank=True, on_delete=models.SET_NULL, related_name='tasks')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-due_date', '-created_at']
        indexes = [
            models.Index(fields=['workspace', 'status']),
            models.Index(fields=['workspace', 'due_date']),
        ]

    def __str__(self):
        return self.title


class Transaction(models.Model):
    TYPE_CHOICES = [
        ('income', 'Entrada'),
        ('expense', 'Saída'),
    ]

    description = models.CharField(max_length=180)
    date = models.DateField()
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='transactions')
    value = models.DecimalField(max_digits=12, decimal_places=2)
    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    selected = models.BooleanField(default=False)
    workspace = models.ForeignKey('Workspace', null=True, blank=True, on_delete=models.SET_NULL, related_name='transactions')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date', '-created_at']
        indexes = [
            models.Index(fields=['workspace', 'date']),
            models.Index(fields=['workspace', 'type']),
            models.Index(fields=['category', 'date']),
        ]

    def __str__(self):
        return f'{self.description} ({self.date})'

    @property
    def signed_amount(self):
        return self.value if self.type == 'income' else -self.value


class TaskStep(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='steps')
    title = models.CharField(max_length=180)
    order = models.PositiveIntegerField(default=0)
    responsible = models.CharField(max_length=120, blank=True, default='')
    responsible_email = models.EmailField(blank=True, default='')
    done = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'created_at']

    def __str__(self):
        return f'{self.title} ({ "feito" if self.done else "pendente" })'


class Workspace(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='owned_workspaces')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class WorkspaceMembership(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='memberships')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='workspace_memberships')
    role = models.CharField(max_length=20, default='member')
    can_edit_tasks = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'workspace')
        indexes = [models.Index(fields=['workspace', 'user'])]

    def __str__(self):
        return f'{self.user} @ {self.workspace} ({self.role})'


class UserProfile(models.Model):
    PLAN_CHOICES = [
        ('starter', 'B\u00e1sico'),
        ('pro', 'Pro'),
        ('team', 'Equipe'),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    is_approved = models.BooleanField(default=False)
    is_guest = models.BooleanField(default=False)
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default='starter')
    payment_confirmed = models.BooleanField(default=False)
    payment_confirmed_at = models.DateTimeField(null=True, blank=True)
    payment_notes = models.CharField(max_length=255, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='approved_profiles')
    subscription_expires = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Perfil de {self.user}'


class WorkspaceAccessRequest(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pendente'),
        ('approved', 'Aprovado'),
        ('rejected', 'Rejeitado'),
    ]
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='access_requests')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='workspace_requests')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('workspace', 'user')
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user} -> {self.workspace} ({self.status})'

# Create your models here.
