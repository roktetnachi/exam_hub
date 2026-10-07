"""Account activation: invites (single or bulk), the forced "choose your password" step on
first sign-in, and the people lists the Roles page shows.

Flow
----
1. An admin adds someone (one at a time or by bulk import). The account gets a temporary
   password and must_change_password=True.
2. The admin invites them (or ticks "send invite" while adding): ExamHub generates a fresh
   temporary password and emails their username, that password and the sign-in link. The
   same details can be downloaded as a CSV in case email isn't set up.
3. On first sign-in ForcePasswordChangeMiddleware sends them to "Choose your password"
   before anything else. Once they've set it, the account is active and the welcome tour
   starts on the next page.
"""
import json
import logging
import os
import secrets

from django.conf import settings
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db.models import Q
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import User

logger = logging.getLogger(__name__)


def _is_admin(user):
    return user.is_superuser or user.is_school_admin


def temp_password():
    # Easy to read out or type: no 0/O/1/l look-alikes.
    alphabet = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(10))


# The public address of ExamHub, used in invites so the link always works — even when the
# invite was sent from a laptop running the site locally. Override with SITE_URL (env or
# settings) if the site moves to another domain.
DEFAULT_SITE_URL = 'https://digitalexamhub.up.railway.app'


def site_url():
    return (os.environ.get('SITE_URL') or getattr(settings, 'SITE_URL', '') or DEFAULT_SITE_URL).rstrip('/')


def login_url(request, user):
    prefix = f'/s/{user.school.slug}' if user.school else (getattr(request, 'school_url_prefix', '') or '')
    who = 'pupil' if user.role == User.Role.PUPIL and not user.is_superuser else 'teacher'
    return f'{site_url()}{prefix}/login/?as={who}'


def invite_email_body(user, school, password):
    site = site_url() + '/'
    return (f'Hi {user.first_name or user.username},\n\n'
            f'Here\'s your ExamHub account at {school}.\n\n'
            f'Username: {user.username}\n'
            f'Temporary password: {password}\n\n'
            f'Changing your password\n'
            f'1. Go to the ExamHub website (link below) and sign in with your username and temporary password.\n'
            f'2. You\'ll be asked to choose your own password straight away.\n'
            f'3. You can change it again any time: click your name at the top right, then "Change password".\n\n'
            f'Forgot your password?\n'
            f'On the sign-in page, choose "Email me a code" and enter your username. We\'ll send a code to {user.email}. '
            f'Sign in with that code, then click your name at the top right and choose "Change password" to set a new one.\n\n'
            f'Website: {site}\n\n'
            f'— {school} ExamHub')


def invite_user(request, user):
    """Give `user` a fresh temporary password, mark it for change on first sign-in and email
    them how to sign in. Returns (credentials_dict, emailed: bool)."""
    password = temp_password()
    user.set_password(password)
    user.must_change_password = True
    user.invited_at = timezone.now()
    user.activated_at = None
    user.save(update_fields=['password', 'must_change_password', 'invited_at', 'activated_at'])
    school = user.school.name if user.school else 'ExamHub'
    url = login_url(request, user)
    emailed = False
    if user.email:
        try:
            send_mail(
                subject=f'Your ExamHub account at {school}',
                message=invite_email_body(user, school, password),
                from_email=None, recipient_list=[user.email])
            emailed = True
        except Exception:
            logger.exception('Invite email to user %s failed', user.pk)
    return ({'id': user.id, 'name': user.get_full_name() or user.username, 'firstName': user.first_name,
             'lastName': user.last_name, 'email': user.email,
             'username': user.username, 'tempPassword': password, 'loginUrl': url,
             'website': site_url() + '/',
             'role': 'pupil' if user.role == User.Role.PUPIL else ('admin' if user.can_manage_classes else 'teacher')},
            emailed)


def invite_many(request, users):
    sent, failed, credentials = [], [], []
    for user in users:
        cred, emailed = invite_user(request, user)
        credentials.append(cred)
        (sent if emailed else failed).append(cred['id'])
    return {'sent': len(sent), 'failed': failed, 'credentials': credentials,
            'invitedAt': timezone.now().isoformat()}


@login_required
@require_POST
def invite_api(request):
    """POST {userIds: [...]} or {all: 'pupils'|'staff', onlyNotInvited: true}. Admins only."""
    if not _is_admin(request.user):
        return HttpResponseForbidden('Only admins can send invites.')
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid request.')
    qs = User.objects.filter(is_active=True, is_superuser=False)
    school = getattr(request, 'school', None)
    if school:
        qs = qs.filter(school=school)
    if payload.get('all') in ('pupils', 'staff'):
        qs = qs.filter(role=User.Role.PUPIL if payload['all'] == 'pupils' else User.Role.TEACHER)
        if payload.get('onlyNotInvited', True):
            qs = qs.filter(invited_at__isnull=True, activated_at__isnull=True).filter(
                Q(last_login__isnull=True) | Q(must_change_password=True))
    else:
        ids = [int(i) for i in (payload.get('userIds') or []) if str(i).isdigit()]
        if not ids:
            return HttpResponseBadRequest('Choose who to invite.')
        qs = qs.filter(id__in=ids)
    users = list(qs.exclude(id=request.user.id).select_related('school'))
    if not users:
        return JsonResponse({'sent': 0, 'failed': [], 'credentials': [], 'note': 'Nobody to invite.'})
    result = invite_many(request, users)
    from .views import log_activity
    log_activity(request.user.get_full_name() or request.user.username, request.user.email, 'invited',
                 f'Sent account invites to {len(users)} {"person" if len(users) == 1 else "people"}.')
    return JsonResponse(result)


