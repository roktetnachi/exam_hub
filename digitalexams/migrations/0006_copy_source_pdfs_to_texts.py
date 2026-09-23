from django.db import migrations


def copy_pdfs(apps, schema_editor):
    Assignment = apps.get_model('digitalexams', 'Assignment')
    AssignmentText = apps.get_model('digitalexams', 'AssignmentText')
    for assignment in Assignment.objects.all():
        if assignment.source_pdf:
            AssignmentText.objects.create(
                assignment=assignment, label='Text 1', pdf=assignment.source_pdf.name, order=0,
            )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0005_assignmenttext_submission_response_text'),
    ]

    operations = [
        migrations.RunPython(copy_pdfs, noop_reverse),
    ]
