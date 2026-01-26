from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0024_taskstep_completed_at'),
    ]

    operations = [
        migrations.CreateModel(
            name='TaskCategory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=80)),
                ('color', models.CharField(blank=True, default='', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('workspace', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='task_categories', to='tracker.workspace')),
            ],
            options={
                'ordering': ['name'],
                'unique_together': {('workspace', 'name')},
            },
        ),
        migrations.AddField(
            model_name='task',
            name='task_category',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='tasks', to='tracker.taskcategory'),
        ),
        migrations.AddIndex(
            model_name='taskcategory',
            index=models.Index(fields=['workspace', 'name'], name='taskcategory_workspace_name_idx'),
        ),
    ]
