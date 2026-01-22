from django.conf import settings
from django.db import models

from tracker.models import Category, Transaction, Workspace


class WhatsAppProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='whatsapp_profile')
    phone_number = models.CharField(max_length=32, unique=True)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='whatsapp_profiles')
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['phone_number'])]

    def __str__(self):
        return f'{self.user} ({self.phone_number})'


class ParsedTransaction(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pendente'),
        ('confirmed', 'Confirmado'),
        ('rejected', 'Rejeitado'),
        ('corrected', 'Corrigido'),
    ]
    TYPE_CHOICES = [
        ('income', 'Entrada'),
        ('expense', 'Saida'),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='whatsapp_parsed_transactions')
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='whatsapp_parsed_transactions')
    transaction = models.ForeignKey(Transaction, null=True, blank=True, on_delete=models.SET_NULL, related_name='whatsapp_parsed_transactions')
    source_message = models.ForeignKey('WhatsAppMessage', null=True, blank=True, on_delete=models.SET_NULL, related_name='parsed_entries')
    description = models.CharField(max_length=200, blank=True, default='')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    date = models.DateField()
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL, related_name='whatsapp_parsed_transactions')
    category_label = models.CharField(max_length=120, blank=True, default='')
    requires_confirmation = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['workspace', 'status']),
            models.Index(fields=['user', 'created_at']),
        ]

    def __str__(self):
        return f'{self.user} {self.amount} {self.type} ({self.status})'


class WhatsAppMessage(models.Model):
    DIRECTION_CHOICES = [
        ('in', 'Entrada'),
        ('out', 'Saida'),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='whatsapp_messages')
    workspace = models.ForeignKey(Workspace, null=True, blank=True, on_delete=models.SET_NULL, related_name='whatsapp_messages')
    message_sid = models.CharField(max_length=120, blank=True, default='')
    direction = models.CharField(max_length=8, choices=DIRECTION_CHOICES, default='in')
    from_number = models.CharField(max_length=32, blank=True, default='')
    to_number = models.CharField(max_length=32, blank=True, default='')
    body = models.TextField(blank=True, default='')
    media_url = models.URLField(blank=True, default='')
    media_type = models.CharField(max_length=80, blank=True, default='')
    raw_payload = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['message_sid', 'created_at'])]

    def __str__(self):
        return f'{self.from_number} -> {self.to_number} ({self.direction})'


class CategoryPreference(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='whatsapp_category_preferences')
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='whatsapp_category_preferences')
    keyword = models.CharField(max_length=80)
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='whatsapp_category_preferences')
    usage_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'workspace', 'keyword')
        indexes = [models.Index(fields=['workspace', 'keyword'])]

    def __str__(self):
        return f'{self.keyword} -> {self.category}'
