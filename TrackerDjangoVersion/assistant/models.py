from django.db import models
from django.conf import settings

from tracker.models import Workspace


class ChatMessage(models.Model):
    ROLE_CHOICES = [
        ('user', 'Usuário'),
        ('assistant', 'Assistente'),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='chat_messages')
    workspace = models.ForeignKey(Workspace, null=True, blank=True, on_delete=models.SET_NULL, related_name='chat_messages')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.user} ({self.role}) {self.created_at:%Y-%m-%d %H:%M}'
