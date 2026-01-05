from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0007_workspaceaccessrequest'),
    ]

    operations = [
        migrations.AddField(
            model_name='workspacemembership',
            name='can_edit_tasks',
            field=models.BooleanField(default=True),
        ),
    ]
