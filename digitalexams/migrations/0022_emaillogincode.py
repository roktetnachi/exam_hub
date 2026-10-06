import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0021_school_user_is_school_admin_alter_schoolclass_name_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='EmailLoginCode',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code_hash', models.CharField(max_length=128)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('expires_at', models.DateTimeField()),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('used_at', models.DateTimeField(blank=True, null=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='login_codes', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['user', '-created_at'], name='digitalexam_user_id_99a0cc_idx')],
            },
        ),
    ]
