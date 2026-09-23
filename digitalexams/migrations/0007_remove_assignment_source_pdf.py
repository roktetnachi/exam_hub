from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0006_copy_source_pdfs_to_texts'),
    ]

    operations = [
        migrations.RemoveField(model_name='assignment', name='source_pdf'),
    ]
