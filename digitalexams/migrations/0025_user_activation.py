from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0024_assignment_visibility_and_manual_close'),
    ]

    operations = [
        migrations.AddField(model_name='user', name='must_change_password', field=models.BooleanField(default=False)),
        migrations.AddField(model_name='user', name='invited_at', field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='user', name='activated_at', field=models.DateTimeField(blank=True, null=True)),
    ]
