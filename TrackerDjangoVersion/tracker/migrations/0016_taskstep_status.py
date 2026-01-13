from django.db import migrations, models


def forwards_set_status(apps, schema_editor):
    TaskStep = apps.get_model('tracker', 'TaskStep')
    TaskStep.objects.filter(done=True).update(status='done')
    TaskStep.objects.filter(done=False).update(status='ongoing')


def backwards_set_done(apps, schema_editor):
    TaskStep = apps.get_model('tracker', 'TaskStep')
    TaskStep.objects.filter(status='done').update(done=True)
    TaskStep.objects.exclude(status='done').update(done=False)


class Migration(migrations.Migration):
    dependencies = [
        ('tracker', '0015_workspaceinvite'),
    ]

    operations = [
        migrations.AddField(
            model_name='taskstep',
            name='status',
            field=models.CharField(choices=[('ongoing', 'Em andamento'), ('done', 'Conclu\u00edda'), ('cancelled', 'Cancelada')], default='ongoing', max_length=20),
        ),
        migrations.RunPython(forwards_set_status, backwards_set_done),
    ]
