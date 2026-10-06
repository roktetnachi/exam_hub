"""Bulk import of classes (with their teachers), teachers and pupils from a spreadsheet.

Classes are imported on the Classes page; teachers and pupils on the Roles page.
3. Teachers — one row per staff member: first/last name, email, role (Teacher/Admin) and,
              optionally, the existing class(es) they teach. New accounts get a temporary
              password; optionally an invite email goes out straight away.

Admins fill in a downloadable .xlsx template (or export their own Excel / Google Sheet as
.xlsx or .csv) and upload it on the Classes page. Two separate imports, run in order:

1. Classes  — one row per class: class name, teacher email(s), optional "rename to".
              Creates new classes and updates the teachers / name of existing ones.
2. Pupils   — one row per pupil-in-a-class: first name, last name, email, class.
              Creates new pupil accounts (with a temporary password) and enrols new or
              existing pupils into classes. The class MUST already exist — a row naming an
              unknown class is reported as an error rather than silently creating it.

Every upload is first checked without saving anything (a "preview"); the admin then
confirms, which re-reads the same file and applies the valid rows in one transaction.
"""
import csv
import io
import re
import secrets

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Count
from django.db.models.functions import Lower
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import SchoolClass, User
from .views import _require_class_admin, _unique_username, assignable_teachers, log_activity

KINDS = ('classes', 'pupils', 'teachers')
MAX_ROWS = 3000
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
CLEAR_TEACHERS_WORDS = {'none', '-', 'no teacher', 'clear'}

# Column headers we recognise, per import. Matching ignores case, punctuation, anything in
# brackets and the words "required"/"optional" — so "Class name *", "CLASS", "Section" and
# "Teacher email(s)" all work, and an existing school spreadsheet usually needs no renaming.
HEADER_ALIASES = {
    'classes': {
        'class': ['class', 'class name', 'name', 'section', 'class section', 'homeroom', 'group'],
        'teachers': ['teacher', 'teachers', 'teacher email', 'teacher emails', 'teacher username',
                     'teachers emails', 'adviser', 'adviser email', 'class teacher', 'class teacher email'],
        'rename_to': ['rename to', 'new name', 'new class name', 'rename'],
    },
    'pupils': {
        'first_name': ['first name', 'firstname', 'given name', 'given names', 'forename'],
        'last_name': ['last name', 'lastname', 'surname', 'family name'],
        'full_name': ['name', 'full name', 'pupil name', 'student name', 'pupil', 'student'],
        'email': ['email', 'email address', 'e mail', 'pupil email', 'student email', 'school email'],
        'class': ['class', 'class name', 'classes', 'section', 'class section', 'homeroom', 'group'],
    },
    'teachers': {
        'first_name': ['first name', 'firstname', 'given name', 'given names', 'forename'],
        'last_name': ['last name', 'lastname', 'surname', 'family name'],
        'full_name': ['name', 'full name', 'teacher name', 'staff name', 'teacher'],
        'email': ['email', 'email address', 'e mail', 'teacher email', 'staff email', 'school email'],
        'role': ['role', 'account type', 'access', 'admin', 'type'],
        'class': ['class', 'classes', 'class name', 'classes taught', 'teaches', 'section', 'sections'],
    },
}
REQUIRED_COLUMNS = {'classes': ['class'], 'pupils': ['email'], 'teachers': ['email']}
ADMIN_WORDS = {'admin', 'administrator', 'school admin', 'yes', 'y', 'true'}
TEACHER_WORDS = {'teacher', 'staff', 'no', 'n', 'false', ''}


def _norm_header(value):
    text = re.sub(r'\(.*?\)|\[.*?\]', ' ', str(value or '').lower())
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    text = re.sub(r'\b(required|optional)\b', ' ', text)
    return ' '.join(text.split())


def _norm_name(value):
    """Class-name key: case-insensitive, whitespace-collapsed, and treating the different
    dashes people type ("-", "–", "—") as the same character."""
    text = re.sub(r'[‐-―]', '-', str(value or ''))
    return ' '.join(text.split()).lower()


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return ' '.join(str(value).replace(' ', ' ').split())


def _split_list(value, seps=r'[;\n]'):
    return [part.strip() for part in re.split(seps, value or '') if part.strip()]


# ---------------------------------------------------------------- reading the file

class ImportFileError(Exception):
    pass


def _rows_from_upload(upload):
    """Return a list of rows (lists of cell strings) from an uploaded .xlsx or .csv file."""
    if upload is None:
        raise ImportFileError('Choose a file to upload (.xlsx or .csv).')
    if upload.size > MAX_UPLOAD_BYTES:
        raise ImportFileError('That file is too big (5 MB max). Remove extra tabs or images and try again.')
    name = (upload.name or '').lower()
    data = upload.read()
    if name.endswith('.csv') or name.endswith('.txt'):
        return _rows_from_csv(data), None
    if name.endswith('.xlsx') or name.endswith('.xlsm'):
        return _rows_from_xlsx(data)
    if name.endswith('.xls'):
        raise ImportFileError('Old .xls files aren\'t supported. In Excel use File → Save As → Excel Workbook (.xlsx).')
    raise ImportFileError('Upload an Excel file (.xlsx) or a CSV. From Google Sheets: File → Download → '
                          'Microsoft Excel (.xlsx).')


def _rows_from_csv(data):
    for encoding in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    return [[_cell(c) for c in row] for row in csv.reader(io.StringIO(text), dialect)]


