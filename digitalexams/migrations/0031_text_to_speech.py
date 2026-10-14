from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0030_spellcheck_audio'),
    ]

    operations = [
        migrations.AddField(
            model_name='assignment',
            name='allow_text_to_speech',
            field=models.BooleanField(default=True),
        ),
    ]
