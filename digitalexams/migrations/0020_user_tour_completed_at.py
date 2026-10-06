from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0019_activitylogentry_school_class'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='tour_completed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
