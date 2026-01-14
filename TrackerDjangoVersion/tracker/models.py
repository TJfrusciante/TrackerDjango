from django.db import models
from django.utils import timezone
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
    completed_at = models.DateTimeField(null=True, blank=True)
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
        ('expense', 'Sa\u00edda'),
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
    STATUS_CHOICES = [
        ('ongoing', 'Em andamento'),
        ('done', 'Conclu\u00edda'),
        ('cancelled', 'Cancelada'),
    ]
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='steps')
    title = models.CharField(max_length=180)
    order = models.PositiveIntegerField(default=0)
    responsible = models.CharField(max_length=120, blank=True, default='')
    responsible_email = models.EmailField(blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ongoing')
    done = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'created_at']

    def __str__(self):
        return f'{self.title} ({self.status})'

    def save(self, *args, **kwargs):
        self.done = self.status == 'done'
        if self.status == 'done':
            if not self.completed_at:
                self.completed_at = timezone.now()
        else:
            self.completed_at = None
        super().save(*args, **kwargs)


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


class WorkspaceInvite(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pendente'),
        ('accepted', 'Aceito'),
        ('declined', 'Recusado'),
    ]
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='invites')
    invited_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_workspace_invites')
    invited_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='workspace_invites')
    role = models.CharField(max_length=20, default='member')
    can_edit_tasks = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['workspace', 'invited_user', 'status'], name='workspace_invite_status_idx')]

    def __str__(self):
        return f'{self.invited_user} -> {self.workspace} ({self.status})'


class UserProfile(models.Model):
    PLAN_CHOICES = [
        ('starter', 'B\u00e1sico'),
        ('pro', 'Pro'),
        ('team', 'Equipe'),
    ]
    BILLING_CHOICES = [
        ('monthly', 'Mensal'),
        ('annual', 'Anual'),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    is_approved = models.BooleanField(default=False)
    is_guest = models.BooleanField(default=False)
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default='starter')
    billing_cycle = models.CharField(max_length=20, choices=BILLING_CHOICES, default='monthly')
    payment_confirmed = models.BooleanField(default=False)
    payment_confirmed_at = models.DateTimeField(null=True, blank=True)
    payment_notes = models.CharField(max_length=255, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='approved_profiles')
    subscription_expires = models.DateField(null=True, blank=True)
    notify_balance_threshold = models.BooleanField(default=False)
    balance_threshold = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    balance_alert_last_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    notify_task_completed = models.BooleanField(default=False)
    notify_step_completed = models.BooleanField(default=False)
    notify_task_reminder = models.BooleanField(default=False)
    notify_task_overdue = models.BooleanField(default=False)
    digest_weekly = models.BooleanField(default=False)
    digest_monthly = models.BooleanField(default=False)
    digest_weekly_last_sent = models.DateField(null=True, blank=True)
    digest_monthly_last_sent = models.DateField(null=True, blank=True)
    notify_budget_alerts = models.BooleanField(default=True)
    notify_goal_alerts = models.BooleanField(default=True)
    notify_admin_new_account = models.BooleanField(default=True)
    notify_admin_payment_request = models.BooleanField(default=True)
    notify_admin_contact = models.BooleanField(default=True)
    email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    deletion_requested_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Perfil de {self.user}'


class PricingConfig(models.Model):
    promo_active = models.BooleanField(default=True)
    promo_limit = models.PositiveIntegerField(default=100)
    promo_monthly_price = models.DecimalField(max_digits=8, decimal_places=2, default=8.99)
    promo_annual_price = models.DecimalField(max_digits=8, decimal_places=2, default=5.99)
    regular_monthly_price = models.DecimalField(max_digits=8, decimal_places=2, default=14.99)
    regular_annual_price = models.DecimalField(max_digits=8, decimal_places=2, default=9.99)
    promo_label = models.CharField(max_length=120, default='Promo\u00e7\u00e3o de lan\u00e7amento')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Configura\u00e7\u00e3o de pre\u00e7os'
        verbose_name_plural = 'Configura\u00e7\u00f5es de pre\u00e7os'

    def __str__(self):
        return self.promo_label or 'Configura\u00e7\u00e3o de pre\u00e7os'

    @classmethod
    def get_solo(cls):
        obj = cls.objects.first()
        if obj:
            return obj
        return cls.objects.create()


