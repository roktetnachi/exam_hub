from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0008_submission_responses_per_text'),
    ]

    operations = [
        migrations.AlterField(
            model_name='assignment',
            name='due_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='assignment',
            name='delivery_mode',
            field=models.CharField(
                choices=[
                    ('browser', 'Any browser'),
                    ('browser_secure', 'Any browser with security'),
                    ('lockdown', 'Lockdown app'),
                    ('paper', 'Paper'),
                ],
                default='browser',
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='assignment',
            name='require_join_code',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='assignment',
            name='join_code',
            field=models.CharField(blank=True, default='', max_length=8),
        ),
        migrations.AddField(
            model_name='assignment',
            name='allow_calculator',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='assignment',
            name='allow_graphing_calculator',
            field=models.BooleanField(default=False),
        ),
    ]