# ---------------- forced password change on first sign-in ----------------

ALLOWED_WHILE_PENDING = ('/welcome/password/', '/logout/', '/login/', '/static/', '/media/', '/admin/',
                         '/api/notifications/')


class ForcePasswordChangeMiddleware:
    """Anyone signed in with a temporary password can only reach the "choose your password"
    page (and sign out) until they've set their own. Pages redirect there; API calls get a
    clear 403 so nothing half-loads behind it."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated and user.must_change_password:
            path = request.path_info
            if not any(path.startswith(p) for p in ALLOWED_WHILE_PENDING) and not path.startswith('/superadmin/'):
                if path.startswith('/api/'):
                    return JsonResponse({'code': 'password_change_required',
                                         'detail': 'Choose your own password first.'}, status=403)
                prefix = getattr(request, 'school_url_prefix', '') or (f'/s/{user.school.slug}' if user.school else '')
                return redirect(f'{prefix}/welcome/password/')
        return self.get_response(request)


@login_required
def set_first_password_view(request):
    user = request.user
    prefix = getattr(request, 'school_url_prefix', '') or (f'/s/{user.school.slug}' if user.school else '')
    if not user.must_change_password:
        return redirect(f'{prefix}/' if prefix else '/')
    error = ''
    if request.method == 'POST':
        new = request.POST.get('password1') or ''
        again = request.POST.get('password2') or ''
        if not new:
            error = 'Choose a password.'
        elif new != again:
            error = "The two passwords don't match."
        elif user.check_password(new):
            error = 'Choose a new password — not the temporary one you were given.'
        else:
            try:
                validate_password(new, user=user)
            except ValidationError as exc:
                error = ' '.join(exc.messages)
        if not error:
            user.set_password(new)
            user.must_change_password = False
            user.activated_at = timezone.now()
            user.save(update_fields=['password', 'must_change_password', 'activated_at'])
            update_session_auth_hash(request, user)
            # tour_completed_at is still empty, so the welcome tour starts on this next page.
            return redirect(f'{prefix}/' if prefix else '/')
    return render(request, 'set_password.html', {'error': error, 'first_name': user.first_name or user.username})


# ---------------- people lists for the Roles page ----------------

def serialize_person(u):
    return {'id': u.id, 'name': u.get_full_name() or u.username, 'firstName': u.first_name, 'lastName': u.last_name,
            'username': u.username, 'email': u.email, 'isActive': u.is_active,
            'status': u.account_status,
            'invitedAt': u.invited_at.isoformat() if u.invited_at else None,
            'lastLogin': u.last_login.isoformat() if u.last_login else None}


@login_required
@require_GET
def pupils_admin_api(request):
    """Every pupil account in the school with classes and activation status (admins only)."""
    if not _is_admin(request.user):
        return HttpResponseForbidden('Only admins can manage pupil accounts.')
    qs = User.objects.filter(role=User.Role.PUPIL, is_superuser=False)
    school = getattr(request, 'school', None)
    if school:
        qs = qs.filter(school=school)
    qs = qs.prefetch_related('classes_enrolled', 'subjects_enrolled').order_by('first_name', 'last_name', 'username')
    out = []
    for u in qs:
        d = serialize_person(u)
        d['classes'] = [c.name for c in u.classes_enrolled.all()]
        d['subjects'] = [s.name for s in u.subjects_enrolled.all()]
        out.append(d)
    return JsonResponse({'pupils': out})


# ---------------- edit / delete people (Roles page, admins only) ----------------

def _school_people(request):
    qs = User.objects.filter(is_superuser=False)
    school = getattr(request, 'school', None)
    return qs.filter(school=school) if school else qs


@login_required
@require_POST
def update_person_api(request, user_id):
    """Edit a staff member's or pupil's first name, last name and email. A new email is also
    applied to the exams they're on (pupils are matched to exams by email; staff own exams by
    email), so nothing gets orphaned by the change."""
    if not _is_admin(request.user):
        return HttpResponseForbidden('Only admins can edit accounts.')
    from django.core.validators import validate_email
    from .models import Assignment, Recipient
    from .views import log_activity
    target = _school_people(request).filter(id=user_id).first()
    if target is None:
        return HttpResponseForbidden("You can't edit that account here.")
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid request.')
    first = (payload.get('firstName') or '').strip()
    last = (payload.get('lastName') or '').strip()
    email = (payload.get('email') or '').strip()
    if not first or not email:
        return HttpResponseBadRequest('A first name and email are required.')
    if len(first) > 150 or len(last) > 150:
        return HttpResponseBadRequest('Names can be at most 150 characters.')
    try:
        validate_email(email)
    except ValidationError:
        return HttpResponseBadRequest("That email address doesn't look right.")
    # Only check for a clash when the email is actually being changed: editing just the name must
    # always work, even if some other (e.g. old test) account happens to share this email.
    email_changed = email.lower() != (target.email or '').lower()
    clash = User.objects.filter(email__iexact=email).exclude(id=target.id)
    if target.role == User.Role.PUPIL and target.school_id:
        clash = clash.filter(school_id=target.school_id)   # pupils: unique within their school only
    if email_changed and clash.exists():
        return HttpResponseBadRequest('Another account already uses that email.')
    old_email, old_name = target.email, target.get_full_name() or target.username
    target.first_name, target.last_name, target.email = first, last, email
    target.save(update_fields=['first_name', 'last_name', 'email'])
    new_name = target.get_full_name() or target.username
    school = getattr(request, 'school', None)
    if target.role == User.Role.PUPIL:
        recips = Recipient.objects.filter(pupil_email__iexact=old_email)
        if school:
            recips = recips.filter(assignment__school=school)
        recips.update(pupil_email=email, pupil_name=new_name)
    else:
        exams = Assignment.objects.filter(teacher_email__iexact=old_email)
        if school:
            exams = exams.filter(school=school)
        exams.update(teacher_email=email, teacher_name=new_name)
    changes = []
    if old_name != new_name:
        changes.append(f'name "{old_name}" → "{new_name}"')
    if (old_email or '').lower() != email.lower():
        changes.append(f'email {old_email} → {email}')
    if changes:
        log_activity(request.user.get_full_name() or request.user.username, request.user.email, 'account_updated',
                     f'Updated {new_name}: ' + ', '.join(changes) + '.')
    data = serialize_person(target)
    data['classes'] = [c.name for c in (target.classes_enrolled.all() if target.role == User.Role.PUPIL else target.classes_taught.all())]
    return JsonResponse(data)


@login_required
@require_POST
def delete_pupils_api(request):
    """Delete one or more pupil accounts (admins only). Their sign-in and class places go;
    the work they already handed in stays on the exams they sat, so marks aren't lost."""
    if not _is_admin(request.user):
        return HttpResponseForbidden('Only admins can delete pupil accounts.')
    from .views import log_activity
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid request.')
    ids = [int(i) for i in (payload.get('userIds') or []) if str(i).isdigit()]
    if not ids:
        return HttpResponseBadRequest('Choose the pupils to delete.')
    pupils = list(_school_people(request).filter(id__in=ids, role=User.Role.PUPIL))
    if len(pupils) != len(set(ids)):
        return HttpResponseBadRequest('Only pupil accounts from this school can be deleted here.')
    names = [p.get_full_name() or p.username for p in pupils]
    User.objects.filter(id__in=[p.id for p in pupils]).delete()
    log_activity(request.user.get_full_name() or request.user.username, request.user.email, 'pupil_deleted',
                 f'Deleted {len(names)} pupil account{"s" if len(names) != 1 else ""}: '
                 + ', '.join(names[:10]) + ('…' if len(names) > 10 else '') + '.')
    return JsonResponse({'deleted': len(names)})


