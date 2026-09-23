import hashlib
import hmac
import json
import mimetypes
import plistlib
import secrets
import string

from django.core.mail import send_mail
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.http import FileResponse, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import (ActivityLogEntry, AllowedBrowserExamKey, Assignment, AssignmentItem, Message, Recipient,
    SchoolClass, Submission, User)


@login_required
def home(request):
    return render(request, 'examhub.html')


def login_view(request):
    if request.user.is_authenticated:
        return redirect('home')
    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == 'POST' and form.is_valid():
        login(request, form.get_user())
        return redirect(request.GET.get('next') or 'home')
    return render(request, 'login.html', {'form': form})


@require_POST
def logout_view(request):
    logout(request)
    return redirect('login')


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


def seb_lockdown_ok(request):
    """True if this request carries a valid Browser Exam Key hash for any key we've been
    given. There's deliberately no soft fallback (User-Agent, etc.) here — those are shown
    client-side as a friendly hint, but this is the actual enforcement."""
    received = request.META.get('HTTP_X_SAFEEXAMBROWSER_REQUESTHASH', '') or request.headers.get(SEB_REQUEST_HASH_HEADER, '')
    if not received:
        return False
    absolute_url = request.build_absolute_uri()
    for key_hash in AllowedBrowserExamKey.objects.values_list('key_hash', flat=True):
        expected = hashlib.sha256((absolute_url + key_hash).encode('utf-8')).hexdigest()
        if hmac.compare_digest(expected, received.strip().lower()):
            return True
    return False


def seb_required_response():
    return JsonResponse({
        'code': 'seb_required',
        'detail': 'This assignment must be opened in Safe Exam Browser.',
    }, status=403)


def is_real_pupil(request):
    """A genuinely-restricted pupil account, as opposed to a teacher or a superuser test
    account that happens to also be flagged role=pupil for convenience while testing."""
    return request.user.role == User.Role.PUPIL and not request.user.is_superuser


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


