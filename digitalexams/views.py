import datetime
import hashlib
import hmac
import json
import logging
import mimetypes
import plistlib
import secrets
import string
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from django.core import signing
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.db.models import Count, F, Max, Prefetch, Q
from django.utils import timezone
from django.utils.text import slugify
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import email_login

logger = logging.getLogger(__name__)
from .models import (ActivityLogEntry, AllowedBrowserExamKey, Assignment, AssignmentItem, ExamResource, Message,
    Recipient, School, SchoolClass, Subject, Submission, User)


@login_required
def home(request):
    # Shown to teachers in the SEB menu, so a missing one-time setup is obvious up front
    # instead of only surfacing when a pupil gets blocked mid-exam.
    seb_keys = AllowedBrowserExamKey.objects.all()
    if getattr(request, 'school', None):
        seb_keys = seb_keys.filter(school=request.school)
    return render(request, 'examhub.html', {'seb_key_count': seb_keys.count(), 'seb_config_key': seb_config_key(request.get_host(), _public_scheme(request)),
                                            'can_skip_current_password': email_login.can_skip_current_password(request),
                                            **tour_context(request.user)})


def tour_context(user):
    """Template context for the first-login guided tour (templates/_tour.html)."""
    return {'tour_role': user.tour_role, 'show_tour': user.tour_completed_at is None}


@login_required
@require_POST
def tour_done_api(request):
    """Marks the guided tour as finished/skipped so it doesn't open again on its own."""
    if request.user.tour_completed_at is None:
        request.user.tour_completed_at = timezone.now()
        request.user.save(update_fields=['tour_completed_at'])
    return JsonResponse({'done': True})


def _teacher_page(request, template):
    """Teacher-only standalone pages (Classes, Activity, Teachers). Pupils get bounced to the dashboard."""
    if request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        prefix = getattr(request, 'school_url_prefix', '') or ''
        return redirect(f'{prefix}/' if prefix else 'home')
    return render(request, template, {'can_manage_classes': request.user.can_manage_classes,
                                      'is_main_admin': request.user.is_superuser,
                                      **tour_context(request.user)})


@login_required
def roles_page(request):
    """Main-admin or school-admin: set each teacher account to Admin or Teacher, and add
    new teacher/admin accounts."""
    if not (request.user.is_superuser or request.user.is_school_admin):
        prefix = getattr(request, 'school_url_prefix', '') or ''
        return redirect(f'{prefix}/classes/' if request.user.role == User.Role.TEACHER else f'{prefix}/')
    return _teacher_page(request, 'teachers.html')


@login_required
def teachers_page(request):
    """Old /teachers/ address — now the Roles page."""
    prefix = getattr(request, 'school_url_prefix', '') or ''
    return redirect(f'{prefix}/roles/' if prefix else 'roles-page')


@login_required
def classes_page(request):
    # Plain teachers work by subject only; the Year groups page is for admins.
    if request.user.role == User.Role.TEACHER and not request.user.can_manage_classes:
        prefix = getattr(request, 'school_url_prefix', '') or ''
        return redirect(f'{prefix}/subjects/')
    return _teacher_page(request, 'classes.html')


@login_required
def activity_page(request):
    if not request.user.can_view_activity:
        prefix = getattr(request, 'school_url_prefix', '') or ''
        return redirect(f'{prefix}/' if prefix else 'home')
    return _teacher_page(request, 'activity.html')


def account_kind(user):
    """'teacher' or 'pupil' for the login switch. Admins (superusers) sign in as teachers."""
    if user.is_superuser or user.role == User.Role.TEACHER:
        return 'teacher'
    return 'pupil'



def _school_home_url(user):
    """Return the correct home URL for a logged-in user, scoped to their school.
    Falls back to /superadmin/ for platform superusers with no school assigned."""
    if user.school:
        return f'/s/{user.school.slug}/'
    if user.is_superuser:
        return '/superadmin/'
    return '/admin/'



def _redirect_after_login(request, user, set_password=False):
    next_url = request.POST.get('next') or request.GET.get('next')
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()},
                                                    require_https=request.is_secure()):
        return redirect(next_url)
    home_url = _school_home_url(user)
    if set_password and home_url.startswith('/s/'):
        home_url += '?set_password=1'  # opens the change-password dialog straight away
    return redirect(home_url)


def login_view(request):
    """Two ways in, both always available: username + password, or a one-time code emailed
    to the account (see email_login.py). The pupil/teacher switch never grants a role; it
    only has to match the account's real one."""
    if request.user.is_authenticated:
        return redirect(_school_home_url(request.user))
    login_as = request.POST.get('login_as') or request.GET.get('as') or 'pupil'
    if login_as not in ('teacher', 'pupil'):
        login_as = 'pupil'
    action = request.POST.get('action') or 'password'
    method = 'code' if action in ('send_code', 'verify_code', 'resend_code') else 'password'
    if request.method == 'GET' and request.GET.get('method') == 'code':
        method = 'code'
    reset = (request.POST.get('reset') or request.GET.get('reset')) == '1'
    school = getattr(request, 'school', None)
    ctx = {'login_as': login_as, 'method': method, 'reset': reset, 'code_step': 'request',
           'code_error': '', 'code_notice': '', 'identifier': request.POST.get('identifier', '').strip(),
           'next': request.POST.get('next') or request.GET.get('next') or ''}

    form = AuthenticationForm(request, data=request.POST if (request.method == 'POST' and action == 'password') else None)
    ctx['form'] = form

    if request.method == 'POST' and action == 'password' and form.is_valid():
        user = form.get_user()
        kind = account_kind(user)
        if kind != login_as:
            form.add_error(None, f'This is a {kind} account. Choose "I\'m a {kind}" above, then sign in again.')
        else:
            login(request, user)
            return _redirect_after_login(request, user)

    elif request.method == 'POST' and action in ('send_code', 'resend_code'):
        if action == 'resend_code':
            pending_id = request.session.get(email_login.SESSION_PENDING_USER)
            user = User.objects.filter(pk=pending_id, is_active=True).first() if pending_id else None
        else:
            user = email_login.find_user(ctx['identifier'], school=school)
        if action == 'send_code' and not ctx['identifier']:
            ctx['code_error'] = 'Enter your username or school email.'
        else:
            result = email_login.issue_code(user) if user else 'sent'  # same reply either way
            if result == 'failed':
                ctx['code_error'] = "We couldn't send the email just now. Try again, or sign in with your password."
            else:
                request.session[email_login.SESSION_PENDING_USER] = user.pk if user else None
                ctx['code_step'] = 'verify'
                ctx['code_notice'] = ('A code was sent a moment ago. Check your inbox (and spam), or wait a minute to resend.'
                                      if result == 'cooldown' else
                                      'If that account exists, we\'ve emailed it a 6-digit code. It expires in 10 minutes.')

    elif request.method == 'POST' and action == 'verify_code':
        ctx['code_step'] = 'verify'
        pending_id = request.session.get(email_login.SESSION_PENDING_USER)
        user = User.objects.filter(pk=pending_id, is_active=True).first() if pending_id else None
        if user is None or not email_login.verify_code(user, request.POST.get('code')):
            ctx['code_error'] = 'That code is incorrect or has expired. Check the latest email, or request a new code.'
        elif account_kind(user) != login_as:
            kind = account_kind(user)
            ctx['code_error'] = f'This is a {kind} account. Choose "I\'m a {kind}" above, then request a new code.'
            ctx['code_step'] = 'request'
        else:
            request.session.pop(email_login.SESSION_PENDING_USER, None)
            login(request, user)
            email_login.mark_otp_login(request)
            return _redirect_after_login(request, user, set_password=reset)

    return render(request, 'login.html', ctx)


@require_POST
def logout_view(request):
    logout(request)
    return redirect('login')


@login_required
@require_POST
def change_password_api(request):
    """Self-service password change for a signed-in pupil or teacher. Normally needs the
    current password; for a short while after signing in with an emailed code it doesn't,
    which is how someone who forgot their password resets it without an admin."""
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid request.')
    current_password = payload.get('currentPassword') or ''
    new_password = payload.get('newPassword') or ''
    skip_current = email_login.can_skip_current_password(request)
    if not new_password or (not current_password and not skip_current):
        return HttpResponseBadRequest('Enter your current password and choose a new one.')
    if not skip_current and not request.user.check_password(current_password):
        return HttpResponseForbidden('Your current password is incorrect.')
    try:
        validate_password(new_password, user=request.user)
    except DjangoValidationError as exc:
        return HttpResponseBadRequest(' '.join(exc.messages))
    request.user.set_password(new_password)
    if request.user.must_change_password:
        request.user.must_change_password = False
        request.user.activated_at = timezone.now()
    request.user.save()
    update_session_auth_hash(request, request.user)  # keeps this session signed in
    request.session.pop(email_login.SESSION_OTP_LOGIN_AT, None)  # the no-old-password window is one use
    return JsonResponse({'changed': True})


def log_activity(actor_name, actor_email, action, description, assignment=None, school_class=None):
    """Best-effort audit-trail write for the teacher-facing activity log. Never lets a
    logging failure break the request that triggered it — this is a nice-to-have, not a
    system of record."""
    try:
        ActivityLogEntry.objects.create(actor_name=actor_name or '', actor_email=actor_email or '',
            action=action, description=description, assignment=assignment, school_class=school_class)
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


def _accepted_seb_keys(request, extra_keys=()):
    """Every key a valid SEB hash may be built from: ExamHub's own config's Config Key (for both
    schemes, since the file may have been downloaded over http or https), anything an admin added
    by hand, the per-launch keys remembered on this SEB session, and any extra keys given."""
    host = request.get_host()
    keys = {seb_config_key(host, 'https'), seb_config_key(host, 'http')}
    seb_keys_qs = AllowedBrowserExamKey.objects.all()
    if getattr(request, 'school', None):
        seb_keys_qs = seb_keys_qs.filter(school=request.school)
    keys.update((k or '').strip().lower() for k in seb_keys_qs.values_list('key_hash', flat=True))
    keys.update(extra_keys)
    if hasattr(request, 'session'):
        keys.update(request.session.get('seb_config_keys', []))
    keys.discard('')
    return keys


def _seb_request_paths(request):
    """The path + query exactly as the pupil's browser asked for it. The school middleware strips
    `/s/<slug>` from request.path before the view runs, but SEB hashes the URL it actually
    requested — so the prefix has to go back on, or no school page ever matches."""
    full = request.get_full_path()
    prefix = getattr(request, 'school_url_prefix', '') or ''
    return [prefix + full if prefix and not full.startswith(prefix + '/') else full]


def _hash_matches(url, keys, received):
    for key in keys:
        expected = hashlib.sha256((url + key).encode('utf-8')).hexdigest()
        if any(hmac.compare_digest(expected, r) for r in received):
            return True
    return False


def seb_lockdown_ok(request, extra_keys=()):
    """True if this request carries a valid Browser Exam Key / Config Key hash, or this SEB
    session already proved itself through SEB's JavaScript API (see seb_js_keys_ok). There's
    deliberately no soft fallback (User-Agent, etc.) here — those are shown client-side as a
    friendly hint, but this is the actual enforcement."""
    # SEB sends two hashes: one built from the Browser Exam Key (changes with the SEB version
    # installed) and one from the Config Key (depends only on the .seb file, so it's the same
    # on every pupil's device). Either one matching a stored key is accepted, so an admin can
    # paste whichever key SEB shows them.
    received = [h.strip().lower() for h in (
        request.META.get('HTTP_X_SAFEEXAMBROWSER_REQUESTHASH', ''),
        request.META.get('HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH', ''),
    ) if h and h.strip()]
    if received:
        # SEB hashes the URL exactly as the pupil's browser sees it. Behind Railway's proxy Django
        # sees the request as plain http:// even though the pupil is on https://, so both schemes
        # are tried — the stored key is still required either way.
        host = request.get_host()
        keys = _accepted_seb_keys(request, extra_keys)
        for path in _seb_request_paths(request):
            for scheme in ('https', 'http'):
                if _hash_matches(f'{scheme}://{host}{path}', keys, received):
                    return True
    # Newer SEB versions (notably macOS / iPadOS) don't add the headers to the page's own
    # background requests; they expose the same hashes to JavaScript instead, which the exam
    # page verifies once per SEB session via /api/seb/verify/.
    return bool(hasattr(request, 'session') and request.session.get('seb_js_verified'))


