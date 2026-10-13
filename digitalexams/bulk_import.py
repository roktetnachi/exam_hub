"""Bulk import of year groups (with their teachers), subjects, teachers and pupils from a spreadsheet.

In the app a "class" is now called a YEAR GROUP (earlier "section") (the model is still SchoolClass and the import
kind is still "classes", so URLs and old templates keep working). A SUBJECT can hold pupils
from several year groups.

Year groups page:  "classes"         — create / rename year groups and set their teachers.
                "section_pupils"  — put EXISTING pupils into year groups (email → year group). Adds only.
Subjects page:  "subjects"        — create / rename subjects and set their teachers.
                "subject_pupils"  — put pupils into subjects: one row per pupil (email → subjects),
                                    or one row per year group (no email) to add everyone in it.
Roles page:     "teachers" / "pupils" — create accounts, optionally straight into year groups/subjects.

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

from .models import SchoolClass, Subject, User
from .views import _require_class_admin, _unique_username, assignable_teachers, log_activity

KINDS = ('classes', 'pupils', 'teachers', 'subjects', 'section_pupils', 'subject_pupils')
# Tab each template's data lives on (read_table looks there first).
SHEET_TITLES = {'classes': 'Year groups', 'pupils': 'Pupils', 'teachers': 'Teachers', 'subjects': 'Subjects',
                'section_pupils': 'Year group pupils', 'subject_pupils': 'Subject pupils'}
MAX_ROWS = 3000
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
CLEAR_TEACHERS_WORDS = {'none', '-', 'no teacher', 'clear'}

# Column headers we recognise, per import. Matching ignores case, punctuation, anything in
# brackets and the words "required"/"optional" — so "Class name *", "CLASS", "Section" and
# "Teacher email(s)" all work, and an existing school spreadsheet usually needs no renaming.
HEADER_ALIASES = {
    'classes': {
        'class': ['year group', 'year group name', 'section', 'section name', 'class', 'class name', 'name',
                  'class section', 'homeroom', 'group'],
        'teachers': ['teacher', 'teachers', 'teacher email', 'teacher emails', 'teacher username',
                     'teachers emails', 'adviser', 'adviser email', 'class teacher', 'class teacher email'],
        'rename_to': ['rename to', 'new name', 'new class name', 'rename'],
    },
    'pupils': {
        'first_name': ['first name', 'firstname', 'given name', 'given names', 'forename'],
        'last_name': ['last name', 'lastname', 'surname', 'family name'],
        'full_name': ['name', 'full name', 'pupil name', 'student name', 'pupil', 'student'],
        'email': ['email', 'email address', 'e mail', 'pupil email', 'student email', 'school email'],
        'class': ['year group', 'year groups', 'year group name', 'section', 'sections', 'section name', 'class',
                  'class name', 'classes', 'class section', 'homeroom', 'group'],
        'subjects': ['subjects', 'subject', 'subject names', 'subject name'],
    },
    'teachers': {
        'first_name': ['first name', 'firstname', 'given name', 'given names', 'forename'],
        'last_name': ['last name', 'lastname', 'surname', 'family name'],
        'full_name': ['name', 'full name', 'teacher name', 'staff name', 'teacher'],
        'email': ['email', 'email address', 'e mail', 'teacher email', 'staff email', 'school email'],
        'role': ['role', 'account type', 'access', 'admin', 'type'],
        'class': ['year groups', 'year group', 'sections', 'section', 'sections taught', 'class', 'classes', 'class name',
                  'classes taught', 'teaches'],
        'subjects': ['subjects', 'subject', 'subjects taught', 'subject names'],
    },
    'subjects': {
        'class': ['subject', 'subject name', 'name', 'course'],
        'teachers': ['teacher', 'teachers', 'teacher email', 'teacher emails', 'teacher username',
                     'teachers emails', 'subject teacher', 'subject teachers'],
        'rename_to': ['rename to', 'new name', 'new subject name', 'rename'],
    },
    'section_pupils': {
        'first_name': ['first name', 'firstname', 'given name', 'given names', 'forename'],
        'last_name': ['last name', 'lastname', 'surname', 'family name'],
        'full_name': ['name', 'full name', 'pupil name', 'student name', 'pupil', 'student'],
        'email': ['email', 'email address', 'e mail', 'pupil email', 'student email', 'school email'],
        'class': ['add to year group', 'add to year groups', 'year group', 'year groups', 'add to section', 'add to sections',
                  'section', 'sections', 'new section', 'class', 'section name'],
    },
    'subject_pupils': {
        'first_name': ['first name', 'firstname', 'given name', 'given names', 'forename'],
        'last_name': ['last name', 'lastname', 'surname', 'family name'],
        'full_name': ['name', 'full name', 'pupil name', 'student name', 'pupil', 'student'],
        'email': ['email', 'email address', 'e mail', 'pupil email', 'student email', 'school email'],
        'class': ['year group', 'whole year group', 'year group name', 'section', 'whole section', 'section name', 'class', 'homeroom'],
        'subjects': ['subjects', 'subject', 'add to subjects', 'add to subject', 'subject names'],
    },
}
REQUIRED_COLUMNS = {'classes': ['class'], 'pupils': ['email'], 'teachers': ['email'], 'subjects': ['class'],
                    'section_pupils': ['email', 'class'], 'subject_pupils': ['subjects']}
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


def _split_list(value, seps=r'[,;\n]'):
    return [part.strip() for part in re.split(seps, value or '') if part.strip()]


def _split_names(value, known):
    """Year group / subject names in one cell, separated by commas (";" still works for older
    files). If the whole cell is itself an existing name — one that contains a comma — it's
    kept as one name instead of being split."""
    value = (value or '').strip()
    if value and _norm_name(value) in known:
        return [value]
    return _split_list(value)


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
        wanted = {_norm_header(kind), _norm_header(SHEET_TITLES.get(kind, kind))}
        preferred = sorted(rows.items(), key=lambda kv: (_norm_header(kv[0]) not in wanted, _norm_header(kv[0]) == 'instructions'))
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


def _school_subjects(school):
    qs = Subject.objects.all()
    if school:
        qs = qs.filter(school=school)
    return qs


# Section / subject imports share one planner: same columns (name, teachers, rename to).
GROUP_KINDS = {
    'classes': {'noun': 'year group', 'page': 'Year groups', 'qs': _school_classes, 'model': SchoolClass, 'log': 'class'},
    'subjects': {'noun': 'subject', 'page': 'Subjects', 'qs': _school_subjects, 'model': Subject, 'log': 'subject'},
}


def _unknown_msg(noun, names, page, fallback='or leave it blank'):
    return (f'{noun.capitalize()} ' + ', '.join(f'"{n}"' for n in names) + f' doesn\'t exist. Create it on the {page} page '
            f'first (or fix the spelling, {fallback}).')


def _pupil_problem(user, school):
    """Why an existing account can't be put into a year group/subject as a pupil, or None."""
    if user.role != User.Role.PUPIL or user.is_superuser:
        return 'This email belongs to a teacher/admin account.'
    if school and user.school_id != school.id:
        return 'This email is already used by an account at another school.'
    if not user.is_active:
        return 'This pupil\'s account is deactivated. Reactivate it first.'
    return None


