from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0010_schoolclass'),
    ]

    operations = [
        migrations.RenameModel(old_name='AssignmentText', new_name='AssignmentItem'),
        migrations.AlterField(
            model_name='assignmentitem',
            name='assignment',
            field=models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='items', to='digitalexams.assignment'),
        ),
        migrations.AlterField(
            model_name='assignmentitem',
            name='pdf',
            field=models.FileField(blank=True, null=True, upload_to='assignments/%Y/%m/'),
        ),
        migrations.AddField(
            model_name='assignmentitem',
            name='item_type',
            field=models.CharField(choices=[
                ('pdf', 'PDF text (annotate + write about it)'),
                ('long_answer', 'Long answer'),
                ('mcq_single', 'Multiple choice (single answer)'),
                ('mcq_multiple', 'Multiple choice (multiple answers)'),
                ('true_false', 'True / False'),
                ('fill_text', 'Fill in the blank'),
                ('table', 'Table'),
            ], default='pdf', max_length=16),
        ),
        migrations.AddField(
            model_name='assignmentitem',
            name='prompt',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='assignmentitem',
            name='options',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='assignmentitem',
            name='correct_answer',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
