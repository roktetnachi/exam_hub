from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

class User(AbstractUser):
    class Role(models.TextChoices):
        TEACHER = "teacher", "Teacher"
        PUPIL = "pupil", "Pupil"
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.PUPIL)

    def save(self, *args, **kwargs):
        # A brand-new superuser (e.g. from `createsuperuser`) is an admin, not a pupil: make
        # it a Teacher so it never shows up in the "assign to" pupil list. Only on creation,
        # so an admin can still deliberately flag a superuser test account as a pupil later.
        if self._state.adding and self.is_superuser and self.role == self.Role.PUPIL:
            self.role = self.Role.TEACHER
        super().save(*args, **kwargs)

    @classmethod
    def assignable_pupils(cls):
        """Accounts a teacher can assign work to: active, role=Pupil, never an admin."""
        return cls.objects.filter(role=cls.Role.PUPIL, is_superuser=False, is_active=True)


class SchoolClass(models.Model):
    """A roster group (e.g. "Grade 10 - Newton") that teachers can assign work to as a
    whole, instead of picking pupils one by one every time. Visible/editable by any
    teacher account, and manageable from Django admin too."""
    name = models.CharField(max_length=120, unique=True)
    teachers = models.ManyToManyField(
        User, related_name='classes_taught', blank=True,
        limit_choices_to={'role': User.Role.TEACHER},
    )
    pupils = models.ManyToManyField(
        User, related_name='classes_enrolled', blank=True,
        limit_choices_to={'role': User.Role.PUPIL, 'is_superuser': False, 'is_active': True},
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Assignment(models.Model):
    class DeliveryMode(models.TextChoices):
        BROWSER = "browser", "Any browser"
        BROWSER_SECURE = "browser_secure", "Any browser with security"
        LOCKDOWN = "lockdown", "Lockdown app"
        PAPER = "paper", "Paper"

    title = models.CharField(max_length=180)
    instructions = models.TextField(blank=True)
    teacher_name = models.CharField(max_length=120)
    teacher_email = models.EmailField()
    opens_at = models.DateTimeField(default=timezone.now)
    due_at = models.DateTimeField(null=True, blank=True)
    duration_minutes = models.PositiveIntegerField(
        default=60,
        help_text='Exam duration in minutes. The countdown starts the moment a pupil opens the assignment.',
    )
    delivery_mode = models.CharField(max_length=16, choices=DeliveryMode.choices, default=DeliveryMode.BROWSER)
    require_join_code = models.BooleanField(default=False)
    join_code = models.CharField(max_length=8, blank=True, default='')
    allow_calculator = models.BooleanField(default=False)
    allow_graphing_calculator = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class AssignmentItem(models.Model):
    """One piece of content in an assignment: a PDF text to annotate, an image or video to
    view, or a standalone question. Kept as a single model (rather than a subtype per question)
    so an assignment's content list can freely mix any of these, in any order."""
    class ItemType(models.TextChoices):
        PDF = 'pdf', 'PDF text (annotate + write about it)'
        IMAGE = 'image', 'Image'
        VIDEO = 'video', 'Video'
        LONG_ANSWER = 'long_answer', 'Long answer'
        MCQ_SINGLE = 'mcq_single', 'Multiple choice (single answer)'
        MCQ_MULTIPLE = 'mcq_multiple', 'Multiple choice (multiple answers)'
        TRUE_FALSE = 'true_false', 'True / False'
        FILL_TEXT = 'fill_text', 'Fill in the blank'
        TABLE = 'table', 'Table'

    assignment = models.ForeignKey(Assignment, related_name='items', on_delete=models.CASCADE)
    item_type = models.CharField(max_length=16, choices=ItemType.choices, default=ItemType.PDF)
    label = models.CharField(max_length=60)
    order = models.PositiveIntegerField(default=0)
    # Optional group label a batch of items are visually clustered under in the pupil's resources
    # panel (e.g. "Task 1"), with a point-total badge — purely presentational, no other behaviour
    # depends on it. Consecutive items sharing the same non-empty section render as one group;
    # items with no section render exactly as before (ungrouped).
    section = models.CharField(max_length=80, blank=True)
    # Only used when item_type == PDF.
    pdf = models.FileField(upload_to='assignments/%Y/%m/', blank=True, null=True)
    # Only used when item_type == IMAGE.
    image = models.FileField(upload_to='assignments/%Y/%m/', blank=True, null=True)
    # Only used when item_type == VIDEO.
    video = models.FileField(upload_to='assignments/%Y/%m/', blank=True, null=True)
    # Only used for question types: the question text itself.
    prompt = models.TextField(blank=True)
    # MCQ_SINGLE / MCQ_MULTIPLE: [{"id": "a", "text": "..."}, ...]. TABLE: {"rows": n, "cols": n,
    # "headers": [...]}  stored here too, for lack of a better single free-form slot.
    options = models.JSONField(default=list, blank=True)
    # Auto-gradable answer key, never sent to pupils. Shape depends on item_type:
    # mcq_single: {"optionId": "a"} · mcq_multiple: {"optionIds": ["a","c"]} ·
    # true_false: {"value": true} · fill_text: {"accepted": ["paris", "Paris"]} ·
    # long_answer / table / pdf: {} (no auto-grading — reviewed manually).
    correct_answer = models.JSONField(default=dict, blank=True)
    # How many points this item is worth. Used both for auto-grading (mcq/true-false/fill-text)
    # and as the ceiling a teacher can award manually (long_answer/table/pdf).
    points = models.FloatField(default=1.0)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f'{self.assignment.title} — {self.label}'


class Recipient(models.Model):
    assignment = models.ForeignKey(Assignment, related_name='recipients', on_delete=models.CASCADE)
    pupil_name = models.CharField(max_length=120)
    pupil_email = models.EmailField()
    # Accommodations — per-pupil overrides of the assignment's own schedule/duration.
    extra_minutes = models.PositiveIntegerField(
        default=0, help_text="Extra exam time for this pupil, added on top of the assignment's duration.")
    opens_override = models.DateTimeField(
        null=True, blank=True,
        help_text="If set, this pupil can start as soon as this time instead of waiting for the assignment's own opens_at (early access).")
    # Read-tracking for the 1:1 chat's unread dots (Phase 9). Set whenever that side actually
    # opens this recipient's message thread (see messages_api) — a message newer than the
    # other side's last-read timestamp shows as unread to them.
    teacher_last_read_at = models.DateTimeField(null=True, blank=True)
    pupil_last_read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['assignment', 'pupil_email'], name='unique_assignment_pupil')]

    def __str__(self):
        return f'{self.pupil_name} — {self.assignment}'


