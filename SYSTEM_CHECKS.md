# ExamHub — System Checks

A living checklist for verifying that ExamHub is healthy before (and after) every deployment or significant change.
Mark each item `✅ Pass`, `❌ Fail`, or `⚠️ Warning` as you work through it.

> **Version coverage:** migrations `0001` – `0029` · v0.22.0 + Unreleased (year groups & subjects, exam privacy, teacher marking, submission notices, exam resources, zoom, new invite email)

---

## 1. Environment & Configuration

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 1.1 | Python version matches `.python-version` | ≥ 3.12 | |
| 1.2 | All packages in `requirements.txt` are installed | No missing packages | |
| 1.3 | `.env` file exists at project root | Present | |
| 1.4 | `SECRET_KEY` is set (and not the insecure default in production) | Non-default value in prod | |
| 1.5 | `DEBUG` is `False` in production | `False` | |
| 1.6 | `ALLOWED_HOSTS` accepts the production domain: `.railway.app` (any Railway domain) + `RAILWAY_PUBLIC_DOMAIN` + anything in the `ALLOWED_HOSTS` env var (comma-separated, for a custom domain) | `digitalexamhub.up.railway.app` (or custom domain) not refused with 400 | |
| 1.7 | `CSRF_TRUSTED_ORIGINS` covers the same hosts (`https://*.railway.app` + each extra host) | Sign-in and form posts accepted on the live domain | |
| 1.8 | `SchoolTenantMiddleware` is listed in `MIDDLEWARE` (after Session/Auth) | Present and correctly ordered | |
| 1.9 | `TENANT_EXEMPT_PATHS` (optional) does not accidentally expose school routes | Only safe paths listed | |
| 1.10 | `digitalexams.accounts.ForcePasswordChangeMiddleware` is listed **after** `SchoolTenantMiddleware` | Present, last in `MIDDLEWARE` | |
| 1.11 | Email configured: `EMAIL_HOST_USER` + `EMAIL_HOST_PASSWORD` (App Password) set in `.env` / Railway | `EMAIL_BACKEND` resolves to SMTP in production | |
| 1.12 | Test email actually arrives (not in spam) from the configured account | Delivered | |
| 1.13 | ⚠️ Database credentials are **not** hard-coded in `settings.py` | Read from env (currently hard-coded — move to `.env` and rotate) | |
| 1.14 | `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` is set | Links and `.seb` files built with `https://` | |
| 1.15 | `SITE_URL` env var (optional) matches the public address used in invite emails | Unset = `https://digitalexamhub.up.railway.app` | |

### How to verify
```bash
# Check installed packages
pip freeze | diff - requirements.txt

# Check DEBUG value
python manage.py shell -c "from django.conf import settings; print('DEBUG:', settings.DEBUG)"

# Check hosts and the address used in invites
python manage.py shell -c "from django.conf import settings; print(settings.ALLOWED_HOSTS, settings.CSRF_TRUSTED_ORIGINS)"
python manage.py shell -c "from digitalexams.accounts import site_url; print(site_url())"

# Check email backend + send a test message
python manage.py shell -c "from django.conf import settings; print(settings.EMAIL_BACKEND, settings.DEFAULT_FROM_EMAIL)"
python manage.py sendtestemail you@school.edu
```

---

## 2. Database Connectivity

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 2.1 | PostgreSQL connection succeeds (Railway proxy) | No `OperationalError` | |
| 2.2 | All migrations are applied (no pending) | `No migrations to apply` | |
| 2.3 | `AUTH_USER_MODEL` points to `digitalexams.User` | Custom user model active | |
| 2.4 | SQLite `db.sqlite3` is **not** the active DB in production | PostgreSQL is default | |

### How to verify
```bash
python manage.py dbshell                  # should open a psql prompt
python manage.py migrate --check          # exit 0 = all applied
python manage.py showmigrations           # all rows show [X]
```

---

## 3. Migrations Integrity

All 29 migrations must be present and applied in order:

