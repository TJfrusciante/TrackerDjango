from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tracker", "0035_task_transaction_icons"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="sidebar_hover_expand",
            field=models.BooleanField(default=True),
        ),
    ]
