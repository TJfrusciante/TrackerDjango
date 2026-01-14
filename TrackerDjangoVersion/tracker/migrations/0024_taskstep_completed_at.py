from django.db import migrations, models
from django.db.models import F


def populate_completed_at(apps, schema_editor):
    TaskStep = apps.get_model('tracker', 'TaskStep')
    TaskStep.objects.filter(status='done', completed_at__isnull=True).update(completed_at=F('created_at'))


def clear_completed_at(apps, schema_editor):
    TaskStep = apps.get_model('tracker', 'TaskStep')
    TaskStep.objects.update(completed_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0023_pricingconfig'),
    ]

    operations = [
        migrations.AddField(
            model_name='taskstep',
            name='completed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(populate_completed_at, clear_completed_at),
    ]
