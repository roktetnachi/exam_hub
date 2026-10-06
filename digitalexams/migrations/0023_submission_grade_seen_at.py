from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0022_emaillogincode'),
    ]

    operations = [
        migrations.AddField(
            model_name='submission',
            name='grade_seen_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
