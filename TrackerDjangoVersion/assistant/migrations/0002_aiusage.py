from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("assistant", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("tracker", "0008_workspacemembership_can_edit_tasks"),
    ]

    operations = [
        migrations.CreateModel(
            name="AiUsage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "feature",
                    models.CharField(
                        choices=[("chat", "Chat"), ("import", "Importa\u00e7\u00e3o")],
                        max_length=20,
                    ),
                ),
                ("model_name", models.CharField(blank=True, default="", max_length=80)),
                ("prompt_tokens", models.PositiveIntegerField(default=0)),
                ("completion_tokens", models.PositiveIntegerField(default=0)),
                ("total_tokens", models.PositiveIntegerField(default=0)),
                ("cost_usd", models.DecimalField(decimal_places=4, default=0, max_digits=10)),
                ("cost_brl", models.DecimalField(decimal_places=4, default=0, max_digits=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_usages",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "workspace",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ai_usages",
                        to="tracker.workspace",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
