from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm
from django import forms
from .models import ActivityLogEntry, AllowedBrowserExamKey, Assignment, AssignmentItem, Message, Recipient, SchoolClass, Submission, User


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


class ExamHubUserCreationForm(RequireUniqueEmailMixin, AdminUserCreationForm):
    pass


class ExamHubUserChangeForm(RequireUniqueEmailMixin, UserChangeForm):
    pass


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Admin manages every account here: username/password plus first name, last name,
    email and role (Teacher/Pupil). Teachers' name+email are then used automatically when
    they create assignments, and pupils' email is what assignments are matched to — so both
    need to be filled in here for the rest of the app to work."""
    fieldsets = DjangoUserAdmin.fieldsets + (
        ('ExamHub role', {'fields': ('role',)}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ('Personal info', {'fields': ('first_name', 'last_name', 'email')}),
        ('ExamHub role', {'fields': ('role',)}),
    )
    form = ExamHubUserChangeForm
    add_form = ExamHubUserCreationForm
    list_display = ('username', 'first_name', 'last_name', 'email', 'role', 'is_staff', 'is_superuser')
    list_filter = DjangoUserAdmin.list_filter + ('role',)


@admin.register(SchoolClass)
class SchoolClassAdmin(admin.ModelAdmin):
    list_display = ('name', 'teacher_list', 'pupil_count', 'created_at')
    filter_horizontal = ('teachers', 'pupils')
    search_fields = ('name',)

    def teacher_list(self, obj):
        return ', '.join(t.get_full_name() or t.username for t in obj.teachers.all()) or '—'
    teacher_list.short_description = 'Teachers'

    def pupil_count(self, obj):
        return obj.pupils.count()
    pupil_count.short_description = 'Pupils'


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
    list_display = ('title', 'teacher_name', 'opens_at', 'due_at', 'duration_minutes')
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
    list_display = ('label', 'key_hash', 'created_at')
    readonly_fields = ('created_at',)

