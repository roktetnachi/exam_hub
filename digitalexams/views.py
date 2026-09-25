import datetime
import hashlib
import hmac
import json
import mimetypes
import plistlib
import secrets
import string
from zoneinfo import ZoneInfo

from django.core import signing
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password
from django.http import FileResponse, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.db.models import Max, Prefetch, Q
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from .models import (ActivityLogEntry, AllowedBrowserExamKey, Assignment, AssignmentItem, Message, Recipient,
    SchoolClass, Submission, User)


@login_required
def home(request):
    # Shown to teachers in the SEB menu, so a missing one-time setup is obvious up front
    # instead of only surfacing when a pupil gets blocked mid-exam.
    return render(request, 'examhub.html', {'seb_key_count': AllowedBrowserExamKey.objects.count(), 'seb_config_key': seb_config_key(request.get_host(), _public_scheme(request))})


def account_kind(user):
    """'teacher' or 'pupil' for the login switch. Admins (superusers) sign in as teachers."""
    if user.is_superuser or user.role == User.Role.TEACHER:
        return 'teacher'
    return 'pupil'


def login_view(request):
    if request.user.is_authenticated:
        return redirect('home')
    login_as = request.POST.get('login_as') or request.GET.get('as') or 'pupil'
    if login_as not in ('teacher', 'pupil'):
        login_as = 'pupil'
    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.get_user()
        kind = account_kind(user)
        if kind != login_as:
            # The switch never grants a role; it only has to match the account's real one.
            form.add_error(None, f'This is a {kind} account. Choose "I\'m a {kind}" above, then sign in again.')
        else:
            login(request, user)
            next_url = request.GET.get('next')
            if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()},
                                                            require_https=request.is_secure()):
                return redirect(next_url)
            return redirect('home')
    return render(request, 'login.html', {'form': form, 'login_as': login_as})


@require_POST
def logout_view(request):
    logout(request)
    return redirect('login')


@login_required
@require_POST
def change_password_api(request):
    """Self-service password change for a signed-in pupil or teacher — there's no email
    infrastructure set up for a "forgot password" link, so this (current password + new
    password, from inside the app) is the only way an account changes its own credentials."""
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid request.')
    current_password = payload.get('currentPassword') or ''
    new_password = payload.get('newPassword') or ''
    if not current_password or not new_password:
        return HttpResponseBadRequest('Enter your current password and choose a new one.')
    if not request.user.check_password(current_password):
        return HttpResponseForbidden('Your current password is incorrect.')
    try:
        validate_password(new_password, user=request.user)
    except DjangoValidationError as exc:
        return HttpResponseBadRequest(' '.join(exc.messages))
    request.user.set_password(new_password)
    request.user.save()
    update_session_auth_hash(request, request.user)  # keeps this session signed in
    return JsonResponse({'changed': True})


def log_activity(actor_name, actor_email, action, description, assignment=None):
    """Best-effort audit-trail write for the teacher-facing activity log. Never lets a
    logging failure break the request that triggered it — this is a nice-to-have, not a
    system of record."""
    try:
        ActivityLogEntry.objects.create(actor_name=actor_name or '', actor_email=actor_email or '',
            action=action, description=description, assignment=assignment)
    except Exception:
        pass


# ---------------- Safe Exam Browser (SEB) "lockdown" delivery mode ----------------
# ExamHub can't enforce real device lockdown itself, but for assignments delivered in
# "lockdown" mode it can require that exam-taking requests actually come from a real Safe
# Exam Browser session, using SEB's own Browser Exam Key (BEK) mechanism: SEB hashes the
# absolute request URL together with a key it derives from its full configuration, and
# sends that hash in the X-SafeExamBrowser-RequestHash header on every request. A teacher
# opens ExamHub's shared lockdown .seb config in a real SEB install once, copies the BEK it
# reports, and stores it via Django admin (AllowedBrowserExamKey) — from then on this check
# recomputes the same hash server-side and only lets the request through if it matches.
SEB_REQUEST_HASH_HEADER = 'X-SafeExamBrowser-RequestHash'


def _public_scheme(request):
    """The scheme the pupil's browser actually used. Behind Railway's proxy Django itself sees
    plain http, but the proxy reports the real one in X-Forwarded-Proto."""
    forwarded = request.META.get('HTTP_X_FORWARDED_PROTO', '')
    return (forwarded.split(',')[0].strip() or request.scheme).lower()


def seb_config_settings(host, scheme, start_path=None):
    """The exact settings in ExamHub's lockdown .seb file for a given site address.
    Kept in one place because the same dict is used both to write the file (seb_config /
    seb_launch_config) and to recompute its Config Key (seb_config_key) — they must never drift
    apart. `start_path` is only set for a one-time launch config (see seb_launch_api), whose
    start page signs the pupil in and opens their paper directly."""
    home = f'{scheme}://{host}{reverse("home")}'
    start = f'{scheme}://{host}{start_path}' if start_path else home
    return {
        'startURL': start,
        'quitURL': home + '?sebquit=1',
        'allowQuit': True,
        'quitURLConfirm': False,
        'hashedQuitPassword': '',
        # Makes SEB send the X-SafeExamBrowser-RequestHash / -ConfigKeyHash headers that
        # seb_lockdown_ok() checks — without this, the whole mechanism silently never fires.
        'sendBrowserExamKey': True,
    }


def _seb_json(value):
    """SEB's own "SEB-JSON" serialisation used for the Config Key (per SEB's developer docs):
    dictionary keys sorted case-insensitively, no whitespace, no character escaping, lowercase
    true/false, empty dicts dropped."""
    if isinstance(value, dict):
        items = sorted(((k, v) for k, v in value.items() if not (isinstance(v, dict) and not v)),
                       key=lambda kv: kv[0].lower())
        return '{' + ','.join(f'"{k}":{_seb_json(v)}' for k, v in items) + '}'
    if isinstance(value, (list, tuple)):
        return '[' + ','.join(_seb_json(v) for v in value) + ']'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return repr(value)
    return f'"{value}"'


def seb_config_key(host, scheme, start_path=None):
    """ExamHub's own lockdown config's Config Key, computed the same way SEB computes it
    (SHA256 of the SEB-JSON of the settings actually in the file). Because ExamHub writes that
    file itself, it can trust its own key automatically — no copying keys out of SEB's settings
    screens into Django admin needed."""
    return hashlib.sha256(_seb_json(seb_config_settings(host, scheme, start_path)).encode('utf-8')).hexdigest()


def seb_lockdown_ok(request, extra_keys=()):
    """True if this request carries a valid Browser Exam Key hash for any key we've been
    given. There's deliberately no soft fallback (User-Agent, etc.) here — those are shown
    client-side as a friendly hint, but this is the actual enforcement."""
    # SEB sends two hashes: one built from the Browser Exam Key (changes with the SEB version
    # installed) and one from the Config Key (depends only on the .seb file, so it's the same
    # on every pupil's device). Either one matching a stored key is accepted, so an admin can
    # paste whichever key SEB shows them.
    received = [h.strip().lower() for h in (
        request.META.get('HTTP_X_SAFEEXAMBROWSER_REQUESTHASH', ''),
        request.META.get('HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH', ''),
    ) if h and h.strip()]
    if not received:
        return False
    # SEB hashes the URL exactly as the pupil's browser sees it. Behind Railway's proxy Django
    # sees the request as plain http:// even though the pupil is on https://, so both schemes
    # are tried — the stored key is still required either way.
    path_and_query = request.get_full_path()
    host = request.get_host()
    # Keys to accept: ExamHub's own config's Config Key (computed, for both schemes since the
    # file may have been downloaded over http or https), plus anything an admin added by hand.
    keys = {seb_config_key(host, 'https'), seb_config_key(host, 'http')}
    keys.update((k or '').strip().lower() for k in AllowedBrowserExamKey.objects.values_list('key_hash', flat=True))
    # A pupil who arrived via a one-time launch link is running a per-launch config (its start
    # page differs), so its Config Key differs too — remembered on their SEB session at sign-in.
    keys.update(extra_keys)
    if hasattr(request, 'session'):
        keys.update(request.session.get('seb_config_keys', []))
    keys.discard('')
    for scheme in ('https', 'http'):
        url = f'{scheme}://{host}{path_and_query}'
        for key in keys:
            expected = hashlib.sha256((url + key).encode('utf-8')).hexdigest()
            if any(hmac.compare_digest(expected, r) for r in received):
                return True
    return False