def _rows_from_xlsx(data):
    try:
        from openpyxl import load_workbook
    except ImportError:  # pragma: no cover - dependency listed in requirements.txt
        raise ImportFileError('Excel support isn\'t installed on the server (openpyxl). Upload a CSV instead.')
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise ImportFileError('That file couldn\'t be opened as an Excel workbook. Re-save it as .xlsx and try again.')
    sheets = {}
    for ws in wb.worksheets:
        if ws.sheet_state != 'visible':
            continue
        sheets[ws.title] = [[_cell(c) for c in row] for row in ws.iter_rows(values_only=True)]
    wb.close()
    return sheets, 'xlsx'


def _find_table(kind, rows):
    """Locate the header row (within the first 15 rows, so a title row above the table is
    fine) and map our field names to column indexes. Returns (header_index, columns) or None."""
    aliases = {field: {_norm_header(a) for a in names} for field, names in HEADER_ALIASES[kind].items()}
    for idx, row in enumerate(rows[:15]):
        columns = {}
        for col, value in enumerate(row):
            key = _norm_header(value)
            for field, names in aliases.items():
                if key in names and field not in columns:
                    columns[field] = col
                    break
        if all(f in columns for f in REQUIRED_COLUMNS[kind]):
            return idx, columns
    return None


def read_table(kind, upload):
    """Return [(sheet_row_number, {field: value})] for every non-blank data row."""
    rows, fmt = _rows_from_upload(upload)
    if fmt == 'xlsx':
        # Prefer the tab named after the import ("Pupils" / "Classes"), then any other tab
        # with matching headers — never the Instructions tab.
        preferred = sorted(rows.items(), key=lambda kv: (_norm_header(kv[0]) != kind, _norm_header(kv[0]) == 'instructions'))
        found = None
        for _title, sheet_rows in preferred:
            if _norm_header(_title) == 'instructions':
                continue
            table = _find_table(kind, sheet_rows)
            if table:
                found = (sheet_rows, table)
                break
    else:
        table = _find_table(kind, rows)
        found = (rows, table) if table else None
    if not found:
        needed = ' and '.join(f'"{HEADER_ALIASES[kind][f][0].title()}"' for f in REQUIRED_COLUMNS[kind])
        extra = ' plus "First name" (or "Name")' if kind in ('pupils', 'teachers') else ''
        raise ImportFileError(f'Couldn\'t find the header row. The sheet needs columns named {needed}{extra}. '
                              f'Download the template to see the expected layout.')
    sheet_rows, (header_idx, columns) = found
    if kind in ('pupils', 'teachers') and 'first_name' not in columns and 'full_name' not in columns:
        who = 'pupil' if kind == 'pupils' else 'teacher'
        raise ImportFileError(f'Add a "First name" column (or a single "Name" column with the {who}\'s full name).')
    out = []
    for offset, row in enumerate(sheet_rows[header_idx + 1:], start=header_idx + 2):
        record = {field: (row[col] if col < len(row) else '') for field, col in columns.items()}
        if not any(record.values()):
            continue
        out.append((offset, record))
    if len(out) > MAX_ROWS:
        raise ImportFileError(f'That sheet has {len(out)} rows — the limit is {MAX_ROWS} per upload. Split it into smaller files.')
    return out


# ---------------------------------------------------------------- planning (shared by preview + commit)

def _school_classes(school):
    qs = SchoolClass.objects.all()
    if school:
        qs = qs.filter(school=school)
    return qs


def _row(row_no, status, item, detail='', message=''):
    return {'row': row_no, 'status': status, 'item': item, 'detail': detail, 'message': message}


def plan_classes(rows, school):
    classes = {_norm_name(c.name): c for c in _school_classes(school).prefetch_related('teachers')}
    teachers = list(assignable_teachers(school))
    by_email = {t.email.lower(): t for t in teachers if t.email}
    by_username = {t.username.lower(): t for t in teachers}
    seen, results, actions = {}, [], []
    for row_no, rec in rows:
        name = rec.get('class', '')
        rename_to = rec.get('rename_to', '')
        raw_teachers = rec.get('teachers', '')
        if not name:
            results.append(_row(row_no, 'error', '—', message='Class name is empty.'))
            continue
        if len(name) > 120 or len(rename_to) > 120:
            results.append(_row(row_no, 'error', name, message='Class names can be at most 120 characters.'))
            continue
        key = _norm_name(name)
        if key in seen:
            results.append(_row(row_no, 'error', name, message=f'Same class is already on row {seen[key]}.'))
            continue
        seen[key] = row_no
        # Teachers: blank = leave as is, NONE = remove all, otherwise replace with this list.
        teacher_list, missing = None, []
        if raw_teachers.strip().lower() in CLEAR_TEACHERS_WORDS:
            teacher_list = []
        elif raw_teachers:
            teacher_list = []
            for token in _split_list(raw_teachers, r'[;,\n/]|\s+'):
                t = by_email.get(token.lower()) or by_username.get(token.lower())
                if t is None:
                    missing.append(token)
                elif t not in teacher_list:
                    teacher_list.append(t)
        if missing:
            results.append(_row(row_no, 'error', name, message='No teacher account for ' + ', '.join(missing)
                                + '. Add them on the Roles page first, or check the spelling.'))
            continue
        teacher_text = ', '.join(t.get_full_name() or t.username for t in teacher_list or [])
        existing = classes.get(key)
        if existing is None:
            if rename_to:
                results.append(_row(row_no, 'error', name, message='"Rename to" only works for classes that already exist.'))
                continue
            results.append(_row(row_no, 'create', name, f'Teachers: {teacher_text}' if teacher_text else 'No teacher yet'))
            actions.append({'type': 'create', 'name': name, 'teachers': teacher_list or []})
            continue
        changes = []
        new_name = None
        if rename_to and rename_to != existing.name:
            other = classes.get(_norm_name(rename_to))
            if other is not None and other.id != existing.id:
                results.append(_row(row_no, 'error', name, message=f'Can\'t rename — a class called "{other.name}" already exists.'))
                continue
            if _norm_name(rename_to) in seen and _norm_name(rename_to) != key:
                results.append(_row(row_no, 'error', name, message=f'"{rename_to}" is also used on row {seen[_norm_name(rename_to)]}.'))
                continue
            seen[_norm_name(rename_to)] = row_no
            new_name = rename_to
            changes.append(f'Rename to "{rename_to}"')
        current_ids = {t.id for t in existing.teachers.all()}
        if teacher_list is not None and {t.id for t in teacher_list} != current_ids:
            changes.append(f'Teachers → {teacher_text or "none"}')
        else:
            teacher_list = None
        if not changes:
            results.append(_row(row_no, 'unchanged', existing.name, 'Already up to date'))
            continue
        results.append(_row(row_no, 'update', existing.name, ' · '.join(changes)))
        actions.append({'type': 'update', 'cls': existing, 'new_name': new_name, 'teachers': teacher_list})
    return results, actions