@login_required
@require_POST
def delete_staff_api(request):
    """Delete one or more teacher/admin accounts (admins only). You can't delete yourself or a
    main admin (superuser), so the school always keeps at least one admin. Exams they created
    stay (they're linked by name/email, not by account) and admins can still open and grade them."""
    if not _is_admin(request.user):
        return HttpResponseForbidden('Only admins can delete staff accounts.')
    from .views import log_activity
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid request.')
    ids = {int(i) for i in (payload.get('userIds') or []) if str(i).isdigit()}
    if not ids:
        return HttpResponseBadRequest('Choose the staff to delete.')
    if request.user.id in ids:
        return HttpResponseBadRequest('You can\'t delete your own account.')
    staff = list(_school_people(request).filter(id__in=ids, role=User.Role.TEACHER))
    if len(staff) != len(ids):
        return HttpResponseBadRequest('Only teacher and admin accounts from this school can be deleted here '
                                      '(main admins are managed in Django admin).')
    names = [f'{t.get_full_name() or t.username}{" (admin)" if t.is_school_admin else ""}' for t in staff]
    User.objects.filter(id__in=[t.id for t in staff]).delete()
    log_activity(request.user.get_full_name() or request.user.username, request.user.email, 'teacher_deleted',
                 f'Deleted {len(names)} staff account{"s" if len(names) != 1 else ""}: '
                 + ', '.join(names[:10]) + ('…' if len(names) > 10 else '') + '.')
    return JsonResponse({'deleted': len(names)})