def seb_js_keys_ok(request, urls, values, extra_keys=()):
    """Checks the values from SEB's JavaScript API (SafeExamBrowser.security.configKey /
    .browserExamKey), which are SHA256(page URL + key) — the same as the request headers, just
    for the page's own URL. `urls` are the page addresses to try (current and as first loaded);
    only addresses on this site count."""
    received = [str(v).strip().lower() for v in values if v and str(v).strip()]
    if not received:
        return False
    host = request.get_host()
    keys = _accepted_seb_keys(request, extra_keys)
    for url in urls:
        url = str(url or '').split('#', 1)[0]
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or parts.netloc != host:
            continue
        if _hash_matches(url, keys, received):
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


def _needs_seb(request, assignment):
    """Lockdown files are only served inside Safe Exam Browser — until the pupil has
    submitted. After that they're just reviewing their own work, so a normal browser is fine."""
    if assignment.delivery_mode != Assignment.DeliveryMode.LOCKDOWN or not is_real_pupil(request):
        return False
    if Submission.objects.filter(recipient__assignment=assignment, status='submitted',
                                 recipient__pupil_email__iexact=request.user.email).exists():
        return False
    return not seb_lockdown_ok(request)


def _seb_ok_once(request):
    """seb_lockdown_ok(), worked out once per request (the assignments list asks per exam)."""
    if not hasattr(request, '_examhub_seb_ok'):
        request._examhub_seb_ok = seb_lockdown_ok(request)
    return request._examhub_seb_ok


def pupil_content_withheld(request, assignment, submission):
    """Should this paper's questions and files be held back from the pupil right now?
    Yes for a real pupil who hasn't handed in yet, while
      * a join-code paper hasn't been started with the right code, or
      * a Lockdown paper is being looked at outside Safe Exam Browser.
    Before, those two gates only guarded the Open button: typing /s/<school>/api/assignments/ or a
    file address into an ordinary browser showed the questions ahead of time."""
    if not is_real_pupil(request) or (submission is not None and submission.status == 'submitted'):
        return False
    if assignment.require_join_code and not (submission is not None and submission.started_at):
        return True
    return assignment.delivery_mode == Assignment.DeliveryMode.LOCKDOWN and not _seb_ok_once(request)


def _pupil_join_code_pending(request, assignment):
    """A real pupil asking for a join-code paper's files before entering the code."""
    if not is_real_pupil(request) or not assignment.require_join_code:
        return False
    sub = Submission.objects.filter(recipient__assignment=assignment,
                                    recipient__pupil_email__iexact=request.user.email).first()
    return not (sub and (sub.started_at or sub.status == 'submitted'))


def join_code_required_response():
    return JsonResponse({'code': 'join_code_required',
                         'detail': 'Enter the join code your teacher gives you to open this paper.'}, status=403)


PUBLIC_MEDIA_PREFIXES = ('school_logos/',)


@require_GET
def public_media(request, path):
    """/media/<path> — only school logos (shown on the sign-in page, before anyone signs in).
    Everything else under /media/ is a 404, so an exam paper can't be fetched by guessing its
    file name; it has to come through item_pdf / resource_file and their checks."""
    import posixpath
    from django.core.files.storage import default_storage
    name = posixpath.normpath(path).lstrip('/')
    if name.startswith('..') or not name.startswith(PUBLIC_MEDIA_PREFIXES):
        raise Http404
    content_type = mimetypes.guess_type(name)[0] or ''
    if not content_type.startswith('image/') or content_type == 'image/svg+xml':
        raise Http404
    try:
        handle = default_storage.open(name, 'rb')
    except (FileNotFoundError, OSError, ValueError):
        raise Http404
    return FileResponse(handle, content_type=content_type)


def _pupil_cannot_see_yet(request, assignment):
    """A pre-created exam's files stay private until pupils are allowed to see the exam."""
    if not is_real_pupil(request):
        return False
    recipient = assignment.recipients.filter(pupil_email__iexact=request.user.email).first()
    return recipient is None or not exam_visible_to_pupils(assignment, recipient)


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


# ---------------- exam window: visible from / closes at ----------------
# An exam is visible to pupils once it isn't hidden and its opening time has passed (a pupil's
# early-access override can bring that forward). It closes at its closing time (due_at) — plus a
# pupil's extra minutes — or the moment a teacher presses "Close now", whichever comes first.
# There's no separate duration: the countdown always runs to the closing time.
# CLOSE_GRACE lets the final auto-submit that each pupil's page sends at closing time still land
# (network lag, a teacher's close noticed on the next poll); after it, any unsubmitted attempt is
# submitted automatically with whatever was last autosaved.
CLOSE_GRACE = datetime.timedelta(seconds=90)


def exam_visible_to_pupils(assignment, recipient=None, now=None):
    if assignment.is_hidden:
        return False
    now = now or timezone.now()
    opens = assignment.opens_at
    if recipient is not None and recipient.opens_override and (opens is None or recipient.opens_override < opens):
        opens = recipient.opens_override
    return opens is None or opens <= now


def exam_closes_at(assignment, recipient=None):
    """When this exam stops accepting work (for this pupil), or None if it's open-ended."""
    times = []
    if assignment.closed_at:
        times.append(assignment.closed_at)
    if assignment.due_at:
        extra = datetime.timedelta(minutes=recipient.extra_minutes) if recipient is not None else datetime.timedelta()
        times.append(assignment.due_at + extra)
    return min(times) if times else None


def assignment_closed(assignment, recipient=None, grace=False):
    closes = exam_closes_at(assignment, recipient)
    if closes is None:
        return False
    return timezone.now() > closes + (CLOSE_GRACE if grace else datetime.timedelta())


def exam_state(assignment, now=None):
    """'hidden' | 'scheduled' (visible later) | 'live' | 'closed' — for the teacher's views."""
    now = now or timezone.now()
    if assignment.closed_at or (assignment.due_at and now > assignment.due_at):
        return 'closed'
    if assignment.is_hidden:
        return 'hidden'
    if assignment.opens_at and assignment.opens_at > now:
        return 'scheduled'
    return 'live'


def finalize_closed_attempts(assignments):
    """Hand in every attempt that's still open on an exam that has closed (after the grace
    period), using the pupil's last autosave. Cheap no-op for exams that are still running."""
    now = timezone.now()
    for assignment in assignments:
        if not assignment.closed_at and not (assignment.due_at and now > assignment.due_at + CLOSE_GRACE):
            continue
        open_subs = Submission.objects.filter(recipient__assignment=assignment, status='draft').select_related('recipient')
        for sub in open_subs:
            closes = exam_closes_at(assignment, sub.recipient)
            if closes and now > closes + CLOSE_GRACE:
                sub.status = 'submitted'
                sub.submitted_at = closes
                sub.locked_at = None
                sub.save(update_fields=['status', 'submitted_at', 'locked_at', 'updated_at'])
                log_activity(sub.recipient.pupil_name, sub.recipient.pupil_email, 'submitted',
                    f'{sub.recipient.pupil_name}\'s work on "{assignment.title}" was handed in automatically when the exam closed.',
                    assignment=assignment)


def assignment_closed_response():
    return JsonResponse({
        'code': 'assignment_closed',
        'detail': 'This exam has closed and no longer accepts new work.',
    }, status=403)


def exam_not_open_response():
    return JsonResponse({'code': 'exam_not_open', 'detail': "This exam isn't open yet."}, status=403)


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


def can_manage_exam(user, assignment):
    """Admins (and the main admin) see and manage every exam in the school; a plain teacher only
    the exams they created (exams are linked to their teacher by email)."""
    if user.is_superuser or user.can_manage_classes:
        return True
    return bool(user.email) and (assignment.teacher_email or '').strip().lower() == user.email.strip().lower()


def not_your_exam_response():
    return HttpResponseForbidden("This exam belongs to another teacher.")


def visible_exams_for(user, qs):
    """Filter an Assignment queryset down to what this teacher may see."""
    if user.is_superuser or user.can_manage_classes:
        return qs
    return qs.filter(teacher_email__iexact=user.email) if user.email else qs.none()


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