def _split_full_name(full):
    """'Dela Cruz, Juan Miguel' → ('Juan Miguel', 'Dela Cruz'). Without a comma we don't
    guess where the surname starts (multi-word surnames are common) and keep it whole."""
    if ',' in full:
        last, first = full.split(',', 1)
        return first.strip(), last.strip()
    return full.strip(), ''


def plan_pupils(rows, school):
    classes = {_norm_name(c.name): c for c in _school_classes(school)}
    emails = {rec.get('email', '').strip().lower() for _, rec in rows if rec.get('email')}
    existing_users = {}
    if emails:
        matches = (User.objects.annotate(email_lower=Lower('email')).filter(email_lower__in=emails)
                   .prefetch_related('classes_enrolled').order_by('id'))
        for u in matches:
            existing_users.setdefault(u.email_lower, u)

    pending = {}   # email -> plan dict (merges several rows for the same pupil)
    results = []
    for row_no, rec in rows:
        email = rec.get('email', '').strip()
        first, last = rec.get('first_name', ''), rec.get('last_name', '')
        if not first and rec.get('full_name'):
            first, last2 = _split_full_name(rec['full_name'])
            last = last or last2
        label = ' '.join(p for p in (first, last) if p) or email or '—'
        class_names = _split_list(rec.get('class', ''))
        if not email:
            results.append(_row(row_no, 'error', label, message='Email is empty.'))
            continue
        try:
            validate_email(email)
        except DjangoValidationError:
            results.append(_row(row_no, 'error', label, message=f'"{email}" isn\'t a valid email address.'))
            continue
        unknown = [c for c in class_names if _norm_name(c) not in classes]
        if unknown:
            results.append(_row(row_no, 'error', label, message='Class ' + ', '.join(f'"{c}"' for c in unknown)
                                + ' doesn\'t exist. Create it on the Classes page first (or fix the spelling, or leave Class blank).'))
            continue
        if len(first) > 150 or len(last) > 150:
            results.append(_row(row_no, 'error', label, message='Names can be at most 150 characters.'))
            continue
        target_classes = [classes[_norm_name(c)] for c in class_names]
        key = email.lower()
        user = existing_users.get(key)
        if user is not None:
            if user.role != User.Role.PUPIL or user.is_superuser:
                results.append(_row(row_no, 'error', label, message='This email belongs to a teacher/admin account.'))
                continue
            if school and user.school_id != school.id:
                results.append(_row(row_no, 'error', label, message='This email is already used by an account at another school.'))
                continue
            if not user.is_active:
                results.append(_row(row_no, 'error', label, message='This pupil\'s account is deactivated. Reactivate it first.'))
                continue
        plan = pending.get(key)
        if plan is None:
            plan = pending[key] = {'email': email, 'first': first, 'last': last, 'user': user, 'classes': []}
            if user is None and not first:
                pending.pop(key)
                results.append(_row(row_no, 'error', label, message='First name is empty (needed for a new pupil).'))
                continue
        enrolled = {c.id for c in user.classes_enrolled.all()} if user else set()
        new_classes = [c for c in target_classes if c.id not in enrolled and c not in plan['classes']]
        plan['classes'].extend(new_classes)
        class_text = ', '.join(c.name for c in target_classes) or 'no class yet'
        if user is None:
            first_row = plan.get('row') is None
            plan.setdefault('row', row_no)
            results.append(_row(row_no, 'create', label, f'{email} → {class_text}',
                                '' if first_row else f'Same pupil as row {plan["row"]} — also enrolled here.'))
            continue
        name_change = (first and first != user.first_name) or (last and last != user.last_name)
        if name_change:
            plan['rename'] = True
        bits = []
        if new_classes:
            bits.append('Add to ' + ', '.join(c.name for c in new_classes))
        if name_change:
            bits.append(f'Update name to "{label}"')
        if bits:
            results.append(_row(row_no, 'update', label, f'{email} · ' + ' · '.join(bits)))
        else:
            results.append(_row(row_no, 'unchanged', label, f'{email} · ' + (f'already in {class_text}' if target_classes else 'already has an account')))
    return results, list(pending.values())