def seb_required_response(request=None):
    got_headers = bool(request and (request.META.get('HTTP_X_SAFEEXAMBROWSER_REQUESTHASH')
                                    or request.META.get('HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH')))
    if request is not None and not got_headers:
        detail = ("This assignment must be opened in Safe Exam Browser using ExamHub's lockdown "
                  "config (from the SEB menu / your teacher's link). This browser didn't identify itself as SEB.")
        reason = 'no_seb_headers'
    else:
        detail = ("Safe Exam Browser was detected, but not with ExamHub's current lockdown config. "
                  "Download a fresh config from this site and open the exam with it (a config from a "
                  "different address, or one edited and re-saved in SEB's settings, won't match).")
        reason = 'key_mismatch'
    return JsonResponse({'code': 'seb_required', 'reason': reason, 'detail': detail}, status=403)


def is_real_pupil(request):
    """A genuinely-restricted pupil account, as opposed to a teacher or a superuser test
    account that happens to also be flagged role=pupil for convenience while testing."""
    return request.user.role == User.Role.PUPIL and not request.user.is_superuser


# ---------------- scheduling timezone (everything schedules in GMT+8 / Asia/Manila) ----------------
# The create/edit forms send naive "YYYY-MM-DDTHH:MM" strings straight from a browser
# <input type="datetime-local">, with no timezone information attached. Assigning those
# directly to a DateTimeField (the old behaviour) let Django's USE_TZ machinery silently treat
# them as UTC wall-clock, which is 8 hours off from what a teacher actually typed. Every
# schedule in this app (opens_at, due_at, a pupil's early-access override) is meant to be GMT+8,
# so all of that naive input is parsed as Asia/Manila here, and always converted back to
# Asia/Manila before being sent to the frontend — regardless of the server's own TIME_ZONE
# setting or the viewer's browser locale, so a date typed as 2pm always displays and enforces
# as 2pm Manila time everywhere.
MANILA_TZ = ZoneInfo('Asia/Manila')


def parse_manila_datetime(raw):
    """Parses a naive datetime-local string (or an already-aware datetime/ISO string) as
    Asia/Manila local time and returns a real timezone-aware datetime ready for storage.
    Returns None for empty/unparseable input."""
    if not raw:
        return None
    if isinstance(raw, datetime.datetime):
        dt = raw
    else:
        try:
            dt = datetime.datetime.fromisoformat(str(raw))
        except ValueError:
            return None
    if timezone.is_naive(dt):
        dt = dt.replace(tzinfo=MANILA_TZ)
    return dt


def manila_isoformat(dt):
    """Serialises an aware datetime for the frontend as a GMT+8 (Asia/Manila) ISO string, so
    every client shows/round-trips the same wall-clock schedule no matter its own timezone."""
    if not dt:
        return None
    return dt.astimezone(MANILA_TZ).isoformat()


def assignment_closed(assignment):
    """True once an assignment's due date (always interpreted as Asia/Manila, same as
    everywhere else it's scheduled) has passed. No due date set means it never auto-closes."""
    return bool(assignment.due_at) and timezone.now() > assignment.due_at


def assignment_closed_response():
    return JsonResponse({
        'code': 'assignment_closed',
        'detail': 'This assignment closed at its scheduled due date and no longer accepts new work.',
    }, status=403)


def _has_unread(recipient, from_role, last_read_at):
    """True if `recipient` has a message from `from_role` newer than `last_read_at` (or any
    message at all from them, if that side has never opened the thread)."""
    qs = recipient.messages.filter(sender_role=from_role)
    if last_read_at:
        qs = qs.filter(created_at__gt=last_read_at)
    return qs.exists()


def auto_grade_item(item, response):
    """Auto-grades a single item from a pupil's raw response value, for the item types
    that support it (mcq_single/mcq_multiple/true_false/fill_text) and only when an answer
    key was actually set for that item. Returns (score, max_score); score is None when the
    item needs a human to grade it (long_answer/table/pdf, or no answer key configured)."""
    max_score = item.points
    ca = item.correct_answer or {}
    is_dict = isinstance(response, dict)
    if item.item_type == AssignmentItem.ItemType.MCQ_SINGLE and ca.get('optionId') is not None:
        got = response.get('optionId') if is_dict else None
        return (max_score if got == ca['optionId'] else 0.0), max_score
    if item.item_type == AssignmentItem.ItemType.MCQ_MULTIPLE and ca.get('optionIds'):
        got = set(response.get('optionIds') or []) if is_dict else set()
        return (max_score if got == set(ca['optionIds']) else 0.0), max_score
    if item.item_type == AssignmentItem.ItemType.TRUE_FALSE and ca.get('value') is not None:
        got = response.get('value') if is_dict else None
        return (max_score if got == ca['value'] else 0.0), max_score
    if item.item_type == AssignmentItem.ItemType.FILL_TEXT and ca.get('accepted'):
        accepted = [str(a).strip().lower() for a in ca['accepted']]
        got = str((response.get('text') if is_dict else '') or '').strip().lower()
        return (max_score if got and got in accepted else 0.0), max_score
    return None, max_score  # long_answer / table / pdf, or no answer key set — manual only


def annotated_recipients():
    """Recipients with their submission and latest-message times fetched in ONE query, instead
    of 3-4 queries per pupil. That per-pupil pattern made the dashboard take ~20 seconds for
    ten assignments when the database is remote (local dev against Railway Postgres)."""
    return (Recipient.objects.select_related('submission')
        .annotate(last_pupil_msg=Max('messages__created_at', filter=Q(messages__sender_role=User.Role.PUPIL)),
                  last_teacher_msg=Max('messages__created_at', filter=Q(messages__sender_role=User.Role.TEACHER)))
        .order_by('id'))


def _recipients_of(assignment):
    cached = getattr(assignment, '_prefetched_objects_cache', {}).get('recipients')
    if cached is not None and all(hasattr(r, 'last_pupil_msg') for r in cached):
        return list(cached)
    return list(annotated_recipients().filter(assignment=assignment))


def _submission_of(recipient):
    try:
        return recipient.submission
    except Submission.DoesNotExist:
        return Submission.objects.create(recipient=recipient)


def _unread_since(last_message_at, last_read_at):
    """Same rule as _has_unread, from pre-fetched timestamps: a message from the other side newer
    than your last read (or any message at all if you've never opened the thread)."""
    return bool(last_message_at) and (last_read_at is None or last_message_at > last_read_at)


