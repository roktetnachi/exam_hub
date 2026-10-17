from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q
from django.utils import timezone


class School(models.Model):
    """A tenant / school organisation. Every piece of data in Andam is scoped to one
    School so that multiple schools can share a single hosted instance without seeing
    each other's assignments, pupils, or results."""
    name = models.CharField(max_length=200, unique=True)
    slug = models.SlugField(
        max_length=60, unique=True,
        help_text='Short identifier used in URLs, e.g. "st-marys". '
                  'Used for subdomain routing (st-marys.examhub.com) or '
                  'path routing (examhub.com/s/st-marys/).',
    )
    logo = models.ImageField(
        upload_to='school_logos/', blank=True, null=True,
        help_text='Optional school logo shown in the header.',
    )
    primary_color = models.CharField(
        max_length=7, default='#0d6efd',
        help_text='Hex color used for branding, e.g. #1a73e8.',
    )
    timezone = models.CharField(
        max_length=60, default='UTC',
        help_text='IANA timezone name, e.g. Asia/Manila. Used for displaying exam times.',
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Inactive schools are locked out — all logins are blocked.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

class User(AbstractUser):
    class Role(models.TextChoices):
        TEACHER = "teacher", "Teacher"
        PUPIL = "pupil", "Pupil"

    # Which school (tenant) this user belongs to. Null only for platform super-admins
    # (i.e. the operator who manages all schools). Every teacher and pupil must have
    # a school set — enforced at the view layer when creating accounts.
    school = models.ForeignKey(
        School, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='users',
        help_text='The school this user belongs to. Leave blank only for platform super-admins.',
    )
    # School-level admin: can manage classes, teachers, and pupils within their own
    # school. Replaces the old is_staff + role == TEACHER pattern for school admins.
    is_school_admin = models.BooleanField(
        default=False,
        help_text='School admin: can create/manage classes, teachers, and pupils '
                  'within their own school.',
    )
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.PUPIL)
    # When this person finished (or skipped) the first-login guided tour. Null = show it on
    # their next page load. They can replay it any time from their account menu.
    tour_completed_at = models.DateTimeField(null=True, blank=True)
    # Account activation. A new account (or a re-invited one) has a temporary password and
    # must_change_password=True: on first sign-in they're sent straight to "Choose your
    # password" before anything else, and only then does the welcome tour start.
    must_change_password = models.BooleanField(default=False)
    invited_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    @property
    def account_status(self):
        """'active' | 'invited' | 'not_invited' — shown on the Roles page."""
        if self.activated_at or (self.last_login and not self.must_change_password):
            return 'active'
        return 'invited' if self.invited_at else 'not_invited'

    @property
    def tour_role(self):
        """Which guided tour this account gets: main_admin / admin / teacher / pupil."""
        if self.is_superuser:
            return 'main_admin'
        if self.can_manage_classes:
            return 'admin'
        return 'teacher' if self.role == self.Role.TEACHER else 'pupil'

    def save(self, *args, **kwargs):
        # A brand-new superuser (e.g. from `createsuperuser`) is an admin, not a pupil: make
        # it a Teacher so it never shows up in the "assign to" pupil list. Only on creation,
        # so an admin can still deliberately flag a superuser test account as a pupil later.
        if self._state.adding and self.is_superuser and self.role == self.Role.PUPIL:
            self.role = self.Role.TEACHER
        super().save(*args, **kwargs)

    @property
    def can_manage_classes(self):
        """Class admins: a platform superuser, or a teacher who is marked as a school admin
        (is_school_admin=True). They create/rename/delete classes, set each class's teachers
        and pupils, and add pupil accounts. Every other teacher only works with the classes
        they've been assigned to (see teachable_pupils)."""
        return self.is_active and (self.is_superuser or self.is_school_admin)

    def teachable_pupils(self, school=None):
        """Pupils this account may assign work to: every assignable pupil for a class admin,
        otherwise only the pupils enrolled in a class this teacher is assigned to.
        `school` is the school being viewed (request.school). It must be passed for platform
        superusers, who have no school of their own — otherwise they'd see every school's pupils."""
        pupils = User.assignable_pupils(school=school or self.school)
        if self.can_manage_classes:
            return pupils
        # A plain teacher sets work only for the SUBJECTS they teach (a subject can hold pupils from
        # several year groups). Being attached to a year group doesn't let them assign to it.
        return pupils.filter(subjects_enrolled__teachers=self).distinct()

    def visible_subjects(self, school=None):
        """All of the school's subjects for an admin; only the subjects this teacher teaches otherwise."""
        school = school or self.school
        qs = Subject.objects.all() if self.can_manage_classes else self.subjects_taught.all()
        if school:
            qs = qs.filter(school=school)
        return qs

    def visible_classes(self, school=None):
        """All of the school's classes for a class admin; only the classes this teacher is
        assigned to otherwise. Always limited to one school (see teachable_pupils)."""
        school = school or self.school
        qs = SchoolClass.objects.all() if self.can_manage_classes else self.classes_taught.all()
        if school:
            qs = qs.filter(school=school)
        return qs

    @classmethod
    def assignable_pupils(cls, school=None):
        """Accounts a teacher can assign work to: active, role=Pupil, never an admin."""
        qs = cls.objects.filter(role=cls.Role.PUPIL, is_superuser=False, is_active=True)
        if school:
            qs = qs.filter(school=school)
        return qs

    @property
    def can_view_activity(self):
        """The Activity log is for admins only; regular teachers don't get the tab."""
        return self.can_manage_classes