def serialize_assignment(assignment, pupil_email=None):
    is_teacher_view = pupil_email is None
    recipients = []
    for recipient in assignment.recipients.all():
        sub, _ = Submission.objects.get_or_create(recipient=recipient)
        # A pupil only ever sees scores once the teacher has explicitly published them —
        # being graded is not the same as being told.
        show_grades = is_teacher_view or sub.published
        recipients.append({'id': recipient.id, 'name': recipient.pupil_name, 'email': recipient.pupil_email,
            'status': sub.status, 'submittedAt': sub.submitted_at.isoformat() if sub.submitted_at else None,
            'startedAt': sub.started_at.isoformat() if sub.started_at else None,
            'annotations': sub.annotations, 'outlineHtml': sub.outline_html, 'responses': sub.responses,
            'extraMinutes': recipient.extra_minutes,
            'opensOverride': recipient.opens_override.isoformat() if recipient.opens_override else None,
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
        'opensAt': assignment.opens_at.isoformat(), 'dueAt': assignment.due_at.isoformat() if assignment.due_at else None,
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
    qs = Assignment.objects.prefetch_related('recipients', 'items').all()
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
        return seb_required_response()
    filename = item.pdf.name.rsplit('/', 1)[-1]
    response = FileResponse(item.pdf.open('rb'), content_type='application/pdf', as_attachment=False, filename=filename)
    response['Content-Disposition'] = f'inline; filename="{filename}"'
    return response


def _serve_item_media(request, item_id, item_type, field_name):
    """Shared by item_image/item_video: same ownership + SEB gating as item_pdf, just serving
    whichever field the item type uses and guessing its content-type from the filename."""
    item = get_object_or_404(AssignmentItem, id=item_id, item_type=item_type)
    assignment = item.assignment
    if request.user.role == User.Role.PUPIL and not request.user.is_superuser:
        if not assignment.recipients.filter(pupil_email__iexact=request.user.email).exists():
            return HttpResponseForbidden('This file is not assigned to your account.')
    if assignment.delivery_mode == Assignment.DeliveryMode.LOCKDOWN and is_real_pupil(request) and not seb_lockdown_ok(request):
        return seb_required_response()
    field = getattr(item, field_name)
    filename = field.name.rsplit('/', 1)[-1]
    content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    response = FileResponse(field.open('rb'), content_type=content_type, as_attachment=False, filename=filename)
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
    for recipient in assignment.recipients.all():
        sub, _ = Submission.objects.get_or_create(recipient=recipient)
        recipients.append({
            'id': recipient.id, 'name': recipient.pupil_name, 'email': recipient.pupil_email,
            'status': sub.status,
            'startedAt': sub.started_at.isoformat() if sub.started_at else None,
            'submittedAt': sub.submitted_at.isoformat() if sub.submitted_at else None,
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
    pupils = User.objects.filter(role=User.Role.PUPIL).order_by('first_name', 'last_name', 'username')
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
    due_at = request.POST.get('due_at') or None

    assignment = Assignment.objects.create(title=request.POST['title'].strip(), instructions=request.POST.get('instructions', '').strip(),
        teacher_name=teacher_name, teacher_email=teacher_email,
        opens_at=request.POST.get('opens_at') or timezone.now(), due_at=due_at, duration_minutes=duration_minutes,
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
        assignment.opens_at = payload['opensAt']
    assignment.due_at = payload.get('dueAt') or None
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
    # Re-fetch from the DB: assigning raw strings to opens_at/due_at above leaves those
    # attributes as plain strings on this in-memory instance until reloaded, and
    # serialize_assignment() calls .isoformat() on them.
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
        return seb_required_response()
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
        return None, seb_required_response()
    payload = json.loads(request.body or '{}')
    submission, _ = Submission.objects.get_or_create(recipient=recipient)
    if submission.status == 'submitted':
        return None, JsonResponse({'detail': 'This work has already been submitted.'}, status=409)
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
        pupils = User.objects.filter(id__in=pupil_ids, role=User.Role.PUPIL)
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
        pupils = User.objects.filter(id__in=payload.get('pupilIds') or [], role=User.Role.PUPIL)
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
    recipient.opens_override = payload.get('opensOverride') or None
    recipient.save()
    # Same gotcha as assignment opens_at/due_at: refetch so opens_override is a real
    # datetime (not the raw string just assigned) before we .isoformat() it below.
    recipient.refresh_from_db()
    log_activity(request.user.get_full_name() or request.user.username, request.user.email,
        'accommodation',
        f'Set accommodations for {recipient.pupil_name} on "{recipient.assignment.title}" '
        f'(+{extra_minutes} min{", early access" if recipient.opens_override else ""}).',
        assignment=recipient.assignment)
    return JsonResponse({
        'id': recipient.id,
        'extraMinutes': recipient.extra_minutes,
        'opensOverride': recipient.opens_override.isoformat() if recipient.opens_override else None,
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
    return JsonResponse({'messages': [
        {'id': m.id, 'senderRole': m.sender_role, 'senderName': m.sender_name, 'text': m.text,
         'createdAt': m.created_at.isoformat()}
        for m in recipient.messages.all()
    ]})


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
    return JsonResponse({'id': message.id, 'senderRole': message.sender_role,
        'senderName': message.sender_name, 'text': message.text,
        'createdAt': message.created_at.isoformat()}, status=201)


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
        return HttpResponseBadRequest('Grade this submission before publishing it.')
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
    start_url = request.build_absolute_uri(reverse('home'))
    quit_url = request.build_absolute_uri(reverse('home')) + '?sebquit=1'
    settings_dict = {
        'startURL': start_url,
        'quitURL': quit_url,
        'allowQuit': True,
        'quitURLConfirm': False,
        'hashedQuitPassword': '',
        # Required for SEB to actually send the X-SafeExamBrowser-RequestHash header that
        # seb_lockdown_ok() checks — without this, the whole mechanism silently never fires.
        'sendBrowserExamKey': True,
    }
    body = plistlib.dumps(settings_dict, fmt=plistlib.FMT_XML)
    response = HttpResponse(body, content_type='application/seb')
    response['Content-Disposition'] = 'attachment; filename="examhub-lockdown.seb"'
    return response
