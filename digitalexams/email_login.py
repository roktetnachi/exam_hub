"""Passwordless sign-in: a 6-digit code emailed to the account's address.

Works alongside the normal username + password sign-in (both stay available). A user who
has forgotten their password signs in with a code, then sets a new password from the
account menu without needing the old one (see OTP_PASSWORD_RESET_WINDOW)."""
import datetime
import logging
import secrets

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from .models import EmailLoginCode, User

logger = logging.getLogger(__name__)

CODE_LIFETIME = datetime.timedelta(minutes=getattr(settings, 'LOGIN_CODE_LIFETIME_MINUTES', 10))
RESEND_COOLDOWN = datetime.timedelta(seconds=getattr(settings, 'LOGIN_CODE_RESEND_SECONDS', 60))
MAX_ATTEMPTS = 5
# After signing in with an emailed code, the account may set a new password without typing
# the old one for this long. That is what replaces the "ask an admin to reset it" step.
OTP_PASSWORD_RESET_WINDOW = datetime.timedelta(minutes=15)
SESSION_PENDING_USER = 'otp_pending_user_id'
SESSION_OTP_LOGIN_AT = 'otp_login_at'


def _hash(user, code):
    return salted_hmac('examhub.email-login-code', f'{user.pk}:{code}').hexdigest()


def find_user(identifier, school=None):
    """The active account matching a username or email, or None. Scoped to the school in
    the URL when there is one, so a code is never sent for another school's account."""
    identifier = (identifier or '').strip()
    if not identifier:
        return None
    qs = User.objects.filter(Q(username__iexact=identifier) | Q(email__iexact=identifier), is_active=True)
    if school is not None:
        qs = qs.filter(school=school)
    matches = list(qs[:2])
    if len(matches) != 1:
        return None
    user = matches[0]
    if not user.email or (user.school and not user.school.is_active):
        return None
    return user


def issue_code(user):
    """Create and email a fresh code. Returns 'sent', 'cooldown' or 'failed'.
    Any earlier unused codes for this account stop working."""
    now = timezone.now()
    latest = user.login_codes.first()
    if latest and now - latest.created_at < RESEND_COOLDOWN:
        return 'cooldown'
    code = f'{secrets.randbelow(1_000_000):06d}'
    user.login_codes.filter(used_at__isnull=True).update(used_at=now)
    EmailLoginCode.objects.create(user=user, code_hash=_hash(user, code), expires_at=now + CODE_LIFETIME)
    school_name = user.school.name if user.school else 'ExamHub'
    minutes = int(CODE_LIFETIME.total_seconds() // 60)
    try:
        send_mail(
            subject=f'Your {school_name} sign-in code: {code}',
            message=(f'Hi {user.get_short_name() or user.username},\n\n'
                     f'Your sign-in code is: {code}\n\n'
                     f'It expires in {minutes} minutes and can only be used once.\n'
                     f"If you didn't try to sign in, you can ignore this email.\n\n"
                     f'— {school_name} ExamHub'),
            from_email=None,
            recipient_list=[user.email],
        )
    except Exception:
        logger.exception('Could not send sign-in code to user %s', user.pk)
        user.login_codes.filter(used_at__isnull=True).update(used_at=now)
        return 'failed'
    return 'sent'


def verify_code(user, code):
    """True if `code` is the account's current, unexpired, unused code. Marks it used."""
    code = ''.join(ch for ch in (code or '') if ch.isdigit())
    now = timezone.now()
    latest = user.login_codes.filter(used_at__isnull=True, expires_at__gt=now).first()
    if latest is None or latest.attempts >= MAX_ATTEMPTS or len(code) != 6:
        if latest is not None:
            latest.attempts += 1
            latest.save(update_fields=['attempts'])
        return False
    if not constant_time_compare(latest.code_hash, _hash(user, code)):
        latest.attempts += 1
        latest.save(update_fields=['attempts'])
        return False
    latest.used_at = now
    latest.save(update_fields=['used_at'])
    return True


def mark_otp_login(request):
    request.session[SESSION_OTP_LOGIN_AT] = timezone.now().isoformat()


def can_skip_current_password(request):
    """True shortly after signing in with an emailed code (proof of owning the email)."""
    stamp = request.session.get(SESSION_OTP_LOGIN_AT)
    if not stamp:
        return False
    try:
        signed_in_at = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return False
    return timezone.now() - signed_in_at < OTP_PASSWORD_RESET_WINDOW