class SchoolClass(models.Model):
    """A roster group (e.g. "Grade 10 - Newton") that teachers can assign work to as a
    whole, instead of picking pupils one by one every time. Only class admins (see
    User.can_manage_classes) create or edit classes; a regular teacher sees only the classes
    listed in `teachers` and can assign work only to those classes' pupils."""
    school = models.ForeignKey(
        School, null=True, blank=True, on_delete=models.CASCADE,
        related_name='classes',
        help_text='The school this class belongs to.',
    )
    # unique=True removed: two different schools are allowed to have a class with
    # the same name (e.g. both have "Grade 10 - Section A").
    name = models.CharField(max_length=120)
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
        # Shown as "Year groups" everywhere in the app; the model/table keep their old name so
        # existing data, URLs (/classes/, /api/classes/) and migrations are untouched.
        verbose_name = 'year group'
        verbose_name_plural = 'year groups'

    def __str__(self):
        return self.name


class Subject(models.Model):
    """A taught subject (e.g. "Pre-U 1 Economics"). Unlike a section, a subject's pupils can
    come from several different sections. Its teachers can assign work to its pupils."""
    school = models.ForeignKey(
        School, null=True, blank=True, on_delete=models.CASCADE, related_name='subjects',
        help_text='The school this subject belongs to.',
    )
    name = models.CharField(max_length=120)
    teachers = models.ManyToManyField(
        User, related_name='subjects_taught', blank=True,
        limit_choices_to={'role': User.Role.TEACHER},
    )
    pupils = models.ManyToManyField(
        User, related_name='subjects_enrolled', blank=True,
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

    school = models.ForeignKey(
        School, null=True, blank=True, on_delete=models.CASCADE,
        related_name='assignments',
        help_text='The school this assignment belongs to.',
    )
    title = models.CharField(max_length=180)
    instructions = models.TextField(blank=True)
    teacher_name = models.CharField(max_length=120)
    teacher_email = models.EmailField()
    opens_at = models.DateTimeField(default=timezone.now)
    due_at = models.DateTimeField(null=True, blank=True)
    # No longer used: an exam now runs until its closing time (due_at) or until the teacher
    # closes it. Kept so older rows and migrations stay valid.
    duration_minutes = models.PositiveIntegerField(default=60)
    # Visibility. Hidden = pre-created; pupils can't see or open it until the teacher makes it
    # visible (or opens_at passes, when it's scheduled). See exam_visible_to_pupils().
    is_hidden = models.BooleanField(default=False)
    # Set when a teacher closes the exam by hand ("Close now"), whether or not a closing time
    # was scheduled. Cleared again by "Reopen".
    closed_at = models.DateTimeField(null=True, blank=True)
    delivery_mode = models.CharField(max_length=16, choices=DeliveryMode.choices, default=DeliveryMode.BROWSER)
    require_join_code = models.BooleanField(default=False)
    join_code = models.CharField(max_length=8, blank=True, default='')
    # Lockdown app only: the quit password built into this exam's Safe Exam Browser launch file.
    # SEB won't close (Quit button, Ctrl/Cmd-Q, closing the window) until someone types it, so a
    # pupil can't leave the exam on their own; the teacher sees it on the Monitor tab. Submitting
    # (or the exam ending) still closes SEB without it, through the quit address. Blank = no quit
    # code (exams made before quit codes existed), and the shared lockdown config still works.
    seb_quit_code = models.CharField(max_length=12, blank=True, default='')
    allow_calculator = models.BooleanField(default=False)
    allow_graphing_calculator = models.BooleanField(default=False)
    # Browser spell check (red underlines + suggestions) in pupils' answer boxes, notepad and notes.
    # Off by default for new exams so the exam tests the pupil's spelling, not the browser's.
    allow_spellcheck = models.BooleanField(default=False)
    # Pupils' "Read aloud" (the browser's text-to-speech in the Accessibility menu). On by default;
    # a teacher turns it off where hearing the text would help (reading comprehension, languages).
    allow_text_to_speech = models.BooleanField(default=True)
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
        AUDIO = 'audio', 'Audio'

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
    # Only used when item_type == AUDIO (listening exams). audio_max_plays = how many times a pupil
    # may play it during the exam (0 = as often as they like); counted on the server
    # (Submission.audio_plays) so reloading the page doesn't reset it. audio_allow_pause = pupils
    # can pause and move back / forward; when off, once a play starts it runs to the end.
    audio = models.FileField(upload_to='assignments/%Y/%m/', blank=True, null=True)
    audio_max_plays = models.PositiveSmallIntegerField(default=0)
    audio_allow_pause = models.BooleanField(default=True)
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


class ExamResource(models.Model):
    """Reference material for an exam — a PDF, an image or a short text — that pupils can open at
    any time from the floating "Resources" panel while they work. Not a question: no points, no
    answer, not part of the question navigator."""
    class Kind(models.TextChoices):
        PDF = 'pdf', 'PDF'
        IMAGE = 'image', 'Image'
        TEXT = 'text', 'Text'

    assignment = models.ForeignKey(Assignment, related_name='resources', on_delete=models.CASCADE)
    kind = models.CharField(max_length=8, choices=Kind.choices)
    title = models.CharField(max_length=120)
    file = models.FileField(upload_to='resources/%Y/%m/', blank=True, null=True)
    text = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f'{self.assignment.title} — {self.title}'


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
    # "Any browser with security" exit lock (Phase 10). Set when the pupil leaves the exam window
    # (switches tab/app, opens a new tab or window, exits fullscreen). While locked_at is set the
    # server refuses saves/submits until the pupil enters resume_code — a one-word lowercase code
    # the teacher reads out from the Monitor tab — or the teacher unlocks them directly.
    locked_at = models.DateTimeField(null=True, blank=True)
    lock_reason = models.CharField(max_length=60, blank=True)
    resume_code = models.CharField(max_length=24, blank=True)
    lock_count = models.PositiveIntegerField(default=0)
    # When the pupil last opened their released grade ("View grade"). A grade released or
    # re-graded after this shows as a new notification on the pupil's bell.
    grade_seen_at = models.DateTimeField(null=True, blank=True)
    # Teacher's pen-on-paper marking, kept separate from the pupil's own work (which is never
    # changed). Shown to the pupil only once the grade is released. Shape:
    #   {"answers": {"<itemId>": [{"id", "start", "end", "quote", "style": underline|circle|highlight,
    #                              "comment"}]},           ← character offsets in the answer's text
    #    "pdf": {"<itemId>": [{"id", "page", "shape": circle|box|underline|pin, "x", "y", "w", "h",
    #                          "comment"}]},               ← same coordinate space as pupil annotations
    #    "annComments": {"<annotation index>": {"fp": "<fingerprint>", "comment"}}}
    teacher_marks = models.JSONField(default=dict, blank=True)
    # When the teacher last looked at this submission. A submission newer than this shows as
    # "new" (dot on the exam, bell item) — this replaced the old submission email.
    teacher_seen_at = models.DateTimeField(null=True, blank=True)
    # How many times the pupil has started each audio item: {"<AssignmentItem id>": n}. Only ever
    # written by audio_play_api (under a row lock) — see save() below.
    audio_plays = models.JSONField(default=dict, blank=True)

    def save(self, *args, **kwargs):
        # Autosave, submit, locking and grading all load the submission, change it and save the
        # whole row. If a play was counted in between, a full save would put the old count back and
        # hand the pupil an extra play — so a plain save() of an existing row leaves audio_plays
        # alone. Pass update_fields=['audio_plays', ...] to write it.
        if self.pk is not None and kwargs.get('update_fields') is None and not kwargs.get('force_insert') \
                and not self._state.adding:
            kwargs['update_fields'] = [f.name for f in self._meta.concrete_fields
                                       if not f.primary_key and f.name != 'audio_plays']
        super().save(*args, **kwargs)

    @property
    def is_new_for_teacher(self):
        return (self.status == 'submitted' and self.submitted_at is not None
                and (self.teacher_seen_at is None or self.teacher_seen_at < self.submitted_at))

    @property
    def is_new_grade_for_pupil(self):
        """A released grade the pupil hasn't opened yet (drives the bell and the "New" chip)."""
        return bool(self.published and self.graded_at
                    and (self.grade_seen_at is None or self.grade_seen_at < self.graded_at))

    def __str__(self):
        return f'{self.recipient} ({self.status})'


class AllowedBrowserExamKey(models.Model):
    """A Browser Exam Key (BEK) value that a real Safe Exam Browser client generated for
    Andam's shared lockdown .seb config on one specific platform/version. SEB computes
    this itself from its full internal settings (which aren't practical to replicate
    ourselves), so the reliable way to get one is: download the lockdown config from the
    teacher dashboard, open it in a real copy of SEB on the platform your pupils will
    actually use, and paste the key it reports here. Add one row per platform (Windows SEB
    and iPad SEB, say, produce different keys for the identical logical config) — Andam
    accepts a lockdown request if its computed hash matches ANY row here."""
    school = models.ForeignKey(
        School, null=True, blank=True, on_delete=models.CASCADE,
        related_name='seb_keys',
        help_text='The school this SEB key belongs to. Each school manages its own keys.',
    )
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
    # Set for class-roster actions (class created, teachers/pupils changed, …) so the Activity
    # page can filter by class. Assignment actions are matched to a class through their
    # recipients instead, so they don't need this.
    school_class = models.ForeignKey(SchoolClass, null=True, blank=True, on_delete=models.SET_NULL, related_name='activity_entries')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'Activity log entries'

    def __str__(self):
        return f'{self.action}: {self.description}'


class EmailLoginCode(models.Model):
    """A one-time 6-digit sign-in code emailed to a user — the passwordless alternative to
    signing in with a password. Only a salted hash of the code is stored. A code is single
    use, expires after a few minutes, and is burned after too many wrong guesses."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='login_codes')
    code_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', '-created_at'])]

    def __str__(self):
        return f'Login code for {self.user} at {self.created_at:%Y-%m-%d %H:%M}'


class StoredFile(models.Model):
    """A copy of every uploaded file (exam PDFs, images, videos, school logos) kept in the
    database. The app runs in more than one place against the same database (a local copy
    and Railway), and Railway wipes its disk on every deploy — so a file saved only to one
    machine's disk goes missing everywhere else. See digitalexams/storage.py."""
    name = models.CharField(max_length=255, unique=True)
    content = models.BinaryField()
    size = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class SupportRequest(models.Model):
    """A message sent through the "Help" chat (templates/_support_chat.html). Every one is
    emailed to SUPPORT_EMAIL and also kept here, so nothing is lost if the email fails —
    Django admin → Support requests lists them. Signed-out visitors (e.g. a school asking
    about Andam from the landing page) can send one too, so user and school are optional."""

    class Topic(models.TextChoices):
        SIGN_IN = 'sign_in', 'Signing in / password'
        EXAM = 'exam', 'Taking or running an exam'
        SETUP = 'setup', 'Setting up my school'
        BUG = 'bug', 'Something is broken'
        SCHOOL_ENQUIRY = 'school_enquiry', 'Bringing Andam to a school'
        OTHER = 'other', 'Something else'

    class Status(models.TextChoices):
        NEW = 'new', 'New'
        ANSWERED = 'answered', 'Answered'
        CLOSED = 'closed', 'Closed'

    name = models.CharField(max_length=120)
    email = models.EmailField()
    role = models.CharField(max_length=40, blank=True, help_text='Who they said they are (pupil, teacher, admin, school…).')
    topic = models.CharField(max_length=20, choices=Topic.choices, default=Topic.OTHER)
    message = models.TextField()
    page_url = models.CharField(max_length=300, blank=True, help_text='The page they sent it from.')
    user_agent = models.CharField(max_length=300, blank=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='support_requests')
    school = models.ForeignKey(School, null=True, blank=True, on_delete=models.SET_NULL, related_name='support_requests')
    email_sent = models.BooleanField(default=False, help_text='False if the email to support could not be sent.')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW)
    notes = models.TextField(blank=True, help_text='Your own notes (never shown to the sender).')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def reference(self):
        return f'AND-{self.pk:05d}' if self.pk else ''

    def __str__(self):
        return f'{self.reference} · {self.name} · {self.get_topic_display()}'
