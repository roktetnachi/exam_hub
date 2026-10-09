from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0028_teacher_marks_and_seen'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExamResource',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('pdf', 'PDF'), ('image', 'Image'), ('text', 'Text')], max_length=8)),
                ('title', models.CharField(max_length=120)),
                ('file', models.FileField(blank=True, null=True, upload_to='resources/%Y/%m/')),
                ('text', models.TextField(blank=True)),
                ('order', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('assignment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='resources', to='digitalexams.assignment')),
            ],
            options={'ordering': ['order', 'id']},
        ),
    ]
