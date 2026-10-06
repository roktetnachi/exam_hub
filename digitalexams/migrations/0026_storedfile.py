from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0025_user_activation'),
    ]

    operations = [
        migrations.CreateModel(
            name='StoredFile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255, unique=True)),
                ('content', models.BinaryField()),
                ('size', models.PositiveBigIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]
