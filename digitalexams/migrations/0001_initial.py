import django.contrib.auth.models
import django.contrib.auth.validators
import django.db.models.deletion
from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
    ]
    operations = [
        migrations.CreateModel(name='User', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('password', models.CharField(max_length=128, verbose_name='password')),
            ('last_login', models.DateTimeField(blank=True, null=True, verbose_name='last login')),
            ('is_superuser', models.BooleanField(default=False, help_text='Designates that this user has all permissions without explicitly granting them.', verbose_name='superuser status')),
            ('username', models.CharField(error_messages={'unique': 'A user with that username already exists.'}, help_text='Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only.', max_length=150, unique=True, validators=[django.contrib.auth.validators.UnicodeUsernameValidator()], verbose_name='username')),
            ('first_name', models.CharField(blank=True, max_length=150, verbose_name='first name')),
            ('last_name', models.CharField(blank=True, max_length=150, verbose_name='last name')),
            ('email', models.EmailField(blank=True, max_length=254, verbose_name='email address')),
            ('is_staff', models.BooleanField(default=False, help_text='Designates whether the user can log into this admin site.', verbose_name='staff status')),
            ('is_active', models.BooleanField(default=True, help_text='Designates whether this user should be treated as active. Unselect this instead of deleting accounts.', verbose_name='active')),
            ('date_joined', models.DateTimeField(default=django.utils.timezone.now, verbose_name='date joined')),
            ('role', models.CharField(choices=[('teacher', 'Teacher'), ('pupil', 'Pupil')], max_length=10)),
            ('groups', models.ManyToManyField(blank=True, help_text='The groups this user belongs to. A user will get all permissions granted to each of the groups.', related_name='user_set', related_query_name='user', to='auth.group', verbose_name='groups')),
            ('user_permissions', models.ManyToManyField(blank=True, help_text='Specific permissions for this user.', related_name='user_set', related_query_name='user', to='auth.permission', verbose_name='user permissions')),
        ], options={'verbose_name': 'user', 'verbose_name_plural': 'users', 'abstract': False}, managers=[('objects', django.contrib.auth.models.UserManager())]),
        migrations.CreateModel(name='Assignment', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('title', models.CharField(max_length=180)), ('instructions', models.TextField(blank=True)),
            ('source_pdf', models.FileField(upload_to='assignments/%Y/%m/')),
            ('teacher_name', models.CharField(max_length=120)), ('teacher_email', models.EmailField(max_length=254)),
            ('opens_at', models.DateTimeField(default=django.utils.timezone.now)), ('due_at', models.DateTimeField()), ('created_at', models.DateTimeField(auto_now_add=True)),
        ], options={'ordering': ['-created_at']}),
        migrations.CreateModel(name='Recipient', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('pupil_name', models.CharField(max_length=120)), ('pupil_email', models.EmailField(max_length=254)),
            ('assignment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='recipients', to='digitalexams.assignment')),
        ]),
        migrations.CreateModel(name='Submission', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('annotations', models.JSONField(blank=True, default=list)), ('outline_html', models.TextField(blank=True)), ('response_html', models.TextField(blank=True)),
            ('status', models.CharField(choices=[('not_started', 'Not started'), ('draft', 'Draft'), ('submitted', 'Submitted')], default='not_started', max_length=16)),
            ('submitted_at', models.DateTimeField(blank=True, null=True)), ('updated_at', models.DateTimeField(auto_now=True)),
            ('recipient', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='submission', to='digitalexams.recipient')),
        ]),
        migrations.AddConstraint(model_name='recipient', constraint=models.UniqueConstraint(fields=('assignment', 'pupil_email'), name='unique_assignment_pupil')),
    ]
