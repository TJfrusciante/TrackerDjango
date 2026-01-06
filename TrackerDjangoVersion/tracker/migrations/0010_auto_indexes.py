from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0009_merge_20260104_2125'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='category',
            index=models.Index(fields=['workspace', 'name'], name='tracker_cat_workspace_name_idx'),
        ),
        migrations.AddIndex(
            model_name='task',
            index=models.Index(fields=['workspace', 'status'], name='tracker_task_workspace_status_idx'),
        ),
        migrations.AddIndex(
            model_name='task',
            index=models.Index(fields=['workspace', 'due_date'], name='tracker_task_workspace_due_idx'),
        ),
        migrations.AddIndex(
            model_name='transaction',
            index=models.Index(fields=['workspace', 'date'], name='tracker_tx_workspace_date_idx'),
        ),
        migrations.AddIndex(
            model_name='transaction',
            index=models.Index(fields=['workspace', 'type'], name='tracker_tx_workspace_type_idx'),
        ),
        migrations.AddIndex(
            model_name='transaction',
            index=models.Index(fields=['category', 'date'], name='tracker_tx_category_date_idx'),
        ),
        migrations.AddIndex(
            model_name='workspacemembership',
            index=models.Index(fields=['workspace', 'user'], name='tracker_membership_ws_user_idx'),
        ),
    ]
