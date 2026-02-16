from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("whatsapp_finance", "0002_rename_whatsapp_fi_workspa_9f363a_idx_whatsapp_fi_workspa_e38eff_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="whatsappmessage",
            name="provider",
            field=models.CharField(
                choices=[("twilio", "Twilio"), ("360dialog", "360dialog"), ("meta", "Meta")],
                default="twilio",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="whatsappmessage",
            name="provider_message_id",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddConstraint(
            model_name="whatsappmessage",
            constraint=models.UniqueConstraint(
                fields=("provider", "provider_message_id"),
                condition=models.Q(provider_message_id__isnull=False) & ~models.Q(provider_message_id=""),
                name="whatsapp_provider_message_uniq",
            ),
        ),
    ]
