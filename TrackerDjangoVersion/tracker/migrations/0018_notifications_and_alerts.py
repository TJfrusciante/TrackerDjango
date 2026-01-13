from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0017_userprofile_notifications'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='notify_task_reminder',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='notify_task_overdue',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='digest_weekly',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='digest_monthly',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='digest_weekly_last_sent',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='digest_monthly_last_sent',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='notify_budget_alerts',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='notify_goal_alerts',
            field=models.BooleanField(default=True),
        ),
        migrations.CreateModel(
            name='Notification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=140)),
                ('body', models.TextField(blank=True, default='')),
                (
                    'level',
                    models.CharField(
                        choices=[('info', 'Info'), ('success', 'Sucesso'), ('warning', 'Alerta'), ('danger', 'Urgente')],
                        default='info',
                        max_length=20,
                    ),
                ),
                ('action_url', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('read_at', models.DateTimeField(blank=True, null=True)),
                (
                    'user',
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notifications', to=settings.AUTH_USER_MODEL),
                ),
                (
                    'workspace',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='notifications',
                        to='tracker.workspace',
                    ),
                ),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['user', 'read_at'], name='tracker_not_user_re_66e6e0_idx'),
                    models.Index(fields=['workspace', 'created_at'], name='tracker_not_worksp_8a2bde_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='CategoryBudget',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                (
                    'period',
                    models.CharField(choices=[('weekly', 'Semanal'), ('monthly', 'Mensal')], default='monthly', max_length=20),
                ),
                ('limit_value', models.DecimalField(decimal_places=2, max_digits=12)),
                ('notify_owner', models.BooleanField(default=True)),
                ('last_notified_total', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('last_notified_at', models.DateTimeField(blank=True, null=True)),
                (
                    'category',
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='budgets', to='tracker.category'),
                ),
                (
                    'workspace',
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='category_budgets', to='tracker.workspace'),
                ),
            ],
            options={
                'indexes': [models.Index(fields=['workspace', 'period'], name='tracker_cat_worksp_1af38d_idx')],
                'unique_together': {('workspace', 'category', 'period')},
            },
        ),
        migrations.CreateModel(
            name='BalanceGoal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                (
                    'period',
                    models.CharField(choices=[('weekly', 'Semanal'), ('monthly', 'Mensal')], default='monthly', max_length=20),
                ),
                (
                    'direction',
                    models.CharField(choices=[('min', 'Minimo'), ('max', 'Maximo')], default='min', max_length=10),
                ),
                ('target_value', models.DecimalField(decimal_places=2, max_digits=12)),
                ('notify_owner', models.BooleanField(default=True)),
                ('last_notified_value', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('last_notified_at', models.DateTimeField(blank=True, null=True)),
                (
                    'workspace',
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='balance_goals', to='tracker.workspace'),
                ),
            ],
            options={'indexes': [models.Index(fields=['workspace', 'period'], name='tracker_bal_worksp_6fb244_idx')]},
        ),
        migrations.CreateModel(
            name='PushSubscription',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('endpoint', models.TextField()),
                ('p256dh', models.CharField(blank=True, default='', max_length=255)),
                ('auth', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'user',
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='push_subscriptions', to=settings.AUTH_USER_MODEL),
                ),
            ],
            options={
                'indexes': [models.Index(fields=['user', 'updated_at'], name='tracker_pus_user_upd_0be5a0_idx')],
                'unique_together': {('user', 'endpoint')},
            },
        ),
    ]