def plan_teachers(rows, school):
    """Like plan_pupils, for staff: new teacher/admin accounts, or updates to existing ones
    (role change, extra classes). Classes must already exist and are only ever ADDED."""
    classes = {_norm_name(c.name): c for c in _school_classes(school)}
    emails = {rec.get('email', '').strip().lower() for _, rec in rows if rec.get('email')}
    existing_users = {}
    if emails:
        for u in (User.objects.annotate(email_lower=Lower('email')).filter(email_lower__in=emails)
                  .prefetch_related('classes_taught').order_by('id')):
            existing_users.setdefault(u.email_lower, u)
    seen, results, plans = {}, [], []
    for row_no, rec in rows:
        email = rec.get('email', '').strip()
        first, last = rec.get('first_name', ''), rec.get('last_name', '')
        if not first and rec.get('full_name'):
            first, last2 = _split_full_name(rec['full_name'])
            last = last or last2
        label = ' '.join(p for p in (first, last) if p) or email or '—'
        if not email:
            results.append(_row(row_no, 'error', label, message='Email is empty.'))
            continue
        try:
            validate_email(email)
        except DjangoValidationError:
            results.append(_row(row_no, 'error', label, message=f'"{email}" isn\'t a valid email address.'))
            continue
        key = email.lower()
        if key in seen:
            results.append(_row(row_no, 'error', label, message=f'Same email is already on row {seen[key]}. Put all their classes on one row, separated by ";".'))
            continue
        seen[key] = row_no
        role_raw = (rec.get('role') or '').strip().lower()
        if role_raw and role_raw not in ADMIN_WORDS and role_raw not in TEACHER_WORDS:
            results.append(_row(row_no, 'error', label, message=f'Role "{rec.get("role")}" — use Teacher or Admin.'))
            continue
        want_admin = None if not role_raw else role_raw in ADMIN_WORDS
        class_names = _split_list(rec.get('class', ''))
        unknown = [c for c in class_names if _norm_name(c) not in classes]
        if unknown:
            results.append(_row(row_no, 'error', label, message='Class ' + ', '.join(f'"{c}"' for c in unknown)
                                + ' doesn\'t exist. Create it on the Classes page first (or fix the spelling, or leave it blank).'))
            continue
        target = []
        for c in class_names:
            cls = classes[_norm_name(c)]
            if cls not in target:
                target.append(cls)
        user = existing_users.get(key)
        role_text = 'Admin' if want_admin else 'Teacher'
        class_text = ', '.join(c.name for c in target)
        if user is None:
            if not first:
                results.append(_row(row_no, 'error', label, message='First name is empty (needed for a new account).'))
                continue
            results.append(_row(row_no, 'create', label, f'{email} · {role_text}' + (f' · teaches {class_text}' if target else '')))
            plans.append({'email': email, 'first': first, 'last': last, 'user': None, 'admin': bool(want_admin), 'classes': target})
            continue
        if user.is_superuser:
            results.append(_row(row_no, 'error', label, message='This is a main admin account — manage it in Django admin.'))
            continue
        if user.role != User.Role.TEACHER:
            results.append(_row(row_no, 'error', label, message='This email belongs to a pupil account.'))
            continue
        if school and user.school_id != school.id:
            results.append(_row(row_no, 'error', label, message='This email is already used by an account at another school.'))
            continue
        current = {c.id for c in user.classes_taught.all()}
        new_classes = [c for c in target if c.id not in current]
        bits = []
        role_change = want_admin is not None and want_admin != user.is_school_admin
        if role_change:
            bits.append(f'Make {role_text}')
        if new_classes:
            bits.append('Add to ' + ', '.join(c.name for c in new_classes))
        rename = (first and first != user.first_name) or (last and last != user.last_name)
        if rename:
            bits.append(f'Update name to "{label}"')
        if not bits:
            results.append(_row(row_no, 'unchanged', label, f'{email} · already up to date'))
            continue
        results.append(_row(row_no, 'update', label, f'{email} · ' + ' · '.join(bits)))
        plans.append({'email': email, 'first': first, 'last': last, 'user': user,
                      'admin': want_admin if role_change else None, 'classes': new_classes, 'rename': rename})
    return results, plans


# ---------------------------------------------------------------- applying

def apply_classes(actions, school, actor):
    actor_name = actor.get_full_name() or actor.username
    for a in actions:
        if a['type'] == 'create':
            cls = SchoolClass.objects.create(name=a['name'], school=school)
            if a['teachers']:
                cls.teachers.set(a['teachers'])
            log_activity(actor_name, actor.email, 'class_created',
                         f'Created class "{cls.name}" (bulk import).', school_class=cls)
            if a['teachers']:
                log_activity(actor_name, actor.email, 'class_teachers',
                             f'Updated teachers of class "{cls.name}": '
                             f'{", ".join(t.get_full_name() or t.username for t in a["teachers"])}.', school_class=cls)
        else:
            cls = a['cls']
            if a['new_name']:
                old = cls.name
                cls.name = a['new_name']
                cls.save(update_fields=['name'])
                log_activity(actor_name, actor.email, 'class_renamed',
                             f'Renamed class "{old}" to "{cls.name}" (bulk import).', school_class=cls)
            if a['teachers'] is not None:
                cls.teachers.set(a['teachers'])
                log_activity(actor_name, actor.email, 'class_teachers',
                             f'Updated teachers of class "{cls.name}": '
                             f'{", ".join(t.get_full_name() or t.username for t in a["teachers"]) or "none"} (bulk import).',
                             school_class=cls)
    return {}


