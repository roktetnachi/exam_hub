import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('digitalexams', '0032_seb_quit_code'),
    ]

    operations = [
        migrations.CreateModel(
            name='SupportRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120)),
                ('email', models.EmailField(max_length=254)),
                ('role', models.CharField(blank=True, help_text='Who they said they are (pupil, teacher, admin, school…).', max_length=40)),
                ('topic', models.CharField(choices=[('sign_in', 'Signing in / password'), ('exam', 'Taking or running an exam'), ('setup', 'Setting up my school'), ('bug', 'Something is broken'), ('school_enquiry', 'Bringing Andam to a school'), ('other', 'Something else')], default='other', max_length=20)),
                ('message', models.TextField()),
                ('page_url', models.CharField(blank=True, help_text='The page they sent it from.', max_length=300)),
                ('user_agent', models.CharField(blank=True, max_length=300)),
                ('email_sent', models.BooleanField(default=False, help_text='False if the email to support could not be sent.')),
                ('status', models.CharField(choices=[('new', 'New'), ('answered', 'Answered'), ('closed', 'Closed')], default='new', max_length=10)),
                ('notes', models.TextField(blank=True, help_text='Your own notes (never shown to the sender).')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('school', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='support_requests', to='digitalexams.school')),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='support_requests', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