def serialize_assignment(assignment, pupil_email=None, request=None):
    """`request` is passed for a pupil's own list so a Lockdown / join-code paper's questions can
    be held back until SEB / the join code has been checked (see pupil_content_withheld)."""
    is_teacher_view = pupil_email is None
    recipients = []
    own_sub = None
    for recipient in _recipients_of(assignment):
        sub = _submission_of(recipient)
        if not is_teacher_view and recipient.pupil_email.lower() == (pupil_email or '').lower():
            own_sub = sub
        # A pupil only ever sees scores once the teacher has explicitly published them —
        # being graded is not the same as being told.
        show_grades = is_teacher_view or sub.published
        recipients.append({'id': recipient.id, 'name': recipient.pupil_name, 'email': recipient.pupil_email,
            'status': sub.status, 'submittedAt': sub.submitted_at.isoformat() if sub.submitted_at else None,
            'startedAt': sub.started_at.isoformat() if sub.started_at else None,
            'annotations': sub.annotations, 'outlineHtml': sub.outline_html, 'responses': sub.responses,
            'extraMinutes': recipient.extra_minutes,
            'opensOverride': manila_isoformat(recipient.opens_override),
            'closesAt': manila_isoformat(exam_closes_at(assignment, recipient)),
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
            'published': sub.published,
            # Teacher's marking on the work — the pupil only gets it with the released grade.
            'teacherMarks': (sub.teacher_marks or {}) if show_grades else {},
            'newSubmission': sub.is_new_for_teacher if is_teacher_view else False,
            'newGrade': sub.is_new_grade_for_pupil if not is_teacher_view else False})
    withheld = (request is not None and not is_teacher_view
                and pupil_content_withheld(request, assignment, own_sub))
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
            data['prompt'] = '' if withheld else item.prompt
            data['options'] = [] if withheld else item.options
        if is_teacher_view:
            # Never sent to pupils — same "gate" reasoning as the join code below.
            data['correctAnswer'] = item.correct_answer
        return data
    items = [serialize_item(item) for item in assignment.items.all()]
    return {'id': assignment.id, 'title': assignment.title, 'prompt': assignment.instructions,
        'createdAt': manila_isoformat(assignment.created_at),
        'items': items,
        'resources': [dict(serialize_resource(r), text='') if (withheld and r.kind == ExamResource.Kind.TEXT)
                      else serialize_resource(r) for r in assignment.resources.all()],
        # True while the questions above are held back; the page fetches the list again once
        # Safe Exam Browser / the join code has been checked.
        'contentWithheld': withheld,
        'teacherName': assignment.teacher_name, 'teacherEmail': assignment.teacher_email,
        'opensAt': manila_isoformat(assignment.opens_at), 'dueAt': manila_isoformat(assignment.due_at),
        'isHidden': assignment.is_hidden,
        'closedAt': manila_isoformat(assignment.closed_at),
        'state': exam_state(assignment),
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
    qs = Assignment.objects.prefetch_related(Prefetch('recipients', queryset=annotated_recipients()), 'items', 'resources').all()
    if getattr(request, 'school', None):
        qs = qs.filter(school=request.school)
    if request.user.role == User.Role.PUPIL and not request.user.is_superuser:
        email = request.user.email
    if email:
        qs = qs.filter(recipients__pupil_email__iexact=email).distinct()
    if not is_real_pupil(request):
        qs = visible_exams_for(request.user, qs)   # teachers: only their own exams (admins: all)
    assignments = list(qs)
    finalize_closed_attempts(assignments)
    if is_real_pupil(request):
        # Pre-created exams stay invisible to pupils until they're made visible / their time comes.
        assignments = [a for a in assignments
                       if exam_visible_to_pupils(a, a.recipients.filter(pupil_email__iexact=email).first())]
    return JsonResponse({'assignments': [serialize_assignment(a, email, request if is_real_pupil(request) else None)
                                         for a in assignments]})


@login_required
@require_GET
def item_pdf(request, item_id):
    """Serve a single PDF-type item's file inline so PDF.js can render it in the workspace."""
    item = get_object_or_404(AssignmentItem, id=item_id, item_type=AssignmentItem.ItemType.PDF)
    assignment = item.assignment
    if getattr(request, 'school', None) and assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not is_real_pupil(request) and not can_manage_exam(request.user, assignment):
        return not_your_exam_response()
    if request.user.role == User.Role.PUPIL and not request.user.is_superuser:
        if not assignment.recipients.filter(pupil_email__iexact=request.user.email).exists():
            return HttpResponseForbidden('This PDF is not assigned to your account.')
    if _pupil_cannot_see_yet(request, assignment):
        return exam_not_open_response()
    if _pupil_join_code_pending(request, assignment):
        return join_code_required_response()
    if _needs_seb(request, assignment):
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
    if getattr(request, 'school', None) and assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not is_real_pupil(request) and not can_manage_exam(request.user, assignment):
        return not_your_exam_response()
    if request.user.role == User.Role.PUPIL and not request.user.is_superuser:
        if not assignment.recipients.filter(pupil_email__iexact=request.user.email).exists():
            return HttpResponseForbidden('This file is not assigned to your account.')
    if _pupil_cannot_see_yet(request, assignment):
        return exam_not_open_response()
    if _pupil_join_code_pending(request, assignment):
        return join_code_required_response()
    if _needs_seb(request, assignment):
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


# ---------------- exam resources (reference material in the pupil's floating "Resources" panel) ----------------
RESOURCE_TEXT_MAX = 20000


def serialize_resource(resource):
    data = {'id': resource.id, 'kind': resource.kind, 'title': resource.title}
    if resource.kind == ExamResource.Kind.TEXT:
        data['text'] = resource.text
    else:
        data['url'] = reverse('resource-file', args=[resource.id])
    return data


def prepare_resource(kind, title, text, upload, position):
    """Validate one resource. Returns (fields_dict, None) or (None, error_message)."""
    if kind not in ExamResource.Kind.values:
        return None, f'Resource {position}: choose PDF, image or text.'
    title = (title or '').strip()[:120] or {'pdf': 'PDF', 'image': 'Image', 'text': 'Note'}[kind] + f' {position}'
    if kind == ExamResource.Kind.TEXT:
        text = (text or '').strip()
        if not text:
            return None, f'"{title}" has no text yet.'
        if len(text) > RESOURCE_TEXT_MAX:
            return None, f'"{title}" is too long (keep it under {RESOURCE_TEXT_MAX:,} characters).'
        return {'kind': kind, 'title': title, 'text': text}, None
    if not upload:
        return None, f'"{title}" is missing its file.'
    content_type = upload.content_type or ''
    if kind == ExamResource.Kind.PDF and content_type not in ('application/pdf', 'application/x-pdf'):
        return None, f'"{title}": please upload a PDF file.'
    if kind == ExamResource.Kind.IMAGE and not content_type.startswith('image/'):
        return None, f'"{title}": please upload an image file.'
    return {'kind': kind, 'title': title, 'file': upload}, None


def _resource_for_manager(request, resource_id=None, assignment_id=None):
    """(object, None) when the user may manage this exam's resources, else (None, error_response)."""
    if request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        return None, HttpResponseForbidden('Only teachers can change exam resources.')
    obj = (get_object_or_404(ExamResource.objects.select_related('assignment'), id=resource_id) if resource_id
           else get_object_or_404(Assignment, id=assignment_id))
    assignment = obj.assignment if resource_id else obj
    if getattr(request, 'school', None) and assignment.school_id != request.school.id:
        return None, HttpResponseForbidden('Forbidden')
    if not can_manage_exam(request.user, assignment):
        return None, not_your_exam_response()
    return obj, None


@login_required
@require_POST
def add_resource_api(request, assignment_id):
    """POST (multipart) kind=pdf|image|text, title, text, file — add one resource to an exam."""
    assignment, error = _resource_for_manager(request, assignment_id=assignment_id)
    if error:
        return error
    order = (assignment.resources.aggregate(m=Max('order'))['m'] or 0) + 1
    fields, problem = prepare_resource(request.POST.get('kind'), request.POST.get('title'),
                                       request.POST.get('text'), request.FILES.get('file'), assignment.resources.count() + 1)
    if problem:
        return HttpResponseBadRequest(problem)
    resource = ExamResource.objects.create(assignment=assignment, order=order, **fields)
    log_activity(request.user.get_full_name() or request.user.username, request.user.email, 'updated',
                 f'Added the resource "{resource.title}" to "{assignment.title}".', assignment=assignment)
    return JsonResponse(serialize_resource(resource), status=201)


@login_required
@require_POST
def delete_resource_api(request, resource_id):
    resource, error = _resource_for_manager(request, resource_id=resource_id)
    if error:
        return error
    assignment, title = resource.assignment, resource.title
    if resource.file:
        try:
            resource.file.delete(save=False)
        except Exception:
            logger.exception('Could not delete the file of resource %s', resource.pk)
    resource.delete()
    log_activity(request.user.get_full_name() or request.user.username, request.user.email, 'updated',
                 f'Removed the resource "{title}" from "{assignment.title}".', assignment=assignment)
    return JsonResponse({'deleted': True})


@login_required
@require_GET
def resource_file(request, resource_id):
    """Serve a PDF/image resource inline, with the same access rules as the exam's own files."""
    resource = get_object_or_404(ExamResource.objects.select_related('assignment'), id=resource_id)
    assignment = resource.assignment
    if not resource.file:
        raise Http404
    if getattr(request, 'school', None) and assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not is_real_pupil(request) and not can_manage_exam(request.user, assignment):
        return not_your_exam_response()
    if request.user.role == User.Role.PUPIL and not request.user.is_superuser:
        if not assignment.recipients.filter(pupil_email__iexact=request.user.email).exists():
            return HttpResponseForbidden('This file is not assigned to your account.')
    if _pupil_cannot_see_yet(request, assignment):
        return exam_not_open_response()
    if _pupil_join_code_pending(request, assignment):
        return join_code_required_response()
    if _needs_seb(request, assignment):
        return seb_required_response(request)
    filename = resource.file.name.rsplit('/', 1)[-1]
    content_type = ('application/pdf' if resource.kind == ExamResource.Kind.PDF
                    else mimetypes.guess_type(filename)[0] or 'application/octet-stream')
    try:
        handle = resource.file.open('rb')
    except (FileNotFoundError, ValueError):
        return missing_file_response()
    response = FileResponse(handle, content_type=content_type, as_attachment=False, filename=filename)
    response['Content-Disposition'] = f'inline; filename="{filename}"'
    return response


@login_required
@require_GET
def assignment_status_api(request, assignment_id):
    """Lightweight polling endpoint for the Monitor tab — just statuses, not the full
    assignment payload (annotations, responses, etc.), so a teacher can leave this tab open
    and poll it every few seconds without re-downloading everything each time."""
    assignment = get_object_or_404(Assignment, id=assignment_id)
    if getattr(request, 'school', None) and assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not is_real_pupil(request) and not can_manage_exam(request.user, assignment):
        return not_your_exam_response()
    if request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        return HttpResponseForbidden('Only teachers can view exam status.')
    finalize_closed_attempts([assignment])
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
            'closesAt': manila_isoformat(exam_closes_at(assignment, recipient)),
            'hasUnreadFromPupil': _unread_since(recipient.last_pupil_msg, recipient.teacher_last_read_at),
            'newSubmission': sub.is_new_for_teacher,
            'locked': bool(sub.locked_at), 'lockReason': sub.lock_reason, 'lockCount': sub.lock_count,
            'resumeCode': sub.resume_code if sub.locked_at else None,
        })
    return JsonResponse({'id': assignment.id, 'recipients': recipients, 'state': exam_state(assignment),
                         'closedAt': manila_isoformat(assignment.closed_at), 'dueAt': manila_isoformat(assignment.due_at),
                         'opensAt': manila_isoformat(assignment.opens_at), 'isHidden': assignment.is_hidden})


@login_required
@require_GET
def pupils_api(request):
    """Real pupil accounts (role=PUPIL), for the teacher's "assign to" picker. An admin
    creates these in Django admin with a name and email; pupils are matched to assignments
    by that email, so an account with a blank email can't be assigned anything."""
    if request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        return HttpResponseForbidden('Only teachers can view the pupil list.')
    pupils = request.user.teachable_pupils(getattr(request, 'school', None)).order_by('first_name', 'last_name', 'username')
    return JsonResponse({'canManageClasses': request.user.can_manage_classes, 'pupils': [
        {'id': p.id, 'name': p.get_full_name() or p.username, 'firstName': p.first_name, 'lastName': p.last_name, 'email': p.email}
        for p in pupils if p.email
    ]})