def _users_by_email(rows, prefetch, school=None):
    """Existing accounts for the emails in the sheet. With a school, only that school's accounts:
    the same pupil email may exist at another school as a separate account."""
    emails = {rec.get('email', '').strip().lower() for _, rec in rows if rec.get('email')}
    out = {}
    if emails:
        qs = User.objects.annotate(email_lower=Lower('email')).filter(email_lower__in=emails)
        if school:
            qs = qs.filter(school=school)
        for u in qs.prefetch_related(*prefetch).order_by('id'):
            out.setdefault(u.email_lower, u)
    return out


def _row_label(rec):
    first, last = rec.get('first_name', ''), rec.get('last_name', '')
    if not first and rec.get('full_name'):
        first, last2 = _split_full_name(rec['full_name'])
        last = last or last2
    return first, last, ' '.join(p for p in (first, last) if p)


def _row(row_no, status, item, detail='', message=''):
    return {'row': row_no, 'status': status, 'item': item, 'detail': detail, 'message': message}


def plan_classes(rows, school, kind='classes'):
    """Year groups (kind='classes') or subjects (kind='subjects'): create, rename, set teachers."""
    cfg = GROUP_KINDS[kind]
    noun, Noun = cfg['noun'], cfg['noun'].capitalize()
    groups = {_norm_name(c.name): c for c in cfg['qs'](school).prefetch_related('teachers')}
    teachers = list(assignable_teachers(school))
    by_email = {t.email.lower(): t for t in teachers if t.email}
    by_username = {t.username.lower(): t for t in teachers}
    seen, results, actions = {}, [], []
    for row_no, rec in rows:
        name = rec.get('class', '')
        rename_to = rec.get('rename_to', '')
        raw_teachers = rec.get('teachers', '')
        if not name:
            results.append(_row(row_no, 'error', '—', message=f'{Noun} name is empty.'))
            continue
        if len(name) > 120 or len(rename_to) > 120:
            results.append(_row(row_no, 'error', name, message=f'{Noun} names can be at most 120 characters.'))
            continue
        key = _norm_name(name)
        if key in seen:
            results.append(_row(row_no, 'error', name, message=f'Same {noun} is already on row {seen[key]}.'))
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
        existing = groups.get(key)
        if existing is None:
            if rename_to:
                results.append(_row(row_no, 'error', name, message=f'"Rename to" only works for {noun}s that already exist.'))
                continue
            results.append(_row(row_no, 'create', name, f'Teachers: {teacher_text}' if teacher_text else 'No teacher yet'))
            actions.append({'type': 'create', 'name': name, 'teachers': teacher_list or []})
            continue
        changes = []
        new_name = None
        if rename_to and rename_to != existing.name:
            other = groups.get(_norm_name(rename_to))
            if other is not None and other.id != existing.id:
                results.append(_row(row_no, 'error', name, message=f'Can\'t rename — a {noun} called "{other.name}" already exists.'))
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


def plan_subjects(rows, school):
    return plan_classes(rows, school, kind='subjects')


def plan_section_pupils(rows, school):
    """Put EXISTING pupils into year groups. One row per pupil (several year groups separated by commas,
    or several rows). Only ever adds; rows with no year group are skipped (the template lists
    every pupil, so most rows may be left blank)."""
    sections = {_norm_name(c.name): c for c in _school_classes(school)}
    users = _users_by_email(rows, ['classes_enrolled'], school)
    planned = {}   # email -> {'user', 'groups': [...]}
    results = []
    for row_no, rec in rows:
        targets_raw = _split_names(rec.get('class', ''), sections)
        if not targets_raw:
            continue
        email = rec.get('email', '').strip()
        _first, _last, label = _row_label(rec)
        label = label or email or '—'
        if not email:
            results.append(_row(row_no, 'error', label, message='Email is empty.'))
            continue
        unknown = [c for c in targets_raw if _norm_name(c) not in sections]
        if unknown:
            results.append(_row(row_no, 'error', label, message=_unknown_msg('year group', unknown, 'Year groups', 'or leave it blank')))
            continue
        user = users.get(email.lower())
        if user is None:
            results.append(_row(row_no, 'error', label, message=f'No pupil account for {email}. Create it on Roles → Pupils first '
                                '(Bulk add pupils there can put new pupils straight into a year group).'))
            continue
        problem = _pupil_problem(user, school)
        if problem:
            results.append(_row(row_no, 'error', label, message=problem))
            continue
        label = user.get_full_name() or label
        plan = planned.setdefault(email.lower(), {'user': user, 'groups': []})
        enrolled = {c.id for c in user.classes_enrolled.all()}
        targets = []
        for c in targets_raw:
            sec = sections[_norm_name(c)]
            if sec not in targets:
                targets.append(sec)
        new = [c for c in targets if c.id not in enrolled and c not in plan['groups']]
        plan['groups'].extend(new)
        if new:
            results.append(_row(row_no, 'update', label, f'{email} · Add to ' + ', '.join(c.name for c in new)))
        else:
            results.append(_row(row_no, 'unchanged', label, f'{email} · already in ' + ', '.join(c.name for c in targets)))
    return results, [p for p in planned.values() if p['groups']]


def _active_pupils(people):
    return [p for p in people if p.role == User.Role.PUPIL and not p.is_superuser and p.is_active]


