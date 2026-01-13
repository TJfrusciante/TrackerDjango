from django.db import migrations, models
from django.db.models import F


def populate_completed_at(apps, schema_editor):
    Task = apps.get_model('tracker', 'Task')
    Task.objects.filter(status='done', completed_at__isnull=True).update(completed_at=F('updated_at'))


def clear_completed_at(apps, schema_editor):
    Task = apps.get_model('tracker', 'Task')
    Task.objects.update(completed_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0020_userprofile_billing_cycle'),
    ]

    operations = [
        migrations.AddField(
            model_name='task',
            name='completed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(populate_completed_at, clear_completed_at),
    ]