class SubscriptionInvite(models.Model):
    PLAN_CHOICES = UserProfile.BILLING_CHOICES
    code = models.CharField(max_length=40, unique=True)
    plan_cycle = models.CharField(max_length=20, choices=PLAN_CHOICES, default='monthly')
    max_uses = models.PositiveIntegerField(default=1)
    used_count = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='subscription_invites')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['code', 'is_active'])]

    def __str__(self):
        return f'Convite {self.code} ({self.plan_cycle})'

    def can_use(self, now=None):
        from django.utils import timezone
        check = now or timezone.now()
        if not self.is_active:
            return False
        if self.expires_at and self.expires_at < check:
            return False
        return self.used_count < self.max_uses


class SubscriptionInviteUse(models.Model):
    invite = models.ForeignKey(SubscriptionInvite, on_delete=models.CASCADE, related_name='uses')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='subscription_invites_used')
    used_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('invite', 'user')
        indexes = [models.Index(fields=['invite', 'used_at'])]

    def __str__(self):
        return f'{self.user} -> {self.invite}'


class MetricEvent(models.Model):
    EVENT_CHOICES = [
        ('login_success', 'Login: sucesso'),
        ('login_failed', 'Login: falha'),
        ('login_blocked', 'Login: bloqueado'),
        ('signup', 'Cadastro'),
        ('invite_sent', 'Convite enviado'),
        ('invite_accepted', 'Convite aceito'),
        ('payment_confirmed', 'Pagamento confirmado'),
        ('subscription_renewed', 'Assinatura renovada'),
        ('ai_request', 'IA: requisi\u00e7\u00e3o'),
        ('data_export', 'Exporta\u00e7\u00e3o de dados'),
        ('deletion_request', 'Solicita\u00e7\u00e3o de exclus\u00e3o'),
        ('password_reset', 'Recupera\u00e7\u00e3o de senha'),
    ]
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='metric_events')
    workspace = models.ForeignKey('Workspace', null=True, blank=True, on_delete=models.SET_NULL, related_name='metric_events')
    event_type = models.CharField(max_length=40, choices=EVENT_CHOICES)
    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['event_type', 'created_at']),
            models.Index(fields=['user', 'created_at']),
        ]

    def __str__(self):
        return f'{self.event_type} ({self.created_at:%Y-%m-%d})'


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


class Notification(models.Model):
    LEVEL_CHOICES = [
        ('info', 'Info'),
        ('success', 'Sucesso'),
        ('warning', 'Alerta'),
        ('danger', 'Urgente'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    workspace = models.ForeignKey('Workspace', null=True, blank=True, on_delete=models.SET_NULL, related_name='notifications')
    title = models.CharField(max_length=140)
    body = models.TextField(blank=True, default='')
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default='info')
    action_url = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'read_at']),
            models.Index(fields=['workspace', 'created_at']),
        ]

    def __str__(self):
        return f'Notifica\u00e7\u00e3o {self.user} ({self.title})'


class CategoryBudget(models.Model):
    PERIOD_CHOICES = [
        ('weekly', 'Semanal'),
        ('monthly', 'Mensal'),
    ]
    workspace = models.ForeignKey('Workspace', on_delete=models.CASCADE, related_name='category_budgets')
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='budgets')
    period = models.CharField(max_length=20, choices=PERIOD_CHOICES, default='monthly')
    limit_value = models.DecimalField(max_digits=12, decimal_places=2)
    notify_owner = models.BooleanField(default=True)
    last_notified_total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    last_notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('workspace', 'category', 'period')
        indexes = [models.Index(fields=['workspace', 'period'])]

    def __str__(self):
        return f'Or\u00e7amento {self.category} ({self.period})'


class BalanceGoal(models.Model):
    PERIOD_CHOICES = [
        ('weekly', 'Semanal'),
        ('monthly', 'Mensal'),
    ]
    DIRECTION_CHOICES = [
        ('min', 'M\u00ednimo'),
        ('max', 'M\u00e1ximo'),
    ]
    workspace = models.ForeignKey('Workspace', on_delete=models.CASCADE, related_name='balance_goals')
    period = models.CharField(max_length=20, choices=PERIOD_CHOICES, default='monthly')
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES, default='min')
    target_value = models.DecimalField(max_digits=12, decimal_places=2)
    notify_owner = models.BooleanField(default=True)
    last_notified_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    last_notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['workspace', 'period'])]

    def __str__(self):
        return f'Meta saldo {self.workspace} ({self.period})'


class PushSubscription(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='push_subscriptions')
    endpoint = models.TextField()
    p256dh = models.CharField(max_length=255, blank=True, default='')
    auth = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'endpoint')
        indexes = [models.Index(fields=['user', 'updated_at'])]

    def __str__(self):
        return f'Push {self.user}'

# Create your models here.