@login_required
@require_POST
def create_assignment(request):
    if request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        return HttpResponseForbidden('Only teachers can create exams.')
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
        return HttpResponseBadRequest('Give the exam a title.')
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
        resources_meta = json.loads(request.POST.get('resources_meta', '[]') or '[]')
        if not isinstance(resources_meta, list):
            raise ValueError
    except (ValueError, json.JSONDecodeError):
        return HttpResponseBadRequest('The resources list is not valid.')
    resource_files = iter(request.FILES.getlist('resource_files'))
    prepared_resources = []
    for i, meta in enumerate(resources_meta):
        meta = meta if isinstance(meta, dict) else {}
        kind = meta.get('kind')
        upload = next(resource_files, None) if kind in (ExamResource.Kind.PDF, ExamResource.Kind.IMAGE) else None
        fields, problem = prepare_resource(kind, meta.get('title'), meta.get('text'), upload, i + 1)
        if problem:
            return HttpResponseBadRequest(problem)
        prepared_resources.append(dict(fields, order=i))
    try:
        recipients = json.loads(request.POST.get('recipients', '[]'))
        if not recipients:
            raise ValueError
    except (ValueError, json.JSONDecodeError):
        return HttpResponseBadRequest('Add at least one pupil with an email address.')
    # A regular teacher may only assign to pupils in the classes they've been assigned to.
    allowed_emails = {e.lower() for e in request.user.teachable_pupils(getattr(request, 'school', None)).values_list('email', flat=True) if e}
    outside = [p.get('name') or p.get('email') for p in recipients
               if (p.get('email') or '').strip() and (p.get('email') or '').strip().lower() not in allowed_emails]
    if outside:
        return HttpResponseForbidden(
            'You can only assign work to pupils in the subjects you teach. Not allowed: ' + ', '.join(outside[:5]) +
            ('…' if len(outside) > 5 else ''))
    visibility = request.POST.get('visibility') or 'now'
    if visibility not in ('now', 'scheduled', 'hidden'):
        return HttpResponseBadRequest('Choose when pupils can see this exam.')
    opens_at = parse_manila_datetime(request.POST.get('opens_at'))
    if visibility == 'scheduled' and not opens_at:
        return HttpResponseBadRequest('Choose the date and time the exam becomes visible.')
    if visibility != 'scheduled':
        opens_at = timezone.now()

    delivery_mode = request.POST.get('delivery_mode') or Assignment.DeliveryMode.BROWSER
    if delivery_mode not in Assignment.DeliveryMode.values:
        return HttpResponseBadRequest('Unrecognised delivery mode.')
    require_join_code = request.POST.get('require_join_code') == 'true'
    join_code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6)) if require_join_code else ''
    allow_calculator = request.POST.get('allow_calculator') == 'true'
    allow_graphing_calculator = request.POST.get('allow_graphing_calculator') == 'true'
    due_at = parse_manila_datetime(request.POST.get('due_at'))
    if due_at and opens_at and due_at <= opens_at:
        return HttpResponseBadRequest('The closing time must be after the exam becomes visible.')

    assignment = Assignment.objects.create(title=request.POST['title'].strip(), instructions=request.POST.get('instructions', '').strip(),
        school=getattr(request, 'school', None),
        teacher_name=teacher_name, teacher_email=teacher_email,
        opens_at=opens_at, due_at=due_at, is_hidden=(visibility == 'hidden'),
        delivery_mode=delivery_mode, require_join_code=require_join_code, join_code=join_code,
        allow_calculator=allow_calculator, allow_graphing_calculator=allow_graphing_calculator)
    for prepared in prepared_items:
        AssignmentItem.objects.create(assignment=assignment, **prepared)
    for prepared in prepared_resources:
        ExamResource.objects.create(assignment=assignment, **prepared)
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
    """Edit an existing exam: title, instructions, visibility, closing time, delivery mode, join
    code, calculator toggles and — optionally — who it's assigned to (`recipients`: the full new
    list of {name, email}; pupils not in it are removed, new ones added). Removing a pupil who has
    already started or handed in deletes their work, so that needs `confirmRemoveStarted: true`.
    Editing the PDFs or questions themselves isn't supported here yet."""
    if request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        return HttpResponseForbidden('Only teachers can edit exams.')
    assignment = get_object_or_404(Assignment, id=assignment_id)
    if getattr(request, 'school', None) and assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not is_real_pupil(request) and not can_manage_exam(request.user, assignment):
        return not_your_exam_response()
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid exam data.')

    title = (payload.get('title') or '').strip()
    if not title:
        return HttpResponseBadRequest('Give the exam a title.')

    # Who it's assigned to (optional). Worked out and checked before anything is saved.
    recipient_plan = None
    if isinstance(payload.get('recipients'), list):
        wanted = {}
        for pupil in payload['recipients']:
            name, email = (pupil.get('name') or '').strip(), (pupil.get('email') or '').strip()
            if name and email:
                wanted.setdefault(email.lower(), (name, email))
        if not wanted:
            return HttpResponseBadRequest('An exam needs at least one pupil.')
        current = {r.pupil_email.lower(): r for r in assignment.recipients.select_related('submission')}
        to_add = [wanted[e] for e in wanted if e not in current]
        to_remove = [current[e] for e in current if e not in wanted]
        allowed = {e.lower() for e in request.user.teachable_pupils(getattr(request, 'school', None))
                   .values_list('email', flat=True) if e}
        outside = [name for name, email in to_add if email.lower() not in allowed]
        if outside:
            return HttpResponseForbidden('You can only assign work to pupils in the subjects you teach. Not allowed: '
                                         + ', '.join(outside[:5]) + ('…' if len(outside) > 5 else ''))
        started = [r for r in to_remove if getattr(r, 'submission', None) is not None
                   and (r.submission.status != 'not_started' or r.submission.started_at)]
        if started and not payload.get('confirmRemoveStarted'):
            return JsonResponse({'code': 'remove_started', 'pupils': [r.pupil_name for r in started],
                                 'error': 'Some of the pupils you removed have already started or handed in. '
                                          'Removing them deletes their work.'}, status=409)
        recipient_plan = (to_add, to_remove)

    delivery_mode = payload.get('deliveryMode') or Assignment.DeliveryMode.BROWSER
    if delivery_mode not in Assignment.DeliveryMode.values:
        return HttpResponseBadRequest('Unrecognised delivery mode.')

    assignment.title = title
    assignment.instructions = (payload.get('instructions') or '').strip()
    visibility = payload.get('visibility')  # optional: 'now' | 'scheduled' | 'hidden'
    if visibility == 'hidden':
        assignment.is_hidden = True
    elif visibility == 'now':
        assignment.is_hidden = False
        if not assignment.opens_at or assignment.opens_at > timezone.now():
            assignment.opens_at = timezone.now()
    elif visibility == 'scheduled' or (visibility is None and payload.get('opensAt')):
        opens = parse_manila_datetime(payload.get('opensAt'))
        if not opens:
            return HttpResponseBadRequest('Choose the date and time the exam becomes visible.')
        assignment.opens_at = opens
        if visibility == 'scheduled':
            assignment.is_hidden = False
    assignment.due_at = parse_manila_datetime(payload.get('dueAt'))
    if assignment.due_at and assignment.opens_at and assignment.due_at <= assignment.opens_at:
        return HttpResponseBadRequest('The closing time must be after the exam becomes visible.')
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
    actor = request.user.get_full_name() or request.user.username
    if recipient_plan:
        to_add, to_remove = recipient_plan
        for name, email in to_add:
            Recipient.objects.create(assignment=assignment, pupil_name=name, pupil_email=email)
        if to_remove:
            Recipient.objects.filter(id__in=[r.id for r in to_remove]).delete()
        if to_add or to_remove:
            bits = []
            if to_add:
                bits.append(f'added {", ".join(n for n, _ in to_add[:5])}{"…" if len(to_add) > 5 else ""}')
            if to_remove:
                bits.append(f'removed {", ".join(r.pupil_name for r in to_remove[:5])}{"…" if len(to_remove) > 5 else ""}')
            log_activity(actor, request.user.email, 'updated',
                         f'Changed who "{assignment.title}" is assigned to: {"; ".join(bits)}.', assignment=assignment)
    assignment.refresh_from_db()
    log_activity(actor, request.user.email,
        'updated', f'Updated settings for "{assignment.title}".', assignment=assignment)
    return JsonResponse(serialize_assignment(assignment))


@login_required
@require_POST
def exam_state_api(request, assignment_id):
    """Teacher controls on a pre-created / running exam:
    show   — make it visible to pupils right now (clears "hidden", opens it from now)
    close  — close it right now, even if a closing time is scheduled; anyone still working has
             their last autosave handed in automatically (after a short grace period)
    reopen — undo a manual close (if its scheduled closing time has also passed, that's cleared
             so it stays open until closed again)"""
    if request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        return HttpResponseForbidden('Only teachers can open or close exams.')
    assignment = get_object_or_404(Assignment, id=assignment_id)
    if getattr(request, 'school', None) and assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not is_real_pupil(request) and not can_manage_exam(request.user, assignment):
        return not_your_exam_response()
    try:
        action = json.loads(request.body or '{}').get('action')
    except json.JSONDecodeError:
        action = None
    now = timezone.now()
    actor = request.user.get_full_name() or request.user.username
    if action == 'show':
        assignment.is_hidden = False
        if not assignment.opens_at or assignment.opens_at > now:
            assignment.opens_at = now
        assignment.save(update_fields=['is_hidden', 'opens_at'])
        log_activity(actor, request.user.email, 'updated', f'Made "{assignment.title}" visible to pupils.', assignment=assignment)
    elif action == 'hide':
        assignment.is_hidden = True
        assignment.save(update_fields=['is_hidden'])
        log_activity(actor, request.user.email, 'updated', f'Hid "{assignment.title}" from pupils.', assignment=assignment)
    elif action == 'close':
        if not assignment.closed_at:
            assignment.closed_at = now
            assignment.save(update_fields=['closed_at'])
            log_activity(actor, request.user.email, 'updated', f'Closed "{assignment.title}".', assignment=assignment)
    elif action == 'reopen':
        assignment.closed_at = None
        if assignment.due_at and assignment.due_at <= now:
            assignment.due_at = None
        assignment.save(update_fields=['closed_at', 'due_at'])
        log_activity(actor, request.user.email, 'updated', f'Reopened "{assignment.title}".', assignment=assignment)
    else:
        return HttpResponseBadRequest('Unknown action.')
    assignment.refresh_from_db()
    return JsonResponse(serialize_assignment(assignment))


@login_required
@require_POST
def delete_assignment_api(request, assignment_id):
    """Deletes an assignment and everything under it (items, recipients, submissions —
    all cascade-deleted). Irreversible and wipes pupil work, so it requires the logged-in
    teacher to re-type their own account password as an extra confirmation step, on top of
    whatever "are you sure?" prompt the frontend already shows."""
    if request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        return HttpResponseForbidden('Only teachers can delete exams.')
    assignment = get_object_or_404(Assignment, id=assignment_id)
    if getattr(request, 'school', None) and assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not is_real_pupil(request) and not can_manage_exam(request.user, assignment):
        return not_your_exam_response()
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
        'deleted', f'Deleted exam "{assignment.title}".')
    assignment.delete()
    return JsonResponse({'deleted': True})


@login_required
@require_POST
def start_submission(request, recipient_id):
    """Called when a pupil opens an assignment; records when their timed attempt began."""
    recipient = get_object_or_404(Recipient, id=recipient_id)
    if getattr(request, 'school', None) and recipient.assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not is_real_pupil(request) and not can_manage_exam(request.user, recipient.assignment):
        return not_your_exam_response()
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
        if not exam_visible_to_pupils(assignment, recipient):
            return exam_not_open_response()
        if assignment_closed(assignment, recipient):
            return assignment_closed_response()
        submission.started_at = timezone.now()
        if submission.status == 'not_started':
            submission.status = 'draft'
        submission.save()
    return JsonResponse({
        'startedAt': submission.started_at.isoformat() if submission.started_at else None,
        'status': submission.status,
    })


def submission_from_request(request, recipient_id, final=False):
    recipient = get_object_or_404(Recipient, id=recipient_id)
    if getattr(request, 'school', None) and recipient.assignment.school_id != request.school.id:
        return None, HttpResponseForbidden('Forbidden')
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
    # Autosaves stop at closing time; the final hand-in gets a short grace period so the
    # submit each pupil's page sends at that moment isn't refused for arriving a second late.
    if assignment_closed(recipient.assignment, recipient, grace=final):
        return None, assignment_closed_response()
    if submission.locked_at and not (final and assignment_closed(recipient.assignment, recipient)):
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
        submission, error = submission_from_request(request, recipient_id, final=True)
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid submission data.')
    if error:
        return error
    submission.status = 'submitted'
    submission.submitted_at = timezone.now()
    submission.locked_at = None
    submission.save()
    assignment = submission.recipient.assignment
    log_activity(submission.recipient.pupil_name, submission.recipient.pupil_email,
        'submitted', f'{submission.recipient.pupil_name} submitted "{assignment.title}".', assignment=assignment)
    # No email to the teacher any more: the submission shows as "new" on their exam list, in
    # Monitor and on the bell until they open it (Submission.is_new_for_teacher).
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
    if getattr(request, 'school', None) and recipient.assignment.school_id != request.school.id:
        return recipient, None, HttpResponseForbidden('Forbidden')
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
    if not assignment_closed(recipient.assignment, recipient) and isinstance(payload.get('responses'), dict):
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
    if getattr(request, 'school', None) and recipient.assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not is_real_pupil(request) and not can_manage_exam(request.user, recipient.assignment):
        return not_your_exam_response()
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
    assignment = recipient.assignment
    return JsonResponse({'locked': bool(submission.locked_at), 'lockReason': submission.lock_reason,
        'closed': assignment_closed(assignment, recipient), 'closedByTeacher': bool(assignment.closed_at),
        'closesAt': manila_isoformat(exam_closes_at(assignment, recipient)), 'status': submission.status})


# ---------------- classes & pupil roster (teacher-side, mirrors what admin can do) ----------------

def _require_teacher(request):
    if request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        return HttpResponseForbidden('Only teachers can manage year groups, subjects and pupils.')
    return None


def _require_class_admin(request):
    """Creating/editing/deleting classes and adding pupil accounts is admin-only. A regular
    teacher is assigned to classes by an admin and can then assign work to those pupils."""
    denied = _require_teacher(request)
    if denied:
        return denied
    if not request.user.can_manage_classes:
        return HttpResponseForbidden('Only admins can create or manage year groups and subjects. Ask an admin to add you to one.')
    return None


def assignable_teachers(school=None):
    qs = User.objects.filter(is_active=True).filter(Q(role=User.Role.TEACHER) | Q(is_superuser=True))
    if school:
        qs = qs.filter(school=school)
    return qs


def serialize_class(cls):
    return {
        'id': cls.id,
        'name': cls.name,
        'teacherNames': [t.get_full_name() or t.username for t in cls.teachers.all()],
        'teachers': [
            {'id': t.id, 'name': t.get_full_name() or t.username, 'email': t.email}
            for t in cls.teachers.all()
        ],
        'pupils': [
            {'id': p.id, 'name': p.get_full_name() or p.username, 'firstName': p.first_name, 'lastName': p.last_name, 'email': p.email}
            for p in cls.pupils.all().order_by('first_name', 'last_name', 'username')
        ],
    }


