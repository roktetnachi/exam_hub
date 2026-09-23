import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0004_assignment_duration_submission_started_at'),
    ]

    operations = [
        migrations.CreateModel(
            name='AssignmentText',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label', models.CharField(max_length=60)),
                ('pdf', models.FileField(upload_to='assignments/%Y/%m/')),
                ('order', models.PositiveIntegerField(default=0)),
                ('assignment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='texts', to='digitalexams.assignment')),
            ],
            options={'ordering': ['order', 'id']},
        ),
        migrations.AddField(
            model_name='submission',
            name='response_text',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='digitalexams.assignmenttext'),
        ),
    ]
