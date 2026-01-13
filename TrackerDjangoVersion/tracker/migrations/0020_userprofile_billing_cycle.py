from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0019_rename_tracker_bal_worksp_6fb244_idx_tracker_bal_workspa_62b22e_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='billing_cycle',
            field=models.CharField(choices=[('monthly', 'Mensal'), ('annual', 'Anual')], default='monthly', max_length=20),
        ),
    ]
