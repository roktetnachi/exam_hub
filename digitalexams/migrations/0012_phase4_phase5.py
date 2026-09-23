import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0011_assignment_items'),
    ]

    operations = [
        migrations.AddField(
            model_name='assignmentitem',
            name='points',
            field=models.FloatField(default=1.0),
        ),
        migrations.AddField(
            model_name='recipient',
            name='extra_minutes',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='recipient',
            name='opens_override',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='submission',
            name='item_scores',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='submission',
            name='total_score',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='submission',
            name='max_score',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='submission',
            name='graded_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='submission',
            name='published',
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name='Message',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sender_role', models.CharField(choices=[('teacher', 'Teacher'), ('pupil', 'Pupil')], max_length=10)),
                ('sender_name', models.CharField(max_length=120)),
                ('text', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('recipient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='messages', to='digitalexams.recipient')),
            ],
            options={
                'ordering': ['created_at'],
            },
        ),
        migrations.CreateModel(
            name='ActivityLogEntry',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('actor_name', models.CharField(blank=True, max_length=120)),
                ('actor_email', models.EmailField(blank=True, max_length=254)),
                ('action', models.CharField(max_length=40)),
                ('description', models.CharField(max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('assignment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='activity_entries', to='digitalexams.assignment')),
            ],
            options={
                'ordering': ['-created_at'],
                'verbose_name_plural': 'Activity log entries',
            },
        ),
    ]
