from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0018_submission_exit_lock'),
    ]

    operations = [
        migrations.AddField(
            model_name='activitylogentry',
            name='school_class',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='activity_entries', to='digitalexams.schoolclass'),
        ),
    ]
