from __future__ import annotations

from django.conf import settings
from django.db import migrations, models, transaction
import django.db.models.deletion


def split_categories_per_workspace(apps, schema_editor):
    Category = apps.get_model('tracker', 'Category')
    Transaction = apps.get_model('tracker', 'Transaction')

    # Associa workspace às categorias baseadas em transações
    for tx in Transaction.objects.select_related('category', 'workspace').all():
        ws = tx.workspace
        cat = tx.category
        # Caso já esteja ok, siga
        if cat.workspace_id == (ws.id if ws else None):
            continue
        # cria/usa categoria equivalente no workspace
        target_cat, _ = Category.objects.get_or_create(
            name=cat.name,
            workspace=ws,
            defaults={'color': cat.color},
        )
        tx.category = target_cat
        tx.save(update_fields=['category'])

    # Deduplicar categorias por (workspace, name)
    with transaction.atomic():
        for ws_id, name in Category.objects.values_list('workspace_id', 'name').distinct():
            cats = Category.objects.filter(workspace_id=ws_id, name=name).order_by('id')
            if cats.count() > 1:
                keep = cats.first()
                others = cats.exclude(pk=keep.pk)
                # Reatribui transações
                Transaction.objects.filter(category__in=others).update(category=keep)
                others.delete()


def create_profiles(apps, schema_editor):
    User = apps.get_model(settings.AUTH_USER_MODEL.split('.')[0], settings.AUTH_USER_MODEL.split('.')[1])
    UserProfile = apps.get_model('tracker', 'UserProfile')
    for user in User.objects.all():
        UserProfile.objects.get_or_create(user=user)


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0005_workspace_workspacemembership_task_workspace_transaction_workspace'),
    ]

    operations = [
        migrations.AlterField(
            model_name='category',
            name='name',
            field=models.CharField(max_length=80),
        ),
        migrations.AddField(
            model_name='category',
            name='workspace',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='categories', to='tracker.workspace'),
        ),
        migrations.RunPython(split_categories_per_workspace, migrations.RunPython.noop),
        migrations.CreateModel(
            name='UserProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('avatar', models.ImageField(blank=True, null=True, upload_to='avatars/')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='profile', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(
            model_name='category',
            constraint=models.UniqueConstraint(fields=('workspace', 'name'), name='uniq_category_workspace_name'),
        ),
        migrations.RunPython(create_profiles, migrations.RunPython.noop),
    ]
