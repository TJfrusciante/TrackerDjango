from __future__ import annotations

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0006_category_workspace_userprofile'),
    ]

    operations = [
        migrations.CreateModel(
            name='WorkspaceAccessRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(choices=[('pending', 'Pendente'), ('approved', 'Aprovado'), ('rejected', 'Rejeitado')], default='pending', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='workspace_requests', to=settings.AUTH_USER_MODEL)),
                ('workspace', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='access_requests', to='tracker.workspace')),
            ],
            options={
                'ordering': ['-created_at'],
                'unique_together': {('workspace', 'user')},
            },
        ),
    ]