class Submission(models.Model):
    recipient = models.OneToOneField(Recipient, related_name='submission', on_delete=models.CASCADE)
    started_at = models.DateTimeField(null=True, blank=True)
    annotations = models.JSONField(default=list, blank=True)
    outline_html = models.TextField(blank=True)
    # One shared notepad (outline_html) across all texts, but a separate written
    # response per text: {"<AssignmentText id>": "<response html>", ...}
    responses = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, default='not_started', choices=[
        ('not_started', 'Not started'), ('draft', 'Draft'), ('submitted', 'Submitted'),
    ])
    submitted_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Grading (Phase 5). item_scores: {"<AssignmentItem id>": {"score": float, "maxScore": float,
    # "auto": bool, "feedback": str}}. auto-gradable items (mcq/true-false/fill-text) are recomputed
    # from correct_answer on every grade save unless a teacher has explicitly overridden them
    # (auto: false); long_answer/table/pdf items are manual-only and keep whatever was last saved.
    item_scores = models.JSONField(default=dict, blank=True)
    total_score = models.FloatField(null=True, blank=True)
    max_score = models.FloatField(null=True, blank=True)
    graded_at = models.DateTimeField(null=True, blank=True)
    # A submission can be fully graded without pupils seeing anything yet — publishing is a
    # separate, explicit teacher action so a grade can be double-checked before it's shown.
    published = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.recipient} ({self.status})'


class AllowedBrowserExamKey(models.Model):
    """A Browser Exam Key (BEK) value that a real Safe Exam Browser client generated for
    ExamHub's shared lockdown .seb config on one specific platform/version. SEB computes
    this itself from its full internal settings (which aren't practical to replicate
    ourselves), so the reliable way to get one is: download the lockdown config from the
    teacher dashboard, open it in a real copy of SEB on the platform your pupils will
    actually use, and paste the key it reports here. Add one row per platform (Windows SEB
    and iPad SEB, say, produce different keys for the identical logical config) — ExamHub
    accepts a lockdown request if its computed hash matches ANY row here."""
    label = models.CharField(max_length=80, help_text='e.g. "Windows SEB 3.7", "iPad SEB"')
    key_hash = models.CharField(max_length=64, help_text='The 64-character Browser Exam Key hex value SEB reports for this config.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Allowed Browser Exam Key (SEB)'
        verbose_name_plural = 'Allowed Browser Exam Keys (SEB)'

    def __str__(self):
        return self.label


class Message(models.Model):
    """A single message in the lightweight 1:1 thread between a teacher and a pupil,
    scoped to one recipient (i.e. one pupil's attempt at one assignment)."""
    recipient = models.ForeignKey(Recipient, related_name='messages', on_delete=models.CASCADE)
    sender_role = models.CharField(max_length=10, choices=User.Role.choices)
    sender_name = models.CharField(max_length=120)
    text = models.TextField()
    # True for a message sent to every recipient of an assignment at once (see
    # broadcast_message_api), purely so the chat thread can label it distinctly from a
    # one-to-one reply.
    is_broadcast = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.sender_name}: {self.text[:40]}'


class ActivityLogEntry(models.Model):
    """A lightweight, append-only audit trail of the actions teachers (and pupils, for
    submission events) take in the app — assignment lifecycle, roster changes, submissions,
    grading. Not meant to be exhaustive, just enough for a teacher to see "what happened
    recently" across the whole shared teacher pool."""
    actor_name = models.CharField(max_length=120, blank=True)
    actor_email = models.EmailField(blank=True)
    action = models.CharField(max_length=40)
    description = models.CharField(max_length=255)
    assignment = models.ForeignKey(Assignment, null=True, blank=True, on_delete=models.SET_NULL, related_name='activity_entries')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'Activity log entries'

    def __str__(self):
        return f'{self.action}: {self.description}'
