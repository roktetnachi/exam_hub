from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0029_examresource'),
    ]

    operations = [
        # Exams that already exist keep spell check on (as before this setting existed); new exams
        # start with it off.
        migrations.AddField(
            model_name='assignment',
            name='allow_spellcheck',
            field=models.BooleanField(default=True),
        ),
        migrations.AlterField(
            model_name='assignment',
            name='allow_spellcheck',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='assignmentitem',
            name='audio',
            field=models.FileField(blank=True, null=True, upload_to='assignments/%Y/%m/'),
        ),
        migrations.AddField(
            model_name='assignmentitem',
            name='audio_max_plays',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='assignmentitem',
            name='audio_allow_pause',
            field=models.BooleanField(default=True),
        ),
        migrations.AlterField(
            model_name='assignmentitem',
            name='item_type',
            field=models.CharField(choices=[('pdf', 'PDF text (annotate + write about it)'), ('image', 'Image'), ('video', 'Video'), ('long_answer', 'Long answer'), ('mcq_single', 'Multiple choice (single answer)'), ('mcq_multiple', 'Multiple choice (multiple answers)'), ('true_false', 'True / False'), ('fill_text', 'Fill in the blank'), ('table', 'Table'), ('audio', 'Audio')], default='pdf', max_length=16),
        ),
        migrations.AddField(
            model_name='submission',
            name='audio_plays',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