def serialize_assignment(assignment, pupil_email=None):
    is_teacher_view = pupil_email is None
    recipients = []
    for recipient in _recipients_of(assignment):
        sub = _submission_of(recipient)
        # A pupil only ever sees scores once the teacher has explicitly published them —
        # being graded is not the same as being told.
        show_grades = is_teacher_view or sub.published
        recipients.append({'id': recipient.id, 'name': recipient.pupil_name, 'email': recipient.pupil_email,
            'status': sub.status, 'submittedAt': sub.submitted_at.isoformat() if sub.submitted_at else None,
            'startedAt': sub.started_at.isoformat() if sub.started_at else None,
            'annotations': sub.annotations, 'outlineHtml': sub.outline_html, 'responses': sub.responses,
            'extraMinutes': recipient.extra_minutes,
            'opensOverride': manila_isoformat(recipient.opens_override),
            # Unread-message dots (Phase 9): whichever side hasn't read the other's latest
            # message yet. hasUnreadFromPupil drives the teacher-side dots (assignment row,
            # Monitor pupil row, bell); hasUnreadFromTeacher drives the pupil's own
            # "Message teacher" button and bell — always computed, cheap either way.
            'hasUnreadFromPupil': _unread_since(recipient.last_pupil_msg, recipient.teacher_last_read_at),
            'hasUnreadFromTeacher': _unread_since(recipient.last_teacher_msg, recipient.pupil_last_read_at),
            # Exit lock (Phase 10). The resume code itself is only ever sent to the teacher's view.
            'locked': bool(sub.locked_at), 'lockReason': sub.lock_reason, 'lockCount': sub.lock_count,
            'resumeCode': sub.resume_code if (is_teacher_view and sub.locked_at) else None,
            'itemScores': sub.item_scores if show_grades else {},
            'totalScore': sub.total_score if show_grades else None,
            'maxScore': sub.max_score if show_grades else None,
            'gradedAt': sub.graded_at.isoformat() if (sub.graded_at and show_grades) else None,
            'published': sub.published})
    def serialize_item(item):
        data = {'id': item.id, 'type': item.item_type, 'label': item.label, 'order': item.order,
            'points': item.points, 'section': item.section}
        if item.item_type == AssignmentItem.ItemType.PDF:
            data['pdfUrl'] = reverse('item-pdf', args=[item.id])
        elif item.item_type == AssignmentItem.ItemType.IMAGE:
            data['imageUrl'] = reverse('item-image', args=[item.id])
        elif item.item_type == AssignmentItem.ItemType.VIDEO:
            data['videoUrl'] = reverse('item-video', args=[item.id])
        else:
            data['prompt'] = item.prompt
            data['options'] = item.options
        if is_teacher_view:
            # Never sent to pupils — same "gate" reasoning as the join code below.
            data['correctAnswer'] = item.correct_answer
        return data
    items = [serialize_item(item) for item in assignment.items.all()]
    return {'id': assignment.id, 'title': assignment.title, 'prompt': assignment.instructions,
        'items': items,
        'teacherName': assignment.teacher_name, 'teacherEmail': assignment.teacher_email,
        'opensAt': manila_isoformat(assignment.opens_at), 'dueAt': manila_isoformat(assignment.due_at),
        'durationMinutes': assignment.duration_minutes,
        'deliveryMode': assignment.delivery_mode,
        'requireJoinCode': assignment.require_join_code,
        # The real code is only ever sent to the teacher's own view — sending it to
        # pupils in the assignments payload would make the "gate" pointless.
        'joinCode': assignment.join_code if is_teacher_view else None,
        'allowCalculator': assignment.allow_calculator,
        'allowGraphingCalculator': assignment.allow_graphing_calculator,
        'recipients': recipients if pupil_email is None else [r for r in recipients if r['email'] == pupil_email]}


@login_required
@require_GET
def assignments_api(request):
    email = request.GET.get('pupil_email')
    qs = Assignment.objects.prefetch_related(Prefetch('recipients', queryset=annotated_recipients()), 'items').all()
    if request.user.role == User.Role.PUPIL and not request.user.is_superuser:
        email = request.user.email
    if email:
        qs = qs.filter(recipients__pupil_email__iexact=email).distinct()
    return JsonResponse({'assignments': [serialize_assignment(a, email) for a in qs]})


@login_required
@require_GET
def item_pdf(request, item_id):
    """Serve a single PDF-type item's file inline so PDF.js can render it in the workspace."""
    item = get_object_or_404(AssignmentItem, id=item_id, item_type=AssignmentItem.ItemType.PDF)
    assignment = item.assignment
    if request.user.role == User.Role.PUPIL and not request.user.is_superuser:
        if not assignment.recipients.filter(pupil_email__iexact=request.user.email).exists():
            return HttpResponseForbidden('This PDF is not assigned to your account.')
    if assignment.delivery_mode == Assignment.DeliveryMode.LOCKDOWN and is_real_pupil(request) and not seb_lockdown_ok(request):
        return seb_required_response(request)
    filename = item.pdf.name.rsplit('/', 1)[-1]
    try:
        handle = item.pdf.open('rb')
    except (FileNotFoundError, ValueError):
        return missing_file_response()
    response = FileResponse(handle, content_type='application/pdf', as_attachment=False, filename=filename)
    response['Content-Disposition'] = f'inline; filename="{filename}"'
    return response


def missing_file_response():
    """The item's file record exists but the file itself isn't on this server's disk — e.g. it
    was uploaded on another copy of the app (local vs Railway share one database but not one
    disk), or the hosting platform wiped its disk on redeploy. A clear 404 instead of a crash."""
    return JsonResponse({'code': 'file_missing',
        'detail': "This file isn't available on the server any more. Ask your teacher to upload it again."}, status=404)


def _serve_item_media(request, item_id, item_type, field_name):
    """Shared by item_image/item_video: same ownership + SEB gating as item_pdf, just serving
    whichever field the item type uses and guessing its content-type from the filename."""
    item = get_object_or_404(AssignmentItem, id=item_id, item_type=item_type)
    assignment = item.assignment
    if request.user.role == User.Role.PUPIL and not request.user.is_superuser:
        if not assignment.recipients.filter(pupil_email__iexact=request.user.email).exists():
            return HttpResponseForbidden('This file is not assigned to your account.')
    if assignment.delivery_mode == Assignment.DeliveryMode.LOCKDOWN and is_real_pupil(request) and not seb_lockdown_ok(request):
        return seb_required_response(request)
    field = getattr(item, field_name)
    filename = field.name.rsplit('/', 1)[-1]
    content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    try:
        handle = field.open('rb')
    except (FileNotFoundError, ValueError):
        return missing_file_response()
    response = FileResponse(handle, content_type=content_type, as_attachment=False, filename=filename)
    response['Content-Disposition'] = f'inline; filename="{filename}"'
    return response


@login_required
@require_GET
def item_image(request, item_id):
    """Serve a single IMAGE-type item's file inline so pupils/teachers can view it."""
    return _serve_item_media(request, item_id, AssignmentItem.ItemType.IMAGE, 'image')


@login_required
@require_GET
def item_video(request, item_id):
    """Serve a single VIDEO-type item's file inline (in a range-friendly way via FileResponse)
    so the browser's <video> element can play it, including seeking."""
    return _serve_item_media(request, item_id, AssignmentItem.ItemType.VIDEO, 'video')


@login_required
@require_GET
def assignment_status_api(request, assignment_id):
    """Lightweight polling endpoint for the Monitor tab — just statuses, not the full
    assignment payload (annotations, responses, etc.), so a teacher can leave this tab open
    and poll it every few seconds without re-downloading everything each time."""
    assignment = get_object_or_404(Assignment, id=assignment_id)
    if request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        return HttpResponseForbidden('Only teachers can view assignment status.')
    recipients = []
    for recipient in annotated_recipients().filter(assignment=assignment):
        sub = _submission_of(recipient)
        recipients.append({
            'id': recipient.id, 'name': recipient.pupil_name, 'email': recipient.pupil_email,
            'status': sub.status,
            'startedAt': sub.started_at.isoformat() if sub.started_at else None,
            'submittedAt': sub.submitted_at.isoformat() if sub.submitted_at else None,
            # So the Monitor tab's "Time left" countdown and unread dots stay current between
            # full reloads (an accommodation or a new pupil message mid-exam shows up on the next poll).
            'extraMinutes': recipient.extra_minutes,
            'hasUnreadFromPupil': _unread_since(recipient.last_pupil_msg, recipient.teacher_last_read_at),
            'locked': bool(sub.locked_at), 'lockReason': sub.lock_reason, 'lockCount': sub.lock_count,
            'resumeCode': sub.resume_code if sub.locked_at else None,
        })
    return JsonResponse({'id': assignment.id, 'recipients': recipients})


@login_required
@require_GET
def pupils_api(request):
    """Real pupil accounts (role=PUPIL), for the teacher's "assign to" picker. An admin
    creates these in Django admin with a name and email; pupils are matched to assignments
    by that email, so an account with a blank email can't be assigned anything."""
    if request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        return HttpResponseForbidden('Only teachers can view the pupil list.')
    pupils = User.assignable_pupils().order_by('first_name', 'last_name', 'username')
    return JsonResponse({'pupils': [
        {'id': p.id, 'name': p.get_full_name() or p.username, 'email': p.email}
        for p in pupils if p.email
    ]})


