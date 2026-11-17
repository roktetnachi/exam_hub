from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0031_text_to_speech'),
    ]

    operations = [
        migrations.AddField(
            model_name='assignment',
            name='seb_quit_code',
            field=models.CharField(blank=True, default='', max_length=12),
        ),
    ]