@login_required
@require_GET
def classes_api(request):
    denied = _require_teacher(request)
    if denied:
        return denied
    classes = request.user.visible_classes(getattr(request, 'school', None)).prefetch_related('pupils', 'teachers')
    data = {'canManageClasses': request.user.can_manage_classes,
            'classes': [serialize_class(c) for c in classes]}
    if request.user.can_manage_classes:
        data['teachers'] = [
            {'id': t.id, 'name': t.get_full_name() or t.username, 'email': t.email}
            for t in assignable_teachers(getattr(request, 'school', None)).order_by('first_name', 'last_name', 'username')
        ]
    return JsonResponse(data)


@login_required
@require_POST
def create_class_api(request):
    denied = _require_class_admin(request)
    if denied:
        return denied
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid class data.')
    name = (payload.get('name') or '').strip()
    if not name:
        return HttpResponseBadRequest('Give the year group a name.')
    existing = SchoolClass.objects.filter(name__iexact=name)
    if getattr(request, 'school', None):
        existing = existing.filter(school=request.school)
    if existing.exists():
        return HttpResponseBadRequest('A year group with that name already exists.')
    pupil_ids = payload.get('pupilIds') or []
    cls = SchoolClass.objects.create(name=name, school=getattr(request, 'school', None))
    if payload.get('teacherIds'):
        cls.teachers.set(assignable_teachers(getattr(request, 'school', None)).filter(id__in=payload['teacherIds']))
    if pupil_ids:
        pupils = User.assignable_pupils(getattr(request, 'school', None)).filter(id__in=pupil_ids)
        cls.pupils.set(pupils)
    log_activity(request.user.get_full_name() or request.user.username, request.user.email,
        'class_created', f'Created year group "{cls.name}".', school_class=cls)
    return JsonResponse(serialize_class(cls), status=201)


@login_required
@require_POST
def update_class_api(request, class_id):
    denied = _require_class_admin(request)
    if denied:
        return denied
    cls = get_object_or_404(SchoolClass, id=class_id)
    if getattr(request, 'school', None) and cls.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid class data.')
    actor = request.user.get_full_name() or request.user.username
    name = (payload.get('name') or '').strip()
    if name and name != cls.name:
        existing = SchoolClass.objects.filter(name__iexact=name).exclude(id=cls.id)
        if getattr(request, 'school', None):
            existing = existing.filter(school=request.school)
        if existing.exists():
            return HttpResponseBadRequest('A year group with that name already exists.')
        old_name = cls.name
        cls.name = name
        cls.save()
        log_activity(actor, request.user.email, 'class_renamed',
            f'Renamed year group "{old_name}" to "{cls.name}".', school_class=cls)
    if 'pupilIds' in payload:
        before = {p.id: p for p in cls.pupils.all()}
        pupils = list(User.assignable_pupils(getattr(request, 'school', None)).filter(id__in=payload.get('pupilIds') or []))
        cls.pupils.set(pupils)
        after = {p.id: p for p in pupils}
        added = [after[i] for i in after if i not in before]
        removed = [before[i] for i in before if i not in after]
        label = lambda people: ', '.join(p.get_full_name() or p.username for p in people[:5]) + ('…' if len(people) > 5 else '')
        if added:
            log_activity(actor, request.user.email, 'class_pupils',
                f'Added {label(added)} to year group "{cls.name}".', school_class=cls)
        if removed:
            log_activity(actor, request.user.email, 'class_pupils',
                f'Removed {label(removed)} from year group "{cls.name}".', school_class=cls)
    if 'teacherIds' in payload:
        before = set(cls.teachers.values_list('id', flat=True))
        cls.teachers.set(assignable_teachers(getattr(request, 'school', None)).filter(id__in=payload.get('teacherIds') or []))
        if set(cls.teachers.values_list('id', flat=True)) != before:
            log_activity(request.user.get_full_name() or request.user.username, request.user.email,
                'class_teachers', f'Updated teachers of year group "{cls.name}": '
                f'{", ".join(t.get_full_name() or t.username for t in cls.teachers.all()) or "none"}.',
                school_class=cls)
    return JsonResponse(serialize_class(cls))


@login_required
@require_POST
def delete_class_api(request, class_id):
    denied = _require_class_admin(request)
    if denied:
        return denied
    cls = get_object_or_404(SchoolClass, id=class_id)
    if getattr(request, 'school', None) and cls.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    log_activity(request.user.get_full_name() or request.user.username, request.user.email,
        'class_deleted', f'Deleted year group "{cls.name}".')
    cls.delete()
    return JsonResponse({'deleted': True})


# ---------------- subjects (pupils can come from several sections) ----------------

def serialize_subject(subject, section_names=None):
    """section_names: optional {pupil_id: [year group names]} so the page can show where each
    pupil comes from without an extra query per pupil."""
    section_names = section_names or {}
    return {
        'id': subject.id,
        'name': subject.name,
        'teachers': [
            {'id': t.id, 'name': t.get_full_name() or t.username, 'email': t.email}
            for t in subject.teachers.all()
        ],
        'pupils': [
            {'id': p.id, 'name': p.get_full_name() or p.username, 'firstName': p.first_name, 'lastName': p.last_name, 'email': p.email,
             'sections': section_names.get(p.id, [])}
            for p in sorted(subject.pupils.all(), key=lambda u: (u.first_name.lower(), u.last_name.lower(), u.username))
        ],
    }


def _pupil_section_names(school):
    out = {}
    qs = SchoolClass.pupils.through.objects.select_related('schoolclass')
    if school:
        qs = qs.filter(schoolclass__school=school)
    for row in qs.order_by('schoolclass__name'):
        out.setdefault(row.user_id, []).append(row.schoolclass.name)
    return out


def _get_school_subject(request, subject_id):
    subject = get_object_or_404(Subject, id=subject_id)
    if getattr(request, 'school', None) and subject.school_id != request.school.id:
        return None
    return subject


@login_required
def subjects_page(request):
    return _teacher_page(request, 'subjects.html')


@login_required
@require_GET
def subjects_api(request):
    denied = _require_teacher(request)
    if denied:
        return denied
    school = getattr(request, 'school', None)
    subjects = request.user.visible_subjects(school).prefetch_related('pupils', 'teachers')
    sections = _pupil_section_names(school)
    data = {'canManageClasses': request.user.can_manage_classes,
            'subjects': [serialize_subject(s, sections) for s in subjects]}
    if request.user.can_manage_classes:
        data['teachers'] = [
            {'id': t.id, 'name': t.get_full_name() or t.username, 'email': t.email}
            for t in assignable_teachers(school).order_by('first_name', 'last_name', 'username')
        ]
    return JsonResponse(data)


@login_required
@require_POST
def create_subject_api(request):
    denied = _require_class_admin(request)
    if denied:
        return denied
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid subject data.')
    school = getattr(request, 'school', None)
    name = (payload.get('name') or '').strip()
    if not name:
        return HttpResponseBadRequest('Give the subject a name.')
    if len(name) > 120:
        return HttpResponseBadRequest('Subject names can be at most 120 characters.')
    existing = Subject.objects.filter(name__iexact=name)
    if school:
        existing = existing.filter(school=school)
    if existing.exists():
        return HttpResponseBadRequest('A subject with that name already exists.')
    subject = Subject.objects.create(name=name, school=school)
    if payload.get('teacherIds'):
        subject.teachers.set(assignable_teachers(school).filter(id__in=payload['teacherIds']))
    if payload.get('pupilIds'):
        subject.pupils.set(User.assignable_pupils(school).filter(id__in=payload['pupilIds']))
    log_activity(request.user.get_full_name() or request.user.username, request.user.email,
                 'subject_created', f'Created subject "{subject.name}".')
    return JsonResponse(serialize_subject(subject, _pupil_section_names(school)), status=201)


@login_required
@require_POST
def update_subject_api(request, subject_id):
    """Any of: name, teacherIds (replace), pupilIds (replace), addPupilIds, removePupilIds."""
    denied = _require_class_admin(request)
    if denied:
        return denied
    subject = _get_school_subject(request, subject_id)
    if subject is None:
        return HttpResponseForbidden('Forbidden')
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid subject data.')
    school = getattr(request, 'school', None)
    actor = request.user.get_full_name() or request.user.username
    label = lambda people: ', '.join(p.get_full_name() or p.username for p in people[:5]) + ('…' if len(people) > 5 else '')
    name = (payload.get('name') or '').strip()
    if name and name != subject.name:
        if len(name) > 120:
            return HttpResponseBadRequest('Subject names can be at most 120 characters.')
        existing = Subject.objects.filter(name__iexact=name).exclude(id=subject.id)
        if school:
            existing = existing.filter(school=school)
        if existing.exists():
            return HttpResponseBadRequest('A subject with that name already exists.')
        old = subject.name
        subject.name = name
        subject.save(update_fields=['name'])
        log_activity(actor, request.user.email, 'subject_renamed', f'Renamed subject "{old}" to "{subject.name}".')
    before = {p.id: p for p in subject.pupils.all()}
    pupils_q = User.assignable_pupils(school)
    if 'pupilIds' in payload:
        subject.pupils.set(list(pupils_q.filter(id__in=payload.get('pupilIds') or [])))
    if payload.get('addPupilIds'):
        subject.pupils.add(*pupils_q.filter(id__in=payload['addPupilIds']))
    if payload.get('removePupilIds'):
        subject.pupils.remove(*subject.pupils.filter(id__in=payload['removePupilIds']))
    after = {p.id: p for p in subject.pupils.all()}
    added = [after[i] for i in after if i not in before]
    removed = [before[i] for i in before if i not in after]
    if added:
        log_activity(actor, request.user.email, 'subject_pupils',
                     f'Added {len(added)} pupil{"s" if len(added) != 1 else ""} to subject "{subject.name}": {label(added)}.')
    if removed:
        log_activity(actor, request.user.email, 'subject_pupils',
                     f'Removed {label(removed)} from subject "{subject.name}".')
    if 'teacherIds' in payload:
        before_t = set(subject.teachers.values_list('id', flat=True))
        subject.teachers.set(assignable_teachers(school).filter(id__in=payload.get('teacherIds') or []))
        if set(subject.teachers.values_list('id', flat=True)) != before_t:
            log_activity(actor, request.user.email, 'subject_teachers',
                         f'Updated teachers of subject "{subject.name}": '
                         f'{", ".join(t.get_full_name() or t.username for t in subject.teachers.all()) or "none"}.')
    return JsonResponse(serialize_subject(subject, _pupil_section_names(school)))


@login_required
@require_POST
def delete_subject_api(request, subject_id):
    denied = _require_class_admin(request)
    if denied:
        return denied
    subject = _get_school_subject(request, subject_id)
    if subject is None:
        return HttpResponseForbidden('Forbidden')
    log_activity(request.user.get_full_name() or request.user.username, request.user.email,
                 'subject_deleted', f'Deleted subject "{subject.name}".')
    subject.delete()
    return JsonResponse({'deleted': True})


def _serialize_teacher(t):
    return {
        'id': t.id, 'name': t.get_full_name() or t.username, 'username': t.username, 'email': t.email,
        'firstName': t.first_name, 'lastName': t.last_name,
        'isMainAdmin': t.is_superuser,
        'isAdmin': t.can_manage_classes,
        'isActive': t.is_active,
        'classes': [c.name for c in t.classes_taught.all()],
        'subjects': [s.name for s in t.subjects_taught.all()],
        'lastLogin': t.last_login.isoformat() if t.last_login else None,
        'status': t.account_status,
        'invitedAt': t.invited_at.isoformat() if t.invited_at else None,
    }


@login_required
@require_GET
def teachers_api(request):
    """Admin only: every teacher account with its admin status and classes."""
    if not (request.user.is_superuser or request.user.is_school_admin):
        return HttpResponseForbidden('Only admins can manage teacher roles.')
    teachers = User.objects.filter(Q(role=User.Role.TEACHER) | Q(is_superuser=True))
    if getattr(request, 'school', None):
        teachers = teachers.filter(school=request.school)
    teachers = (teachers.prefetch_related('classes_taught', 'subjects_taught')
                .order_by('-is_superuser', '-is_school_admin', 'first_name', 'last_name', 'username'))
    return JsonResponse({'teachers': [_serialize_teacher(t) for t in teachers]})


