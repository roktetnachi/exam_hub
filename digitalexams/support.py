"""Starter guide and Help chat.

* ``/guide/`` — the starter guide for school admins, teachers and pupils (templates/guide.html).
  Public, so a school thinking about Andam can read it before it has accounts; signed-in
  people get their own role's tab first and a link back to their school.
* ``POST /support/message/`` — the Help chat's "Message support" step
  (templates/_support_chat.html). Saves a SupportRequest and emails it to SUPPORT_EMAIL with
  Reply-To set to the sender, so answering from that inbox goes straight back to them.

Both paths are in TENANT_EXEMPT_PATHS (settings.py): they work with or without /s/<school>/.
"""
import logging

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from .models import SupportRequest

logger = logging.getLogger(__name__)

GUIDE_ROLES = ('admin', 'teacher', 'pupil')
MAX_MESSAGE = 4000
MIN_MESSAGE = 5


def _guide_role_for(user):
    """Which guide tab a signed-in person sees first."""
    if not user.is_authenticated:
        return 'admin'
    role = getattr(user, 'tour_role', '') or ''
    if role in ('main_admin', 'admin'):
        return 'admin'
    return 'pupil' if role == 'pupil' else 'teacher'


def guide_page(request):
    from .views import _school_home_url  # local import: views imports a lot
    user = request.user
    role = request.GET.get('role', '')
    if role not in GUIDE_ROLES:
        role = _guide_role_for(user)
    home_url = _school_home_url(user) if user.is_authenticated else ''
    contact = (getattr(settings, 'CONTACT_EMAIL', '') or '').strip()
    return render(request, 'guide.html', {
        'guide_role': role,
        'home_url': home_url,
        'support_email': settings.SUPPORT_EMAIL,
        'contact_email': contact,
    })


def _client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '') or 'unknown'


def _one_line(value, limit):
    """Trimmed, single-line, length-limited text (safe for an email subject)."""
    return ' '.join(str(value or '').split())[:limit]


def _payload(request):
    if request.content_type == 'application/json':
        import json
        try:
            data = json.loads(request.body or b'{}')
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None
    return request.POST


@require_POST
def support_message_api(request):
    data = _payload(request)
    if data is None:
        return JsonResponse({'error': 'That message could not be read. Please try again.'}, status=400)

    # Bots fill in every box, including this one that people never see.
    if str(data.get('website', '') or '').strip():
        return JsonResponse({'ok': True, 'reference': '', 'emailed': True})

    user = request.user if request.user.is_authenticated else None
    name = _one_line(data.get('name'), 120) or (_one_line(user.get_full_name() or user.username, 120) if user else '')
    email = _one_line(data.get('email'), 254) or (user.email if user and user.email else '')
    role = _one_line(data.get('role'), 40)
    topic = str(data.get('topic') or '')
    if topic not in SupportRequest.Topic.values:
        topic = SupportRequest.Topic.OTHER
    message = str(data.get('message') or '').strip()
    page_url = _one_line(data.get('page'), 300)

    errors = {}
    if not name:
        errors['name'] = 'Please tell us your name.'
    try:
        validate_email(email)
    except ValidationError:
        errors['email'] = 'Please give an email address we can reply to.'
    if len(message) < MIN_MESSAGE:
        errors['message'] = 'Please write a little more about what you need.'
    elif len(message) > MAX_MESSAGE:
        errors['message'] = f'Please keep it under {MAX_MESSAGE} characters.'
    if errors:
        return JsonResponse({'error': next(iter(errors.values())), 'fields': errors}, status=400)

    limit = getattr(settings, 'SUPPORT_MESSAGES_PER_HOUR', 6)
    key = f'support-msg:{"u" + str(user.pk) if user else "ip" + _client_ip(request)}'
    sent = cache.get(key, 0)
    if sent >= limit:
        return JsonResponse({'error': 'You have sent a few messages already — we will reply to those first. '
                                      f'If it is urgent, email {settings.SUPPORT_EMAIL}.'}, status=429)
    cache.set(key, sent + 1, 60 * 60)

    school = getattr(request, 'school', None) or (user.school if user else None)
    if not role and user:
        role = {'main_admin': 'main admin', 'admin': 'admin'}.get(user.tour_role, user.tour_role)
    req = SupportRequest.objects.create(
        name=name, email=email, role=role, topic=topic, message=message, page_url=page_url,
        user_agent=_one_line(request.META.get('HTTP_USER_AGENT'), 300), user=user, school=school)

    req.email_sent = _email_support(req)
    req.save(update_fields=['email_sent'])
    return JsonResponse({'ok': True, 'reference': req.reference, 'emailed': req.email_sent, 'replyTo': email})


def _email_support(req):
    topic = req.get_topic_display()
    subject = f'[Andam support] {req.reference} · {topic} · {req.name}'
    if req.school:
        subject += f' ({req.school.name})'
    lines = [
        f'Reference: {req.reference}',
        f'From:      {req.name} <{req.email}>',
        f'Role:      {req.role or "—"}',
        f'School:    {req.school.name if req.school else "— (not signed in)"}',
        f'Account:   {req.user.username if req.user else "— (not signed in)"}',
        f'Topic:     {topic}',
        f'Page:      {req.page_url or "—"}',
        f'Browser:   {req.user_agent or "—"}',
        '',
        req.message,
        '',
        '—',
        'Sent from the Andam Help chat. Reply to this email to answer them directly.',
        'All messages are also listed in Django admin → Support requests.',
    ]
    mail = EmailMessage(subject=_one_line(subject, 200), body='\n'.join(lines),
                        from_email=settings.DEFAULT_FROM_EMAIL, to=[settings.SUPPORT_EMAIL],
                        reply_to=[req.email])
    try:
        mail.send(fail_silently=False)
        return True
    except Exception:  # noqa: BLE001 — never lose the message over a mail problem
        logger.exception('Support email %s could not be sent', req.reference)
        return False
