from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0016_taskstep_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='notify_balance_threshold',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='balance_threshold',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='balance_alert_last_value',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='notify_task_completed',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='notify_step_completed',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='notify_admin_new_account',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='notify_admin_payment_request',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='notify_admin_contact',
            field=models.BooleanField(default=True),
        ),
    ]