@login_required
@require_POST
def set_teacher_admin_api(request, user_id):
    if not (request.user.is_superuser or request.user.is_school_admin):
        return HttpResponseForbidden('Only admins can change teacher roles.')
    target = get_object_or_404(User, id=user_id)
    if target.is_superuser:
        return HttpResponseBadRequest("Main admins are always admins — change superuser status in Django admin instead.")
    if target.role != User.Role.TEACHER:
        return HttpResponseBadRequest('Only teacher accounts can be made admins.')
    if getattr(request, 'school', None) and target.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid request.')
    make_admin = bool(payload.get('isAdmin'))
    if target.is_school_admin != make_admin or target.is_staff != make_admin:
        target.is_school_admin = make_admin
        target.is_staff = make_admin
        target.save(update_fields=['is_school_admin', 'is_staff'])
        name = target.get_full_name() or target.username
        log_activity(request.user.get_full_name() or request.user.username, request.user.email,
            'admin_granted' if make_admin else 'admin_revoked',
            f'Made {name} an admin.' if make_admin else f'Changed {name} back to a regular teacher.')
    target = User.objects.prefetch_related('classes_taught', 'subjects_taught').get(id=target.id)
    return JsonResponse(_serialize_teacher(target))


@login_required
@require_POST
def create_teacher_api(request):
    """Add a staff account as either "admin" or "teacher". Returns a one-time
    temporary password to hand over (they can change it from their account menu)."""
    if not (request.user.is_superuser or request.user.is_school_admin):
        return HttpResponseForbidden('Only admins can add teachers and admins.')
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid request.')
    first_name = (payload.get('firstName') or '').strip()
    last_name = (payload.get('lastName') or '').strip()
    email = (payload.get('email') or '').strip()
    role = payload.get('role')
    if role not in ('admin', 'teacher'):
        return HttpResponseBadRequest('Choose a role: Admin or Teacher.')
    if not first_name or not email:
        return HttpResponseBadRequest('A first name and email are required.')
    try:
        from django.core.validators import validate_email
        validate_email(email)
    except DjangoValidationError:
        return HttpResponseBadRequest('That email address doesn\'t look right.')
    if User.objects.filter(email__iexact=email).exists():
        return HttpResponseBadRequest('An account with that email already exists.')
    username = _unique_username((email.split('@')[0] or first_name).lower())
    temp_password = secrets.token_urlsafe(6)
    is_admin = (role == 'admin')
    user = User.objects.create_user(must_change_password=True, 
        username=username, email=email, first_name=first_name, last_name=last_name,
        password=temp_password, role=User.Role.TEACHER,
        school=getattr(request, 'school', None),
        is_staff=is_admin, is_school_admin=is_admin
    )
    name = user.get_full_name() or user.username
    class_ids = [int(i) for i in (payload.get('classIds') or []) if str(i).isdigit()]
    if class_ids:
        classes = SchoolClass.objects.filter(id__in=class_ids)
        if getattr(request, 'school', None):
            classes = classes.filter(school=request.school)
        for cls in classes:
            cls.teachers.add(user)
    subject_ids = [int(i) for i in (payload.get('subjectIds') or []) if str(i).isdigit()]
    if subject_ids:
        subjects = Subject.objects.filter(id__in=subject_ids)
        if getattr(request, 'school', None):
            subjects = subjects.filter(school=request.school)
        for subject in subjects:
            subject.teachers.add(user)
    log_activity(request.user.get_full_name() or request.user.username, request.user.email, 'teacher_created',
                 f'Added {name} ({email}) as {"an admin" if role == "admin" else "a teacher"}.')
    from .accounts import login_url, site_url
    data = {'tempPassword': temp_password, 'emailed': False,
            'loginUrl': login_url(request, user), 'website': site_url() + '/'}
    if payload.get('invite'):
        from .accounts import invite_user
        cred, emailed = invite_user(request, user)
        data.update({'tempPassword': cred['tempPassword'], 'emailed': emailed})
    data.update(_serialize_teacher(User.objects.prefetch_related('classes_taught', 'subjects_taught').get(id=user.id)))
    return JsonResponse(data, status=201)


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
    """Lets a class admin add a pupil on the spot while building a class or
    an assignment. Generates a one-time temporary password the teacher can hand to the
    pupil — there's no self-registration or password-reset flow yet, so this is the only
    way a brand-new pupil gets into their account. Class admins only."""
    denied = _require_class_admin(request)
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
    # The same email may be used by a pupil at another school (separate account); within this
    # school it must be unique.
    clash = User.objects.filter(email__iexact=email)
    if getattr(request, 'school', None):
        clash = clash.filter(school=request.school)
    if clash.exists():
        return HttpResponseBadRequest('Someone at this school already uses that email.')
    username_base = email.split('@')[0].lower() or first_name.lower()
    username = _unique_username(username_base)
    temp_password = secrets.token_urlsafe(6)
    pupil = User.objects.create_user(must_change_password=True, 
        username=username, email=email, first_name=first_name, last_name=last_name,
        password=temp_password, role=User.Role.PUPIL,
        school=getattr(request, 'school', None),
    )
    class_id = payload.get('classId')
    cls = SchoolClass.objects.filter(id=class_id)
    if getattr(request, 'school', None):
        cls = cls.filter(school=request.school)
    cls = cls.first() if class_id else None
    if cls:
        cls.pupils.add(pupil)
    subject_ids = [int(i) for i in (payload.get('subjectIds') or []) if str(i).isdigit()]
    if subject_ids:
        subjects = Subject.objects.filter(id__in=subject_ids)
        if getattr(request, 'school', None):
            subjects = subjects.filter(school=request.school)
        for subject in subjects:
            subject.pupils.add(pupil)
    log_activity(request.user.get_full_name() or request.user.username, request.user.email,
        'pupil_created', f'Added pupil {pupil.get_full_name() or pupil.username} ({pupil.email})'
        + (f' to year group "{cls.name}".' if cls else '.'), school_class=cls)
    emailed = False
    if payload.get('invite'):
        from .accounts import invite_user
        cred, emailed = invite_user(request, pupil)
        temp_password = cred['tempPassword']
    pupil.refresh_from_db()
    from .accounts import serialize_person
    data = serialize_person(pupil)
    from .accounts import login_url, site_url
    data.update({'tempPassword': temp_password, 'emailed': emailed, 'classes': [cls.name] if cls else [],
                 'loginUrl': login_url(request, pupil), 'website': site_url() + '/'})
    return JsonResponse(data, status=201)


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
    if getattr(request, 'school', None) and recipient.assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not is_real_pupil(request) and not can_manage_exam(request.user, recipient.assignment):
        return not_your_exam_response()
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
    """Shared guard for the 1:1 chat endpoints: the exam's own teacher (or an admin) can message
    its pupils; a pupil only their own thread."""
    if getattr(request, 'school', None) and recipient.assignment.school_id != request.school.id:
        return HttpResponseForbidden('Not allowed.')
    if request.user.role == User.Role.PUPIL and not request.user.is_superuser:
        if recipient.pupil_email.lower() != request.user.email.lower():
            return HttpResponseForbidden('This is not your exam.')
    elif request.user.role != User.Role.TEACHER and not request.user.is_superuser:
        return HttpResponseForbidden('Not allowed.')
    elif not can_manage_exam(request.user, recipient.assignment):
        return not_your_exam_response()
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
    if getattr(request, 'school', None) and assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not is_real_pupil(request) and not can_manage_exam(request.user, assignment):
        return not_your_exam_response()
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


ACTIVITY_CATEGORIES = {
    'assignments': ['created', 'updated', 'deleted', 'broadcast', 'accommodation'],
    'submissions': ['submitted', 'locked', 'unlocked', 'resumed'],
    'grading': ['graded', 'published'],
    'classes': ['class_created', 'class_renamed', 'class_deleted', 'class_teachers', 'class_pupils', 'pupil_created',
                'subject_created', 'subject_renamed', 'subject_deleted', 'subject_teachers', 'subject_pupils'],
    'roles': ['admin_granted', 'admin_revoked', 'teacher_created', 'teacher_deleted'],
}


def _notification_assignments(request):
    qs = Assignment.objects.all()
    if getattr(request, 'school', None):
        qs = qs.filter(school=request.school)
    if not is_real_pupil(request):
        qs = visible_exams_for(request.user, qs)
    return qs


@login_required
@require_GET
def notifications_api(request):
    """Everything the bell (and the pop-up alerts) should show, on any page.
    Teachers: pupils locked out of an exam, and unread pupil messages — for their own
    assignments (admins: every assignment in the school). Pupils: unread teacher messages and
    grades released since they last looked. Each item has a stable `key` that changes when
    something new happens (a new lock, a newer message, a re-grade), so the page can pop up
    an alert exactly once per event."""
    user = request.user
    items = []
    recipients = annotated_recipients().select_related('assignment')
    assignments = _notification_assignments(request)
    if is_real_pupil(request):
        recipients = recipients.filter(assignment__in=assignments, pupil_email__iexact=user.email)
        for r in recipients:
            sub = getattr(r, 'submission', None)
            if _unread_since(r.last_teacher_msg, r.pupil_last_read_at):
                last = r.messages.filter(sender_role=User.Role.TEACHER).order_by('-created_at').first()
                items.append({'key': f'msg:{r.id}:{r.last_teacher_msg.isoformat()}', 'kind': 'message',
                    'assignmentId': r.assignment_id, 'assignmentTitle': r.assignment.title, 'recipientId': r.id,
                    'from': r.assignment.teacher_name or 'Your teacher', 'text': last.text[:280] if last else '',
                    'at': r.last_teacher_msg.isoformat()})
            if sub and sub.published and sub.graded_at and (sub.grade_seen_at is None or sub.grade_seen_at < sub.graded_at):
                items.append({'key': f'grade:{r.id}:{sub.graded_at.isoformat()}', 'kind': 'graded',
                    'assignmentId': r.assignment_id, 'assignmentTitle': r.assignment.title, 'recipientId': r.id,
                    'totalScore': sub.total_score, 'maxScore': sub.max_score, 'at': sub.graded_at.isoformat()})
    else:
        if user.role != User.Role.TEACHER and not user.is_superuser:
            return JsonResponse({'items': []})
        if not user.can_manage_classes:
            assignments = assignments.filter(teacher_email__iexact=user.email)
        recipients = recipients.filter(assignment__in=assignments)
        locked = recipients.filter(submission__locked_at__isnull=False).exclude(submission__status='submitted')
        for r in locked:
            sub = r.submission
            items.append({'key': f'lock:{r.id}:{sub.lock_count}', 'kind': 'lock',
                'assignmentId': r.assignment_id, 'assignmentTitle': r.assignment.title, 'recipientId': r.id,
                'pupilName': r.pupil_name, 'reason': sub.lock_reason, 'resumeCode': sub.resume_code,
                'at': sub.locked_at.isoformat()})
        fresh = (recipients.filter(submission__status='submitted', submission__submitted_at__isnull=False)
                 .filter(Q(submission__teacher_seen_at__isnull=True) | Q(submission__submitted_at__gt=F('submission__teacher_seen_at'))))
        for r in fresh[:50]:
            sub = r.submission
            items.append({'key': f'submitted:{r.id}:{sub.submitted_at.isoformat()}', 'kind': 'submitted',
                'assignmentId': r.assignment_id, 'assignmentTitle': r.assignment.title, 'recipientId': r.id,
                'pupilName': r.pupil_name, 'at': sub.submitted_at.isoformat()})
        unread = recipients.filter(last_pupil_msg__isnull=False).filter(
            Q(teacher_last_read_at__isnull=True) | Q(last_pupil_msg__gt=F('teacher_last_read_at')))
        for r in unread:
            last = r.messages.filter(sender_role=User.Role.PUPIL).order_by('-created_at').first()
            items.append({'key': f'msg:{r.id}:{r.last_pupil_msg.isoformat()}', 'kind': 'message',
                'assignmentId': r.assignment_id, 'assignmentTitle': r.assignment.title, 'recipientId': r.id,
                'pupilName': r.pupil_name, 'from': r.pupil_name, 'text': last.text[:280] if last else '',
                'at': r.last_pupil_msg.isoformat()})
    items.sort(key=lambda i: i['at'], reverse=True)
    return JsonResponse({'items': items})


