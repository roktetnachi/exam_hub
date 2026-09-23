from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('digitalexams', '0002_alter_user_groups_alter_user_is_superuser')]
    operations = [
        migrations.AlterField(
            model_name='user',
            name='role',
            field=models.CharField(choices=[('teacher', 'Teacher'), ('pupil', 'Pupil')], default='pupil', max_length=10),
        ),
    ]