def plan_subject_pupils(rows, school):
    """Put pupils into subjects. A row with an Email adds that pupil; a row with no Email but a
    Year group adds everyone currently in that year group. Subjects: one or more, separated by commas.
    Only ever adds. Rows with no subject are skipped."""
    subjects = {_norm_name(s.name): s for s in _school_subjects(school).prefetch_related('pupils')}
    sections = {_norm_name(c.name): c for c in _school_classes(school).prefetch_related('pupils')}
    members = {s.id: {p.id for p in s.pupils.all()} for s in subjects.values()}
    users = _users_by_email(rows, [], school)
    adds = {}      # subject id -> [users]
    results = []
    for row_no, rec in rows:
        subject_names = _split_names(rec.get('subjects', ''), subjects)
        if not subject_names:
            continue
        email = rec.get('email', '').strip()
        section_name = rec.get('class', '').strip()
        _first, _last, label = _row_label(rec)
        unknown = [n for n in subject_names if _norm_name(n) not in subjects]
        if unknown:
            results.append(_row(row_no, 'error', label or email or section_name or '—',
                                message=_unknown_msg('subject', unknown, 'Subjects', 'or use "Bulk import subjects"')))
            continue
        targets = []
        for n in subject_names:
            subj = subjects[_norm_name(n)]
            if subj not in targets:
                targets.append(subj)
        if email:
            label = label or email
            user = users.get(email.lower())
            if user is None:
                results.append(_row(row_no, 'error', label, message=f'No pupil account for {email}. Create it on Roles → Pupils first.'))
                continue
            problem = _pupil_problem(user, school)
            if problem:
                results.append(_row(row_no, 'error', label, message=problem))
                continue
            people, who = [user], user.get_full_name() or label
        elif section_name:
            sec = sections.get(_norm_name(section_name))
            if sec is None:
                results.append(_row(row_no, 'error', section_name, message=_unknown_msg('year group', [section_name], 'Year groups', 'or put a pupil email instead')))
                continue
            people, who = _active_pupils(sec.pupils.all()), f'Everyone in {sec.name}'
            if not people:
                results.append(_row(row_no, 'unchanged', who, f'{sec.name} has no pupils yet — nothing to add.'))
                continue
        else:
            results.append(_row(row_no, 'error', label or '—', message='Put a pupil Email — or leave Email blank and put a Year group '
                                'to add everyone in that year group.'))
            continue
        bits, already = [], []
        for subj in targets:
            new = [p for p in people if p.id not in members[subj.id]]
            if new:
                members[subj.id].update(p.id for p in new)
                adds.setdefault(subj.id, (subj, []))[1].extend(new)
                bits.append(f'{subj.name} (+{len(new)})' if len(people) > 1 else subj.name)
            else:
                already.append(subj.name)
        detail_who = email or f'{len(people)} pupil{"s" if len(people) != 1 else ""}'
        if bits:
            results.append(_row(row_no, 'update', who, f'{detail_who} · Add to ' + ', '.join(bits)
                                + (f' · already in {", ".join(already)}' if already else '')))
        else:
            results.append(_row(row_no, 'unchanged', who, f'{detail_who} · already in ' + ', '.join(already)))
    return results, list(adds.values())


def _split_full_name(full):
    """'Dela Cruz, Juan Miguel' → ('Juan Miguel', 'Dela Cruz'). Without a comma we don't
    guess where the surname starts (multi-word surnames are common) and keep it whole."""
    if ',' in full:
        last, first = full.split(',', 1)
        return first.strip(), last.strip()
    return full.strip(), ''


def plan_pupils(rows, school):
    classes = {_norm_name(c.name): c for c in _school_classes(school)}
    subjects = {_norm_name(s.name): s for s in _school_subjects(school)}
    emails = {rec.get('email', '').strip().lower() for _, rec in rows if rec.get('email')}
    existing_users = {}
    if emails:
        # Only this school's accounts: a pupil email used at another school is a different person
        # here, so a new account is created for this school.
        matches = User.objects.annotate(email_lower=Lower('email')).filter(email_lower__in=emails)
        if school:
            matches = matches.filter(school=school)
        matches = matches.prefetch_related('classes_enrolled', 'subjects_enrolled').order_by('id')
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
        class_names = _split_names(rec.get('class', ''), classes)
        subject_names = _split_names(rec.get('subjects', ''), subjects)
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
            results.append(_row(row_no, 'error', label, message=_unknown_msg('year group', unknown, 'Year groups', 'or leave Year group blank')))
            continue
        unknown = [n for n in subject_names if _norm_name(n) not in subjects]
        if unknown:
            results.append(_row(row_no, 'error', label, message=_unknown_msg('subject', unknown, 'Subjects', 'or leave Subjects blank')))
            continue
        target_subjects = []
        for n in subject_names:
            if subjects[_norm_name(n)] not in target_subjects:
                target_subjects.append(subjects[_norm_name(n)])
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
            plan = pending[key] = {'email': email, 'first': first, 'last': last, 'user': user, 'classes': [], 'subjects': []}
            if user is None and not first:
                pending.pop(key)
                results.append(_row(row_no, 'error', label, message='First name is empty (needed for a new pupil).'))
                continue
        enrolled = {c.id for c in user.classes_enrolled.all()} if user else set()
        new_classes = [c for c in target_classes if c.id not in enrolled and c not in plan['classes']]
        plan['classes'].extend(new_classes)
        in_subjects = {s.id for s in user.subjects_enrolled.all()} if user else set()
        new_subjects = [s for s in target_subjects if s.id not in in_subjects and s not in plan['subjects']]
        plan['subjects'].extend(new_subjects)
        class_text = ', '.join(c.name for c in target_classes) or 'no year group yet'
        if target_subjects:
            class_text += ' · subjects: ' + ', '.join(s.name for s in target_subjects)
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
        if new_subjects:
            bits.append('Add to subject ' + ', '.join(s.name for s in new_subjects))
        if name_change:
            bits.append(f'Update name to "{label}"')
        if bits:
            results.append(_row(row_no, 'update', label, f'{email} · ' + ' · '.join(bits)))
        else:
            results.append(_row(row_no, 'unchanged', label, f'{email} · ' + (f'already in {class_text}' if (target_classes or target_subjects) else 'already has an account')))
    return results, list(pending.values())


