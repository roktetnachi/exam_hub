from django.db import migrations, models


def migrate_response_html(apps, schema_editor):
    """Best-effort carry-over: put any existing single response into the first text's slot."""
    Submission = apps.get_model('digitalexams', 'Submission')
    for submission in Submission.objects.all():
        old_html = getattr(submission, 'response_html', '') or ''
        if not old_html:
            continue
        first_text = submission.recipient.assignment.texts.order_by('order', 'id').first()
        if first_text:
            submission.responses = {str(first_text.id): old_html}
            submission.save(update_fields=['responses'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0007_remove_assignment_source_pdf'),
    ]

    operations = [
        migrations.AddField(
            model_name='submission',
            name='responses',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(migrate_response_html, noop_reverse),
        migrations.RemoveField(model_name='submission', name='response_html'),
        migrations.RemoveField(model_name='submission', name='response_text'),
    ]