@login_required
@require_POST
def create_assignment(request):
    if request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        return HttpResponseForbidden('Only teachers can create assignments.')
    if not request.user.email:
        return HttpResponseBadRequest(
            'Your account has no email on file, so submissions can\'t be sent to you. '
            'Ask your admin to add one to your account in Django admin.')
    teacher_name = request.user.get_full_name().strip() or request.user.username
    teacher_email = request.user.email

    pdfs = request.FILES.getlist('pdfs')
    images = request.FILES.getlist('images')
    videos = request.FILES.getlist('videos')
    try:
        items_meta = json.loads(request.POST.get('items_meta', '[]'))
        if not isinstance(items_meta, list) or not items_meta:
            raise ValueError
    except (ValueError, json.JSONDecodeError):
        return HttpResponseBadRequest('Add at least one text or question.')
    if not request.POST.get('title', '').strip():
        return HttpResponseBadRequest('Give the assignment a title.')
    for pdf in pdfs:
        if pdf.content_type not in ('application/pdf', 'application/x-pdf'):
            return HttpResponseBadRequest('Please upload PDF files only.')
    for image in images:
        if not (image.content_type or '').startswith('image/'):
            return HttpResponseBadRequest('Please upload image files only.')
    for video in videos:
        if not (video.content_type or '').startswith('video/'):
            return HttpResponseBadRequest('Please upload video files only.')

    valid_types = set(AssignmentItem.ItemType.values)
    pdf_iter = iter(pdfs)
    image_iter = iter(images)
    video_iter = iter(videos)
    prepared_items = []
    for i, meta in enumerate(items_meta):
        item_type = meta.get('type')
        if item_type not in valid_types:
            return HttpResponseBadRequest(f'Item {i + 1}: unrecognised content type.')
        label = (meta.get('label') or '').strip() or f'Item {i + 1}'
        section = (meta.get('section') or '').strip()
        try:
            points = float(meta.get('points') if meta.get('points') not in (None, '') else 1.0)
            if points <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return HttpResponseBadRequest(f'"{label}" needs a valid point value.')
        if item_type == AssignmentItem.ItemType.PDF:
            pdf_file = next(pdf_iter, None)
            if not pdf_file:
                return HttpResponseBadRequest(f'"{label}" is missing its PDF file.')
            prepared_items.append({'item_type': item_type, 'label': label, 'pdf': pdf_file, 'order': i, 'points': points, 'section': section})
            continue
        if item_type == AssignmentItem.ItemType.IMAGE:
            image_file = next(image_iter, None)
            if not image_file:
                return HttpResponseBadRequest(f'"{label}" is missing its image file.')
            prepared_items.append({'item_type': item_type, 'label': label, 'image': image_file, 'order': i, 'points': points, 'section': section})
            continue
        if item_type == AssignmentItem.ItemType.VIDEO:
            video_file = next(video_iter, None)
            if not video_file:
                return HttpResponseBadRequest(f'"{label}" is missing its video file.')
            prepared_items.append({'item_type': item_type, 'label': label, 'video': video_file, 'order': i, 'points': points, 'section': section})
            continue
        prompt = (meta.get('prompt') or '').strip()
        if not prompt:
            return HttpResponseBadRequest(f'"{label}" needs a question prompt.')
        options = meta.get('options') or []
        correct_answer = meta.get('correctAnswer') or {}
        if item_type in (AssignmentItem.ItemType.MCQ_SINGLE, AssignmentItem.ItemType.MCQ_MULTIPLE):
            if not isinstance(options, list) or len(options) < 2:
                return HttpResponseBadRequest(f'"{label}" needs at least two answer options.')
        prepared_items.append({'item_type': item_type, 'label': label, 'prompt': prompt,
            'options': options, 'correct_answer': correct_answer, 'order': i, 'points': points, 'section': section})
    try:
        recipients = json.loads(request.POST.get('recipients', '[]'))
        if not recipients:
            raise ValueError
    except (ValueError, json.JSONDecodeError):
        return HttpResponseBadRequest('Add at least one pupil with an email address.')
    try:
        duration_minutes = int(request.POST.get('duration_minutes') or 60)
        if duration_minutes < 1:
            raise ValueError
    except ValueError:
        return HttpResponseBadRequest('Exam duration must be a whole number of minutes.')

    delivery_mode = request.POST.get('delivery_mode') or Assignment.DeliveryMode.BROWSER
    if delivery_mode not in Assignment.DeliveryMode.values:
        return HttpResponseBadRequest('Unrecognised delivery mode.')
    require_join_code = request.POST.get('require_join_code') == 'true'
    join_code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6)) if require_join_code else ''
    allow_calculator = request.POST.get('allow_calculator') == 'true'
    allow_graphing_calculator = request.POST.get('allow_graphing_calculator') == 'true'
    due_at = parse_manila_datetime(request.POST.get('due_at'))

    assignment = Assignment.objects.create(title=request.POST['title'].strip(), instructions=request.POST.get('instructions', '').strip(),
        teacher_name=teacher_name, teacher_email=teacher_email,
        opens_at=parse_manila_datetime(request.POST.get('opens_at')) or timezone.now(), due_at=due_at, duration_minutes=duration_minutes,
        delivery_mode=delivery_mode, require_join_code=require_join_code, join_code=join_code,
        allow_calculator=allow_calculator, allow_graphing_calculator=allow_graphing_calculator)
    for prepared in prepared_items:
        AssignmentItem.objects.create(assignment=assignment, **prepared)
    for pupil in recipients:
        if pupil.get('name', '').strip() and pupil.get('email', '').strip():
            Recipient.objects.create(assignment=assignment, pupil_name=pupil['name'].strip(), pupil_email=pupil['email'].strip())
    if not assignment.recipients.exists():
        assignment.delete()
        return HttpResponseBadRequest('Add at least one complete pupil entry.')
    log_activity(teacher_name, teacher_email, 'created',
        f'Created "{assignment.title}" for {assignment.recipients.count()} pupil(s).', assignment=assignment)
    # The browser reloads the assignment list after creation, so returning the
    # new ID is sufficient and avoids serialising a partially created upload.
    return JsonResponse({'id': assignment.id}, status=201)


@login_required
@require_POST
def update_assignment_api(request, assignment_id):
    """Settings-only edit for an existing assignment: title, instructions, schedule,
    exam duration, delivery mode, join code and calculator toggles. Editing the PDFs or
    questions themselves isn't supported here yet — see the Create tab's own notice."""
    if request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        return HttpResponseForbidden('Only teachers can edit assignments.')
    assignment = get_object_or_404(Assignment, id=assignment_id)
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid assignment data.')

    title = (payload.get('title') or '').strip()
    if not title:
        return HttpResponseBadRequest('Give the assignment a title.')

    try:
        duration_minutes = int(payload.get('durationMinutes') or 60)
        if duration_minutes < 1:
            raise ValueError
    except (TypeError, ValueError):
        return HttpResponseBadRequest('Exam duration must be a whole number of minutes.')

    delivery_mode = payload.get('deliveryMode') or Assignment.DeliveryMode.BROWSER
    if delivery_mode not in Assignment.DeliveryMode.values:
        return HttpResponseBadRequest('Unrecognised delivery mode.')

    assignment.title = title
    assignment.instructions = (payload.get('instructions') or '').strip()
    if payload.get('opensAt'):
        assignment.opens_at = parse_manila_datetime(payload['opensAt'])
    assignment.due_at = parse_manila_datetime(payload.get('dueAt'))
    assignment.duration_minutes = duration_minutes
    assignment.delivery_mode = delivery_mode

    require_join_code = bool(payload.get('requireJoinCode'))
    if require_join_code and not assignment.require_join_code:
        # Turning it on fresh (it wasn't required before) — generate a new code.
        assignment.join_code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    elif not require_join_code:
        assignment.join_code = ''
    assignment.require_join_code = require_join_code

    assignment.allow_calculator = bool(payload.get('allowCalculator'))
    assignment.allow_graphing_calculator = bool(payload.get('allowGraphingCalculator'))
    assignment.save()
    assignment.refresh_from_db()
    log_activity(request.user.get_full_name() or request.user.username, request.user.email,
        'updated', f'Updated settings for "{assignment.title}".', assignment=assignment)
    return JsonResponse(serialize_assignment(assignment))