@login_required
@require_POST
def notifications_read_all_api(request):
    """Bell -> "Mark all as read". Clears the same items the bell shows, in the same scope:
    teachers — unread pupil messages and new submissions on their exams (admins: the school's);
    pupils — unread teacher messages and released grades. Lockouts are left alone: they clear
    when the pupil is unlocked, because someone is waiting on them."""
    user = request.user
    now = timezone.now()
    assignments = _notification_assignments(request)
    if is_real_pupil(request):
        recipients = Recipient.objects.filter(assignment__in=assignments, pupil_email__iexact=user.email)
        messages_read = recipients.update(pupil_last_read_at=now)
        cleared = Submission.objects.filter(recipient__in=recipients, published=True).update(grade_seen_at=now)
        return JsonResponse({'ok': True, 'messages': messages_read, 'grades': cleared})
    if user.role != User.Role.TEACHER and not user.is_superuser:
        return HttpResponseForbidden('Not allowed.')
    if not user.can_manage_classes:
        assignments = assignments.filter(teacher_email__iexact=user.email)
    recipients = Recipient.objects.filter(assignment__in=assignments)
    messages_read = recipients.update(teacher_last_read_at=now)
    seen = Submission.objects.filter(recipient__in=recipients, status='submitted').update(teacher_seen_at=now)
    return JsonResponse({'ok': True, 'messages': messages_read, 'submissions': seen})


@login_required
@require_POST
def submission_seen_api(request, recipient_id=None):
    """The teacher opened a pupil's submission (or pressed "Mark all as seen" on an exam):
    clears its "new" dot / bell item. Body for the bulk form: {"assignmentId": id}."""
    if is_real_pupil(request):
        return HttpResponseForbidden('Not allowed.')
    now = timezone.now()
    if recipient_id is not None:
        recipient = get_object_or_404(Recipient, id=recipient_id)
        if getattr(request, 'school', None) and recipient.assignment.school_id != request.school.id:
            return HttpResponseForbidden('Forbidden')
        if not can_manage_exam(request.user, recipient.assignment):
            return not_your_exam_response()
        Submission.objects.filter(recipient=recipient).update(teacher_seen_at=now)
        return JsonResponse({'seen': 1})
    try:
        assignment_id = int(json.loads(request.body or '{}').get('assignmentId'))
    except (TypeError, ValueError, json.JSONDecodeError):
        return HttpResponseBadRequest('Missing exam.')
    assignment = get_object_or_404(Assignment, id=assignment_id)
    if getattr(request, 'school', None) and assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not can_manage_exam(request.user, assignment):
        return not_your_exam_response()
    n = Submission.objects.filter(recipient__assignment=assignment, status='submitted').update(teacher_seen_at=now)
    return JsonResponse({'seen': n})


# Marking is comment-only now: new marks are 'comment' (written answers) and 'pin' (PDF).
# The other styles/shapes are only kept so marks made before the change still load and save.
MARK_STYLES = {'comment', 'underline', 'circle', 'highlight'}
PDF_SHAPES = {'pin', 'highlight', 'underline', 'circle', 'box'}
MAX_MARKS_BYTES = 400_000


def _clean_teacher_marks(raw):
    """Validate and normalise the teacher's marking JSON (see Submission.teacher_marks).
    Returns (marks, error)."""
    if not isinstance(raw, dict):
        return None, 'Invalid marking data.'
    if len(json.dumps(raw)) > MAX_MARKS_BYTES:
        return None, 'Too much marking on one paper to save — remove some marks or shorten comments.'
    text = lambda v, n=2000: str(v or '')[:n]
    num = lambda v: round(float(v), 2) if isinstance(v, (int, float)) else 0.0
    out = {'answers': {}, 'pdf': {}, 'annComments': {}}
    for item_id, marks in (raw.get('answers') or {}).items():
        if not str(item_id).isdigit() or not isinstance(marks, list):
            continue
        cleaned = []
        for m in marks[:300]:
            if not isinstance(m, dict) or m.get('style') not in MARK_STYLES:
                continue
            try:
                start, end = int(m.get('start')), int(m.get('end'))
            except (TypeError, ValueError):
                continue
            if start < 0 or end <= start:
                continue
            cleaned.append({'id': text(m.get('id'), 40), 'start': start, 'end': end, 'quote': text(m.get('quote'), 500),
                            'style': m['style'], 'comment': text(m.get('comment'))})
        if cleaned:
            out['answers'][str(item_id)] = cleaned
    for item_id, marks in (raw.get('pdf') or {}).items():
        if not str(item_id).isdigit() or not isinstance(marks, list):
            continue
        cleaned = []
        for m in marks[:300]:
            if not isinstance(m, dict) or m.get('shape') not in PDF_SHAPES:
                continue
            try:
                page = int(m.get('page'))
            except (TypeError, ValueError):
                continue
            cleaned.append({'id': text(m.get('id'), 40), 'page': page, 'shape': m['shape'],
                            'x': num(m.get('x')), 'y': num(m.get('y')), 'w': num(m.get('w')), 'h': num(m.get('h')),
                            'comment': text(m.get('comment'))})
        if cleaned:
            out['pdf'][str(item_id)] = cleaned
    for key, c in (raw.get('annComments') or {}).items():
        if str(key).isdigit() and isinstance(c, dict) and text(c.get('comment')).strip():
            out['annComments'][str(key)] = {'fp': text(c.get('fp'), 200), 'comment': text(c.get('comment'))}
    return out, None


@login_required
@require_POST
def save_teacher_marks_api(request, recipient_id):
    """Save the teacher's comments on a pupil's paper (plus any older underlines / highlights)."""
    denied = _require_teacher(request)
    if denied:
        return denied
    recipient = get_object_or_404(Recipient, id=recipient_id)
    if getattr(request, 'school', None) and recipient.assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not can_manage_exam(request.user, recipient.assignment):
        return not_your_exam_response()
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid marking data.')
    marks, error = _clean_teacher_marks(payload.get('teacherMarks'))
    if error:
        return HttpResponseBadRequest(error)
    submission = _submission_of(recipient)
    submission.teacher_marks = marks
    submission.save(update_fields=['teacher_marks', 'updated_at'])
    return JsonResponse({'teacherMarks': marks, 'savedAt': timezone.now().isoformat()})


@login_required
@require_POST
def grade_seen_api(request, recipient_id):
    """A pupil opened "View grade" — clears the grade notification from their bell."""
    recipient = get_object_or_404(Recipient, id=recipient_id)
    denied = _recipient_access_check(request, recipient)
    if denied:
        return denied
    if is_real_pupil(request):
        Submission.objects.filter(recipient=recipient).update(grade_seen_at=timezone.now())
    return JsonResponse({'seen': True})


def _activity_category(action):
    for category, actions in ACTIVITY_CATEGORIES.items():
        if action in actions:
            return category
    return 'other'


def _class_activity_q(classes):
    """Entries tied to any of `classes`: roster actions on the class itself, plus anything
    about an assignment that was set for at least one of the class's pupils."""
    emails = list(User.objects.filter(classes_enrolled__in=classes).exclude(email='')
                  .values_list('email', flat=True).distinct())
    return Q(school_class__in=classes) | Q(assignment__recipients__pupil_email__in=emails)


@login_required
@require_GET
def activity_log_api(request):
    """Activity feed for the Activity page (and the old dashboard modal). Admins see
    everything; a regular teacher sees their own actions plus anything touching the classes
    they're assigned to. Filters: ?class=<id>, ?category=assignments|submissions|grading|classes,
    ?q=<text>, ?offset=, ?limit= (max 300)."""
    denied = _require_teacher(request)
    if denied:
        return denied
    if not request.user.can_view_activity:
        return HttpResponseForbidden('The activity log is only available to admins.')
    try:
        limit = max(1, min(int(request.GET.get('limit') or 100), 300))
        offset = max(0, int(request.GET.get('offset') or 0))
    except ValueError:
        limit, offset = 100, 0

    user = request.user
    visible_classes = user.visible_classes(getattr(request, 'school', None))
    entries = ActivityLogEntry.objects.select_related('assignment', 'school_class')
    if getattr(request, 'school', None):
        school_users_emails = User.objects.filter(school=request.school).exclude(email='').values_list('email', flat=True)
        entries = entries.filter(
            Q(assignment__school=request.school) |
            Q(school_class__school=request.school) |
            Q(actor_email__in=school_users_emails)
        )
    if not user.can_manage_classes:
        entries = entries.filter(_class_activity_q(visible_classes) | Q(actor_email__iexact=user.email) if user.email
                                 else _class_activity_q(visible_classes))

    class_id = request.GET.get('class')
    selected_class = None
    if class_id:
        selected_class = visible_classes.filter(id=class_id).first()
        if not selected_class:
            return HttpResponseForbidden("You don't have access to that class.")
        entries = entries.filter(_class_activity_q(SchoolClass.objects.filter(id=selected_class.id)))

    category = request.GET.get('category')
    if category in ACTIVITY_CATEGORIES:
        entries = entries.filter(action__in=ACTIVITY_CATEGORIES[category])
    query = (request.GET.get('q') or '').strip()
    if query:
        entries = entries.filter(Q(description__icontains=query) | Q(actor_name__icontains=query))

    entries = entries.distinct().order_by('-created_at', '-id')
    page = list(entries[offset:offset + limit + 1])
    has_more = len(page) > limit
    page = page[:limit]
    return JsonResponse({
        'entries': [
            {'id': e.id, 'actorName': e.actor_name, 'actorEmail': e.actor_email, 'action': e.action,
             'category': _activity_category(e.action), 'description': e.description,
             'assignmentId': e.assignment_id, 'assignmentTitle': e.assignment.title if e.assignment else None,
             'classId': e.school_class_id, 'className': e.school_class.name if e.school_class else None,
             'createdAt': e.created_at.isoformat()}
            for e in page
        ],
        'hasMore': has_more,
        'nextOffset': offset + len(page),
        'classes': [{'id': c.id, 'name': c.name} for c in visible_classes.order_by('name')],
        'selectedClass': {'id': selected_class.id, 'name': selected_class.name} if selected_class else None,
        'canManageClasses': user.can_manage_classes,
    })


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
    if getattr(request, 'school', None) and recipient.assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not is_real_pupil(request) and not can_manage_exam(request.user, recipient.assignment):
        return not_your_exam_response()
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
    if getattr(request, 'school', None) and recipient.assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not is_real_pupil(request) and not can_manage_exam(request.user, recipient.assignment):
        return not_your_exam_response()
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
    if getattr(request, 'school', None) and assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not is_real_pupil(request) and not can_manage_exam(request.user, assignment):
        return not_your_exam_response()
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
        return HttpResponseBadRequest('Missing exam.')
    assignment = get_object_or_404(Assignment, id=assignment_id)
    if getattr(request, 'school', None) and assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not is_real_pupil(request) and not can_manage_exam(request.user, assignment):
        return not_your_exam_response()
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


