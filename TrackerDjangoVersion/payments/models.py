from django.conf import settings
from django.db import models


class MpSubscription(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pendente'),
        ('authorized', 'Autorizada'),
        ('active', 'Ativa'),
        ('paused', 'Pausada'),
        ('cancelled', 'Cancelada'),
        ('expired', 'Expirada'),
        ('rejected', 'Rejeitada'),
    ]
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='mp_subscription')
    plan_cycle = models.CharField(max_length=20, choices=[('monthly', 'Mensal'), ('annual', 'Anual')], default='monthly')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    preapproval_id = models.CharField(max_length=120, blank=True, default='')
    init_point = models.URLField(blank=True, default='')
    payer_email = models.EmailField(blank=True, default='')
    reason = models.CharField(max_length=140, blank=True, default='')
    auto_recurring = models.JSONField(blank=True, default=dict)
    last_event_at = models.DateTimeField(null=True, blank=True)
    last_payment_status = models.CharField(max_length=40, blank=True, default='')
    next_payment_at = models.DateTimeField(null=True, blank=True)
    grace_until = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user} ({self.plan_cycle})'


class MpWebhookEvent(models.Model):
    topic = models.CharField(max_length=80, blank=True, default='')
    mp_id = models.CharField(max_length=120, blank=True, default='')
    payload = models.JSONField(blank=True, default=dict)
    processed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['topic', 'mp_id'])]

    def __str__(self):
        return f'{self.topic}:{self.mp_id}'

# Create your models here.