@login_required
@require_POST
def delete_assignment_api(request, assignment_id):
    """Deletes an assignment and everything under it (items, recipients, submissions —
    all cascade-deleted). Irreversible and wipes pupil work, so it requires the logged-in
    teacher to re-type their own account password as an extra confirmation step, on top of
    whatever "are you sure?" prompt the frontend already shows."""
    if request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        return HttpResponseForbidden('Only teachers can delete assignments.')
    assignment = get_object_or_404(Assignment, id=assignment_id)
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid request.')
    password = payload.get('password') or ''
    if not password:
        return HttpResponseBadRequest('Enter your password to confirm deletion.')
    if not request.user.check_password(password):
        return HttpResponseForbidden('Incorrect password.')
    log_activity(request.user.get_full_name() or request.user.username, request.user.email,
        'deleted', f'Deleted assignment "{assignment.title}".')
    assignment.delete()
    return JsonResponse({'deleted': True})


@login_required
@require_POST
def start_submission(request, recipient_id):
    """Called when a pupil opens an assignment; records when their timed attempt began."""
    recipient = get_object_or_404(Recipient, id=recipient_id)
    # Anyone who isn't the actual, restricted pupil this work belongs to is "acting as teacher"
    # in this app (that includes the shared superuser account used for previews) — matches the
    # frontend's own read-only-preview condition (document.body.dataset.role !== 'pupil').
    if not is_real_pupil(request):
        return HttpResponseForbidden("Teachers can't start or submit work on a pupil's behalf. Preview a pupil's task list to see it read-only, or have the pupil sign in themselves.")
    if request.user.role == User.Role.PUPIL and not request.user.is_superuser:
        if recipient.pupil_email.lower() != request.user.email.lower():
            return HttpResponseForbidden('This work is not assigned to your account.')
    assignment = recipient.assignment
    if assignment.delivery_mode == Assignment.DeliveryMode.LOCKDOWN and is_real_pupil(request) and not seb_lockdown_ok(request):
        return seb_required_response(request)
    if assignment.require_join_code:
        try:
            payload = json.loads(request.body or '{}')
        except json.JSONDecodeError:
            payload = {}
        submitted_code = (payload.get('joinCode') or '').strip().upper()
        if submitted_code != assignment.join_code.upper():
            return JsonResponse({'detail': 'Incorrect join code.'}, status=403)
    submission, _ = Submission.objects.get_or_create(recipient=recipient)
    if not submission.started_at and submission.status != 'submitted':
        if assignment_closed(assignment):
            return assignment_closed_response()
        submission.started_at = timezone.now()
        if submission.status == 'not_started':
            submission.status = 'draft'
        submission.save()
    return JsonResponse({
        'startedAt': submission.started_at.isoformat() if submission.started_at else None,
        'status': submission.status,
    })


def submission_from_request(request, recipient_id):
    recipient = get_object_or_404(Recipient, id=recipient_id)
    # Same reasoning as start_submission: block anyone who isn't the actual restricted pupil,
    # not just accounts with role=TEACHER — a superuser previewing is "acting as teacher" too.
    if not is_real_pupil(request):
        return None, HttpResponseForbidden("Teachers can't save or submit work on a pupil's behalf. Preview a pupil's task list to see it read-only, or have the pupil sign in themselves.")
    if request.user.role == User.Role.PUPIL and not request.user.is_superuser:
        if recipient.pupil_email.lower() != request.user.email.lower():
            return None, HttpResponseForbidden('This work is not assigned to your account.')
    if recipient.assignment.delivery_mode == Assignment.DeliveryMode.LOCKDOWN and is_real_pupil(request) and not seb_lockdown_ok(request):
        return None, seb_required_response(request)
    payload = json.loads(request.body or '{}')
    submission, _ = Submission.objects.get_or_create(recipient=recipient)
    if submission.status == 'submitted':
        return None, JsonResponse({'detail': 'This work has already been submitted.'}, status=409)
    if assignment_closed(recipient.assignment):
        return None, assignment_closed_response()
    if submission.locked_at:
        return None, exam_locked_response()
    submission.annotations = payload.get('annotations', [])
    submission.outline_html = payload.get('outlineHtml', '')
    responses = payload.get('responses', {})
    submission.responses = responses if isinstance(responses, dict) else {}
    submission.status = 'draft'
    submission.save()
    return submission, None


@login_required
@require_POST
def save_submission(request, recipient_id):
    try:
        submission, error = submission_from_request(request, recipient_id)
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid save data.')
    if error:
        return error
    return JsonResponse({'status': submission.status, 'updatedAt': submission.updated_at.isoformat()})


@login_required
@require_POST
def submit_submission(request, recipient_id):
    try:
        submission, error = submission_from_request(request, recipient_id)
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid submission data.')
    if error:
        return error
    submission.status = 'submitted'
    submission.submitted_at = timezone.now()
    submission.save()
    assignment = submission.recipient.assignment
    log_activity(submission.recipient.pupil_name, submission.recipient.pupil_email,
        'submitted', f'{submission.recipient.pupil_name} submitted "{assignment.title}".', assignment=assignment)
    review_url = request.build_absolute_uri('/?review=' + str(submission.recipient_id))
    send_mail(f'ExamHub submission: {assignment.title}',
        f'{submission.recipient.pupil_name} submitted their annotated work. Review it at {review_url}',
        None, [assignment.teacher_email], fail_silently=False)
    return JsonResponse({'status': 'submitted', 'submittedAt': submission.submitted_at.isoformat()})


# ---------------- "Any browser with security" exit lock (Phase 10) ----------------
# A web page can't stop a browser opening a new tab or window, but it can tell the instant
# the pupil leaves the exam (tab hidden, window loses focus, fullscreen exited). The pupil page
# reports that here; from then on saves/submits are refused until they enter a one-word resume
# code their teacher reads out from Monitor (a fresh code every time, so an old one can't be
# reused), or the teacher unlocks them. Enforced server-side, so reloading the page or poking
# at it in developer tools doesn't get round it.
RESUME_WORDS = (
    'apple amber anchor arrow badge basil beach berry bloom brick brook cabin candle canyon cedar '
    'chalk cherry cloud clover cobalt comet coral cotton crane crystal daisy delta desert dolphin '
    'eagle ember falcon feather fern flint forest fossil galaxy garden ginger glacier grape harbor '
    'hazel honey island ivory jasmine jungle kettle kiwi lagoon lantern lemon lilac lotus maple '
    'marble meadow melon mango mint moss nectar nutmeg oasis ocean olive orbit orchid otter panda '
    'papaya parrot peach pebble pepper pine planet plum pollen poppy prism quartz rabbit radish '
    'rain raven reef ribbon river robin rocket saffron salmon sand shell silver sky sparrow spruce '
    'star stone summit sunset swan tiger topaz tulip tundra velvet violet walnut willow winter zebra'
).split()


def new_resume_code(previous=''):
    choices = [w for w in RESUME_WORDS if w != previous]
    return secrets.choice(choices)


def exam_locked_response():
    return JsonResponse({
        'code': 'exam_locked',
        'detail': 'Your exam is paused because you left the exam window. Ask your teacher for the resume code.',
    }, status=403)


def _own_submission(request, recipient_id):
    """(recipient, submission, error) for a real pupil acting on their own attempt."""
    recipient = get_object_or_404(Recipient, id=recipient_id)
    if not is_real_pupil(request):
        return recipient, None, HttpResponseForbidden('Only the pupil taking this exam can do this.')
    if recipient.pupil_email.lower() != request.user.email.lower():
        return recipient, None, HttpResponseForbidden('This work is not assigned to your account.')
    submission, _ = Submission.objects.get_or_create(recipient=recipient)
    return recipient, submission, None


@login_required
@require_POST
def lock_submission_api(request, recipient_id):
    recipient, submission, error = _own_submission(request, recipient_id)
    if error:
        return error
    if recipient.assignment.delivery_mode != Assignment.DeliveryMode.BROWSER_SECURE or submission.status == 'submitted':
        return JsonResponse({'locked': False})
    if submission.locked_at:  # already locked (e.g. two exit events in a row): keep the same code
        return JsonResponse({'locked': True, 'lockReason': submission.lock_reason})
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        payload = {}
    # Save whatever they had at the moment they left, so nothing typed before the lock is lost
    # even if time runs out while they're waiting for the code.
    if not assignment_closed(recipient.assignment) and isinstance(payload.get('responses'), dict):
        submission.annotations = payload.get('annotations', submission.annotations)
        submission.outline_html = payload.get('outlineHtml', submission.outline_html)
        submission.responses = payload['responses']
        if submission.status == 'not_started':
            submission.status = 'draft'
    reason = (payload.get('reason') or 'Left the exam window')[:60]
    submission.locked_at = timezone.now()
    submission.lock_reason = reason
    submission.resume_code = new_resume_code(submission.resume_code)
    submission.lock_count += 1
    submission.save()
    log_activity(recipient.pupil_name, recipient.pupil_email, 'locked',
        f'{recipient.pupil_name} was locked out of "{recipient.assignment.title}" ({reason.lower()}).',
        assignment=recipient.assignment)
    return JsonResponse({'locked': True, 'lockReason': reason})