| Migration | Description |
|-----------|-------------|
| `0001_initial` | Base schema — User, Assignment, Submission, Recipient |
| `0002` | User groups / superuser flag adjustments |
| `0003` | `user.role` default set to `PUPIL` |
| `0004` | `Assignment.duration_minutes` + `Submission.started_at` |
| `0005` | `AssignmentText` model + `Submission.response_text` |
| `0006` | Copy source PDFs to texts |
| `0007` | Remove `Assignment.source_pdf` |
| `0008` | Per-text submission responses (`responses` JSON field) |
| `0009` | Delivery settings (mode, join code, calculator flags) |
| `0010` | `SchoolClass` model |
| `0011` | `AssignmentItem` model (replaces AssignmentText) |
| `0012` | Phase 4 & 5 — grading (`item_scores`, `total_score`, `published`) |
| `0013` | `ActivityLogEntry` BigAutoField + `Message` model |
| `0014` | `AllowedBrowserExamKey` (SEB integration) |
| `0015` | `AssignmentItem.image`, `section` field, video support |
| `0016` | Assignable pupils constraint + `SchoolClass.pupils` M2M |
| `0017` | `Message.is_broadcast` + per-recipient read-tracking timestamps |
| `0018` | Submission exit-lock fields (`locked_at`, `resume_code`, `lock_count`) |
| `0019` | `ActivityLogEntry.school_class` FK |
| `0020` | `User.tour_completed_at` |
| `0021` | `School` model + `User.school` FK + `User.is_school_admin` + school FKs on `SchoolClass`, `Assignment`, `AllowedBrowserExamKey` |
| `0022` | `EmailLoginCode` (passwordless sign-in codes) |
| `0023` | `Submission.grade_seen_at` (pupil grade notification) |
| `0024` | `Assignment.is_hidden` + `Assignment.closed_at` (visibility / manual close); `duration_minutes` no longer used |
| `0025` | `User.must_change_password`, `User.invited_at`, `User.activated_at` (invites + first-login password) |
| `0026` | `StoredFile` (uploaded files kept in the database as well as on disk) |
| `0027` | `Subject` model (school, name, teachers M2M, pupils M2M) + "Year group" labels on `SchoolClass` |
| `0028` | `Submission.teacher_marks` (teacher's marking JSON) + `Submission.teacher_seen_at` (new-submission notices) |
| `0029` | `ExamResource` (guide PDFs / images / text attached to an exam) |

```bash
python manage.py showmigrations digitalexams
```

---

## 4. Static Files & Media

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 4.1 | `collectstatic` completes without errors | Exit 0 | |
| 4.2 | `STATIC_ROOT` (`staticfiles/`) is populated | Files present | |
| 4.3 | WhiteNoise middleware is in `MIDDLEWARE` | Position 2 (after Security) | |
| 4.4 | `MEDIA_ROOT` (`media/`) exists and is writable | Writable directory | |
| 4.5 | Uploaded PDFs / images / videos are served at `/media/` | HTTP 200 response | |
| 4.6 | `STORAGES["default"]` is `digitalexams.storage.DatabaseBackedStorage` | Uploads also saved to `StoredFile` | |
| 4.7 | `python manage.py sync_media_to_db` run on the machine holding `media/` reports 0 "not found" | All files in DB | |
| 4.8 | An exam PDF uploaded locally opens on the Railway site (and survives a redeploy) | Teacher review + pupil view load it | |

```bash
python manage.py collectstatic --noinput
ls staticfiles/
```

---

## 5. Authentication & User Roles

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 5.1 | Login page renders at `/s/<slug>/login/` with **Password** and **Email me a code** tabs | HTTP 200 | |
| 5.2 | Unauthenticated requests redirect to `/login/` | HTTP 302 → login | |
| 5.3 | Superuser created via `createsuperuser` gets `role=TEACHER` | No pupil-role superusers | |
| 5.4 | `can_manage_classes` works: superuser ✅ / staff teacher ✅ / teacher ❌ | Per role | |
| 5.5 | Pupil login redirects to home, not teacher dashboard | HTTP 200 home view | |
| 5.6 | Password-change endpoint (`/api/account/change-password/`) returns 200 | Valid change accepted | |
| 5.7 | Email code: username or email → 6-digit code emailed; correct code signs in | HTTP 302 → home | |
| 5.8 | Email code: wrong code ×5 burns it; expired after 10 min; reused code rejected | "incorrect or has expired" | |
| 5.9 | Email code: unknown account gets the same reply and no email; 60 s resend cooldown | No enumeration | |
| 5.10 | Email code: account from another school (other `/s/<slug>/`) gets no code | No email sent | |
| 5.11 | "Forgot password?" → code sign-in → change-password dialog opens without old password (once, 15 min) | Password changed | |
| 5.12 | Sign out works from the Exams page and teacher pages | HTTP 302 → login | |
| 5.13 | Pupil sign-in goes straight to "Loading your exams…" → their task list (no teacher dashboard, role switch or "Preview as a pupil" page flashes) | Pupil layout from first paint | |
| 5.14 | Every password box has a show / hide (eye) button (sign-in, first password, change password, Roles, superadmin) | Toggles between dots and text | |
| 5.15 | Same pupil email at two schools: each school's sign-in page (`/s/<slug>/login/`) signs in that school's account; username always works on the main sign-in page | Correct account | |

---

## 6. Assignment & Submission Workflows

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 6.1 | `POST /api/assignments/create/` creates exam + items + recipients (`visibility`, `opens_at`, `due_at`; no duration), plus any resources (`resources_meta` + `resource_files`) | HTTP 201 | |
| 6.2 | Recipient can start a submission (`/api/submissions/<id>/start/`) | HTTP 200, `started_at` set | |
| 6.3 | Autosave (`/api/submissions/<id>/save/`, every ~8 s on change) persists responses; no "Save draft" button for pupils | HTTP 200, `responses` updated | |
| 6.4 | Final submit (`/api/submissions/<id>/submit/`) sets status `submitted` | HTTP 200, `submitted_at` set | |
| 6.5 | Auto-grading runs on submit for MCQ / True-False / Fill-text items | `item_scores` populated | |
| 6.6 | Manual grade save (`/api/submissions/<id>/grade/`) updates `item_scores` | HTTP 200 | |
| 6.7 | Grade publish (`/api/submissions/<id>/publish/`) sets `published=True` | HTTP 200 | |
| 6.8 | Bulk publish (`/api/assignments/<id>/publish-grades/`) covers all submissions | HTTP 200 | |
| 6.9 | Pupil opening **submitted** work: no fullscreen / exit lock / copy guard; Lockdown files load outside SEB; "read-only" notice shown | Normal browser view | |
| 6.10 | Submitting sends **no** email to the teacher; it shows as a new submission instead (see 20.8) | `mail.outbox` empty, `newSubmission: true` | |
| 6.11 | Edit exam → *Assigned to*: ticking adds pupils, unticking removes them; removing a pupil who started asks for confirmation (409 `remove_started` → `confirmRemoveStarted`) | Recipients updated | |
| 6.12 | Exams dashboard: "Created from / to", "Last 7 days", "Last 30 days", "Clear" filter the list (Manila dates); "select all" only selects filtered exams | Filtered rows only | |

---

## 7. Security & Lockdown (SEB)

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 7.1 | SEB config download (`/api/seb/config/`) requires teacher login | HTTP 200 for teachers, 302 for guests | |
| 7.2 | SEB check endpoint (`/api/seb/check/`) validates Browser Exam Key hash | Matches `AllowedBrowserExamKey` | |
| 7.3 | At least one `AllowedBrowserExamKey` row exists in the DB for lockdown exams | Admin verified | |
| 7.4 | `examhub-lockdown.seb` config file is present in project root | File present | |
| 7.5 | `Any browser with security` delivery mode triggers exit-lock on tab switch | `locked_at` set, saves refused | |
| 7.6 | Teacher can unlock a pupil via `/api/submissions/<id>/unlock/` | `locked_at` cleared | |
| 7.7 | Pupil can resume with a one-word code via `/api/submissions/<id>/resume/` | `locked_at` cleared | |
| 7.8 | CSRF protection active on all POST/PUT/DELETE endpoints | 403 on missing CSRF | |
| 7.9 | After a domain change: `.seb` file downloaded again and old Allowed Browser Exam Keys replaced; a Lockdown exam opens in SEB on the new domain | Exam opens | |
| 7.10 | In a Lockdown exam the Resources window and its files load inside SEB, and are refused outside it | 200 in SEB / SEB-required outside | |
| 7.11 | Inside SEB, a Lockdown exam opens straight into the paper (no "Open in Safe Exam Browser" dialog): `/s/<slug>/api/seb/check/` returns `ok: true` — the hash is checked against the full `/s/<slug>/…` URL | Exam opens, timer starts | |
| 7.12 | Newer SEB (macOS / iPad) without hash headers: the page verifies via `POST /api/seb/verify/` (SEB JavaScript API) and the exam opens; in a normal browser it stays blocked | Opens in SEB only | |
| 7.13 | One-time launch (`/seb/enter/<token>/`): works with headers (redirect) and with the JavaScript API (small "Opening your exam…" page); an expired / used link says so | Pupil signed in inside SEB | |
| 7.14 | Submitting inside SEB (any delivery mode) quits SEB within ~2 s via the quit address `/?sebquit=1` (`quitURLConfirm` false); if it doesn't, "You can now close Safe Exam Browser" is shown | SEB closes | |

---

## 8. Messaging & Real-Time Chat

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 8.1 | Teacher can send a message (`/api/recipients/<id>/messages/send/`) | HTTP 200, `Message` created | |
| 8.2 | Broadcast message (`/api/assignments/<id>/broadcast-message/`) creates one `Message` per recipient | Count matches recipients | |
| 8.3 | Unread dot: messages newer than `teacher_last_read_at` / `pupil_last_read_at` are flagged | Correct unread count | |
| 8.4 | Message thread is scoped to one `Recipient` (not global) | No cross-assignment leakage | |

---

## 9. Permissions (API-level)

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 9.1 | Regular teacher cannot create/update/delete classes | HTTP 403 | |
| 9.2 | Regular teacher cannot create pupil accounts | HTTP 403 | |
| 9.3 | Regular teacher can only assign to pupils in their own classes | HTTP 403 for outside pupils | |
| 9.4 | Only superusers **and school admins** (`is_school_admin`) can access `/roles/`, manage accounts and send invites | HTTP 403 / redirect for others | |
| 9.5 | Pupils are redirected away from `/classes/`, `/activity/`, `/roles/`, `/teachers/` | HTTP 302 → `/` | |
| 9.6 | Activity is **admin-only**: plain teachers get no Activity tab, `/activity/` redirects, `/api/activity-log/` → 403 | HTTP 302 / 403 | |
| 9.7 | Data is scoped to the school in the URL: a superuser on `/s/<new-school>/` sees only that school's pupils, classes and teachers | No other school's data | |
| 9.8 | Pupils cannot call `/api/assignments/<id>/state/`, `/api/accounts/invite/` or `/api/accounts/pupils/` | HTTP 403 | |
| 9.9 | **Teachers only see their own exams**: dashboard, notifications and every exam action (status, edit, open/close, delete, messages, grading, publishing, unlock, files) refuse another teacher's exam; admins see all in the school | "This exam belongs to another teacher." (403) | |
| 9.10 | Plain teachers assign **by subject only**: "Assign to" shows their subjects, no year groups; assigning a pupil outside their subjects is refused | HTTP 403 | |
| 9.11 | Plain teachers: no "Teacher view / Pupil preview" switch, no "Preview as a pupil", no Year groups tab (`/classes/` → `/subjects/`) | Teacher view only | |
| 9.12 | Teacher marks (`/api/submissions/<id>/marks/`) and "seen" endpoints: exam's own teacher or admins only | HTTP 403 for others | |
| 9.13 | Exam resources: add / delete only by the exam's teacher or admins; files (`/api/resources/<id>/file/`) only for assigned pupils, the exam's teacher and admins | HTTP 403 for others | |

---

## 10. Guided Tour

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 10.1 | New user sees `const AUTO_START = true` on first page load | Tour auto-starts | |
| 10.2 | `POST /api/account/tour-done/` sets `tour_completed_at` | HTTP 200, field set | |
| 10.3 | After marking done, `const AUTO_START = false` on subsequent loads | Tour does not repeat | |
| 10.4 | `data-start-tour` attribute is present in nav (manual replay) | Attribute in HTML | |
| 10.5 | Correct `ROLE` constant per user type: `main_admin` / `admin` / `teacher` / `pupil` | Role string matches | |
| 10.6 | Skip / Finish posts to `/s/<slug>/api/account/tour-done/` (200); tour doesn't restart on other tabs or next sign-in, even in a new browser | Tour shown once per account | |
| 10.7 | A newly invited account sees the tour only **after** choosing their own password | `AUTO_START = true` on first home load after `/welcome/password/` | |

---

## 11. Deployment (Railway)

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 11.1 | `Procfile` specifies the correct `web` process | `gunicorn examhub.wsgi` | |
| 11.2 | Railway build succeeds (no pip install errors) | Build log clean | |
| 11.3 | Production URL responds with HTTP 200 | `https://digitalexamhub.up.railway.app/` | |
| 11.4 | Migrations run automatically on deploy (or via release command) | No unapplied migrations on live DB | |
| 11.5 | SSL/TLS active on the Railway domain | HTTPS, valid cert | |
| 11.6 | `sslmode=require` in DB `OPTIONS` | Encrypted connection | |

---

## 12. Django System Check Framework

Run Django's built-in checks before every deployment:

```bash
python manage.py check --deploy
```

> **Important:** All issues with severity **ERROR** must be fixed before deploying.
> **WARNING**-level items (e.g. `SECURE_HSTS_SECONDS` not set) should be reviewed and addressed for production hardening.

---

## 13. Multi-Tenancy & School Middleware

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 13.1 | `GET /s/<valid-slug>/` resolves and sets `request.school` | HTTP 200, `request.school` populated | |
| 13.2 | `GET /s/nonexistent/` returns 404 | HTTP 404 | |
| 13.3 | Request to an **inactive** school returns 403 | HTTP 403 with clear message | |
| 13.4 | Teacher/pupil from School A accessing `/s/<school-b-slug>/` returns 403 | HTTP 403 | |
| 13.5 | Bare path `/classes/` for authenticated user auto-redirects to `/s/<slug>/classes/` | HTTP 302 → `/s/<slug>/classes/` | |
| 13.6 | Exempt paths (`/admin/`, `/superadmin/`, `/static/`, `/media/`) bypass middleware | No tenant resolution, no 404 | |
| 13.7 | `request.school_url_prefix` is set to `/s/<slug>` on tenant requests | Prefix string correct | |
| 13.8 | Per-school `primary_color` rendered in the `<style>` block of all pages | Hex color matches `School.primary_color` | |
| 13.9 | Per-school `name` appears in page `<title>` / header | School name displayed | |

---

## 14. Superadmin Dashboard

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 14.1 | `GET /superadmin/` unauthenticated redirects to login | HTTP 302 → `/login/` | |
| 14.2 | `GET /superadmin/` for a regular teacher/admin returns 403 | HTTP 403 | |
| 14.3 | `GET /superadmin/` for platform superuser returns 200 with school list | HTTP 200 | |
| 14.4 | `POST /superadmin/api/schools/create/` creates `School` + first admin account | School and user exist in DB | |
| 14.5 | `POST /superadmin/api/schools/<id>/update/` updates school metadata | Fields persisted | |
| 14.6 | `POST /superadmin/api/schools/<id>/toggle-active/` flips `is_active` | Bool toggled, HTTP 200 | |
| 14.7 | `GET /superadmin/api/schools/<id>/admins/` lists school admins | Returns list, superuser only | |
| 14.8 | `POST /superadmin/api/schools/<id>/create-admin/` adds an `is_school_admin` user | User created and scoped to school | |
| 14.9 | Admins dialog → *Edit* changes an admin's first name, last name and email (`POST /superadmin/api/schools/<id>/admins/<user_id>/update/`); a new email follows to the exams they created | Saved, exams kept | |

---

## 15. Teacher-Role Management

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 15.1 | `GET /roles/` ("People & roles") returns 200 for superusers and school admins; teachers redirected to `/classes/`; pupils to `/` | Per-role HTTP 302 / 200 | |
| 15.2 | `GET /teachers/` redirects to `/roles/` | HTTP 302 | |
| 15.3 | `GET /api/teachers/` returns staff list with `status` / `invitedAt` (admins only, others 403) | HTTP 200 for admins | |
| 15.4 | `POST /api/teachers/create/` with `role=admin` creates user with `is_school_admin=True` and returns `tempPassword` | HTTP 201, user in DB | |
| 15.5 | `POST /api/teachers/create/` with `role=teacher` creates non-admin user | `is_school_admin=False` | |
| 15.6 | Invalid roles (`main_admin`, `pupil`) and duplicate emails are rejected | HTTP 400 | |
| 15.7 | `POST /api/teachers/<id>/admin/` promotes teacher; subsequent class-create by that teacher succeeds | HTTP 200, `is_school_admin=True` | |
| 15.8 | `POST /api/teachers/<id>/admin/` on a superuser or pupil returns 400 | HTTP 400, no mutation | |
| 15.9 | Promote/demote actions log `admin_granted` / `admin_revoked` in `ActivityLogEntry` | Log entries present | |
| 15.10 | `Roles` nav link visible to superusers and school admins only | Hidden for teachers and pupils | |
| 15.11 | `POST /api/teachers/create/` with `classIds` adds the teacher to those classes; `invite: true` emails them | HTTP 201, `emailed: true` | |
| 15.12 | Pupils tab lists every pupil with classes and status (`GET /api/accounts/pupils/`) | HTTP 200 | |
| 15.13 | Classes page has no "create pupil" form and no activity (log button, class activity, recent activity) | Not in HTML | |
| 15.14 | *Edit* changes name / email; duplicate emails rejected; pupil's exams and teacher's owned exams follow the new email | HTTP 200, links kept | |
| 15.15 | *Delete* (one) and *Delete selected* (bulk, must type DELETE) remove pupil accounts after a confirmation dialog; submitted work stays | Accounts gone, submissions kept | |
| 15.16 | Only admins can edit/delete; only pupils of this school can be deleted | HTTP 403 / 400 otherwise | |
| 15.17 | *Edit* with only the name changed saves, even if another account elsewhere shares the email (duplicate check only runs when the email changes) | HTTP 200 | |
| 15.18 | Pupil emails are unique **within a school**: the same email can be added at another school; staff emails stay unique everywhere | Allowed / refused per rule | |
| 15.19 | *Delete* staff (single, or bulk after typing DELETE) — not yourself, not a main admin; their exams stay (`POST /api/accounts/staff/delete/`) | Accounts gone, exams kept | |
| 15.20 | Lists sort by Last name A–Z (default) / Z–A / First name A–Z / Z–A on Roles, Year groups, Subjects and "Assign to"; choice remembered across pages | Order changes, ticks kept | |

---

## 16. Accounts, Invites & First Sign-in

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 16.1 | New accounts (Roles page, bulk import, superadmin) have `must_change_password=True` | Field set | |
| 16.2 | Invite (single / selected / "everyone not yet invited") email: subject "Your ExamHub account at <school>"; "Hi <first name>", username, temporary password, how to change the password, forgot-password steps ("Email me a code" with their username), then **Website: https://digitalexamhub.up.railway.app/** (no "Sign in here" link) | Email received, reads correctly | |
| 16.3 | Invite sets a **new** temporary password and `invited_at`; status shows "Invited" | Status chip updated | |
| 16.4 | Re-inviting an Active account warns first, then resets their password | Confirmation shown | |
| 16.5 | Invite email failure → "Download logins (.csv)" offered | CSV has name, email, username, temp password, link | |
| 16.6 | First sign-in with the temporary password → `/welcome/password/`; other pages redirect there, APIs return `403 password_change_required` | Held on the password page | |
| 16.7 | Re-using the temporary password, mismatched or weak passwords are rejected with a clear message | Error shown | |
| 16.8 | After choosing a password: `must_change_password=False`, `activated_at` set, status "Active", redirected home | HTTP 302 → home | |
| 16.9 | Signing in with an email code while on a temporary password also lands on `/welcome/password/` | Held on the password page | |
| 16.10 | Existing accounts from before v0.22 are not forced to change password | `must_change_password=False` | |

---

## 17. Bulk Import

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 17.1 | Classes page: "Bulk import classes" only (template pre-filled with current classes) | Classes import works | |
| 17.2 | Roles → Staff → "Bulk add teachers": template has Role (Teacher/Admin) and Classes dropdowns of **existing** classes | `.xlsx` downloads | |
| 17.3 | Teachers import: new accounts created with role + classes; existing staff updated (role, extra classes); unknown class = error row | Preview counts correct | |
| 17.4 | Teachers import rejects pupil emails, other-school emails and duplicate rows | Error rows | |
| 17.5 | Roles → Pupils → "Bulk add pupils": Class column optional; unknown class = error row | Preview counts correct | |
| 17.6 | "Email invites to new accounts when I import" sends invites (`invite=1`) and reports sent / failed | Invite emails received | |
| 17.7 | Nothing is saved on preview; errors block import unless skipped | DB unchanged before confirm | |
| 17.8 | Teachers and pupils templates: "Blank template" or "Template with current staff / pupils" (`?prefill=1`); re-uploading the pre-filled file unchanged changes nothing | Both download; no-op import | |
| 17.9 | Every template has separate **First name** and **Last name** columns; pre-filled lists are last name A–Z | Columns present | |
| 17.10 | Teachers template uses **Subjects** (with a "Subject list" tab), no year-group column; older files with a Sections column still import | Subjects assigned | |
| 17.11 | Several year groups / subjects / emails in one cell are separated by **commas** (`;` still accepted; a name that itself contains a comma is kept whole) | Split correctly | |
| 17.12 | Year groups page: *Bulk import year groups* and *Bulk add pupils to year groups*; Subjects page: *Bulk import subjects* and *Bulk add pupils to subjects* | Preview then import | |
| 17.13 | Headers "Year group", "Section" and "Class" are all accepted | Imports | |

---

## 18. Exam Visibility & Closing

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 18.1 | Create with **Visible now** → pupils see it immediately | In pupil task list | |
| 18.2 | **At a set time** → invisible to pupils (list, start, PDF/media) until then; status "Scheduled" | 403 `exam_not_open` before time | |
| 18.3 | **Keep hidden** → invisible until teacher presses *Make visible now*; status "Hidden" | Appears after `show` | |
| 18.4 | **Automatically at the end time**: pupil timer counts down to the end time (+ extra minutes) | Countdown matches | |
| 18.5 | **I'll close it manually**: pupil timer shows "No time limit" | No countdown | |
| 18.6 | *Close exam now* (even with an end time): pupils see "Your teacher has closed this exam" within ~10 s and work is handed in, including unsaved typing | Submissions `submitted` | |
| 18.7 | Autosave refused after close; final submit accepted within 90 s grace | 403 save / 200 submit | |
| 18.8 | Offline pupils: open attempts auto-submitted from last autosave after the grace period (on next list / monitor poll) | `status=submitted`, activity logged | |
| 18.9 | *Reopen* clears a manual close; if the end time has passed it becomes manual-close | State "Live", `dueAt` null | |
| 18.10 | Extra time extends a scheduled close for that pupil but not a manual close | Per-pupil `closesAt` | |
| 18.11 | Dashboard and exam page show the status chip (Hidden / Scheduled / Live / Closed); Settings and bulk schedule use the same options | Consistent | |
| 18.12 | UI says "Exam(s)" everywhere (nav, buttons, headings, tour, login) | No "assignment" wording | |

---

## 19. Teacher Review & Grading

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 19.1 | Review page: PDF fits width, zooms, stays sharp; annotations sit on the right text at every zoom (also at 125% / 150% Windows scaling) | Visually aligned | |
| 19.1a | If a PDF can't be loaded, the review shows the server's reason, not a blank viewer | "PDF unavailable" + reason | |
| 19.2 | Highlights, highlight notes and sticky notes all render; "Annotations" toggle hides/shows them | Toggle works | |
| 19.3 | Answers are in collapsible sections per item with score chip, correct answer and feedback; Expand / Collapse all (Review screen and Grade tab) | Sections toggle | |
| 19.4 | Clicking an annotation in the list opens its page and flashes it | Jump + flash | |
| 19.5 | **Grade tab = mark + grade in one place:** choosing a submitted pupil shows their work (PDF with annotations on the left, items on the right) with the marking tools, and each item has its score box and feedback; *Save grades* / *Release grade* at the top and bottom | Work view inside the Grade tab | |
| 19.6 | Pupil *View grade*: total, %, per-item score and feedback; opening it clears the grade notification | `grade_seen_at` set | |
| 19.7 | Grade tab, written answers: select words → **Underline / Highlight / Comment** (no Circle; no "Mark this answer" toggle needed); numbered margin comments; style change / delete from the list; autosaves ("Marks saved") | Saved to `teacher_marks.answers` | |
| 19.8 | Grade tab, PDF "Mark:" toolbar: select, **Highlight** (drag), **Underline** (drag), **Comment pin** (click) — no Circle / Box; older circles and boxes still display | Saved to `teacher_marks.pdf` | |
| 19.9 | Select tool → click a pupil's highlight / note → comment on it; shows as "💬 You: …" in the annotation list | Saved to `teacher_marks.annComments` | |
| 19.10 | Pupil sees marks and comments **only after the grade is released** (on their answer, on the PDF, in *View grade*) | Hidden before publish | |
| 19.11 | Review screen and Grade tab: − / + / Fit and Ctrl + scroll also zoom **image** items | Image resizes | |
| 19.12 | **Review screen is read-only:** all answers, annotations, existing marks and feedback, but no Mark toolbar, no selection pop-up, no score boxes; header button "Mark & grade this pupil" opens the Grade tab | Nothing editable | |
| 19.13 | Grade tab "Read-only view" opens the Review screen; "Back to grading" returns to the same pupil | Same pupil | |
| 19.14 | Saving grades keeps the open items, scroll position and PDF; header total, "Released / Not released" and item chips update | No reload jump | |
| 19.15 | Changing pupil with unsaved scores/feedback asks "Save your changes?" (Save and continue / Discard changes) | No silent loss | |
| 19.16 | Bell "Open their work" (new submission) opens that pupil in the Grade tab | Grade tab | |

---

## 20. Notifications

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 20.1 | Bell present on Exams, Classes, Activity and Roles pages | Visible with count | |
| 20.2 | Pupil locked out → teacher pop-up on **any** page with resume code, *Unlock now*, *Open monitor*; lockouts shown before messages | Pop-up within ~10 s | |
| 20.3 | *Unlock now* clears the lock (`locked_at` null) | Pupil continues | |
| 20.4 | New pupil message → teacher pop-up with text and *Reply*; reading the thread clears it | Bell count drops | |
| 20.5 | Plain teachers only get alerts for their own exams; admins for the whole school | Scoped correctly | |
| 20.6 | Grade released → pupil pop-up + bell (never during an unsubmitted exam); re-grade notifies again | Pop-up shown | |
| 20.7 | Each alert pops up only once per event (per browser) | No repeats | |
| 20.8 | Pupil submits → teacher sees "N new" on the exam row, a "New" chip in Monitor and a 📥 bell item ("Open their work"); all clear once the teacher opens that pupil's work | Indicators appear, then clear | |
| 20.9 | Monitor → *Mark all as seen* clears every new-submission indicator for that exam (`POST /api/submissions/seen/`) | Count drops to 0 | |

---

## 21. Year Groups & Subjects

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 21.1 | UI says **Year group(s)** everywhere (nav, pages, dialogs, tour, Roles, templates); `/classes/`, `/sections/` and `/year-groups/` all open the page | No "class" / "section" wording for year groups | |
| 21.2 | Subjects page (`/subjects/`): create, rename, delete; assign teachers; add pupils from **any** year group with the grouped picker (ticks survive search / filter) | Saved | |
| 21.3 | A subject's teachers can assign exams to its pupils; plain teachers see "My subjects" read-only | Per role | |
| 21.4 | "Assign to" picker: a pupil in several groups is ticked everywhere at once and counted once | One recipient per pupil | |
| 21.5 | Subject changes appear in Activity under "Year groups & subjects" | Logged | |

---

## 22. Pupil Exam Screen

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 22.1 | Long answers scroll inside the answer box; pupil can scroll back to the top; toolbar and Submit stay visible | Scrolls both ways | |
| 22.2 | Navigator "‹ Question n of N ›" + grid of numbered squares grouped by section, answered vs not answered | Grid matches answers | |
| 22.3 | Contents panel: bar headings with points, current item highlighted, Expand all / Collapse all; 7+ items start collapsed except the current one | As described | |
| 22.4 | PDF zoom: − / % / + / Fit and Ctrl + scroll (trackpad pinch); page stays sharp when zoomed in; wide pages scroll instead of being cut off on the left | Zooms 30%–400% | |
| 22.5 | Highlights, highlight notes, image notes and sticky-note dragging made **while zoomed** land on the right text and stay in place at every zoom (stored in unzoomed page coordinates) | Aligned at 100% and zoomed | |
| 22.6 | Image items: − / % / + / Fit / 100% and Ctrl + scroll; big images start fitted to the screen | Zooms | |
| 22.7 | **Exam focus:** a pupil in an unsubmitted exam sees no "Back to my tasks", bell or account menu — only the exam, Notepad / Resources / Calculator, Message teacher and Submit, plus the "Exam in progress" line | Nothing leads to other exams | |
| 22.8 | Browser Back button during an exam keeps the pupil on the exam (toast: press Submit when finished) | Stays on exam | |
| 22.9 | After submitting (normal browser) the task list, bell and account menu return; "View submission" shows the back button | Normal navigation | |
| 22.10 | **Scientific calculator** (when the exam allows the calculator): sin / cos / tan and inverses (2nd), DEG / RAD, ln, log, eˣ, 10ˣ, √, ∛, x², x³, xʸ, ʸ√x, 1/x, n!, %, π, e, EXP, abs (\|x\|), ( ), Ans, Rand, MC / MR / M+ / M−; correct order of operations (e.g. 2+3×4 = 14, −2² = −4); errors shown as "Math error" / "Syntax error" | Correct results | |
| 22.11 | Calculator keyboard: digits, + − × ÷ ^ ( ) . % !, Enter (=), Backspace, Esc (AC) while it has focus; typing doesn't go into the answer box | Works | |
| 22.12 | Arriving straight into an exam (`?open=…`, e.g. from SEB) shows no grade pop-up over it; held pop-ups appear after submitting | No pop-up during the exam | |
| 22.13 | **Instructions screen:** opening an unsubmitted exam shows a wide white card with the school logo, title, facts and instructions; the exam starts (`started_at`, timer) only on **Start exam**; "Back to my tasks" leaves without starting; returning shows **Continue exam** | Nothing starts before Start | |
| 22.14 | Answer box text size: selecting text + choosing a size changes it; choosing a size with just a cursor makes the next typed text that size; font, colours and buttons keep the selection | Sizes visibly change | |
| 22.15 | Answer toolbar fits in two rows in the answer pane; word count updates while typing and per item | Tidy, live count | |
| 22.16 | Question navigator shows ‹ › and the item label only (no "n of N"); clicking the label opens the grid | No count text | |
| 22.17 | Released grade appears without reloading: pupil on the task list or reading back submitted work sees the grade / marks within ~20 s; "View submission" and the pop-up's "View grade" always show the latest | Marks visible | |

---

## 23. Exam Resources

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 23.1 | Builder step 1 → "Resources for pupils": **+ PDF**, **+ Image**, **+ Text**, each with a title; a missing file / empty text is caught before saving | Exam created with resources | |
| 23.2 | Edit exam (Settings) → Resources: add and remove take effect immediately, even during a live exam (`POST /api/assignments/<id>/resources/add/`, `POST /api/resources/<id>/delete/`) | Pupils see the change on next open | |
| 23.3 | Exam's Content tab lists the resources (Open link for PDF / image, text shown) | Listed | |
| 23.4 | Pupil toolbar shows **Resources (n)** only when the exam has resources | Hidden otherwise | |
| 23.5 | Resources window floats like the Notepad: drag by the header, resize from the corner, minimise (– or double-click header), close; one tab per resource | Works without covering the exam permanently | |
| 23.6 | PDF resources render page by page with − / % / + / Fit; images zoom; text keeps its line breaks | Readable | |
| 23.7 | Resources aren't questions: no points, not in the navigator or grading | Totals unchanged | |
| 23.8 | Wrong file types rejected (PDF slot needs a PDF, Image slot an image); text limited to 20,000 characters | HTTP 400 | |

---

## 24. Exam Builder Items

| # | Check | Expected | Status |
|---|-------|----------|--------|
| 24.1 | **+ New exam** starts with one empty section and **no** items (no default PDF) | Empty | |
| 24.2 | New items are labelled Item 1, Item 2, … in order; removing Item 2 of 4 renames the rest to Item 2, Item 3 | Renumbered | |
| 24.3 | A label the teacher typed (e.g. "Essay") is kept when others renumber | Kept | |
| 24.4 | ↑ / ↓ move an item one place (into the neighbouring section at a section's edge); first ↑ and last ↓ are disabled | Moves | |
| 24.5 | Dragging the ⠿ handle moves an item anywhere, including into another section | Moves | |
| 24.6 | The created exam keeps the on-screen order and each item's section | Same order for pupils | |

---

## Running the Test Suite

```bash
# Full test run
python manage.py test digitalexams

# With verbosity
python manage.py test digitalexams --verbosity=2

# Specific test class
python manage.py test digitalexams.tests.ClassPermissionTests
python manage.py test digitalexams.tests.ClassAndActivityPageTests
python manage.py test digitalexams.tests.TeacherRoleTests
python manage.py test digitalexams.tests.RolesTabAndTourTests
python manage.py test digitalexams.tests.MultiTenancyAndSuperAdminTests

# v0.22.0 test modules
python manage.py test digitalexams.test_email_login digitalexams.test_tour digitalexams.test_school_scoping
python manage.py test digitalexams.test_notifications digitalexams.test_exam_window digitalexams.test_invites digitalexams.test_people_and_files

# Unreleased: year groups & subjects, marking + submission notices, exam resources
python manage.py test digitalexams.test_sections_subjects digitalexams.test_marking digitalexams.test_resources digitalexams.test_seb
```

> **Last full run (2026-10-07):** 125 tests — all pass except the 3 known failures below.

> **Known failing (pre-existing):** `TeacherRoleTests.test_main_admin_can_promote_and_demote`, `TeacherRoleTests.test_only_main_admin_can_manage_roles`, `RolesTabAndTourTests.test_roles_tab_on_every_teacher_page_for_main_admin_only`. They expect Roles to be superuser-only; school admins can now use it. Decide which rule is correct and update the tests or the code.

> **Note:** The test suite uses Django's test database (a separate, temporary DB). It does **not** touch the production PostgreSQL instance.

---

*Last reviewed: 2026-10-07 (v0.22.0 + Unreleased, migrations up to 0029)*
