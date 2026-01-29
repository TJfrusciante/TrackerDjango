from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0034_userprofile_grace_notified_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='task',
            name='icon',
            field=models.CharField(blank=True, default='', max_length=60),
        ),
        migrations.AddField(
            model_name='transaction',
            name='icon',
            field=models.CharField(blank=True, default='', max_length=60),
        ),
    ]
