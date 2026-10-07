from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from django import forms
from .models import ActivityLogEntry, AllowedBrowserExamKey, Assignment, AssignmentItem, Message, Recipient, School, SchoolClass, Subject, Submission, User


class RequireUniqueEmailMixin:
    """Every ExamHub account needs an email: assignments are matched to pupils by email, so
    a pupil saved without one silently never appears in the teacher's "assign to" list.
    Emails must also be unique, or two accounts would receive each other's assignments."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'email' in self.fields:
            self.fields['email'].required = True
            self.fields['email'].help_text = 'Required. Pupils are matched to assignments by this email.'
        if 'is_staff' in self.fields:
            self.fields['is_staff'].help_text = (
                'For teachers, this also makes them an ExamHub class admin: they can create and edit '
                'classes, assign teachers to classes, and add pupils. Teachers without it can only '
                'assign work to pupils in the classes they are assigned to.')

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip()
        if not email:
            raise forms.ValidationError('An email is required for every ExamHub account.')
        clash = User.objects.filter(email__iexact=email)
        if self.instance and self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError('Another account already uses this email.')
        return email


class ExamHubUserCreationForm(RequireUniqueEmailMixin, UserCreationForm):
    pass


class ExamHubUserChangeForm(RequireUniqueEmailMixin, UserChangeForm):
    pass


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    """Platform-level view of all tenant schools. Only accessible to super admins."""
    list_display = ('name', 'slug', 'timezone', 'is_active', 'user_count', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'slug')
    readonly_fields = ('created_at',)
    prepopulated_fields = {'slug': ('name',)}

    def user_count(self, obj):
        return obj.users.count()
    user_count.short_description = 'Users'


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Admin manages every account here: username/password plus first name, last name,
    email and role (Teacher/Pupil). Teachers' name+email are then used automatically when
    they create assignments, and pupils' email is what assignments are matched to — so both
    need to be filled in here for the rest of the app to work."""
    fieldsets = DjangoUserAdmin.fieldsets + (
        ('ExamHub role', {'fields': ('role', 'school', 'is_school_admin')}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ('Personal info', {'fields': ('first_name', 'last_name', 'email')}),
        ('ExamHub role', {'fields': ('role', 'school', 'is_school_admin')}),
    )
    form = ExamHubUserChangeForm
    add_form = ExamHubUserCreationForm
    list_display = ('username', 'first_name', 'last_name', 'email', 'role', 'school', 'is_school_admin', 'is_superuser')
    list_filter = DjangoUserAdmin.list_filter + ('role', 'school', 'is_school_admin')


@admin.register(SchoolClass)
class SchoolClassAdmin(admin.ModelAdmin):
    list_display = ('name', 'school', 'teacher_list', 'pupil_count', 'created_at')
    list_filter = ('school',)
    filter_horizontal = ('teachers', 'pupils')
    search_fields = ('name',)

    def teacher_list(self, obj):
        return ', '.join(t.get_full_name() or t.username for t in obj.teachers.all()) or '—'
    teacher_list.short_description = 'Teachers'

    def pupil_count(self, obj):
        return obj.pupils.count()
    pupil_count.short_description = 'Pupils'


@admin.register(Subject)
class SubjectAdmin(SchoolClassAdmin):
    pass


class RecipientInline(admin.TabularInline):
    model = Recipient
    extra = 0
    fields = ('pupil_name', 'pupil_email', 'extra_minutes', 'opens_override')


class AssignmentItemInline(admin.TabularInline):
    model = AssignmentItem
    extra = 0
    fields = ('order', 'item_type', 'label', 'section', 'pdf', 'image', 'video', 'prompt', 'options', 'correct_answer', 'points')


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = ('title', 'school', 'teacher_name', 'is_hidden', 'opens_at', 'due_at', 'closed_at')
    list_filter = ('school',)
    inlines = [AssignmentItemInline, RecipientInline]


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ('recipient', 'status', 'submitted_at', 'total_score', 'max_score', 'published')
    readonly_fields = ('submitted_at', 'updated_at', 'graded_at')


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('recipient', 'sender_role', 'sender_name', 'is_broadcast', 'created_at')
    readonly_fields = ('created_at',)
    search_fields = ('sender_name', 'text')


@admin.register(ActivityLogEntry)
class ActivityLogEntryAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'action', 'actor_name', 'description', 'assignment')
    list_filter = ('action',)
    readonly_fields = ('created_at',)
    search_fields = ('actor_name', 'actor_email', 'description')

@admin.register(AllowedBrowserExamKey)
class AllowedBrowserExamKeyAdmin(admin.ModelAdmin):
    list_display = ('label', 'school', 'key_hash', 'created_at')
    list_filter = ('school',)
    readonly_fields = ('created_at',)

