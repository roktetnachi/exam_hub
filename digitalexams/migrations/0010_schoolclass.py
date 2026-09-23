from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('digitalexams', '0009_assignment_delivery_settings'),
    ]

    operations = [
        migrations.CreateModel(
            name='SchoolClass',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('pupils', models.ManyToManyField(blank=True, related_name='classes_enrolled', to=settings.AUTH_USER_MODEL)),
                ('teachers', models.ManyToManyField(blank=True, related_name='classes_taught', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['name'],
            },
        ),
    ]