def plan_teachers(rows, school):
    """Like plan_pupils, for staff: new teacher/admin accounts, or updates to existing ones
    (role change, extra year groups/subjects). Year groups and subjects must already exist and are
    only ever ADDED."""
    classes = {_norm_name(c.name): c for c in _school_classes(school)}
    subjects = {_norm_name(s.name): s for s in _school_subjects(school)}
    emails = {rec.get('email', '').strip().lower() for _, rec in rows if rec.get('email')}
    existing_users = {}
    if emails:
        for u in (User.objects.annotate(email_lower=Lower('email')).filter(email_lower__in=emails)
                  .prefetch_related('classes_taught', 'subjects_taught').order_by('id')):
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
            results.append(_row(row_no, 'error', label, message=f'Same email is already on row {seen[key]}. Put all their year groups/subjects on one row, separated by commas.'))
            continue
        seen[key] = row_no
        role_raw = (rec.get('role') or '').strip().lower()
        if role_raw and role_raw not in ADMIN_WORDS and role_raw not in TEACHER_WORDS:
            results.append(_row(row_no, 'error', label, message=f'Role "{rec.get("role")}" — use Teacher or Admin.'))
            continue
        want_admin = None if not role_raw else role_raw in ADMIN_WORDS
        class_names = _split_names(rec.get('class', ''), classes)
        unknown = [c for c in class_names if _norm_name(c) not in classes]
        if unknown:
            results.append(_row(row_no, 'error', label, message=_unknown_msg('year group', unknown, 'Year groups')))
            continue
        subject_names = _split_names(rec.get('subjects', ''), subjects)
        unknown = [n for n in subject_names if _norm_name(n) not in subjects]
        if unknown:
            results.append(_row(row_no, 'error', label, message=_unknown_msg('subject', unknown, 'Subjects')))
            continue
        target_subjects = []
        for n in subject_names:
            if subjects[_norm_name(n)] not in target_subjects:
                target_subjects.append(subjects[_norm_name(n)])
        target = []
        for c in class_names:
            cls = classes[_norm_name(c)]
            if cls not in target:
                target.append(cls)
        user = existing_users.get(key)
        role_text = 'Admin' if want_admin else 'Teacher'
        class_text = ', '.join(c.name for c in target + target_subjects)
        if user is None:
            if not first:
                results.append(_row(row_no, 'error', label, message='First name is empty (needed for a new account).'))
                continue
            results.append(_row(row_no, 'create', label, f'{email} · {role_text}' + (f' · teaches {class_text}' if class_text else '')))
            plans.append({'email': email, 'first': first, 'last': last, 'user': None, 'admin': bool(want_admin),
                          'classes': target, 'subjects': target_subjects})
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
        current_subjects = {s.id for s in user.subjects_taught.all()}
        new_subjects = [s for s in target_subjects if s.id not in current_subjects]
        bits = []
        role_change = want_admin is not None and want_admin != user.is_school_admin
        if role_change:
            bits.append(f'Make {role_text}')
        if new_classes:
            bits.append('Add to ' + ', '.join(c.name for c in new_classes))
        if new_subjects:
            bits.append('Teaches ' + ', '.join(s.name for s in new_subjects))
        rename = (first and first != user.first_name) or (last and last != user.last_name)
        if rename:
            bits.append(f'Update name to "{label}"')
        if not bits:
            results.append(_row(row_no, 'unchanged', label, f'{email} · already up to date'))
            continue
        results.append(_row(row_no, 'update', label, f'{email} · ' + ' · '.join(bits)))
        plans.append({'email': email, 'first': first, 'last': last, 'user': user,
                      'admin': want_admin if role_change else None, 'classes': new_classes, 'subjects': new_subjects,
                      'rename': rename})
    return results, plans


# ---------------------------------------------------------------- applying

def apply_classes(actions, school, actor, kind='classes'):
    """Year groups (kind='classes') or subjects (kind='subjects')."""
    cfg = GROUP_KINDS[kind]
    noun, prefix, model = cfg['noun'], cfg['log'], cfg['model']
    actor_name = actor.get_full_name() or actor.username
    link = (lambda obj: {'school_class': obj}) if kind == 'classes' else (lambda obj: {})
    names = lambda people: ', '.join(t.get_full_name() or t.username for t in people)
    for a in actions:
        if a['type'] == 'create':
            obj = model.objects.create(name=a['name'], school=school)
            if a['teachers']:
                obj.teachers.set(a['teachers'])
            log_activity(actor_name, actor.email, f'{prefix}_created',
                         f'Created {noun} "{obj.name}" (bulk import).', **link(obj))
            if a['teachers']:
                log_activity(actor_name, actor.email, f'{prefix}_teachers',
                             f'Updated teachers of {noun} "{obj.name}": {names(a["teachers"])}.', **link(obj))
        else:
            obj = a['cls']
            if a['new_name']:
                old = obj.name
                obj.name = a['new_name']
                obj.save(update_fields=['name'])
                log_activity(actor_name, actor.email, f'{prefix}_renamed',
                             f'Renamed {noun} "{old}" to "{obj.name}" (bulk import).', **link(obj))
            if a['teachers'] is not None:
                obj.teachers.set(a['teachers'])
                log_activity(actor_name, actor.email, f'{prefix}_teachers',
                             f'Updated teachers of {noun} "{obj.name}": {names(a["teachers"]) or "none"} (bulk import).',
                             **link(obj))
    return {}


def apply_subjects(actions, school, actor):
    return apply_classes(actions, school, actor, kind='subjects')


def _people_text(people):
    return ', '.join(u.get_full_name() or u.username for u in people[:5]) + ('…' if len(people) > 5 else '')


def apply_section_pupils(plans, school, actor):
    actor_name = actor.get_full_name() or actor.username
    per_section = {}
    for p in plans:
        for sec in p['groups']:
            sec.pupils.add(p['user'])
            per_section.setdefault(sec, []).append(p['user'])
    for sec, people in per_section.items():
        log_activity(actor_name, actor.email, 'class_pupils',
                     f'Added {len(people)} pupil{"s" if len(people) != 1 else ""} to year group "{sec.name}" '
                     f'(bulk import): {_people_text(people)}.', school_class=sec)
    return {}


def apply_subject_pupils(adds, school, actor):
    actor_name = actor.get_full_name() or actor.username
    for subject, people in adds:
        subject.pupils.add(*people)
        log_activity(actor_name, actor.email, 'subject_pupils',
                     f'Added {len(people)} pupil{"s" if len(people) != 1 else ""} to subject "{subject.name}" '
                     f'(bulk import): {_people_text(people)}.')
    return {}