SEB_ENTER_PAGE = '''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Opening your exam…</title></head><body style="font-family:system-ui,sans-serif;padding:32px;color:#1f2328">
<p id="m">Opening your exam…</p>
<script>
(function(){
  var m = document.getElementById('m'), done = false;
  function fail(t){ if(!done){ done = true; m.textContent = t; } }
  function send(){
    if(done) return;
    var s = window.SafeExamBrowser && window.SafeExamBrowser.security;
    if(!s || !s.configKey){ fail('This link only works inside Safe Exam Browser. Go back to ExamHub and click Open again.'); return; }
    done = true;
    fetch(location.pathname, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: location.href.split('#')[0], configKey: s.configKey, browserExamKey: s.browserExamKey || ''})})
      .then(function(r){ return r.json(); })
      .then(function(d){ if(d.ok){ location.replace(d.redirect); } else { done = false; fail(d.detail || 'This exam link could not be verified.'); } })
      .catch(function(){ done = false; fail('Could not reach ExamHub. Check the connection and try again.'); });
  }
  var s = window.SafeExamBrowser && window.SafeExamBrowser.security;
  if(s && typeof s.updateKeys === 'function'){ try { s.updateKeys(send); } catch(e) {} setTimeout(send, 1500); }
  else { setTimeout(send, window.SafeExamBrowser ? 0 : 700); }
})();
</script></body></html>'''


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def seb_enter(request, token):
    """SEB's start page for a one-time launch: sign the pupil in and open their paper."""
    user, aid = read_seb_launch_token(token, require_unused=False)
    prefix = f'/s/{user.school.slug}' if (user and user.school) else ''
    open_url = lambda aid: f"{prefix}{reverse('home')}?open={aid}"
    # SEB reloading this page after it already signed the pupil in: just carry on.
    if (user and request.user.is_authenticated and request.user.pk == user.pk
            and request.session.get('seb_config_keys')):
        return JsonResponse({'ok': True, 'redirect': open_url(aid)}) if request.method == 'POST' else redirect(open_url(aid))
    user, aid = read_seb_launch_token(token)
    if not user and request.method == 'POST':
        return JsonResponse({'ok': False, 'detail': 'This exam link has expired or was already used. '
                                                     'Go back to ExamHub and click Open again.'}, status=403)
    if not user:
        return HttpResponse('<p style="font-family:sans-serif;padding:24px">This exam link has expired or was already used. '
                            f'<a href="{reverse("login")}">Sign in</a> and open the paper again.</p>', status=403)
    host = request.get_host()
    start_path = reverse('seb-enter', args=[token])
    token_keys = [seb_config_key(host, s, start_path) for s in ('https', 'http')]
    if request.method == 'POST':
        # The page below, inside a newer SEB that only exposes its keys to JavaScript.
        try:
            payload = json.loads(request.body or '{}')
        except json.JSONDecodeError:
            payload = {}
        if not seb_js_keys_ok(request, [payload.get('url')], [payload.get('configKey'), payload.get('browserExamKey')],
                              extra_keys=token_keys):
            return JsonResponse({'ok': False, 'detail': 'This link only works inside Safe Exam Browser, opened from ExamHub.'}, status=403)
        login(request, user, backend='django.contrib.auth.backends.ModelBackend')
        request.session['seb_config_keys'] = token_keys
        request.session['seb_js_verified'] = True
        return JsonResponse({'ok': True, 'redirect': open_url(aid)})
    if seb_lockdown_ok(request, extra_keys=token_keys):
        login(request, user, backend='django.contrib.auth.backends.ModelBackend')
        request.session['seb_config_keys'] = token_keys
        return redirect(open_url(aid))
    # No SEB headers on this request. Newer SEB versions only expose their keys to JavaScript,
    # so hand back a tiny page that reads them and posts them here; in any other browser it
    # just says the link only works in SEB.
    return HttpResponse(SEB_ENTER_PAGE)


@login_required
@require_POST
def seb_verify_api(request):
    """The exam page inside a newer SEB that doesn't send the hash headers on background requests:
    it posts SafeExamBrowser.security.configKey / .browserExamKey (hashes of the page URL) and,
    if they match, this SEB session counts as verified for the rest of the sign-in."""
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid data.')
    urls = payload.get('urls') if isinstance(payload.get('urls'), list) else [payload.get('url')]
    ok = seb_js_keys_ok(request, urls[:4], [payload.get('configKey'), payload.get('browserExamKey')])
    if ok:
        request.session['seb_js_verified'] = True
    return JsonResponse({'ok': ok})


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


# ---------------- Super Admin Management (Platform Superusers) ----------------

def _superadmin_check(request):
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next={request.path}")
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform super-administrator privileges required.")
    return None


@login_required
def superadmin_dashboard(request):
    """Super Admin Dashboard for managing multi-tenant schools, their administrators,
    and cross-tenant metrics."""
    check = _superadmin_check(request)
    if check:
        return check

    schools = School.objects.annotate(
        teacher_count=Count('users', filter=Q(users__role=User.Role.TEACHER), distinct=True),
        pupil_count=Count('users', filter=Q(users__role=User.Role.PUPIL), distinct=True),
        admin_count=Count('users', filter=Q(users__is_school_admin=True), distinct=True),
        class_count=Count('classes', distinct=True),
        assignment_count=Count('assignments', distinct=True),
    ).order_by('name')

    total_schools = schools.count()
    active_schools = schools.filter(is_active=True).count()
    total_teachers = User.objects.filter(role=User.Role.TEACHER, school__isnull=False).count()
    total_pupils = User.objects.filter(role=User.Role.PUPIL, school__isnull=False).count()
    total_assignments = Assignment.objects.count()

    context = {
        'schools': schools,
        'total_schools': total_schools,
        'active_schools': active_schools,
        'total_teachers': total_teachers,
        'total_pupils': total_pupils,
        'total_assignments': total_assignments,
    }
    return render(request, 'superadmin.html', context)


@login_required
@require_POST
def superadmin_create_school_api(request):
    """API endpoint to create a new School tenant and optionally an initial school admin."""
    check = _superadmin_check(request)
    if check:
        return check

    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest(json.dumps({'error': 'Invalid JSON format.'}), content_type='application/json')

    name = (data.get('name') or '').strip()
    slug = (data.get('slug') or '').strip().lower()
    primary_color = (data.get('primary_color') or '#2563eb').strip()
    tz = (data.get('timezone') or 'UTC').strip()

    if not name:
        return HttpResponseBadRequest(json.dumps({'error': 'School name is required.'}), content_type='application/json')

    if not slug:
        slug = slugify(name)

    if School.objects.filter(name=name).exists():
        return HttpResponseBadRequest(json.dumps({'error': f'A school named "{name}" already exists.'}), content_type='application/json')

    if School.objects.filter(slug=slug).exists():
        return HttpResponseBadRequest(json.dumps({'error': f'The slug "{slug}" is already in use.'}), content_type='application/json')

    school = School.objects.create(
        name=name,
        slug=slug,
        primary_color=primary_color,
        timezone=tz,
        is_active=True,
    )

    # Optional initial school admin
    admin_username = (data.get('admin_username') or '').strip()
    admin_password = data.get('admin_password') or ''
    admin_email = (data.get('admin_email') or '').strip()
    admin_full_name = (data.get('admin_full_name') or '').strip()

    if admin_username and admin_password:
        if User.objects.filter(username=admin_username).exists():
            return JsonResponse({'ok': True, 'school_id': school.id, 'warning': f'School created, but username "{admin_username}" already exists.'})

        parts = admin_full_name.split(' ', 1)
        first_name = parts[0] if parts else ''
        last_name = parts[1] if len(parts) > 1 else ''

        User.objects.create_user(must_change_password=True, 
            username=admin_username,
            email=admin_email,
            password=admin_password,
            first_name=first_name,
            last_name=last_name,
            role=User.Role.TEACHER,
            school=school,
            is_school_admin=True,
        )

    return JsonResponse({'ok': True, 'school_id': school.id, 'slug': school.slug})


@login_required
@require_POST
def superadmin_update_school_api(request, school_id):
    """API endpoint to update school details."""
    check = _superadmin_check(request)
    if check:
        return check

    school = get_object_or_404(School, id=school_id)
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest(json.dumps({'error': 'Invalid JSON format.'}), content_type='application/json')

    name = (data.get('name') or '').strip()
    slug = (data.get('slug') or '').strip().lower()
    primary_color = (data.get('primary_color') or '#2563eb').strip()
    tz = (data.get('timezone') or 'UTC').strip()
    is_active = data.get('is_active')

    if not name:
        return HttpResponseBadRequest(json.dumps({'error': 'School name is required.'}), content_type='application/json')
    if not slug:
        slug = slugify(name)

    if School.objects.filter(name=name).exclude(id=school.id).exists():
        return HttpResponseBadRequest(json.dumps({'error': f'Another school with name "{name}" already exists.'}), content_type='application/json')
    if School.objects.filter(slug=slug).exclude(id=school.id).exists():
        return HttpResponseBadRequest(json.dumps({'error': f'The slug "{slug}" is already in use.'}), content_type='application/json')

    school.name = name
    school.slug = slug
    school.primary_color = primary_color
    school.timezone = tz
    if is_active is not None:
        school.is_active = bool(is_active)
    school.save()

    return JsonResponse({'ok': True, 'school_id': school.id, 'slug': school.slug})


@login_required
@require_POST
def superadmin_toggle_school_api(request, school_id):
    """API endpoint to toggle school active status."""
    check = _superadmin_check(request)
    if check:
        return check

    school = get_object_or_404(School, id=school_id)
    school.is_active = not school.is_active
    school.save(update_fields=['is_active'])

    return JsonResponse({'ok': True, 'id': school.id, 'is_active': school.is_active})


@login_required
@require_GET
def superadmin_school_admins_api(request, school_id):
    """API endpoint to list all administrators for a specific school."""
    check = _superadmin_check(request)
    if check:
        return check

    school = get_object_or_404(School, id=school_id)
    admins = User.objects.filter(school=school, is_school_admin=True).order_by('username')
    return JsonResponse([
        {
            'id': a.id,
            'username': a.username,
            'email': a.email,
            'full_name': a.get_full_name(),
            'first_name': a.first_name,
            'last_name': a.last_name,
            'is_active': a.is_active,
        }
        for a in admins
    ], safe=False)


@login_required
@require_POST
def superadmin_update_school_admin_api(request, school_id, user_id):
    """Platform superadmin: change a school admin's first name, last name and email. A new email
    is also applied to the exams they created (exams are linked to their teacher by email)."""
    check = _superadmin_check(request)
    if check:
        return check
    school = get_object_or_404(School, id=school_id)
    admin_user = get_object_or_404(User, id=user_id, school=school, is_school_admin=True)
    err = lambda msg: HttpResponseBadRequest(json.dumps({'error': msg}), content_type='application/json')
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return err('Invalid JSON format.')
    first = (data.get('first_name') or '').strip()
    last = (data.get('last_name') or '').strip()
    email = (data.get('email') or '').strip()
    if not first or not email:
        return err('A first name and email are required.')
    if len(first) > 150 or len(last) > 150:
        return err('Names can be at most 150 characters.')
    from django.core.validators import validate_email
    try:
        validate_email(email)
    except DjangoValidationError:
        return err("That email address doesn't look right.")
    old_email = admin_user.email or ''
    if email.lower() != old_email.lower() and User.objects.filter(email__iexact=email).exclude(id=admin_user.id).exists():
        return err('Another account already uses that email.')
    admin_user.first_name, admin_user.last_name, admin_user.email = first, last, email
    admin_user.save(update_fields=['first_name', 'last_name', 'email'])
    new_name = admin_user.get_full_name() or admin_user.username
    if old_email:
        Assignment.objects.filter(school=school, teacher_email__iexact=old_email).update(teacher_email=email, teacher_name=new_name)
    return JsonResponse({'id': admin_user.id, 'username': admin_user.username, 'email': admin_user.email,
                         'full_name': admin_user.get_full_name(), 'first_name': first, 'last_name': last,
                         'is_active': admin_user.is_active})


@login_required
@require_POST
def superadmin_create_school_admin_api(request, school_id):
    """API endpoint to create a new School Admin for a specific school."""
    check = _superadmin_check(request)
    if check:
        return check

    school = get_object_or_404(School, id=school_id)
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest(json.dumps({'error': 'Invalid JSON format.'}), content_type='application/json')

    username = (data.get('username') or '').strip()
    email = (data.get('email') or '').strip()
    password = data.get('password') or ''
    full_name = (data.get('full_name') or '').strip()

    if not username or not password:
        return HttpResponseBadRequest(json.dumps({'error': 'Username and password are required.'}), content_type='application/json')

    if User.objects.filter(username=username).exists():
        return HttpResponseBadRequest(json.dumps({'error': f'Username "{username}" is already taken.'}), content_type='application/json')

    parts = full_name.split(' ', 1)
    first_name = parts[0] if parts else ''
    last_name = parts[1] if len(parts) > 1 else ''

    admin_user = User.objects.create_user(must_change_password=True, 
        username=username,
        email=email,
        password=password,
        first_name=first_name,
        last_name=last_name,
        role=User.Role.TEACHER,
        school=school,
        is_school_admin=True,
    )

    return JsonResponse({'ok': True, 'admin_id': admin_user.id, 'username': admin_user.username})

