from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0003_user_role_default'),
    ]

    operations = [
        migrations.AddField(
            model_name='assignment',
            name='duration_minutes',
            field=models.PositiveIntegerField(
                default=60,
                help_text='Exam duration in minutes. The countdown starts the moment a pupil opens the assignment.',
            ),
        ),
        migrations.AddField(
            model_name='submission',
            name='started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