@login_required
@require_POST
def resume_submission_api(request, recipient_id):
    recipient, submission, error = _own_submission(request, recipient_id)
    if error:
        return error
    if not submission.locked_at:
        return JsonResponse({'locked': False})
    try:
        code = (json.loads(request.body or '{}').get('code') or '').strip().lower()
    except json.JSONDecodeError:
        code = ''
    if not code or not hmac.compare_digest(code, submission.resume_code.lower()):
        return JsonResponse({'detail': "That code isn't right. Check with your teacher."}, status=403)
    submission.locked_at = None
    submission.save(update_fields=['locked_at'])
    log_activity(recipient.pupil_name, recipient.pupil_email, 'resumed',
        f'{recipient.pupil_name} resumed "{recipient.assignment.title}" with the teacher\'s code.', assignment=recipient.assignment)
    return JsonResponse({'locked': False})


@login_required
@require_POST
def unlock_submission_api(request, recipient_id):
    denied = _require_teacher(request)
    if denied:
        return denied
    recipient = get_object_or_404(Recipient, id=recipient_id)
    submission, _ = Submission.objects.get_or_create(recipient=recipient)
    submission.locked_at = None
    submission.save(update_fields=['locked_at'])
    log_activity(request.user.get_full_name() or request.user.username, request.user.email, 'unlocked',
        f'Unlocked {recipient.pupil_name} on "{recipient.assignment.title}".', assignment=recipient.assignment)
    return JsonResponse({'locked': False})


@login_required
@require_GET
def lock_status_api(request, recipient_id):
    """Polled by a locked pupil's screen so it clears by itself if the teacher unlocks them."""
    recipient = get_object_or_404(Recipient, id=recipient_id)
    denied = _recipient_access_check(request, recipient)
    if denied:
        return denied
    submission, _ = Submission.objects.get_or_create(recipient=recipient)
    return JsonResponse({'locked': bool(submission.locked_at), 'lockReason': submission.lock_reason})


# ---------------- classes & pupil roster (teacher-side, mirrors what admin can do) ----------------

def _require_teacher(request):
    if request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        return HttpResponseForbidden('Only teachers can manage classes and pupils.')
    return None


def serialize_class(cls):
    return {
        'id': cls.id,
        'name': cls.name,
        'teacherNames': [t.get_full_name() or t.username for t in cls.teachers.all()],
        'pupils': [
            {'id': p.id, 'name': p.get_full_name() or p.username, 'email': p.email}
            for p in cls.pupils.all().order_by('first_name', 'last_name', 'username')
        ],
    }


@login_required
@require_GET
def classes_api(request):
    denied = _require_teacher(request)
    if denied:
        return denied
    classes = SchoolClass.objects.all().prefetch_related('pupils', 'teachers')
    return JsonResponse({'classes': [serialize_class(c) for c in classes]})


@login_required
@require_POST
def create_class_api(request):
    denied = _require_teacher(request)
    if denied:
        return denied
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid class data.')
    name = (payload.get('name') or '').strip()
    if not name:
        return HttpResponseBadRequest('Give the class a name.')
    if SchoolClass.objects.filter(name__iexact=name).exists():
        return HttpResponseBadRequest('A class with that name already exists.')
    pupil_ids = payload.get('pupilIds') or []
    cls = SchoolClass.objects.create(name=name)
    cls.teachers.add(request.user)
    if pupil_ids:
        pupils = User.assignable_pupils().filter(id__in=pupil_ids)
        cls.pupils.set(pupils)
    log_activity(request.user.get_full_name() or request.user.username, request.user.email,
        'class_created', f'Created class "{cls.name}".')
    return JsonResponse(serialize_class(cls), status=201)


@login_required
@require_POST
def update_class_api(request, class_id):
    denied = _require_teacher(request)
    if denied:
        return denied
    cls = get_object_or_404(SchoolClass, id=class_id)
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid class data.')
    name = (payload.get('name') or '').strip()
    if name and name.lower() != cls.name.lower():
        if SchoolClass.objects.filter(name__iexact=name).exclude(id=cls.id).exists():
            return HttpResponseBadRequest('A class with that name already exists.')
        cls.name = name
        cls.save()
    if 'pupilIds' in payload:
        pupils = User.assignable_pupils().filter(id__in=payload.get('pupilIds') or [])
        cls.pupils.set(pupils)
    cls.teachers.add(request.user)
    return JsonResponse(serialize_class(cls))


@login_required
@require_POST
def delete_class_api(request, class_id):
    denied = _require_teacher(request)
    if denied:
        return denied
    cls = get_object_or_404(SchoolClass, id=class_id)
    log_activity(request.user.get_full_name() or request.user.username, request.user.email,
        'class_deleted', f'Deleted class "{cls.name}".')
    cls.delete()
    return JsonResponse({'deleted': True})


def _unique_username(base):
    base = base or 'pupil'
    candidate = base
    n = 1
    while User.objects.filter(username=candidate).exists():
        n += 1
        candidate = f'{base}{n}'
    return candidate


@login_required
@require_POST
def create_pupil_api(request):
    """Lets a teacher (not just admin) add a pupil on the spot while building a class or
    an assignment. Generates a one-time temporary password the teacher can hand to the
    pupil — there's no self-registration or password-reset flow yet, so this is the only
    way a brand-new pupil gets into their account."""
    denied = _require_teacher(request)
    if denied:
        return denied
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid pupil data.')
    first_name = (payload.get('firstName') or '').strip()
    last_name = (payload.get('lastName') or '').strip()
    email = (payload.get('email') or '').strip()
    if not first_name or not email:
        return HttpResponseBadRequest('At least a first name and email are required.')
    if User.objects.filter(email__iexact=email).exists():
        return HttpResponseBadRequest('A pupil with that email already exists.')
    username_base = email.split('@')[0].lower() or first_name.lower()
    username = _unique_username(username_base)
    temp_password = secrets.token_urlsafe(6)
    pupil = User.objects.create_user(
        username=username, email=email, first_name=first_name, last_name=last_name,
        password=temp_password, role=User.Role.PUPIL,
    )
    class_id = payload.get('classId')
    if class_id:
        cls = SchoolClass.objects.filter(id=class_id).first()
        if cls:
            cls.pupils.add(pupil)
    log_activity(request.user.get_full_name() or request.user.username, request.user.email,
        'pupil_created', f'Added pupil {pupil.get_full_name() or pupil.username} ({pupil.email}).')
    return JsonResponse({
        'id': pupil.id, 'name': pupil.get_full_name() or pupil.username, 'email': pupil.email,
        'username': pupil.username, 'tempPassword': temp_password,
    }, status=201)


# ---------------- accommodations, 1:1 chat, activity log (Phase 4) ----------------

@login_required
@require_POST
def update_recipient_accommodations_api(request, recipient_id):
    """Per-pupil accommodations: extra exam time and/or early access, layered on top of
    the assignment's own schedule. Its own tiny endpoint rather than folded into the
    assignment-settings edit, since it's set per-pupil, not per-assignment."""
    denied = _require_teacher(request)
    if denied:
        return denied
    recipient = get_object_or_404(Recipient, id=recipient_id)
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid accommodation data.')
    try:
        extra_minutes = int(payload.get('extraMinutes') or 0)
        if extra_minutes < 0:
            raise ValueError
    except (TypeError, ValueError):
        return HttpResponseBadRequest('Extra time must be zero or a positive number of minutes.')
    recipient.extra_minutes = extra_minutes
    recipient.opens_override = parse_manila_datetime(payload.get('opensOverride'))
    recipient.save()
    recipient.refresh_from_db()
    log_activity(request.user.get_full_name() or request.user.username, request.user.email,
        'accommodation',
        f'Set accommodations for {recipient.pupil_name} on "{recipient.assignment.title}" '
        f'(+{extra_minutes} min{", early access" if recipient.opens_override else ""}).',
        assignment=recipient.assignment)
    return JsonResponse({
        'id': recipient.id,
        'extraMinutes': recipient.extra_minutes,
        'opensOverride': manila_isoformat(recipient.opens_override),
    })