def apply_pupils(plans, school, actor):
    actor_name = actor.get_full_name() or actor.username
    credentials, added_per_class, added_per_subject, created = [], {}, {}, 0
    for p in plans:
        user = p['user']
        if user is None:
            username = _unique_username((p['email'].split('@')[0] or p['first']).lower()[:140])
            temp_password = secrets.token_urlsafe(6)
            user = User.objects.create_user(must_change_password=True, 
                username=username, email=p['email'], first_name=p['first'], last_name=p['last'],
                password=temp_password, role=User.Role.PUPIL, school=school)
            created += 1
            credentials.append({'id': user.id, 'name': user.get_full_name() or user.username, 'firstName': user.first_name,
                                'lastName': user.last_name, 'email': user.email,
                                'username': user.username, 'tempPassword': temp_password,
                                'loginUrl': _login_url(user),
                                'classes': ', '.join(c.name for c in p['classes'] + p.get('subjects', []))})
        elif p.get('rename'):
            user.first_name = p['first'] or user.first_name
            user.last_name = p['last'] or user.last_name
            user.save(update_fields=['first_name', 'last_name'])
        for cls in p['classes']:
            cls.pupils.add(user)
            added_per_class.setdefault(cls, []).append(user)
        for subject in p.get('subjects', []):
            subject.pupils.add(user)
            added_per_subject.setdefault(subject, []).append(user)
    for cls, people in added_per_class.items():
        log_activity(actor_name, actor.email, 'class_pupils',
                     f'Added {len(people)} pupil{"s" if len(people) != 1 else ""} to year group "{cls.name}" '
                     f'(bulk import): {_people_text(people)}.', school_class=cls)
    for subject, people in added_per_subject.items():
        log_activity(actor_name, actor.email, 'subject_pupils',
                     f'Added {len(people)} pupil{"s" if len(people) != 1 else ""} to subject "{subject.name}" '
                     f'(bulk import): {_people_text(people)}.')
    if created:
        log_activity(actor_name, actor.email, 'pupil_created',
                     f'Created {created} pupil account{"s" if created != 1 else ""} (bulk import).')
    return {'credentials': credentials}


