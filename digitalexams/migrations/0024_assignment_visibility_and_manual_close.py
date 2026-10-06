from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0023_submission_grade_seen_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='assignment',
            name='is_hidden',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='assignment',
            name='closed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='assignment',
            name='duration_minutes',
            field=models.PositiveIntegerField(default=60),
        ),
    ]