def _recipient_access_check(request, recipient):
    """Shared guard for the 1:1 chat endpoints: any teacher can message any recipient
    (this app has no per-assignment ownership — see _require_teacher); a pupil only their
    own."""
    if request.user.role == User.Role.PUPIL and not request.user.is_superuser:
        if recipient.pupil_email.lower() != request.user.email.lower():
            return HttpResponseForbidden('This is not your assignment.')
    elif request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        return HttpResponseForbidden('Not allowed.')
    return None


@login_required
@require_GET
def messages_api(request, recipient_id):
    recipient = get_object_or_404(Recipient, id=recipient_id)
    denied = _recipient_access_check(request, recipient)
    if denied:
        return denied
    messages = [
        {'id': m.id, 'senderRole': m.sender_role, 'senderName': m.sender_name, 'text': m.text,
         'isBroadcast': m.is_broadcast, 'createdAt': m.created_at.isoformat()}
        for m in recipient.messages.all()
    ]
    # Opening this thread marks it read for whichever side just opened it — a real pupil
    # reading their own chat, or a teacher (or a superuser previewing) reading it from Monitor.
    if is_real_pupil(request):
        recipient.pupil_last_read_at = timezone.now()
        recipient.save(update_fields=['pupil_last_read_at'])
    else:
        recipient.teacher_last_read_at = timezone.now()
        recipient.save(update_fields=['teacher_last_read_at'])
    return JsonResponse({'messages': messages})


@login_required
@require_POST
def send_message_api(request, recipient_id):
    recipient = get_object_or_404(Recipient, id=recipient_id)
    denied = _recipient_access_check(request, recipient)
    if denied:
        return denied
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid message data.')
    text = (payload.get('text') or '').strip()
    if not text:
        return HttpResponseBadRequest("Message can't be empty.")
    if len(text) > 2000:
        return HttpResponseBadRequest('Message is too long.')
    # A genuine pupil account (not a superuser/teacher wearing a pupil hat for testing or
    # previewing) is always the pupil side — that's enforced above and never trusts the
    # client. Anyone else (a real teacher, or a superuser test account that also happens to
    # be flagged as role=pupil) is sending from the teacher-side Monitor chat UNLESS they're
    # in the pupil "Message teacher" UI, which sets asPupil so the message is attributed to
    # the pupil being previewed rather than showing up as the teacher talking to themselves.
    is_real_pupil = request.user.role == User.Role.PUPIL and not request.user.is_superuser
    if is_real_pupil:
        sender_role, sender_name = User.Role.PUPIL, (request.user.get_full_name() or request.user.username)
    elif payload.get('asPupil'):
        sender_role, sender_name = User.Role.PUPIL, recipient.pupil_name
    else:
        sender_role, sender_name = User.Role.TEACHER, (request.user.get_full_name() or request.user.username)
    message = Message.objects.create(recipient=recipient, sender_role=sender_role,
        sender_name=sender_name, text=text)
    # Sending a message yourself obviously means you've "read" the thread up to now too —
    # otherwise a teacher's own reply would immediately show back to them as unread.
    if is_real_pupil:
        recipient.pupil_last_read_at = timezone.now()
        recipient.save(update_fields=['pupil_last_read_at'])
    elif not payload.get('asPupil'):
        recipient.teacher_last_read_at = timezone.now()
        recipient.save(update_fields=['teacher_last_read_at'])
    return JsonResponse({'id': message.id, 'senderRole': message.sender_role,
        'senderName': message.sender_name, 'text': message.text, 'isBroadcast': message.is_broadcast,
        'createdAt': message.created_at.isoformat()}, status=201)


@login_required
@require_POST
def broadcast_message_api(request, assignment_id):
    """Sends one message to every current recipient of an assignment at once — for a
    teacher who wants to remind or notify the whole class rather than messaging pupils
    one by one. Shows up in each pupil's own 1:1 thread with the teacher, tagged as a
    broadcast so it reads differently from a personal reply."""
    denied = _require_teacher(request)
    if denied:
        return denied
    assignment = get_object_or_404(Assignment, id=assignment_id)
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid message data.')
    text = (payload.get('text') or '').strip()
    if not text:
        return HttpResponseBadRequest("Message can't be empty.")
    if len(text) > 2000:
        return HttpResponseBadRequest('Message is too long.')
    sender_name = request.user.get_full_name() or request.user.username
    now = timezone.now()
    recipients = list(assignment.recipients.all())
    Message.objects.bulk_create([
        Message(recipient=r, sender_role=User.Role.TEACHER, sender_name=sender_name,
            text=text, is_broadcast=True, created_at=now)
        for r in recipients
    ])
    # The teacher just sent it, so it's "read" on their side for every one of these threads —
    # only the pupil side should show it as unread.
    Recipient.objects.filter(id__in=[r.id for r in recipients]).update(teacher_last_read_at=now)
    log_activity(sender_name, request.user.email, 'broadcast',
        f'Messaged all {len(recipients)} pupil(s) on "{assignment.title}".', assignment=assignment)
    return JsonResponse({'sent': len(recipients)}, status=201)


@login_required
@require_GET
def activity_log_api(request):
    denied = _require_teacher(request)
    if denied:
        return denied
    try:
        limit = min(int(request.GET.get('limit') or 100), 300)
    except ValueError:
        limit = 100
    entries = ActivityLogEntry.objects.all()[:limit]
    return JsonResponse({'entries': [
        {'id': e.id, 'actorName': e.actor_name, 'actorEmail': e.actor_email, 'action': e.action,
         'description': e.description, 'assignmentId': e.assignment_id,
         'createdAt': e.created_at.isoformat()}
        for e in entries
    ]})


# ---------------- grading & publishing (Phase 5) ----------------

@login_required
@require_POST
def grade_submission_api(request, recipient_id):
    """Recomputes auto-gradable item scores fresh from the answer key on every save (so
    editing an answer key, or a pupil's saved answer changing, never leaves a stale score),
    and merges in whatever manual scores/feedback the teacher entered for the rest. Passing
    an explicit score for an otherwise auto-graded item overrides it and sticks until the
    teacher grades again with a different override."""
    denied = _require_teacher(request)
    if denied:
        return denied
    recipient = get_object_or_404(Recipient, id=recipient_id)
    submission, _ = Submission.objects.get_or_create(recipient=recipient)
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid grading data.')
    manual_scores = payload.get('scores') or {}
    if not isinstance(manual_scores, dict):
        return HttpResponseBadRequest('Invalid grading data.')

    existing = submission.item_scores or {}
    item_scores = {}
    total = 0.0
    max_total = 0.0
    for item in recipient.assignment.items.all():
        key = str(item.id)
        manual = manual_scores.get(key) if isinstance(manual_scores.get(key), dict) else None
        prior = existing.get(key) or {}
        auto_score, max_score = auto_grade_item(item, submission.responses.get(key))
        # A manual entry's 'score' is optional: the teacher can leave feedback on an
        # auto-graded item (e.g. explaining a wrong MCQ answer) without also having to
        # override its score.
        has_manual_score = manual is not None and manual.get('score') not in (None, '')
        feedback = (manual.get('feedback') if manual and 'feedback' in manual else prior.get('feedback', '')) or ''
        feedback = feedback.strip() if isinstance(feedback, str) else ''
        if has_manual_score:
            try:
                score = float(manual['score'])
            except (TypeError, ValueError):
                return HttpResponseBadRequest(f'Invalid score for "{item.label}".')
            score = max(0.0, min(score, item.points))
            item_scores[key] = {'score': score, 'maxScore': item.points, 'auto': False, 'feedback': feedback}
        elif auto_score is not None:
            item_scores[key] = {'score': auto_score, 'maxScore': max_score, 'auto': True, 'feedback': feedback}
        elif prior:
            item_scores[key] = {**prior, 'feedback': feedback}
        else:
            item_scores[key] = {'score': 0.0, 'maxScore': item.points, 'auto': False, 'feedback': feedback}
        total += item_scores[key]['score']
        max_total += item.points

    submission.item_scores = item_scores
    submission.total_score = total
    submission.max_score = max_total
    submission.graded_at = timezone.now()
    submission.save()
    log_activity(request.user.get_full_name() or request.user.username, request.user.email,
        'graded', f'Graded {recipient.pupil_name} on "{recipient.assignment.title}" ({total:g}/{max_total:g}).',
        assignment=recipient.assignment)
    return JsonResponse({'itemScores': item_scores, 'totalScore': total, 'maxScore': max_total,
        'gradedAt': submission.graded_at.isoformat(), 'published': submission.published})