def _login_url(user):
    from .accounts import site_url
    who = 'pupil' if user.role == User.Role.PUPIL else 'teacher'
    return f'{site_url()}/s/{user.school.slug}/login/?as={who}' if user.school else f'{site_url()}/login/'


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
            credentials.append({'id': user.id, 'name': user.get_full_name() or user.username, 'firstName': user.first_name,
                                'lastName': user.last_name, 'email': user.email,
                                'username': user.username, 'tempPassword': temp_password,
                                'loginUrl': _login_url(user),
                                'classes': ', '.join(c.name for c in p['classes'] + p.get('subjects', [])),
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
        for subject in p.get('subjects', []):
            subject.teachers.add(user)
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
    if kind == 'section_pupils' and not any(rec.get('class') for _, rec in rows):
        return HttpResponseBadRequest('No year group filled in. Put the year group in the "Add to year group" column for each pupil you want to add.')
    if kind == 'subject_pupils' and not any(rec.get('subjects') for _, rec in rows):
        return HttpResponseBadRequest('No subjects filled in. Put the subject(s) in the "Subjects" column of each pupil (or year group) row.')
    commit = request.POST.get('commit') in ('1', 'true')
    skip_errors = request.POST.get('skipErrors') in ('1', 'true')
    with transaction.atomic():
        planner = {'classes': plan_classes, 'pupils': plan_pupils, 'teachers': plan_teachers, 'subjects': plan_subjects,
                   'section_pupils': plan_section_pupils, 'subject_pupils': plan_subject_pupils}[kind]
        results, actions = planner(rows, school)
        summary = _summary(results)
        payload = {'kind': kind, 'committed': False, 'summary': summary, 'rows': results}
        if not commit:
            return JsonResponse(payload)
        if summary['error'] and not skip_errors:
            return HttpResponseBadRequest(f'{summary["error"]} row(s) have errors. Fix them, or choose to skip them.')
        applier = {'classes': apply_classes, 'pupils': apply_pupils, 'teachers': apply_teachers, 'subjects': apply_subjects,
                   'section_pupils': apply_section_pupils, 'subject_pupils': apply_subject_pupils}[kind]
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


def _ref_sheet(wb, title, headers, widths, rows, empty_text):
    """A reference tab (e.g. "Year group list") whose first column feeds a dropdown. Returns the
    number of data rows written."""
    from openpyxl.styles import Font
    ref = wb.create_sheet(title)
    _style_header(ref, headers, widths, required=set())
    for i, values in enumerate(rows, start=2):
        for col, value in enumerate(values, start=1):
            ref.cell(row=i, column=col, value=value)
    if not rows:
        ref.cell(row=2, column=1, value=empty_text).font = Font(italic=True, color='D9231A')
    return len(rows)


def _dropdown(ws, sheet_title, count, cells, prompt, col='A'):
    if not count:
        return
    from openpyxl.worksheet.datavalidation import DataValidation
    dv = DataValidation(type='list', formula1=f"'{sheet_title}'!${col}$2:${col}${count + 1}",
                        allow_blank=True, showErrorMessage=False)
    dv.prompt = prompt
    dv.showInputMessage = True
    ws.add_data_validation(dv)
    dv.add(cells)


def build_template(kind, school, prefill=False):
    """prefill=True (teachers / pupils only): the data tab lists everyone already in the school, by last
    name A–Z, so the file can be edited and re-uploaded. Otherwise the data tab is blank."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_TITLES[kind]
    classes = list(_school_classes(school).prefetch_related('teachers')
                   .annotate(pupil_count=Count('pupils', distinct=True)).order_by('name'))
    subjects = list(_school_subjects(school).prefetch_related('teachers')
                    .annotate(pupil_count=Count('pupils', distinct=True)).order_by('name'))
    teachers = list(assignable_teachers(school).order_by('last_name', 'first_name', 'username'))
    school_name = school.name if school else 'ExamNook'
    teacher_names = lambda obj: ', '.join(t.get_full_name() or t.username for t in obj.teachers.all())
    section_rows = [(c.name, teacher_names(c), c.pupil_count) for c in classes]
    subject_rows = [(s.name, teacher_names(s), s.pupil_count) for s in subjects]

    def pupils_with_sections():
        pupils = list(User.assignable_pupils(school).exclude(email='')
                      .prefetch_related('classes_enrolled', 'subjects_enrolled'))
        rows = []
        for p in pupils:
            secs = sorted(c.name for c in p.classes_enrolled.all())
            # Last name A–Z (then first name), the app's default order.
            rows.append(((p.last_name or p.first_name).lower(), p.first_name.lower(), p.email.lower(), p,
                         ', '.join(secs), ', '.join(sorted(s.name for s in p.subjects_enrolled.all()))))
        rows.sort(key=lambda r: r[:3])
        return [(r[3], r[4], r[5]) for r in rows]

    if kind in GROUP_KINDS:
        cfg = GROUP_KINDS[kind]
        noun, Noun, page = cfg['noun'], cfg['noun'].capitalize(), cfg['page']
        existing = classes if kind == 'classes' else subjects
        headers = [f'{Noun} name *', 'Teacher emails', 'Rename to']
        _style_header(ws, headers, [34, 52, 30], required={headers[0]})
        # Pre-filled with the school's current sections/subjects so "update" = edit and re-upload.
        for i, obj in enumerate(existing, start=2):
            ws.cell(row=i, column=1, value=obj.name)
            ws.cell(row=i, column=2, value=', '.join(t.email or t.username for t in obj.teachers.all()))
        if kind == 'classes':
            examples = [('Year group name *', 'Teacher emails', 'Rename to'),
                        ('Pre-U 1', 'ana@school.edu', ''),
                        ('Pre-U 2', 'ben@school.edu, carla@school.edu', ''),
                        ('Grade 7 - Newton', '', 'Grade 7 - Einstein')]
            then = ('Then put pupils into the year groups with "Bulk add pupils to year groups" on the Year groups page '
                    '(or straight from Roles → Pupils → Bulk add).')
            name_example = 'Pre-U 1'
        else:
            examples = [('Subject name *', 'Teacher emails', 'Rename to'),
                        ('Pre-U 1 Economics', 'ana@school.edu', ''),
                        ('Pre-U 1 Mathematics', 'ben@school.edu, carla@school.edu', ''),
                        ('Pre-U 1 Physics', '', 'Pre-U 1 Physics (H2)')]
            then = ('Then add pupils — from any year group — with "Bulk add pupils to subjects" on the Subjects page. '
                    'You can add a whole year group to a subject in one row.')
            name_example = 'Pre-U 1 Economics'
        _instructions_sheet(
            wb, f'{Noun}s & teachers import — {school_name}',
            f'Use this file to add many {noun}s at once and set which teacher(s) teach each one. '
            f'Your existing {noun}s are already listed on the {ws.title} tab — edit them to update, '
            f'add new rows to create new {noun}s.',
            [f'Fill in the "{ws.title}" tab — one row per {noun}. Keep the header row as it is.',
             f'Put each {noun}\'s teacher email(s) in "Teacher emails". Several teachers? Separate them with commas '
             '(e.g. ana@school.edu, ben@school.edu). The "Teacher list" tab shows every teacher account.',
             'Save the file (.xlsx). In Google Sheets: File → Download → Microsoft Excel (.xlsx) or CSV.',
             f'In ExamNook go to {page} → Bulk import {noun}s, upload the file and click "Check file".',
             'Review the preview (green = new, blue = update, red = error). Fix any errors, or skip them, then click Import.',
             then],
            [(f'{Noun} name *', 'Yes', f'The {noun} name exactly as you want it to appear. '
              f'If a {noun} with this name already exists it is UPDATED, otherwise it is CREATED. '
              'Matching ignores upper/lower case and extra spaces.', name_example),
             ('Teacher emails', 'No', f'Email (or username) of the teacher(s). Separate several with commas. '
              f'This REPLACES the {noun}\'s current teachers. Leave blank to keep the current teachers; '
              'type NONE to remove all teachers. Teachers must already have an account (Roles page).',
              'ana@school.edu, ben@school.edu'),
             ('Rename to', 'No', f'Only to rename an EXISTING {noun}: put the new name here. '
              'Leave blank otherwise.', examples[3][2])],
            examples,
            ['Rows are checked first — nothing is saved until you click Import on the preview.',
             f'Deleting a row from this file does NOT delete the {noun}. Delete {noun}s on the {page} page.',
             'Blank rows are ignored. Up to 3,000 rows per upload.'])
        n = _ref_sheet(wb, 'Teacher list', ['First name', 'Last name', 'Email', 'Username'], [20, 22, 36, 22],
                       [(t.first_name, t.last_name, t.email, t.username) for t in teachers],
                       'No teacher accounts yet — add them on the Roles page.')
        _dropdown(ws, 'Teacher list', n, 'B2:B3001', 'Pick a teacher, or type several emails separated by commas', col='C')

    elif kind == 'section_pupils':
        headers = ['First name', 'Last name', 'Email *', 'Current year group(s)', 'Add to year group *']
        _style_header(ws, headers, [20, 22, 34, 28, 34], required={'Email *', 'Add to year group *'})
        grey = Font(color='6B7280')
        for i, (p, secs, _subs) in enumerate(pupils_with_sections(), start=2):
            ws.cell(row=i, column=1, value=p.first_name or p.username).font = grey
            ws.cell(row=i, column=2, value=p.last_name).font = grey
            ws.cell(row=i, column=3, value=p.email)
            ws.cell(row=i, column=4, value=secs).font = grey
        ws['E1'].fill = PatternFill('solid', fgColor='11793A')
        ws.auto_filter.ref = f'A1:E{max(ws.max_row, 2)}'
        _instructions_sheet(
            wb, f'Add pupils to year groups — {school_name}',
            'Use this file to put pupils who ALREADY have an account into year groups. Every pupil is listed on the '
            '"Year group pupils" tab with the year group(s) they\'re in now — just fill in "Add to year group" for the ones '
            'you want to add and leave the rest blank. (To create new pupil accounts, use Roles → Pupils → Bulk add.)',
            ['Make sure the year groups exist (Year groups page, or "Bulk import year groups"). The "Year group list" tab shows them.',
             'On the "Year group pupils" tab, pick a year group in "Add to year group" for each pupil to add. Rows left blank are skipped.',
             'A pupil going into two year groups? Type both, separated by a comma.',
             'Save the file (.xlsx) and upload it on Year groups → Bulk add pupils to year groups. Click "Check file".',
             'Review the preview, then click Import.'],
            [('Email *', 'Yes', 'The pupil\'s school email — must match an existing pupil account.', 'juan.delacruz@school.edu'),
             ('First name', 'No', 'Just for reference — ignored on import.', 'Juan'),
             ('Last name', 'No', 'Just for reference — ignored on import. Rows are listed by last name A–Z.', 'Dela Cruz'),
             ('Current year group(s)', 'No', 'Just for reference — ignored on import.', 'Pre-U 1'),
             ('Add to year group *', 'Yes (to add)', 'An EXISTING year group (dropdown). Several: separate with commas. '
              'Leave blank to skip the row.', 'Pre-U 2')],
            [('First name', 'Last name', 'Email *', 'Current year group(s)', 'Add to year group *'),
             ('Juan', 'Dela Cruz', 'juan.delacruz@school.edu', '', 'Pre-U 1'),
             ('Maria', 'Santos', 'maria.santos@school.edu', 'Pre-U 1', ''),
             ('Ken', 'Tan', 'ken.tan@school.edu', '', 'Pre-U 2, Chess Club')],
            ['Rows are checked first — nothing is saved until you click Import on the preview.',
             'This only ADDS — nobody is taken out of a year group. Remove pupils on the Year groups page.',
             'Using your own sheet? It needs an "Email" column and a "Year group" (or "Add to year group") column.',
             'Up to 3,000 rows per upload.'])
        n = _ref_sheet(wb, 'Year group list', ['Year group name', 'Teachers', 'Pupils now'], [34, 44, 12], section_rows,
                       'No year groups yet — create them first.')
        _dropdown(ws, 'Year group list', n, 'E2:E3001', 'Pick an existing year group (or type several separated by commas)')

    elif kind == 'subject_pupils':
        headers = ['First name', 'Last name', 'Email', 'Year group', 'Subjects *']
        _style_header(ws, headers, [34, 22, 34, 24, 46], required={'Subjects *'})
        ws['E1'].fill = PatternFill('solid', fgColor='11793A')
        grey = Font(color='6B7280')
        bold = Font(bold=True, color='28186F')
        band = PatternFill('solid', fgColor='EEF5FB')
        r = 2
        # One row per section first: fill in Subjects there to add the WHOLE section.
        for c in classes:
            ws.cell(row=r, column=1, value=f'(everyone in this year group — {c.pupil_count} pupil{"s" if c.pupil_count != 1 else ""})').font = bold
            ws.cell(row=r, column=4, value=c.name).font = bold
            for col in range(1, 6):
                ws.cell(row=r, column=col).fill = band
            r += 1
        for p, secs, subs in pupils_with_sections():
            ws.cell(row=r, column=1, value=p.first_name or p.username).font = grey
            ws.cell(row=r, column=2, value=p.last_name).font = grey
            ws.cell(row=r, column=3, value=p.email)
            ws.cell(row=r, column=4, value=secs).font = grey
            r += 1
        _instructions_sheet(
            wb, f'Add pupils to subjects — {school_name}',
            'Use this file to put pupils into subjects. A subject can take pupils from several year groups. '
            'The "Subject pupils" tab starts with one shaded row per SECTION, then one row per PUPIL (by last name A–Z). '
            'Fill in "Subjects" on a year group row to add everyone in that year group, or on a pupil row to add just that pupil.',
            ['Make sure the subjects exist (Subjects page, or "Bulk import subjects"). The "Subject list" tab shows them.',
             'Whole year group taking a subject? Type the subject(s) on that year group\'s shaded row.',
             'Only some pupils? Type the subject(s) on each pupil\'s row. Mix both freely; rows left blank are skipped.',
             'Several subjects in one cell: separate them with commas (e.g. Economics, Mathematics).',
             'Save the file (.xlsx) and upload it on Subjects → Bulk add pupils to subjects. Click "Check file".',
             'Review the preview (it says how many pupils each row adds), then click Import.'],
            [('Email', 'For pupil rows', 'The pupil\'s school email. Leave BLANK on a year group row.', 'juan.delacruz@school.edu'),
             ('First name', 'No', 'Just for reference — ignored on import.', 'Juan'),
             ('Last name', 'No', 'Just for reference — ignored on import.', 'Dela Cruz'),
             ('Year group', 'For year group rows', 'On a year group row (no email): the year group whose pupils are all added. '
              'On a pupil row it is only for reference.', 'Pre-U 1'),
             ('Subjects *', 'Yes (to add)', 'EXISTING subject(s), separated by commas. Leave blank to skip the row.',
              'Pre-U 1 Economics, Pre-U 1 Mathematics')],
            [('First name', 'Last name', 'Email', 'Year group', 'Subjects *'),
             ('(everyone in this year group)', '', '', 'Pre-U 1', 'Pre-U 1 Economics'),
             ('(everyone in this year group)', '', '', 'Pre-U 2', 'Pre-U 1 Economics, Pre-U 1 Physics'),
             ('Ken', 'Tan', 'ken.tan@school.edu', 'Year 10', 'Pre-U 1 Economics')],
            ['Rows are checked first — nothing is saved until you click Import on the preview.',
             'This only ADDS — nobody is taken out of a subject. Remove pupils on the Subjects page.',
             'A year group row adds the pupils who are in that year group at the moment you import.',
             'Using your own sheet? It needs a "Subjects" column plus "Email" and/or "Year group".',
             'Up to 3,000 rows per upload.'])
        n = _ref_sheet(wb, 'Subject list', ['Subject name', 'Teachers', 'Pupils now'], [34, 44, 12], subject_rows,
                       'No subjects yet — create them first (Subjects → Bulk import subjects).')
        _dropdown(ws, 'Subject list', n, 'E2:E5001', 'Pick a subject (or type several separated by commas)')

    elif kind == 'pupils':
        headers = ['First name *', 'Last name', 'Email *', 'Year group', 'Subjects']
        _style_header(ws, headers, [24, 24, 36, 30, 40], required={'First name *', 'Email *'})
        if prefill:
            for i, (p, secs, subs) in enumerate(pupils_with_sections(), start=2):
                for col, value in enumerate((p.first_name, p.last_name, p.email, secs, subs), start=1):
                    ws.cell(row=i, column=col, value=value)
        _instructions_sheet(
            wb, f'Pupils import — {school_name}',
            'Use this file to create pupil accounts and, optionally, put them into a year group and subjects in one go. '
            'Year groups and subjects you name must ALREADY exist in ExamNook. Leave them blank to just create the account.',
            ['Make sure the year groups and subjects exist. The "Year group list" and "Subject list" tabs show them.',
             'Fill in the "Pupils" tab — one row per pupil. Pick the year group from the dropdown in the "Year group" column.',
             'Moving from another spreadsheet? Copy the columns and use Paste special → Values only so the formatting stays clean.',
             'Save the file (.xlsx). In Google Sheets: File → Download → Microsoft Excel (.xlsx) or CSV.',
             'In ExamNook go to Roles → Pupils → Bulk add, upload the file and click "Check file".',
             'Review the preview, tick "Email invites" if you want each new pupil emailed their sign-in details, '
             'then click Import. You can also download the logins file (shown only once).'],
            [('First name *', 'Yes (new pupils)', 'Pupil\'s first / given name(s).', 'Juan Miguel'),
             ('Last name', 'No', 'Pupil\'s surname. Instead of First/Last name you may use one "Name" column; '
              'write it as "Surname, First names" to split it correctly.', 'Dela Cruz'),
             ('Email *', 'Yes', 'The pupil\'s school email — this is how ExamNook recognises them. '
              'If the pupil already has an account, they are simply added (no new account).',
              'juan.delacruz@school.edu'),
             ('Year group', 'No', 'Name of an EXISTING year group (see the "Year group list" tab). Several: separate with commas. '
              'Leave blank to add them later.', 'Pre-U 1'),
             ('Subjects', 'No', 'EXISTING subject(s), separated by commas. Leave blank to add them later '
              '(Subjects → Bulk add pupils to subjects).', 'Pre-U 1 Economics, Pre-U 1 Mathematics')],
            [('First name *', 'Last name', 'Email *', 'Year group', 'Subjects'),
             ('Juan Miguel', 'Dela Cruz', 'juan.delacruz@school.edu', 'Pre-U 1', 'Pre-U 1 Economics'),
             ('Maria', 'Santos', 'maria.santos@school.edu', 'Pre-U 1', ''),
             ('Ken', 'Tan', 'ken.tan@school.edu', 'Pre-U 2', 'Pre-U 1 Economics, Pre-U 1 Physics')],
            ['Rows are checked first — nothing is saved until you click Import on the preview.',
             'Downloaded "with current pupils"? Everyone already in ExamNook is listed. Re-uploading them changes nothing '
             'unless you edit a row: a new name is updated; extra year groups / subjects are added. Add new pupils at the bottom.',
             'Existing pupils are never removed from other year groups or subjects — this import only ADDS.',
             'A pupil email that belongs to a teacher, or to another school, is reported as an error.',
             'Column names are flexible: "Surname", "Email address", "Class" etc. also work.',
             'Blank rows are ignored. Up to 3,000 rows per upload.'])
        n = _ref_sheet(wb, 'Year group list', ['Year group name', 'Teachers', 'Pupils now'], [34, 44, 12], section_rows,
                       'No year groups yet — import year groups first.')
        _dropdown(ws, 'Year group list', n, 'D2:D3001', 'Pick an existing year group (or type several separated by commas)')
        n = _ref_sheet(wb, 'Subject list', ['Subject name', 'Teachers', 'Pupils now'], [34, 44, 12], subject_rows,
                       'No subjects yet — leave "Subjects" blank, or create subjects first.')
        _dropdown(ws, 'Subject list', n, 'E2:E3001', 'Pick a subject (or type several separated by commas)')

    else:
        # Teachers are linked to the SUBJECTS they teach. (A "Sections"/"Year group" column is still
        # accepted if present, for older files, but the template no longer has one.)
        headers = ['First name *', 'Last name', 'Email *', 'Role', 'Subjects']
        _style_header(ws, headers, [22, 22, 34, 14, 48], required={'First name *', 'Email *'})
        if prefill:
            # Main admins (superusers) are managed in Django admin, so they're left out.
            staff = (assignable_teachers(school).filter(is_superuser=False).exclude(email='')
                     .prefetch_related('subjects_taught').order_by('last_name', 'first_name', 'email'))
            for i, t in enumerate(staff, start=2):
                values = (t.first_name, t.last_name, t.email, 'Admin' if t.is_school_admin else 'Teacher',
                          ', '.join(sorted(sj.name for sj in t.subjects_taught.all())))
                for col, value in enumerate(values, start=1):
                    ws.cell(row=i, column=col, value=value)
        _instructions_sheet(
            wb, f'Teachers import — {school_name}',
            'Use this file to create teacher and admin accounts in one go and, if you like, say which of your '
            'EXISTING subjects each one teaches. The "Subject list" tab shows them. '
            'Existing staff (same email) are updated: role changed and/or subjects added.',
            ['Fill in the "Teachers" tab — one row per person. Keep the header row as it is.',
             'Role: pick Teacher or Admin from the dropdown (blank = Teacher for new people, unchanged for existing ones).',
             'Subjects: pick one from the dropdown, or type several separated by commas. Leave blank to assign later.',
             'Save the file (.xlsx). In Google Sheets: File → Download → Microsoft Excel (.xlsx) or CSV.',
             'In ExamNook go to Roles → Staff → Bulk add, upload the file and click "Check file".',
             'Review the preview, tick "Email invites" to send each new person their sign-in details, then click Import.'],
            [('First name *', 'Yes (new people)', 'First / given name(s).', 'Ana'),
             ('Last name', 'No', 'Surname. Or use one "Name" column written as "Surname, First names".', 'Reyes'),
             ('Email *', 'Yes', 'Their school email — this is how ExamNook recognises them.', 'ana.reyes@school.edu'),
             ('Role', 'No', 'Teacher (works only with the subjects and year groups they\'re assigned) or Admin (can set up '
              'year groups and subjects and add people).', 'Teacher'),
             ('Subjects', 'No', 'EXISTING subject(s) they teach, separated by commas. Only ever ADDS — it never removes '
              'them from other subjects.', 'Pre-U 1 Economics, Pre-U 2 Economics')],
            [('First name *', 'Last name', 'Email *', 'Role', 'Subjects'),
             ('Ana', 'Reyes', 'ana.reyes@school.edu', 'Teacher', 'Pre-U 1 Economics'),
             ('Ben', 'Cruz', 'ben.cruz@school.edu', 'Admin', ''),
             ('Carla', 'Lim', 'carla.lim@school.edu', 'Teacher', 'Pre-U 1 Physics, Pre-U 2 Physics')],
            ['Rows are checked first — nothing is saved until you click Import on the preview.',
             'Downloaded "with current staff"? Everyone already in ExamNook is listed (main admins excepted). Re-uploading them '
             'changes nothing unless you edit a row: a new name or role is updated; extra subjects are added. Add new staff at the bottom.',
             'A subject name that doesn\'t exist is shown as an error — fix the spelling or create it first '
             '(Subjects → Bulk import subjects).',
             'An email that belongs to a pupil, or to another school, is reported as an error.',
             'Blank rows are ignored. Up to 3,000 rows per upload.'])
        n = _ref_sheet(wb, 'Subject list', ['Subject name', 'Teachers now', 'Pupils'], [34, 44, 10], subject_rows,
                       'No subjects yet — leave "Subjects" blank, or create subjects first.')
        _dropdown(ws, 'Subject list', n, 'E2:E3001', 'Pick an existing subject (or type several separated by commas)')
        from openpyxl.worksheet.datavalidation import DataValidation
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
        prefill = request.GET.get('prefill') in ('1', 'true') and kind in ('pupils', 'teachers')
        content = build_template(kind, getattr(request, 'school', None), prefill=prefill)
    except ImportError:
        return HttpResponseBadRequest('Excel templates need openpyxl installed on the server (pip install openpyxl).')
    stamp = timezone.localdate().isoformat()
    label = {'classes': 'year-groups', 'section_pupils': 'year-group-pupils', 'subject_pupils': 'subject-pupils'}.get(kind, kind)
    filename = f'examnook-{label}-{"current" if prefill else "template"}-{stamp}.xlsx'
    response = HttpResponse(content, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response