def apply_pupils(plans, school, actor):
    actor_name = actor.get_full_name() or actor.username
    credentials, added_per_class, created = [], {}, 0
    for p in plans:
        user = p['user']
        if user is None:
            username = _unique_username((p['email'].split('@')[0] or p['first']).lower()[:140])
            temp_password = secrets.token_urlsafe(6)
            user = User.objects.create_user(must_change_password=True, 
                username=username, email=p['email'], first_name=p['first'], last_name=p['last'],
                password=temp_password, role=User.Role.PUPIL, school=school)
            created += 1
            credentials.append({'id': user.id, 'name': user.get_full_name() or user.username, 'email': user.email,
                                'username': user.username, 'tempPassword': temp_password,
                                'classes': ', '.join(c.name for c in p['classes'])})
        elif p.get('rename'):
            user.first_name = p['first'] or user.first_name
            user.last_name = p['last'] or user.last_name
            user.save(update_fields=['first_name', 'last_name'])
        for cls in p['classes']:
            cls.pupils.add(user)
            added_per_class.setdefault(cls, []).append(user)
    for cls, people in added_per_class.items():
        names = ', '.join(u.get_full_name() or u.username for u in people[:5]) + ('…' if len(people) > 5 else '')
        log_activity(actor_name, actor.email, 'class_pupils',
                     f'Added {len(people)} pupil{"s" if len(people) != 1 else ""} to class "{cls.name}" '
                     f'(bulk import): {names}.', school_class=cls)
    if created:
        log_activity(actor_name, actor.email, 'pupil_created',
                     f'Created {created} pupil account{"s" if created != 1 else ""} (bulk import).')
    return {'credentials': credentials}


def apply_teachers(plans, school, actor):
    actor_name = actor.get_full_name() or actor.username
    credentials, created, updated = [], 0, 0
    for p in plans:
        user = p['user']
        if user is None:
            username = _unique_username((p['email'].split('@')[0] or p['first']).lower()[:140])
            temp_password = secrets.token_urlsafe(6)
            user = User.objects.create_user(must_change_password=True,
                username=username, email=p['email'], first_name=p['first'], last_name=p['last'],
                password=temp_password, role=User.Role.TEACHER, school=school,
                is_school_admin=p['admin'], is_staff=p['admin'])
            created += 1
            credentials.append({'id': user.id, 'name': user.get_full_name() or user.username, 'email': user.email,
                                'username': user.username, 'tempPassword': temp_password,
                                'classes': ', '.join(c.name for c in p['classes']),
                                'role': 'Admin' if p['admin'] else 'Teacher'})
        else:
            fields = []
            if p.get('rename'):
                user.first_name = p['first'] or user.first_name
                user.last_name = p['last'] or user.last_name
                fields += ['first_name', 'last_name']
            if p['admin'] is not None:
                user.is_school_admin = user.is_staff = p['admin']
                fields += ['is_school_admin', 'is_staff']
            if fields:
                user.save(update_fields=fields)
            updated += 1
        for cls in p['classes']:
            cls.teachers.add(user)
    if created or updated:
        log_activity(actor_name, actor.email, 'teacher_created',
                     f'Bulk import: {created} new staff account{"s" if created != 1 else ""}, {updated} updated.')
    return {'credentials': credentials}


# ---------------------------------------------------------------- views

def _summary(results):
    counts = {'create': 0, 'update': 0, 'unchanged': 0, 'error': 0}
    for r in results:
        counts[r['status']] = counts.get(r['status'], 0) + 1
    return counts


@login_required
@require_POST
def bulk_import_api(request, kind):
    """POST multipart: file=<xlsx|csv>, commit=0|1, skipErrors=0|1.
    commit=0 → preview only (nothing saved). commit=1 → apply. If any row has an error the
    import is refused unless skipErrors=1, in which case only the valid rows are applied."""
    denied = _require_class_admin(request)
    if denied:
        return denied
    if kind not in KINDS:
        return HttpResponseBadRequest('Unknown import type.')
    school = getattr(request, 'school', None)
    try:
        rows = read_table(kind, request.FILES.get('file'))
    except ImportFileError as exc:
        return HttpResponseBadRequest(str(exc))
    if not rows:
        return HttpResponseBadRequest('No rows found under the header row. Fill in the template and upload it again.')
    commit = request.POST.get('commit') in ('1', 'true')
    skip_errors = request.POST.get('skipErrors') in ('1', 'true')
    with transaction.atomic():
        planner = {'classes': plan_classes, 'pupils': plan_pupils, 'teachers': plan_teachers}[kind]
        results, actions = planner(rows, school)
        summary = _summary(results)
        payload = {'kind': kind, 'committed': False, 'summary': summary, 'rows': results}
        if not commit:
            return JsonResponse(payload)
        if summary['error'] and not skip_errors:
            return HttpResponseBadRequest(f'{summary["error"]} row(s) have errors. Fix them, or choose to skip them.')
        applier = {'classes': apply_classes, 'pupils': apply_pupils, 'teachers': apply_teachers}[kind]
        payload.update(applier(actions, school, request.user))
        payload['committed'] = True
    # "Send invites now": email every NEW account its sign-in details (fresh temporary password).
    if request.POST.get('invite') in ('1', 'true') and payload.get('credentials'):
        from .accounts import invite_many
        new_users = list(User.objects.filter(id__in=[c['id'] for c in payload['credentials']]).select_related('school'))
        invited = invite_many(request, new_users)
        by_id = {c['id']: c for c in invited['credentials']}
        for c in payload['credentials']:
            c['tempPassword'] = by_id.get(c['id'], {}).get('tempPassword', c['tempPassword'])
        payload['invited'] = {'sent': invited['sent'], 'failed': len(invited['failed'])}
    return JsonResponse(payload)