@login_required
@require_POST
def publish_submission_api(request, recipient_id):
    """Shows (or hides again) one pupil's grade to them. A submission can be fully graded
    without the pupil seeing anything — publishing is a separate, explicit action so a
    grade can be double-checked first."""
    denied = _require_teacher(request)
    if denied:
        return denied
    recipient = get_object_or_404(Recipient, id=recipient_id)
    submission = get_object_or_404(Submission, recipient=recipient)
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        payload = {}
    publish = payload.get('publish', True)
    if publish and submission.graded_at is None:
        return HttpResponseBadRequest('Grade this submission before releasing it to the pupil.')
    submission.published = bool(publish)
    submission.save()
    if publish:
        log_activity(request.user.get_full_name() or request.user.username, request.user.email,
            'published', f"Published {recipient.pupil_name}'s grade for \"{recipient.assignment.title}\".",
            assignment=recipient.assignment)
    return JsonResponse({'published': submission.published})


@login_required
@require_POST
def publish_assignment_grades_api(request, assignment_id):
    """Bulk-publishes every graded-but-not-yet-published submission for an assignment, for
    the common "grade everyone, then release all at once" workflow."""
    denied = _require_teacher(request)
    if denied:
        return denied
    assignment = get_object_or_404(Assignment, id=assignment_id)
    submissions = Submission.objects.filter(recipient__assignment=assignment, graded_at__isnull=False, published=False)
    count = submissions.update(published=True)
    if count:
        log_activity(request.user.get_full_name() or request.user.username, request.user.email,
            'published', f'Published grades for {count} pupil(s) on "{assignment.title}".',
            assignment=assignment)
    return JsonResponse({'published': count})


# ---------------- one-click SEB launch that keeps the pupil signed in ----------------
# SEB is a separate browser with its own cookies, so a pupil signed in to ExamHub in Chrome
# would otherwise land on the login page inside SEB and then have to find the paper again.
# Instead, clicking Open on a lockdown paper asks for a short-lived, single-use launch token:
# SEB is handed a config whose start page (/seb/enter/<token>/) signs that pupil in and opens
# that paper directly. The token is signed (can't be forged), expires after a few minutes, is
# only accepted from SEB running that exact config, and stops working the moment it's used
# (it's bound to the account's last_login, which signing in updates).
SEB_LAUNCH_SALT = 'examhub-seb-launch'
SEB_LAUNCH_MAX_AGE = 300  # seconds


def _launch_stamp(user):
    return user.last_login.isoformat() if user.last_login else ''


def make_seb_launch_token(user, assignment_id):
    return signing.dumps({'u': user.pk, 'a': assignment_id, 'l': _launch_stamp(user)}, salt=SEB_LAUNCH_SALT)


def read_seb_launch_token(token, require_unused=True):
    """Returns (user, assignment_id) for a valid token, else (None, None)."""
    try:
        data = signing.loads(token, salt=SEB_LAUNCH_SALT, max_age=SEB_LAUNCH_MAX_AGE)
    except signing.BadSignature:  # includes SignatureExpired
        return None, None
    user = User.objects.filter(pk=data.get('u'), is_active=True).first()
    if not user or (require_unused and _launch_stamp(user) != data.get('l')):
        return None, None
    return user, data.get('a')


@login_required
@require_POST
def seb_launch_api(request):
    try:
        assignment_id = int(json.loads(request.body or '{}').get('assignmentId'))
    except (TypeError, ValueError, json.JSONDecodeError):
        return HttpResponseBadRequest('Missing assignment.')
    assignment = get_object_or_404(Assignment, id=assignment_id)
    if is_real_pupil(request) and not assignment.recipients.filter(pupil_email__iexact=request.user.email).exists():
        return HttpResponseForbidden('This work is not assigned to your account.')
    token = make_seb_launch_token(request.user, assignment.id)
    scheme = 'sebs' if _public_scheme(request) == 'https' else 'seb'
    return JsonResponse({'sebUrl': f"{scheme}://{request.get_host()}{reverse('seb-launch-config', args=[token])}"})


@require_GET
def seb_launch_config(request, token):
    """The per-launch .seb file. Deliberately NOT login_required: SEB downloads its config
    with its own HTTP client, which has none of the browser's cookies — the signed token is
    what authorises it."""
    user, _ = read_seb_launch_token(token)
    if not user:
        return HttpResponseForbidden('This exam link has expired. Go back to ExamHub and click Open again.')
    settings_dict = seb_config_settings(request.get_host(), _public_scheme(request), reverse('seb-enter', args=[token]))
    response = HttpResponse(plistlib.dumps(settings_dict, fmt=plistlib.FMT_XML), content_type='application/seb')
    response['Content-Disposition'] = 'attachment; filename="examhub-exam.seb"'
    return response


@require_GET
def seb_enter(request, token):
    """SEB's start page for a one-time launch: sign the pupil in and open their paper."""
    open_url = lambda aid: f"{reverse('home')}?open={aid}"
    # SEB reloading this page after it already signed the pupil in: just carry on.
    same_user, aid = read_seb_launch_token(token, require_unused=False)
    if (same_user and request.user.is_authenticated and request.user.pk == same_user.pk
            and request.session.get('seb_config_keys')):
        return redirect(open_url(aid))
    user, aid = read_seb_launch_token(token)
    if not user:
        return HttpResponse('<p style="font-family:sans-serif;padding:24px">This exam link has expired or was already used. '
                            f'<a href="{reverse("login")}">Sign in</a> and open the paper again.</p>', status=403)
    host = request.get_host()
    start_path = reverse('seb-enter', args=[token])
    token_keys = [seb_config_key(host, s, start_path) for s in ('https', 'http')]
    if not seb_lockdown_ok(request, extra_keys=token_keys):
        return HttpResponseForbidden('This link only works inside Safe Exam Browser.')
    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    request.session['seb_config_keys'] = token_keys
    return redirect(open_url(aid))


@login_required
@require_GET
def seb_check_api(request):
    """Lets the pupil page ask "is this request really SEB with ExamHub's config?" before
    opening a lockdown paper — the same check start/save/submit enforce, just without side
    effects. Replaces trusting the browser's User-Agent, which some SEB setups don't mark."""
    has_headers = bool(request.META.get('HTTP_X_SAFEEXAMBROWSER_REQUESTHASH')
                       or request.META.get('HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH'))
    return JsonResponse({'ok': seb_lockdown_ok(request), 'sebHeaders': has_headers})


@login_required
@require_GET
def seb_config(request):
    """Generates ExamHub's single, shared Safe Exam Browser lockdown config as a real
    .seb file (Apple-plist format, which is what SEB expects). One config covers every
    "lockdown" assignment site-wide — startURL is just the app's own login/home page, since
    ExamHub is a single-page app where pupils pick their assignment from their dashboard the
    same way whether they're in a normal browser or in SEB. This is deliberately a minimal,
    conservative set of settings covering only what's needed to make the Browser Exam Key
    mechanism work (see seb_lockdown_ok above); open the downloaded file in SEB's own
    Configuration Tool to add further lockdown restrictions (blocking task-switching,
    clipboard, etc.) per your school's policy — hand-guessing SEB's ~200 other settings here
    risks producing a config that's subtly wrong in ways nobody can check without SEB itself.
    """
    settings_dict = seb_config_settings(request.get_host(), _public_scheme(request))
    body = plistlib.dumps(settings_dict, fmt=plistlib.FMT_XML)
    response = HttpResponse(body, content_type='application/seb')
    response['Content-Disposition'] = 'attachment; filename="examhub-lockdown.seb"'
    return response