# ---------------------------------------------------------------- templates

def _style_header(ws, headers, widths, required):
    from openpyxl.styles import Alignment, Font, PatternFill
    fill_req = PatternFill('solid', fgColor='28186F')
    fill_opt = PatternFill('solid', fgColor='4B91C5')
    for col, (header, width) in enumerate(zip(headers, widths), start=1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = fill_req if header in required else fill_opt
        cell.alignment = Alignment(vertical='center')
        ws.column_dimensions[cell.column_letter].width = width
    ws.row_dimensions[1].height = 22
    ws.freeze_panes = 'A2'


def _instructions_sheet(wb, title, intro, steps, columns, examples, notes):
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    ws = wb.create_sheet('Instructions')
    ws.sheet_properties.tabColor = 'F2B705'
    ws.column_dimensions['A'].width = 24
    ws.column_dimensions['B'].width = 14
    ws.column_dimensions['C'].width = 62
    ws.column_dimensions['D'].width = 34
    wrap = Alignment(wrap_text=True, vertical='top')
    thin = Side(style='thin', color='D0D7E2')
    box = Border(top=thin, bottom=thin, left=thin, right=thin)
    head_fill = PatternFill('solid', fgColor='EEF5FB')
    r = 1
    ws.cell(row=r, column=1, value=title).font = Font(bold=True, size=16, color='28186F')
    r += 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
    c = ws.cell(row=r, column=1, value=intro)
    c.alignment = wrap
    ws.row_dimensions[r].height = 48
    r += 2
    ws.cell(row=r, column=1, value='How to use this file').font = Font(bold=True, size=12)
    r += 1
    for i, step in enumerate(steps, start=1):
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
        c = ws.cell(row=r, column=1, value=f'{i}. {step}')
        c.alignment = wrap
        ws.row_dimensions[r].height = 32 if len(step) > 95 else 18
        r += 1
    r += 1
    ws.cell(row=r, column=1, value='Columns').font = Font(bold=True, size=12)
    r += 1
    for col, h in enumerate(['Column', 'Required?', 'What to put in it', 'Example'], start=1):
        c = ws.cell(row=r, column=col, value=h)
        c.font = Font(bold=True)
        c.fill = head_fill
        c.border = box
    r += 1
    for row in columns:
        for col, value in enumerate(row, start=1):
            c = ws.cell(row=r, column=col, value=value)
            c.alignment = wrap
            c.border = box
        ws.row_dimensions[r].height = max(18, 15 * (1 + len(row[2]) // 60))
        r += 1
    r += 1
    ws.cell(row=r, column=1, value='Example (copy this layout — don\'t type here)').font = Font(bold=True, size=12)
    r += 1
    for i, row in enumerate(examples):
        for col, value in enumerate(row, start=1):
            c = ws.cell(row=r, column=col, value=value)
            c.border = box
            if i == 0:
                c.font = Font(bold=True)
                c.fill = head_fill
            else:
                c.font = Font(italic=True, color='6B7280')
        r += 1
    r += 1
    ws.cell(row=r, column=1, value='Good to know').font = Font(bold=True, size=12)
    r += 1
    for note in notes:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
        c = ws.cell(row=r, column=1, value=f'•  {note}')
        c.alignment = wrap
        ws.row_dimensions[r].height = 32 if len(note) > 95 else 18
        r += 1
    return ws


def build_template(kind, school):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    classes = list(_school_classes(school).prefetch_related('teachers')
                   .annotate(pupil_count=Count('pupils', distinct=True)).order_by('name'))
    teachers = list(assignable_teachers(school).order_by('first_name', 'last_name', 'username'))
    school_name = school.name if school else 'ExamHub'

    if kind == 'classes':
        ws.title = 'Classes'
        headers = ['Class name *', 'Teacher emails', 'Rename to']
        _style_header(ws, headers, [34, 52, 30], required={'Class name *'})
        # Pre-filled with the school's current classes so "update" = edit and re-upload.
        for i, cls in enumerate(classes, start=2):
            ws.cell(row=i, column=1, value=cls.name)
            ws.cell(row=i, column=2, value='; '.join(t.email or t.username for t in cls.teachers.all()))
        _instructions_sheet(
            wb, f'Classes & teachers import — {school_name}',
            'Use this file to add many classes at once and set which teacher(s) teach each one. '
            'Your existing classes are already listed on the Classes tab — edit them to update, '
            'add new rows to create new classes.',
            ['Fill in the "Classes" tab — one row per class. Keep the header row as it is.',
             'Put each class\'s teacher email(s) in "Teacher emails". Several teachers? Separate them with ";" '
             '(e.g. ana@school.edu; ben@school.edu). The "Teacher list" tab shows every teacher account.',
             'Save the file (.xlsx). In Google Sheets: File → Download → Microsoft Excel (.xlsx) or CSV.',
             'In ExamHub go to Classes → Bulk import classes, upload the file and click "Check file".',
             'Review the preview (green = new, blue = update, red = error). Fix any errors, or skip them, then click Import.',
             'Then add teachers and pupils on the Roles page (Bulk add) — they can be put straight into these classes.'],
            [('Class name *', 'Yes', 'The class / section name exactly as you want it to appear. '
              'If a class with this name already exists it is UPDATED, otherwise it is CREATED. '
              'Matching ignores upper/lower case and extra spaces.', 'Grade 7 - Newton'),
             ('Teacher emails', 'No', 'Email (or username) of the teacher(s). Separate several with ";". '
              'This REPLACES the class\'s current teachers. Leave blank to keep the current teachers; '
              'type NONE to remove all teachers. Teachers must already have an account (Roles page).',
              'ana@school.edu; ben@school.edu'),
             ('Rename to', 'No', 'Only to rename an EXISTING class: put the new name here. '
              'Leave blank otherwise.', 'Grade 7 - Einstein')],
            [('Class name *', 'Teacher emails', 'Rename to'),
             ('Grade 7 - Newton', 'ana@school.edu', ''),
             ('Grade 8 - Curie', 'ben@school.edu; carla@school.edu', ''),
             ('Pre-U 1 - Hawking', '', 'Pre-U 1 - Hawking A')],
            ['Rows are checked first — nothing is saved until you click Import on the preview.',
             'Deleting a row from this file does NOT delete the class. Delete classes on the Classes page.',
             'Column names are flexible: "Class", "Section" or "Class name" all work, so you can upload your own sheet.',
             'Blank rows are ignored. Up to 3,000 rows per upload.'])
        ref = wb.create_sheet('Teacher list')
        _style_header(ref, ['Teacher name', 'Email', 'Username'], [30, 36, 22], required=set())
        for i, t in enumerate(teachers, start=2):
            ref.cell(row=i, column=1, value=t.get_full_name() or t.username)
            ref.cell(row=i, column=2, value=t.email)
            ref.cell(row=i, column=3, value=t.username)
        if teachers:
            dv = DataValidation(type='list', formula1=f"'Teacher list'!$B$2:$B${len(teachers) + 1}",
                                allow_blank=True, showErrorMessage=False)
            dv.prompt = 'Pick a teacher, or type several emails separated by ";"'
            dv.showInputMessage = True
            ws.add_data_validation(dv)
            dv.add('B2:B3001')
    elif kind == 'pupils':
        ws.title = 'Pupils'
        headers = ['First name *', 'Last name', 'Email *', 'Class']
        _style_header(ws, headers, [24, 24, 36, 34], required={'First name *', 'Email *'})
        _instructions_sheet(
            wb, f'Pupils import — {school_name}',
            'Use this file to create pupil accounts and, optionally, put them into classes in one go. '
            'A class you name must ALREADY exist in ExamHub (create it on the Classes page first). '
            'Leave Class blank to just create the account.',
            ['Make sure the classes exist (Classes page, or the Classes & teachers template). '
             'The "Class list" tab shows the classes that exist right now.',
             'Fill in the "Pupils" tab — one row per pupil. Pick the class from the dropdown in the "Class" column.',
             'Moving from another spreadsheet? Copy the columns and use Paste special → Values only so the formatting stays clean.',
             'Save the file (.xlsx). In Google Sheets: File → Download → Microsoft Excel (.xlsx) or CSV.',
             'In ExamHub go to Roles → Pupils → Bulk add, upload the file and click "Check file".',
             'Review the preview, tick "Email invites" if you want each new pupil emailed their sign-in details, '
             'then click Import. You can also download the logins file (shown only once).'],
            [('First name *', 'Yes (new pupils)', 'Pupil\'s first / given name(s).', 'Juan Miguel'),
             ('Last name', 'No', 'Pupil\'s surname. Instead of First/Last name you may use one "Name" column; '
              'write it as "Surname, First names" to split it correctly.', 'Dela Cruz'),
             ('Email *', 'Yes', 'The pupil\'s school email — this is how ExamHub recognises them. '
              'If the pupil already has an account, they are simply added to the class (no new account).',
              'juan.delacruz@school.edu'),
             ('Class', 'No', 'Name of an EXISTING class (see the "Class list" tab). To put a pupil in '
              'more than one class, separate them with ";" or add a second row with the same email. '
              'Leave blank to add them to a class later.',
              'Grade 7 - Newton')],
            [('First name *', 'Last name', 'Email *', 'Class'),
             ('Juan Miguel', 'Dela Cruz', 'juan.delacruz@school.edu', 'Grade 7 - Newton'),
             ('Maria', 'Santos', 'maria.santos@school.edu', 'Grade 7 - Newton'),
             ('Ken', 'Tan', 'ken.tan@school.edu', 'Grade 8 - Curie; Chess Club')],
            ['Rows are checked first — nothing is saved until you click Import on the preview.',
             'Existing pupils are never removed from other classes — this import only ADDS.',
             'A pupil email that belongs to a teacher, or to another school, is reported as an error.',
             'Column names are flexible: "Surname", "Email address", "Section" etc. also work.',
             'Blank rows are ignored. Up to 3,000 rows per upload.'])
        ref = wb.create_sheet('Class list')
        _style_header(ref, ['Class name', 'Teachers', 'Pupils now'], [34, 44, 12], required=set())
        for i, cls in enumerate(classes, start=2):
            ref.cell(row=i, column=1, value=cls.name)
            ref.cell(row=i, column=2, value=', '.join(t.get_full_name() or t.username for t in cls.teachers.all()))
            ref.cell(row=i, column=3, value=cls.pupil_count)
        if not classes:
            ref.cell(row=2, column=1, value='No classes yet — import classes first.').font = Font(italic=True, color='D9231A')
        else:
            dv = DataValidation(type='list', formula1=f"'Class list'!$A$2:$A${len(classes) + 1}",
                                allow_blank=True, showErrorMessage=False)
            dv.prompt = 'Pick an existing class (or type several separated by ";")'
            dv.showInputMessage = True
            ws.add_data_validation(dv)
            dv.add('D2:D3001')
    else:
        ws.title = 'Teachers'
        headers = ['First name *', 'Last name', 'Email *', 'Role', 'Classes']
        _style_header(ws, headers, [22, 22, 34, 14, 44], required={'First name *', 'Email *'})
        _instructions_sheet(
            wb, f'Teachers import — {school_name}',
            'Use this file to create teacher and admin accounts in one go and, if you like, say which of your '
            'EXISTING classes each one teaches. Classes must already exist (create them on the Classes page first) — '
            'the "Class list" tab shows them. Existing staff (same email) are updated: role changed and/or classes added.',
            ['Fill in the "Teachers" tab — one row per person. Keep the header row as it is.',
             'Role: pick Teacher or Admin from the dropdown (blank = Teacher for new people, unchanged for existing ones).',
             'Classes: pick one from the dropdown, or type several separated by ";". Leave blank to assign classes later.',
             'Save the file (.xlsx). In Google Sheets: File → Download → Microsoft Excel (.xlsx) or CSV.',
             'In ExamHub go to Roles → Staff → Bulk add, upload the file and click "Check file".',
             'Review the preview, tick "Email invites" to send each new person their sign-in details, then click Import.'],
            [('First name *', 'Yes (new people)', 'First / given name(s).', 'Ana'),
             ('Last name', 'No', 'Surname. Or use one "Name" column written as "Surname, First names".', 'Reyes'),
             ('Email *', 'Yes', 'Their school email — this is how ExamHub recognises them.', 'ana.reyes@school.edu'),
             ('Role', 'No', 'Teacher (works only with the classes they\'re assigned) or Admin (can set up classes and '
              'add people).', 'Teacher'),
             ('Classes', 'No', 'EXISTING class(es) they teach, separated by ";". Only ever ADDS — it never removes '
              'them from other classes.', 'Grade 7 - Newton; Grade 8 - Curie')],
            [('First name *', 'Last name', 'Email *', 'Role', 'Classes'),
             ('Ana', 'Reyes', 'ana.reyes@school.edu', 'Teacher', 'Grade 7 - Newton'),
             ('Ben', 'Cruz', 'ben.cruz@school.edu', 'Admin', ''),
             ('Carla', 'Lim', 'carla.lim@school.edu', 'Teacher', 'Grade 8 - Curie; Pre-U 1 - Hawking')],
            ['Rows are checked first — nothing is saved until you click Import on the preview.',
             'A class name that doesn\'t exist is shown as an error — fix the spelling or create the class first.',
             'An email that belongs to a pupil, or to another school, is reported as an error.',
             'Blank rows are ignored. Up to 3,000 rows per upload.'])
        ref = wb.create_sheet('Class list')
        _style_header(ref, ['Class name', 'Teachers now', 'Pupils'], [34, 44, 10], required=set())
        for i, cls in enumerate(classes, start=2):
            ref.cell(row=i, column=1, value=cls.name)
            ref.cell(row=i, column=2, value=', '.join(t.get_full_name() or t.username for t in cls.teachers.all()))
            ref.cell(row=i, column=3, value=cls.pupil_count)
        if not classes:
            ref.cell(row=2, column=1, value='No classes yet — leave "Classes" blank, or create classes first.').font = Font(italic=True, color='D9231A')
        else:
            dv = DataValidation(type='list', formula1=f"'Class list'!$A$2:$A${len(classes) + 1}",
                                allow_blank=True, showErrorMessage=False)
            dv.prompt = 'Pick an existing class (or type several separated by ";")'
            dv.showInputMessage = True
            ws.add_data_validation(dv)
            dv.add('E2:E3001')
        roles = DataValidation(type='list', formula1='"Teacher,Admin"', allow_blank=True)
        ws.add_data_validation(roles)
        roles.add('D2:D3001')
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


@login_required
@require_GET
def bulk_template(request, kind):
    denied = _require_class_admin(request)
    if denied:
        return denied
    if kind not in KINDS:
        return HttpResponseBadRequest('Unknown template.')
    try:
        content = build_template(kind, getattr(request, 'school', None))
    except ImportError:
        return HttpResponseBadRequest('Excel templates need openpyxl installed on the server (pip install openpyxl).')
    stamp = timezone.localdate().isoformat()
    filename = f'examhub-{kind}-template-{stamp}.xlsx'
    response = HttpResponse(content, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response
