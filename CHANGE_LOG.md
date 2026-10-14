# Andam — Change Log

(Called ExamHub until 13 Oct 2026, then ExamNook until 14 Oct 2026.) All notable changes to Andam are documented here, in reverse-chronological order.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

> Planned / in-progress work. Move items to a versioned section when shipped.

### Upgrade notes
- **2026-10-13 (accessibility, offline protection):** run `python manage.py migrate` (new migration `0031_text_to_speech`). Commit the new folder `digitalexams/static/digitalexams/a11y/` (the two reading fonts, served by ExamHub itself). `collectstatic` in the Procfile picks them up on Railway; nothing else to install.
- **2026-10-12 (spell check, audio, PDF export):** run `pip install -r requirements.txt` (new: `reportlab`, `pypdf`) and `python manage.py migrate` (new migration `0030_spellcheck_audio`). Commit the new folder `digitalexams/fonts/` (fonts the PDF export embeds), `digitalexams/pdf_export.py`, the migration and `test_spellcheck_audio_export.py`. The Procfile now gives gunicorn `--timeout 120` so a whole-class export isn't cut off at 30 seconds. After deploying, run `python manage.py sync_media_to_db` once more if audio files were uploaded on a local copy.
- Run `python manage.py migrate` (new migrations `0026_storedfile`, `0027_subject_and_section_labels`, `0028_teacher_marks_and_seen` and `0029_examresource`).
- Then, **on the computer that holds the uploaded files** (the local copy with the `media/` folder), run `python manage.py sync_media_to_db` once. It copies every exam PDF / image / video / school logo into the database so the Railway site can serve them too.
- If the site runs on a custom domain (not `*.railway.app`), set the `ALLOWED_HOSTS` environment variable in Railway to that domain, and `SITE_URL` to its full address (used in invite emails; default `https://digitalexamhub.up.railway.app`).
- After any change of domain: download the `.seb` file again for Lockdown exams and replace the old entries under Allowed Browser Exam Keys.
- **Before deploying the 2026-10-08 security changes, set two Railway variables on the web service** or the site won't start:
  - `DATABASE_URL` → "Add Reference" to the Postgres service's `DATABASE_URL` (`${{Postgres.DATABASE_URL}}`). Locally it comes from `.env` (already there).
  - `SECRET_KEY` → a long random value (if it isn't set already). Setting / changing it signs everyone out once.
- `DEBUG` now switches itself off on Railway (it detects `RAILWAY_ENVIRONMENT`) and stays on locally. Override with `DEBUG=1` / `DEBUG=0`.
- `media/` is no longer tracked by git (exam papers were in the repository). **Before pushing that change, run `python manage.py sync_media_to_db` on the computer with the `media/` folder** so every uploaded file is in the database; Railway then serves them from there. Files already in old commits stay in the history unless it is rewritten.
- After the deploy works, **rotate the database password** (Postgres service → Database → Credentials), redeploy the web service, and update `DATABASE_URL` in your local `.env`. The old password is in the git history of `settings.py`.
- Commit the migrations `0027`–`0029` and the new test files and templates (`test_seb.py`, `test_url_access.py`, `test_graphcalc.py`, `templates/_graphcalc.html`) — they were not yet in git.
- The 2026-10-09 changes (comment-only marking, notifications, dashboard tabs, navigation, flag for review, pupil task sections) need **no migration and no new settings**. Existing highlights / underlines stay as they are and still show. One new address, `POST /api/notifications/read-all/`, ships with `urls.py`.
- Pupils' "Flag for review" marks are kept in their browser only (per exam), so they don't carry over to another device.

### Changed — Renamed to Andam, pen-nib logo (2026-10-14)
No migration, no new settings. Safe Exam Browser keys stay valid (only the download file names changed, not the settings inside the file).
- **Why:** examnook.com is an existing exam-prep product, so the ExamNook name was dropped. **Andam** is Cebuano for "ready"; the descriptor is "Digital exams for schools".
- **New name everywhere people see it:** page titles, landing page, sign-in and password pages, teacher and pupil portal, guided tour, super-admin page, invite and sign-in-code emails (sender "Andam", subject "Your Andam account at <school>"), bulk-import templates, PDF export metadata, SEB messages. Code names, the `examhub` project folder and browser-storage keys are unchanged, so nobody loses saved settings.
- **Downloads renamed:** `andam-lockdown.seb`, `andam-exam.seb`, `andam-<label>-template-<date>.xlsx`, `andam-<label>-logins-<date>.csv`.
- **Logo:** a fountain-pen nib that also reads as the letter A, with an ash-red collar. Files in `static/digitalexams/brand/`: `andam-mark.svg` (on light), `andam-mark-on-blue.svg`, `andam-icon.svg` (app icon), `andam-favicon.svg` (simplified for browser tabs), `andam-apple-touch-icon.png`, `andam-icon-512.png`, `andam-favicon-32.png`, `andam-logo.png` and `andam-logo-on-blue.png` (full lockup). The wordmark is ANDAM in Outfit 600 with wide letter-spacing.
- **Landing page:** hero now reads "Ready for every exam." with the tag "Andam · Cebuano for "ready""; the closing sign-in section asks "Andam na ka?".
- **Sign-in page:** blue panel shows the logo with "Digital exams for schools", the heading "Andam na ka?" ("Are you ready? Your exams are waiting.") and a new third point, "Practice papers and real exams in one place". On a phone the blue header shows the same line.
- **Default email sender** is now `Andam <no-reply@andam.local>` when `EMAIL_HOST_USER` isn't set.
- **Old files you can delete:** `static/digitalexams/brand/examnook-*`, `brand/apple-touch-icon.png` and `brand/favicon-32.png` are no longer used.

### Changed — Sign-in goes straight to one page; sign-in pages restyled (2026-10-13, seventh batch)
No migration, no new settings.
- **The landing page no longer lists schools.** Every "Sign in" button goes straight to the one sign-in page (`/login/`), so which schools use ExamNook stays private. The footer now just says "Ready for your exam?" with a Sign in button. School sign-in addresses (`/s/<school>/login/`, used in invite emails) still work.
- **Sign-in page redesigned** in ExamNook's colours: on a laptop, a deep-blue ruled-paper panel with the logo on the left and the form on a warm off-white card on the right; on phones, a compact blue header with the form below. Ash-red Sign in button, deep-blue pupil/teacher switch, a link back to the landing page. On a school's own sign-in address the school's name and logo still appear on the card. Nothing about how signing in works has changed.
- **Clearer wrong-password message:** "That username and password don't match. Check for typing mistakes (capital letters count), or sign in with an email code instead."
- **"Choose your password"** (first sign-in) restyled to match.
- **Landing colours:** page background is now the light grey-beige `#F4F5F0`; cards are a warm off-white `#FBF8F1` (not pure white) with a soft border.
- Tests: `test_landing.py` 10 tests. Full suite: 169 tests, only the 3 known Roles failures.

### Changed — Landing page rebuilt: interactive, screenshots first (2026-10-13, sixth batch)
No migration, no new settings. Replaces the Aentoria-style layout below.
- **New order:** hero → What it is → **Screens** (straight after) → How it works → Fair and secure → Accessibility → Your school → FAQ → Sign in.
- **Things visitors can do:** highlight words in the hero's exam paper with any of the four highlighter colours (and clear them); click a question type or delivery mode to read what it does; switch the **Screens** tour between Pupils and Teachers and click through nine real screens (it also moves on by itself while on screen, with a progress bar, and stops as soon as someone clicks); open any screenshot full size; press "Switch to another tab" to see a monitored exam pause, then type the teacher's resume code (lantern) to carry on; try text size, fonts, colours and read aloud on a sample question.
- **How it works:** each of the five step cards now has its own colour (egg white, deep blue, soft blue), a large outlined step number, a real close-up of that screen (Build, Deliver, Monitor with a locked pupil, Mark, the exported PDF) and ticks that check off as the card arrives. On phones the cards simply follow each other.
- **Colours refined:** deep blue `#1C3A6E`, ash red `#B2554B` for actions only, egg white `#F7F1E4` cards on a cool blue-mist page `#EDF0F5`, slate ink `#1D2433` text. The hero is a sheet of ruled exam paper with a red margin rule instead of clouds; no more giant outline words. Buttons are sentence case.
- **Screenshots** retaken with SSC's school colour set to ExamNook's deep blue (the school-colour setting), so the app and the page match; new close-ups `shots/step-build.webp`, `step-deliver.webp`, `step-monitor.webp`, `step-mark.webp`.
- Tests: `test_landing.py` 8 tests. Full suite: 167 tests, only the 3 known Roles failures.

### Changed — Renamed to ExamNook, new logo and colours (2026-10-13, fifth batch)
No migration, no new settings. Code names (the `examhub` project folder, `examhub.html`, saved-in-browser keys) are unchanged, so nothing pupils have saved is lost.
- **New name everywhere people see it:** page titles, sign-in and password pages, the teacher and pupil portal, the super-admin page, invite and sign-in-code emails (subject, body, sender name "ExamNook"), bulk-import templates and their file names (`examnook-…xlsx`, `examnook-…-logins.csv`), Safe Exam Browser messages and file names (`examnook-lockdown.seb`, `examnook-exam.seb`), the guided tour, and PDF export details. The SEB config's contents are unchanged, so its Config Key stays the same and SEB keeps working.
- **Logo:** an arch (the lowercase n, the "nook") with an exam page tucked inside. Files in `static/digitalexams/brand/`: `examnook-icon.svg` (favicon/app icon), `examnook-mark.svg`, `examnook-mark-on-blue.svg`, `examnook-logo.png`, `examnook-logo-on-blue.png`, `apple-touch-icon.png`, `examnook-icon-512.png`, `favicon-32.png`. Every page now has the ExamNook favicon; inside a school's pages the school's own name and logo still lead.
- **Landing page colours:** deep blue `#1B3A6B`, ash red `#A9504A` for action buttons, egg white `#F4EDDC` cards, slate ink `#1E2430` text. Headings and the wordmark use Outfit (served by ExamNook, `landing/outfit-latin-wght-normal.woff2`, OFL).
- **Screenshots retaken** with the school shown as "SSC" and the teacher as "Teacher Rhox", plus three new ones in the Screens carousel: the notepad in use, the Resources panel open, and the scientific calculator.
- **Website address:** `SITE_URL` still defaults to `https://digitalexamhub.up.railway.app`. When the new examnook address is live, set `SITE_URL` (and `ALLOWED_HOSTS` if it's a custom domain) in Railway, then download the `.seb` file again for Lockdown exams.
- Tests: `test_landing.py` now 8 tests. Full suite: 167 tests, only the 3 known Roles failures.

### Changed — Landing page redesign in the style of the Aentoria Webflow template (2026-10-13, fourth batch)
No migration, no new settings. Replaces the first landing-page design below.
- **Look:** off-white page, sky-blue hero with drifting clouds and a giant "ExamHub" word that drifts as you scroll, lime pill buttons with a sliding switch, sparkle badges, a floating white header that hides on the way down and comes back on the way up (with a menu on phones), and a dark footer holding the school sign-in.
- **Interaction:** smooth scrolling (Lenis, served by ExamHub); the hero heading rises in word by word, then the highlighter sweeps and glass cards ("Saved a few seconds ago", a sticky note, "10 kinds of question") arrive; a statement that fills in word by word as you scroll; "10" and "4" count up; **How it works** is five cards that stack on top of each other as you scroll; **Screens** is a swipeable carousel of real ExamHub screenshots (sample exam, made-up pupils) with arrows, dots and arrow keys; fanned screenshots that spread on hover; an FAQ accordion; the same working Accessibility sample as before.
- No testimonials, logos, awards or prices — only things ExamHub actually does. All motion is switched off when the device asks for reduced motion.
- New files in `static/digitalexams/landing/`: `inter-latin-opsz-normal.woff2` (Inter, OFL) + licence, `lenis.min.js` (MIT) + licence, and `shots/*.webp` (six screenshots). The Bricolage Grotesque files from the first design are no longer used and can be deleted (`bricolage-grotesque-*.woff2`, `LICENSE-BricolageGrotesque.txt`).
- Tests: `test_landing.py` now 6 tests (checks the assets exist and nothing loads from a CDN). Full suite: 165 tests, only the 3 known Roles failures.

### Added — Public landing page (2026-10-13, third batch)
No migration needed. Optional new variable `CONTACT_EMAIL` (Railway → Variables).
- **Signed-out visitors to the site root (`/`) now get a landing page** (`templates/landing.html`, view `landing`) instead of being sent straight to the plain sign-in page. Signed-in people are still sent to their own school as before (`SchoolTenantMiddleware`).
- Sections: hero with an ExamHub exam paper being highlighted (the strokes and a sticky note play once; skipped when the device asks for reduced motion) · **For pupils** · **For teachers** (Build → Deliver → Monitor → Mark → Export) · **Fair and secure** · **Accessibility** with a working sample question (text size, Easy-read and dyslexia fonts, night mode, overlays, read aloud) · **Run it for your whole school** · **Sign in** — one button per active school going to `/s/<slug>/login/` (a drop-down when there are more than 8; inactive schools aren't listed).
- **"Bring ExamHub to your school"** opens an email to `CONTACT_EMAIL`, falling back to `EMAIL_HOST_USER`; with neither set the email buttons aren't shown.
- Fonts served by ExamHub (`static/digitalexams/landing/` Bricolage Grotesque for headings, plus the Atkinson Hyperlegible and OpenDyslexic files already shipped for pupils) — no outside requests. Works down to phone width.
- Tests: `test_landing.py` (5 tests). Full suite: 164 tests, only the 3 known Roles failures.

### Changed — UI / UX polish for the new features (2026-10-13, second batch)
No migration needed.
- **Pupil answer pane:** the formatting toolbar and word count only show for written answers (PDF / long answer) — not on recordings, images, videos or multiple-choice / true-false / fill-in / table questions (`syncAnswerToolbar()`). Answer options, boxes and the "Nothing to answer here" note have proper padding instead of touching the pane edges (they were cut off at iPad width).
- **Exam toolbar:** below 1180 px the buttons (Notepad, Read aloud, Resources, Calculator, Graphing calculator, Hide notes) show their icon only, so the toolbar and timer stay on one line on an iPad; the names stay as tooltips and for screen readers.
- **Question navigator:** wraps instead of squeezing the current question's name to nothing; the Flag button is no longer cut off.
- **Audio player:** Play and the time on the first line, a full-width progress bar underneath — the bar no longer shrinks to a dot on narrow screens. The "now playing" bar sits in the empty middle of the footer (above it on narrow screens) instead of covering "Your work saves automatically".
- **Instructions card:** shows "1 recording — 2 plays each" for listening exams, and "Need larger text, a reading font or different colours? **Accessibility options**", which opens the menu. The menu now opens above the instructions card (it lives on `<body>`; the sticky top bar's layer kept it underneath).
- **Create → Delivery mode** and **Settings:** the join-code / calculator / spell check / read-aloud checkboxes are grouped as cards under **Starting the exam** and **Tools pupils get**, each with a one-line explanation, ticked cards highlighted; a note that Accessibility options are always available to pupils. **Deliver** tab lists the tools under their own heading and shows "Accessibility — always available".
- **Export dialog:** "Everyone who handed in" / "Everyone who started" are greyed out with "Nobody has … yet" when no pupil fits; the description only mentions PDF pages when the exam has a PDF.

### Added — Accessibility menu, read aloud, offline protection, Chinese character count (2026-10-13)
Migration `0031_text_to_speech`.
- **Accessibility menu for pupils** (top bar, every pupil page, stays visible during an exam): **text size** (5 steps, up to 175 % — question text, answer box, options, fill-in and table boxes, notepad, notes, resources, instructions card, task list), **font** (Standard · Easy-read = Atkinson Hyperlegible · Dyslexia = OpenDyslexic), **line spacing** (Normal / Wider / Widest), **letter spacing** (Normal / Wide), **colours** (Standard · Night — the whole page inverted, PDF pages included, with pictures, videos and highlight colours turned back · High contrast — black text, dark borders, strong focus outline), **colour overlay** (cream, blue, green, pink, grey tint over everything, a visual-stress aid), **read-aloud speed and voice**. Saved in the browser per account (`localStorage`, key `examhub-a11y:<username>`) and applied on every pupil page; **Reset to standard**. The two fonts are served by ExamHub (`static/digitalexams/a11y/`, OFL licence) — no outside requests, so they work in Safe Exam Browser. Only the screen changes: nothing is added to the pupil's saved answer.
- **Read aloud** (browser text-to-speech, no outside service): **Read aloud** in the exam toolbar reads the selected words, or with nothing selected the whole question with its answer options (A, B…) or the current PDF page. Chinese is read with a Chinese voice when the device has one; long text is read in short pieces (some browsers stop long speech part-way). **Stop reading** stops it; leaving the exam stops it. Teachers can turn it off per exam (**Allow read aloud**, on by default; Create → Delivery mode and Settings; `Assignment.allow_text_to_speech`); the instructions card then says "Read aloud off". Which voices exist depends on the device.
- **Offline protection** for pupils sitting an exam: a copy of the answers, highlights, notes and notepad is kept in the browser whenever it differs from what the server last confirmed (checked every 3 s and at each autosave) and removed as soon as the server has it. When the connection drops, a banner says "You're offline — keep working", the save line says "Offline — kept on this device", and leaving the page asks first; saving resumes by itself when the connection is back ("You're back online — your work is saved"). If the page was reloaded or the device restarted before then, opening the exam again puts the kept copy back and saves it — only when the server still holds exactly what that copy was based on, so newer work saved from another device is never overwritten. Submitting while offline says the work is safe and to press Submit again once back online. The copy is cleared on hand-in. New `updatedAt` per pupil in the exams API; counting an audio play no longer changes `updated_at`.
- **Word / character count**: Chinese and Japanese are counted by character, everything else by word; a mixed answer shows both ("120 characters · 15 words").
- Tests: `AccessibilityAndOfflineTests` in `test_spellcheck_audio_export.py` (read-aloud setting saved / kept, pupils get the menu and teachers don't, font files shipped, no look-behind regex — older iPad Safari can't parse it, `updatedAt` sent and unchanged by an audio play). Full suite: 159 tests, only the 3 known Roles failures.

### Added — Spell check on/off, audio items, export to PDF (2026-10-12)
Migration `0030_spellcheck_audio`. New dependencies `reportlab==5.0.1`, `pypdf==6.20.0` (both pure Python).
- **Spell check on / off per exam** (`Assignment.allow_spellcheck`): "Allow spell check" in Create → Delivery mode and in Settings; shown on the Deliver tab and on the pupil's instructions card ("Spell check off"). Off by default for new exams; exams that already existed keep it on, as they were. When off, every place the pupil types during the exam (answer boxes, notepad, sticky / highlight notes, fill-in and table boxes, chat) gets `spellcheck="false"`, `autocorrect="off"`, `autocomplete="off"` and Grammarly's opt-out attributes — also on fields added later (MutationObserver) and on `<body>` (the attribute is inherited). Everything is restored when the pupil leaves the exam (`applyExamSpellcheck()`). The update API keeps the current value when `allowSpellcheck` isn't sent, so older open pages can't switch it off by accident.
- **Audio items** for listening exams (`AssignmentItem.AUDIO`: `audio`, `audio_max_plays` 0–10 with 0 = unlimited, `audio_allow_pause`). Builder: **Audio** tile with file, **Plays allowed** (default 2) and **Pupils can pause and go back / forward** (default off); audio isn't marked (0 points, no score box). Pupil player: Play / Pause / ↺ 10s / seek bar as allowed, "Plays left: n of m", and a small bar at the bottom left while it plays on another item. Plays are counted on the server (`Submission.audio_plays`, new `POST /api/submissions/<recipient>/audio/<item>/play/`, row-locked) and refused at the limit (409 `no_plays_left`); a limited recording is only sent once a play has been counted (403 `play_not_started`). The file is fetched once as a blob, so seeking works and replays don't download it again. Teachers get a normal player with "Played x of y times" in Review / Grade. Served by `GET /api/items/<id>/audio/` with the same checks as video; `sync_media_to_db` copies audio too. Image / video / audio items now say "Nothing to answer here…" in the answer pane instead of "This item type isn't supported yet".
- **Export to PDF** (`digitalexams/pdf_export.py`). Grade tab → **⤓ Export PDF** (this pupil, everyone who handed in, or everyone who started; a ZIP with a PDF each or one combined PDF; with or without scores / feedback / comments; with or without the exam's PDF pages), and **⤓ PDF** on the pupil bar for one click. Each PDF: cover block (school, exam, pupil, status, times, total and whether it's released, who exported it), then every item — question, answer (rich text with bold / lists / tables / sizes / alignment), numbered teacher comments with the marked words tinted, MCQ choices with the correct answer, true/false and fill-in answers, tables, images, audio play count — score and feedback; the exam's PDF pages follow with the pupil's highlights and notes (N1, N2…) and the teacher's marks (1, 2… as in Grade); the notepad at the end; "Page x of y" footer. Endpoints `GET /api/submissions/<recipient>/export.pdf?marks=&pages=` and `GET /api/assignments/<id>/export/?format=zip|pdf&who=submitted|started&marks=&pages=`; teachers who can see the exam only; logged as `exported` (Activity → Grading).
- Fonts for the PDF in `digitalexams/fonts/`: DejaVu Sans (Latin, Greek, Cyrillic, maths) and a cut-down Noto Sans CJK SC (~9,500 common simplified + traditional Chinese characters and Japanese kana, 2.9 MB) so Chinese prints in every PDF viewer; only the characters used are embedded. Licences alongside.
- Procfile: gunicorn `--timeout 120` (a class of 30 with a 12-page paper takes ~6 s locally; big scans take longer).
- `Submission.save()` no longer writes `audio_plays` on a plain save of an existing row, so an autosave / submit / lock / grade that loaded the submission just before a play was counted can't put the old count back (and hand out an extra play). Write it with `update_fields`.
- Tests: `test_spellcheck_audio_export.py` (12 tests: setting saved / kept, page controls, audio creation and upload checks, play limit and gating, a stale save not undoing a play, locked pupils, export with / without marks, permissions, ZIP / combined / pages=0, nothing to export, HTML conversion and comment offsets). Full suite: 155 tests, only the 3 known Roles failures.

### Changed — Breadcrumbs, calmer teacher alerts, flag for review, pupil task sections (2026-10-09, second batch)
No migration needed.
- **Breadcrumbs** on the exam screen: "← Live exams › Exam title › Grade › Pupil". The first goes back to that dashboard tab, the exam title goes to Monitor (`renderCrumbs()`, redrawn on every screen change).
- **Teacher alerts:** only a **lockout** still pops up (a pupil is waiting). A new **pupil message** shows a small toast at the bottom right (Reply / Later, closes itself after ~9 s) and stays in the bell until read.
- **Bell:** items are grouped **Needs action** (lockouts) / **Today** / **Earlier**, and there's **Mark all as read** — new `POST /api/notifications/read-all/` (`notifications_read_all_api`). Teachers: clears unread pupil messages and new-submission items on exams they can see; pupils: unread teacher messages and released grades. Lockouts are never cleared this way.
- **Flag for review (pupils):** "⚑ Flag" button beside the question navigator; flagged questions get a ⚑ in the question grid (with a count in the legend) and are listed in the Submit confirmation next to the blank ones, each a button that jumps to it. Flags are the pupil's own reminders, kept in that browser per exam (`localStorage`); the teacher never sees them.
- **Pupil task list in three sections:** **To do** (open ones first, soonest deadline first, then not open yet, then closed) · **Submitted** ("Handed in 2 hours ago", "Waiting for your grade") · **Graded** ("View grade · 12/15", new ones first). Deadlines read "Ends in 2 days" with the exact date underneath, shown in amber when it's under 24 hours. On phones each exam is a card with a full-width button.
- Removed the PDF viewer's diagnostic line ("Text: N spans, pointer-events=…"); a missing text layer now only logs a console warning.
- Tests: `test_notifications` (mark-all-read for teacher and pupil, scope, lockouts kept; toast / crumbs / flags / task-list markup; no diagnostic line).

### Changed — Comment-only marking, quieter grade notices, dashboard tabs and navigation (2026-10-09)
No migration needed.
- **Marking is comment-only.** Grade tab, written answers: selecting words opens a comment box straight away (no Underline / Highlight / Comment pop-up). The words get one fixed light-red tint and a number matching the margin comment. Enter or clicking away saves; a comment left empty is dropped, so an accidental selection leaves nothing behind. The style picker is gone from the comment list.
- **PDF toolbar is Select + Comment only** (the Highlight and Underline tools are removed). Comment + click drops a numbered pin with its comment box; a pin closed without a comment is removed. **C** toggles the Comment tool.
- Older highlights / underlines / circles / boxes still show (read-only, tagged as older marks); the saved data format is unchanged. The server accepts the new `comment` style (`MARK_STYLES` in `views.py`).
- **Comments link both ways:** click a numbered comment to scroll to and flash its words; click the tinted words to flash the comment (review screen, Grade tab and the pupil's view).
- **Pupils get no pop-up when a grade is released.** It waits in the bell (count badge, "Graded: <exam> (x / y)", one short wiggle when it arrives) and a **"New grade"** chip on the task list. Opening it shows the submitted work with a **grade panel at the top** (total, %, score + feedback per item, 💬 count; each row jumps to that question). The grade summary modal is removed (`openGradeInWork()` replaces `openGradeSummary()`). New `newGrade` flag in the assignments API (`Submission.is_new_grade_for_pupil`). Teacher pop-ups (lockouts, pupil messages) are unchanged.
- **Dashboard tabs:** All · Live · Scheduled · Hidden · Closed with counts, a **Needs grading** toggle (handed-in work not yet graded or released), a title search, per-tab empty messages, and "✎ N to grade" / "N graded, not released" under Submitted. The tab is remembered and kept in the address (`?list=`).
- **Addresses follow the screen** for teachers: `?open=<id>&tab=<tab>&pupil=<recipientId>`. Refresh, browser Back / Forward and copied links land on the same exam, tab and pupil (previously the address was wiped after loading). The back link says where it goes ("← Live exams").
- **Grade tab:** ‹ › beside the pupil list, **Next ungraded →**, and an "N of M graded" progress bar; keys J / K / N. Unsaved scores still prompt before moving.
- **Menu button below 900 px** (iPad portrait), where the top menu used to disappear completely.
- **Submit with blank answers:** each blank question is a button in the confirmation that jumps to it.
- Wording: bell is "Notifications" everywhere; the pupil list in Grade says "released" (was "published"); "Comments saved".
- Mobile dashboard labels fixed (the Created column was labelled "Due").
- Tests: `test_marking` (comment style saved; tabs / navigation markup), `test_notifications` (`newGrade` flag; no grade modal; pupils bell-only), `test_resources` (no Highlight / Underline tools).

### Security — addresses can't be used to get round the checks (2026-10-08)
Audit of what a signed-out visitor, a pupil, a teacher or someone from another school can reach by typing addresses. The page/API checks were already sound (school in the URL must match the account → 403; every exam, file, submission, chat and grading call re-checks the school and ownership; pupils are bounced from teacher pages). Gaps found and closed:
- **Uploaded exam files were public under `/media/`.** Anyone who knew or guessed a file name could download an exam paper without signing in (confirmed on the live site). `/media/` now serves **school logos only** (`school_logos/`, needed on the sign-in page); everything else is 404. Exam PDFs, images, videos and resources are only served through the checked views (`/api/items/<id>/pdf|image|video/`, `/api/resources/<id>/file/`). New view `public_media` in `views.py`, routed in `examhub/urls.py` (the `DEBUG`-only `static()` media route is gone).
- **`DEBUG` was hard-coded on in production**, so error pages showed internals and a wrong address listed every URL in the app. Now off on Railway automatically; errors still go to Railway's log (`LOGGING`); session and CSRF cookies are https-only when `DEBUG` is off.
- **Lockdown and join-code questions could be read early** by opening `/s/<school>/api/assignments/` in an ordinary browser (the checks only guarded the Open button). For a pupil who hasn't handed in, question text, answer options and text resources are now held back (`contentWithheld: true`) until Safe Exam Browser is confirmed (Lockdown) or the join code has been entered (join code); join-code papers' PDF / image / video / resource files return 403 `join_code_required` until then. The exam page fetches the paper again once the gate is passed. Teachers and admins (including previews) always see everything. (`pupil_content_withheld()`, `_pupil_join_code_pending()` in `views.py`.)
- **An ordinary account with no school could open any school's `/s/<slug>/` pages** (the middleware skipped the check when `school_id` was empty). Now refused with 403 unless the account is a platform superuser.
- **Database password removed from `settings.py`**: the database now comes from `DATABASE_URL` (via `dj-database-url`; SSL required on Railway's public proxy, not on its private network). `SECRET_KEY` must be set when `DEBUG` is off — the old default (which is public) only works locally.
- `sync_media_to_db` also copies **exam resource** files into the database (it only did items and logos).
- Known limitation, unchanged: Safe Exam Browser's Config Key can be worked out from the downloadable `.seb` file, so a determined pupil could fake the SEB check outside SEB. Supervision / school-managed devices, or SEB Server, are the real controls.

### Added — Graphing calculator (graphing phase)
- **Teachers choose per exam:** "Allow scientific calculator" (the existing one) and/or "Allow graphing calculator" — the "(not yet available to pupils)" note is gone. The delivery summary shows both.
- **Pupils get a "Graphing calculator" button** in the exam toolbar when allowed: a draggable window laid out like a handheld graphing calculator, with 2nd functions (blue labels) and keyboard input (arrows, Enter, Backspace, Esc).
  - **Y=**: Y1–Y0 in their own colours; click "=" (or ◀ to it + ENTER) to turn a graph on / off.
  - **WINDOW** (Xmin, Xmax, Xscl, Ymin, Ymax, Yscl — accepts expressions like `-2π`), **ZOOM** (ZStandard, Zoom In / Out, ZDecimal, ZSquare, ZTrig, ZInteger, ZoomFit), **GRAPH** with a free cursor (arrows or click).
  - **TRACE**: arrows along the curve (pans the window at the edge), ▲▼ switch curve, type a number to jump to that X.
  - **2nd TABLE / TBLSET** (TblStart, ΔTbl).
  - **2nd CALC**: value, zero, minimum, maximum, intersect, dy/dx, ∫f(x)dx — with the handheld steps (Left Bound? / Right Bound? / Guess?, First curve? / Second curve?) and ∫ shading.
  - **MODE**: radian / degree. **Home screen**: calculations with Ans, history recall (▲ / 2nd ENTRY), starting a line with an operator continues from Ans, errors like `ERR: DIVIDE BY 0`.
  - Handheld order of operations: `-2²` = −4, `^` left to right, implied multiplication (`2X`, `3sin(X)`), odd roots of negatives (`(-8)^(1/3)` = −2). Graphs break at asymptotes (no false line on 1/x); zero ignores sign changes across a pole.
  - All in `templates/_graphcalc.html` (markup, styles, `GCMath` parser/numerics, `GraphCalc` screens) — no outside service, no `eval`. Uses the existing `allow_graphing_calculator` field (no migration).
  - Not yet: stats lists, regressions and probability functions (normalcdf, invNorm, binompdf/cdf) — next phase.

### Changed — Long lists of subjects and pupils
- **Exam "Assign to" (create and Edit exam):** each subject / year group is one line, **closed by default** — click its name to open it; its checkbox still ticks everyone without opening it. A badge shows how many are picked in each ("4 selected" / "all selected"), a running total ("15 pupils selected"), **Open all / Close all**, and a **search box** for subjects, year groups or pupils (a pupil match opens their groups with just the matching pupils). The search bar stays visible while scrolling; groups you opened stay open when re-sorting. With one or two groups they start open.
- **Subjects and Year groups pages:** the list on the left scrolls on its own and stays in view while the details scroll; search at the top and the "New subject / year group" box at the bottom stay visible; a count ("40 subjects", "3 of 40 subjects"). The selected subject is scrolled into view.
- **Subjects → "Add pupils from any year group":** each year group starts closed (tick to take everyone not yet in the subject, click the name to pick pupils); searching or choosing one year group opens them; a "n ticked" badge per group.

### Fixed — 2026-10-08
- The graphing calculator's template note showed as text at the bottom of every page (a multi-line `{# … #}` comment, which Django only supports on one line; now `{% comment %}`).

### Added — Resources for pupils, zoom, simpler marking tools
- **Exam resources** (migration `0029_examresource`): besides questions, a teacher can attach guide material to an exam — **PDF**, **image** or **text** (e.g. a source, a formula sheet, a map). Add them in the builder ("Resources for pupils", step 1) or any time later from **Settings** (the Edit exam dialog — takes effect straight away, even mid-exam). Pupils open them from a **Resources** button in the exam toolbar: a floating window like the Notepad that can be dragged, resized and minimised, with a tab per resource, page-by-page PDFs with zoom, zoomable images and plain text. Resources aren't marked and don't appear in the question navigator. They're listed on the exam's Content tab. Files are served with the same access rules as the exam's own files (assigned pupils, the exam's teacher, admins).
- **Zoom** for the pupil's PDF viewer (− / % / + / Fit, or Ctrl + scroll / trackpad pinch). Highlights and notes stay exactly in place at any zoom, and the page is redrawn sharp when zoomed in. Images (pupil exam, review screen, resources) get the same zoom; the review screen's existing − / + / Fit buttons now work on images too. Zooming past the width scrolls instead of cutting off the left edge.

### Changed — Instructions screen, better answer box, simpler navigator, tidier builder
- **Instructions first.** Opening an exam shows a wide white card in the middle of the screen with the school's logo (set in Superadmin), the exam title, key facts (items, points, end time or "no time limit", calculator, resources) and the teacher's instructions. The exam — and its clock — only starts when the pupil presses **Start exam** ("Continue exam" when coming back; "Back to my tasks" before starting). Works the same when arriving straight from Safe Exam Browser.
- **Text size now works in the answer box.** Choosing a size applies to the selected text, or — with just a cursor — to what the pupil types next. (The editor writes CSS `font-size: xxx-large` spans, which the old code never converted, and picking from the drop-down lost the selection; selections are now remembered and restored for every toolbar control.) The size box follows the text at the cursor.
- **Answer area tidied**: two neat toolbar rows (font, size, bold/italic/underline, super/subscript · lists, indent, alignment, table, undo/redo), a roomier 16px writing surface, the teacher's instructions keep their line breaks, and a live **word count** next to the autosave status.
- **Question navigator**: just ‹ › and the item's label (no "1 of 3"); the label still opens the grid of all items.
- **Exam builder**: a new exam starts with **no** items (no default PDF); items are labelled **Item 1, Item 2…** and renumber themselves when one is removed or moved (labels you typed yourself are kept); items can be **rearranged** with ↑ / ↓ or by dragging the ⠿ handle, including into another section. Removing the last item or a section with items is allowed (the exam still needs at least one item to be created).
- **Fixed: pupils not seeing the teacher's marks** when the grade was released while their page was already open. Opening submitted work and the "View grade" pop-up now fetch the latest first, and the background refresh (every 20 s) also runs while a pupil is reading back submitted work, redrawing the task list or the open paper when a grade or marks arrive.

### Changed — Marking happens in the Grade tab; Review is read-only; scientific calculator
- **Grade tab = read, mark and grade in one place.** Choosing a submitted pupil shows their work right in the tab — the PDF viewer with their annotations on the left, every item on the right — with the pen-on-paper tools (select words → Underline / Highlight / Comment; PDF Highlight / Underline / Comment pin; comment on the pupil's annotations), and each item's **score** and **feedback** underneath. *Save grades* and *Release grade* sit at the top and bottom. Saving keeps your place (open items, scroll, PDF page) and updates the total and chips. Switching pupil with unsaved scores or feedback asks first.
- **Review screen is read-only**: all answers, annotations, existing marks and feedback, but no tools or score boxes. "Mark & grade this pupil" goes to the Grade tab; "Read-only view" in the Grade tab comes back. The bell's "Open their work" now opens the Grade tab.
- The work view is one shared block (`#reviewLayout`) moved between the Review screen and the Grade tab (`setupWorkspace(a, pupil, 'review' | 'grade')`, `openGrading()`).
- **Scientific calculator** (replaces the basic one, same "Allow on-screen calculator" setting): sin / cos / tan and inverses, DEG / RAD, ln, log, eˣ, 10ˣ, √, ∛, x², x³, xʸ, ʸ√x, 1/x, n!, %, π, e, EXP, |x|, brackets, Ans, Rand and memory (MC / MR / M+ / M−). Shows the expression and the result; proper order of operations with implied multiplication (2π, 3(4+1)); "Math error" / "Syntax error" instead of wrong answers; keyboard input while it has focus. Evaluated by its own parser — no `eval`.
- Fixed: a grade pop-up from another exam could appear over an exam a pupil had just been taken into (e.g. from Safe Exam Browser). Pop-ups now wait until the exam is submitted.

### Changed — Exam focus & quitting Safe Exam Browser
- **Pupils only see the exam while answering.** Once an exam opens (from the task list or straight from the SEB launch), "Back to my tasks", the bell and the account menu are hidden; what's left is answering (with Notepad, Resources, Calculator), **Message teacher** and **Submit**. A line at the top says the exam is in progress and saves automatically. The browser's Back button stays on the exam. Everything comes back once the work is submitted (or the exam closes); viewing submitted work is unaffected. Teacher previews are unaffected.
- **Submitting inside Safe Exam Browser closes SEB.** Previously only Lockdown-mode exams did this; now any exam submitted inside SEB goes to the config's quit address (`/?sebquit=1`, no confirmation), which makes SEB quit. If SEB doesn't quit, the pupil lands on "You can now close Safe Exam Browser" (the root redirect now keeps `?sebquit=1`).

### Changed — Marking tools
- Marking a written answer: **Underline**, **Highlight** and **Comment** (Circle removed).
- Marking the PDF: **Highlight** (drag), **Underline** and **Comment pin** (Circle and Box removed). Circles and boxes made before this change still show.

### Tests
- New: `test_sections_subjects.py`, `test_marking.py`, `test_resources.py`, `test_seb.py` (SEB hashes behind `/s/<slug>/`, launch link, JavaScript API, quit address); `test_invites.py` checks the new invite email. Full suite: 125 tests, only the 3 known Roles failures (see SYSTEM_CHECKS.md).
- 2026-10-08: `test_url_access.py` (10 tests — `/media/` not public, logos still public, school-less account and slug switching refused, signed-out → login, pupils kept out of teacher pages and other pupils' work, join-code and Lockdown questions held back, teachers see everything, hand-in unlocks reading back) and `test_graphcalc.py` (page carries the calculator, exam flag reaches the pupil, template note not shown). Full suite: 136 tests, still only the 3 known Roles failures.

### Changed — New invite email
- Invite email rewritten (subject "Your ExamHub account at <school>"): greeting, username and temporary password, how to change the password, what to do if they forget it ("Email me a code" with their username, then Change password), and the website (https://digitalexamhub.up.railway.app/) at the end. The separate "Sign in here" link is gone. The address can be changed with the `SITE_URL` environment variable.
- The "added" confirmation on Roles, the single-pupil confirmation and the downloaded logins CSV show the same address.

### Fixed
- **Lockdown exams stuck on the "Open in Safe Exam Browser" dialog inside SEB** (live and local). SEB opened and signed the pupil in, but the exam page then asked for SEB again. Cause: the school middleware removes `/s/<slug>` from the request path before the view runs, so the server checked SEB's hash against `…/api/…` while SEB had hashed `…/s/ssc/api/…` — no school page could ever match. The check now uses the address the browser actually requested (`_seb_request_paths`).
- **Newer SEB versions that don't send the hash headers** (notably macOS / iPadOS) are supported through SEB's JavaScript API: the exam page posts `SafeExamBrowser.security.configKey` / `browserExamKey` to `POST /api/seb/verify/`, and the one-time launch page (`/seb/enter/<token>/`) does the same before signing the pupil in. A verified SEB session then passes the lockdown checks for its background requests.
- **Lockdown (Safe Exam Browser) didn't open after the Railway domain was changed.** `ALLOWED_HOSTS` was hard-coded to the old domain, so requests to the new one were refused. It now accepts any `*.railway.app` domain, Railway's own `RAILWAY_PUBLIC_DOMAIN`, and anything listed in the `ALLOWED_HOSTS` environment variable; `CSRF_TRUSTED_ORIGINS` follows the same list, and `SECURE_PROXY_SSL_HEADER` is set so links are built with `https://` behind Railway's proxy (`examhub/settings.py`).
- **PDFs not opening in the teacher review (and anywhere else) on the live site.** The local copy and Railway share one database but not one disk, and Railway's disk is wiped on every deploy, so files uploaded on one machine (or before a redeploy) were missing elsewhere. Uploads are now stored on disk **and** in the database (`StoredFile` model, `digitalexams/storage.py` `DatabaseBackedStorage`); reads fall back to the database copy and re-cache it to disk.
- Review page now explains why a PDF can't be opened (e.g. "this file isn't available on the server any more") and points to the annotations still listed on the right, instead of a bare "Couldn't open this PDF".
- PDF / image / video requests now include the `/s/<slug>` prefix (no redirect hop).
- **Pupil sign-in flash:** pupils briefly saw the teacher dashboard, the "Teacher view / Pupil preview" switch and the "Preview as a pupil" page before their exams. The page now renders in the pupil layout from the start, with a "Loading your exams…" placeholder (and an error message instead of the teacher dashboard if loading fails).

### Added — Marking like pen on paper, easier exam navigation, submission notices
- **Teacher marking on written answers** (review screen → "✎ Mark this answer"): select words in the pupil's answer and choose **Underline**, **Highlight** or **Comment** (Circle was in the first version; removed — see "Changed — Marking tools"). Each mark gets a number and a margin comment (optional); change its style or delete it from the list; marks save automatically. The pupil's own answer is never changed.
- **Teacher marking on the PDF**: a "Mark:" toolbar above the review PDF — **Highlight**, **Underline** (drag) and **Comment pin** (click; Circle and Box were in the first version and have been removed), each with an optional comment; with the select tool, click one of the pupil's highlights/notes to **comment on their annotation**. Marks and comments are listed under the item ("Your marks on the PDF", and "💬 You: …" on the pupil's annotations).
- **Pupils see the marking only when the grade is released**: on their paper (marks drawn on their answer with a "Your teacher's comments" list; marks and 💬 badges on the PDF, click to read) and in "View grade".
- Stored in `Submission.teacher_marks` (migration `0028`); saved with `POST /api/submissions/<id>/marks/` (exam's teacher / admins only, validated and size-limited). Grade tab links now read "Review & mark up…".
- **Long answers scroll properly**: the answer area scrolls on its own (toolbar and Submit stay in view), so pupils can always scroll back up.
- **Question navigator** instead of one button per item: "‹ Question 7 of 30 ›" plus a grid of numbered squares (grouped by section) showing answered / not answered; the navigator, the content and the Contents panel stay in step.
- **Clearer Contents panel**: items and sections are full-width bars with a chevron and points; "Expand all / Collapse all"; long exams (7+ items) start with only the current item open; the current item is highlighted.
- **No more email to the teacher on submission.** New submissions show as a "N new" badge on the Exams list, a "New" chip in Monitor (with "Mark all as seen") and a 📥 item on the bell, until the teacher opens that pupil's work. (`Submission.teacher_seen_at`, `POST /api/submissions/<id>/seen/`, `POST /api/submissions/seen/`.)

### Added / changed — Date filter, editing who an exam is for, teacher view
- **Filter exams by the date they were created**: "Created from / to" on the Exams dashboard, plus "Last 7 days", "Last 30 days" and "Clear". The list gets a "Created" column; bulk "select all" only selects exams in the current filter. Dates are Manila dates. (`createdAt` added to the exam JSON.)
- **Edit exam → "Assigned to"**: the Settings dialog now lists the teacher's subjects (admins: subjects and year groups) with everyone currently on the exam ticked. Untick to remove, tick to add. Removing a pupil who has already started or handed in asks for confirmation first (their work is deleted). Pupils already on the exam who are no longer in any of the teacher's lists appear under "Already on this exam" so they aren't dropped by accident. Teachers can only add pupils from their own subjects. (`recipients` / `confirmRemoveStarted` on `POST /api/assignments/<id>/update/`; leaving `recipients` out keeps the list unchanged.)
- **Same pupil email allowed at different schools.** A pupil email only has to be unique within a school (adding one by hand, bulk import, Roles → Edit, Django admin). The same email at another school is a separate account. Staff emails stay unique everywhere.
- **Plain teachers see only the teacher view**: no "Teacher view / Pupil preview" switch and no "Preview as a pupil" button. Admins keep both.
- **Plain teachers don't get the Year groups tab**: only Exams and My subjects. `/classes/` sends them to Subjects; the dashboard button reads "My subjects". The tour no longer shows them Year groups or Pupil preview.

### Changed — Exam privacy & assigning by subject
- **Teachers only see their own exams.** The dashboard list, notifications and every exam action (monitor/status, edit, open/close, delete, messages, broadcast, accommodations, grading, publishing, unlock, files) are limited to the exam's own teacher. Admins and the main admin still see every exam in the school. Other teachers get "This exam belongs to another teacher." (`can_manage_exam()` / `visible_exams_for()` in views.py.)
- **Plain teachers assign by subject only.** "Assign to" shows just the subjects they teach (tick a whole subject or individual pupils); year groups aren't offered, and being attached to a year group no longer lets a teacher assign to its pupils (`User.teachable_pupils`). Admins keep both subjects and year groups. Wording on Roles, the tour and the Year groups page updated to match.

### Changed — Year groups, teacher template, admin editing
- **"Sections" are now called "Year groups"** everywhere (nav, pages, dialogs, tour, Roles, Activity, messages, bulk templates — e.g. "Bulk add pupils to year groups", "Year group list" tab). URLs and data unchanged; `/year-groups/` also works. Import headers "Year group", "Section" and "Class" are all accepted. Exam-builder "sections" (parts of an exam) are unrelated and unchanged.
- **Bulk add teachers / pupils: choose "Blank template" or "Template with current staff / pupils"** (`GET /api/bulk/<teachers|pupils>/template/?prefill=1`). The pre-filled file lists everyone already in the school (last name A–Z; main admins left out of staff) with their role, year group and subjects; re-uploading it unchanged changes nothing.
- Pupils template column "Section" is now "Year group" (missed in the rename).
- **Bulk add teachers template uses Subjects only** (no Sections/Year groups column; a "Subject list" tab feeds the dropdown). Older files with a Sections column still import.
- **Superadmin can edit a school admin's first name, last name and email** (Admins dialog → Edit). `POST /superadmin/api/schools/<id>/admins/<user_id>/update/`. A new email is also applied to the exams they created.

### Fixed
- **Roles → Edit refused name-only changes** with "Another account already uses that email" whenever some other account (e.g. an old test login or one at another school) shared the person's email. The duplicate check now only runs when the email is actually changed.

### Added — Sections & subjects
- **"Classes" are now called "Sections"** everywhere in the app (nav, pages, dialogs, tour, Roles, Activity, messages). Data, URLs (`/classes/`, `/api/classes/`) and the `SchoolClass` model are unchanged; `/sections/` also works.
- **Subjects** (`Subject` model: school, name, teachers, pupils — migration `0027`). A subject can mix pupils from several sections. New **Subjects** page (`/subjects/`): create / rename / delete, assign teachers, see each pupil's section and the section mix, and add pupils from any section with a picker grouped by section (tick a section to take everyone not yet in the subject; picks survive search and the section filter). Regular teachers see "My subjects" (read-only).
  - API: `GET /api/subjects/`, `POST /api/subjects/create/`, `POST /api/subjects/<id>/update/` (`name`, `teacherIds`, `pupilIds`, `addPupilIds`, `removePupilIds`), `POST /api/subjects/<id>/delete/`.
  - A subject's teachers can assign exams to its pupils (`teachable_pupils` now covers sections **and** subjects; `User.visible_subjects()`).
  - Exam "Assign to" picker groups pupils under **Subjects** and **Sections**; a pupil listed in several groups is ticked everywhere at once and counted once. (Also fixes the group "select all" boxes being sent as a recipient called "on".)
- **New bulk imports** (preview first, add-only, `.xlsx` or `.csv`):
  - Sections page → **Bulk import sections** (was "Bulk import classes") and **Bulk add pupils to sections** (`kind=section_pupils`): template lists every pupil with their current section; fill in "Add to section". Existing accounts only — new accounts are still created on Roles.
  - Subjects page → **Bulk import subjects** (`kind=subjects`: subject name, teacher emails, rename to) and **Bulk add pupils to subjects** (`kind=subject_pupils`): one shaded row per section (fill in Subjects to add the whole section) followed by one row per pupil.
  - Roles → bulk add teachers / pupils: new optional **Subjects** column; "Class(es)" column is now "Section(s)" (old headers still accepted). Add-one forms on Roles can pick subjects too; the people list shows sections and subjects.
- **First name and last name are separate columns in every template** (Section pupils, Subject pupils, Teacher list, and the downloaded logins CSV; the Teachers/Pupils templates already were). Pre-filled pupil and teacher lists are in last-name A–Z order.
- **Sort by name** on Roles (Staff and Pupils), Sections, Subjects (list and add-pupils picker) and the exam "Assign to" picker: Last name A–Z (default), Last name Z–A, First name A–Z, First name Z–A. The choice is remembered in the browser and shared across those pages (`templates/_name_sort.html`). Re-sorting "Assign to" keeps pupils already ticked.
- **Commas separate several sections / subjects / teacher emails in one cell** (e.g. `Pre-U 1 Economics, Pre-U 1 Physics`) in every bulk import and template. `;` still works for older files; a cell that exactly matches an existing name containing a comma is kept whole.
- **Delete staff** on Roles → Staff (single or bulk, type DELETE for bulk): `POST /api/accounts/staff/delete/`. You can't delete yourself or a main admin; their exams stay.
- **Show / hide password** eye button on every password box (`templates/_password_toggle.html`).
- Activity: subject changes are logged and appear under the "Sections & subjects" filter.
- Tests: `test_sections_subjects.py`; wording in older tests updated (Classes → Sections).

### Added
- **Edit people** (Roles → Staff / Pupils → *Edit*): first name, last name, email. A pupil's new email/name is applied to every exam they're on; a teacher's to the exams they own. `POST /api/accounts/<id>/update/`.
- **Delete pupils** individually (*Delete* on a row) or in bulk (select → *Delete selected*), with a confirmation dialog listing who will be deleted; bulk deletes require typing DELETE. Submitted work stays on the exams. `POST /api/accounts/pupils/delete/`.
- `python manage.py sync_media_to_db` management command.
- Tests: `test_people_and_files.py`.

---

## [0.22.0] — 2026-10-06

### Upgrade notes
- Run `python manage.py migrate` — four new migrations (`0022`–`0025`).
- **`settings.py` still contains the live Railway database password.** Running `migrate` locally applies to the live DB. Move the credentials to `.env` and rotate the password.
- To send real email (sign-in codes, invites, submission notices), set in `.env` / Railway: `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` (Gmail / Google Workspace **App Password**), optionally `DEFAULT_FROM_EMAIL`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_BACKEND`. Without `EMAIL_HOST_USER`, mail prints to the console.

### Added — Sign-in & accounts
- **Passwordless sign-in with an emailed code** (alongside username + password) — `digitalexams/email_login.py`.
  - Login page has two tabs: *Password* and *Email me a code*. Identifier = username or email, scoped to the school in the URL.
  - 6-digit code, 10-minute expiry, single use, salted-hash storage, 5 wrong attempts burns it, 60 s resend cooldown, same reply whether or not the account exists.
  - "Forgot password?" → sign in with a code → change-password dialog opens without asking for the old password (15-minute, one-use window).
  - Model `EmailLoginCode`; migration `0022_emaillogincode`.
- **Account invites** (single, selected, or "everyone not yet invited") — `digitalexams/accounts.py`.
  - Each invite sets a fresh temporary password and emails username + password + sign-in link. Logins can also be downloaded as CSV if email isn't working.
  - `POST /api/accounts/invite/` (`{userIds}` or `{all: "staff"|"pupils", onlyNotInvited}`), admins only.
  - Adding a person (one-by-one or bulk) can send the invite straight away.
- **Forced password change on first sign-in** — `ForcePasswordChangeMiddleware`. Anyone on a temporary password is held on `/welcome/password/` ("Choose your password") until they set their own; API calls return `403 password_change_required`. The welcome tour starts only after this.
  - User fields `must_change_password`, `invited_at`, `activated_at`; `User.account_status` = `active` / `invited` / `not_invited`. Migration `0025_user_activation`.
  - Every account created with a temporary password (Roles page, bulk import, superadmin) now has `must_change_password=True`.
- **Email settings via environment** in `settings.py` (SMTP when `EMAIL_HOST_USER` is set).

### Added — People & roles
- **Roles page rebuilt as "People & roles"** with **Staff** and **Pupils** tabs: search, status filter, per-person status (Not invited / Invited / Active), Send / Resend / Reset & re-invite, multi-select invites, add-one form (optional classes + "Email them an invite now").
- **Pupil account creation moved from Classes to Roles.** Classes page keeps "Add existing pupils to this class".
- **Bulk add teachers** (`kind=teachers`): First name, Last name, Email, Role (Teacher/Admin dropdown), Classes (dropdown of existing classes, `;`-separated). Existing staff are updated (role change, classes added — never removed). Unknown classes are errors.
- Bulk add pupils: **Class is now optional.** Bulk imports accept `invite=1` to email new accounts.
- Shared bulk-import dialog `templates/_bulk_import.html` (used by Classes and Roles).
- `GET /api/accounts/pupils/` (admin list with classes + status); `/api/teachers/` now includes `status`, `invitedAt`; `/api/teachers/create/` accepts `classIds`, `invite`; `/api/pupils/create/` accepts `invite`, class optional.

### Added — Exams
- **"Assignments" renamed "Exams"** throughout the UI (nav, buttons, headings, tour, login page, Activity filters, API error messages). Code identifiers and URLs unchanged.
- **Visibility control**: *Visible now* / *At a set time* / *Keep hidden — I'll open it*. Hidden and not-yet-visible exams are invisible to pupils (task list, start, files). `Assignment.is_hidden`.
- **Closing control**: *Automatically at the end time* or *I'll close it manually*. Teacher can **Close exam now**, **Reopen**, **Make visible now**, **Hide** from the exam page status bar — `POST /api/assignments/<id>/state/` (`show` / `hide` / `close` / `reopen`). `Assignment.closed_at`. Migration `0024_assignment_visibility_and_manual_close`.
- **No exam duration**: timers count down to the end time (+ a pupil's extra minutes); "No time limit" when closed manually. `duration_minutes` kept but unused.
- **Automatic hand-in on close**: pupils' pages poll every 10 s and submit when the exam closes; a 90 s grace window accepts that final submit; afterwards any open attempt is submitted from its last autosave (`finalize_closed_attempts`).
- Exam status chip (Hidden / Scheduled / Live / Closed) on the dashboard list and exam page; Settings dialog and bulk schedule edit use the same choices.
- **Autosave replaces "Save draft"** for pupils (every 8 s when changed, plus when the tab is hidden), with a "Saved hh:mm" indicator.

### Added — Review, grading, notifications
- **Teacher review page rebuilt**: zoomable, fit-to-width, high-DPI PDF viewer; annotations scaled correctly at any zoom (pupil coordinates are at scale 1.2); show/hide annotations; wider-viewer toggle; per-item **collapsible sections** (question, answer, correct answer, feedback, annotation list that jumps to the page and flashes the annotation); expand/collapse all; score chip; "Grade this pupil".
- **Grade tab**: a review button on every item (opens that item's section) and "Review full submission"; "Back to grading" returns to the same pupil.
- **Notifications on every page** — `templates/_notifications.html`, `GET /api/notifications/`:
  - Teachers: pop-up + bell for **pupil locked out** (with resume code, *Unlock now*, *Open monitor*) and **new pupil message** (*Reply*). Lockouts shown first. Plain teachers: own exams; admins: whole school.
  - Pupils: bell for teacher messages; pop-up + bell when a **grade is released** (never while mid-exam).
  - Bell added to Classes / Activity / Roles pages.
- **Pupil "View grade"**: total, percentage and per-item score + feedback; from the task list, the paper, or the notification. `POST /api/submissions/<id>/grade-seen/`; `Submission.grade_seen_at` (migration `0023_submission_grade_seen_at`).
- **Viewing submitted work** opens in a normal browser regardless of delivery mode (no fullscreen, exit lock or copy guard); Lockdown-mode files are served outside SEB once submitted; can be opened after the end time.

### Changed
- **Activity tab is admin-only** (nav, dashboard button, page and API). Classes page no longer shows activity (log button, class activity, recent activity).
- **Data scoped to the school in the URL** (`request.school`) instead of the account's own school — fixes platform superusers seeing every school's pupils, classes and teachers.
- Bulk import of classes stays on the Classes page; teachers and pupils are bulk-added on Roles.
- Submission email to the teacher uses `fail_silently=True`, so an email failure can't turn a saved submission into an error.

### Fixed
- **Guided tour restarting** on every tab / sign-in: "tour done" was posted without the `/s/<slug>` prefix, redirected by the middleware into a GET and rejected. Now prefixed (+ a per-browser fallback).
- **Sign out** on the Exams page posted to an unprefixed URL and failed the same way.
- Tour highlight targets now include the school prefix.
- Review "Back" button when arriving from a `?review=` link.

### Tests
- New: `test_email_login.py`, `test_tour.py`, `test_school_scoping.py`, `test_notifications.py`, `test_exam_window.py`, `test_invites.py`.
- Known failing (pre-existing, unchanged): `TeacherRoleTests.test_main_admin_can_promote_and_demote`, `TeacherRoleTests.test_only_main_admin_can_manage_roles`, `RolesTabAndTourTests.test_roles_tab_on_every_teacher_page_for_main_admin_only` — they expect the Roles page to be superuser-only, but school admins (`is_school_admin`) can now use it. Update the tests or the rule.

---

### Previously unreleased (shipped in 0.22.0)
- **Bulk import of classes, teachers and pupils** — `digitalexams/bulk_import.py`.
  - Classes (create, set/replace teachers by email, rename) on the Classes page; teachers and pupils on the Roles page.
  - Downloadable `.xlsx` templates: tab 1 = data, tab 2 = **Instructions**, tab 3 = reference list (teachers / existing classes) feeding dropdowns. The classes template is pre-filled with the school's current classes.
  - Accepts `.xlsx` or `.csv`. Header names are flexible; a title row above the header is fine.
  - Upload is previewed first (new / update / no change / error per row); nothing is saved until confirmed. Errors block the import unless "skip rows with errors" is chosen. Applied in one transaction.
  - New accounts get a temporary password; a one-time logins CSV can be downloaded after import.
  - Endpoints: `GET /api/bulk/<classes|pupils|teachers>/template/`, `POST /api/bulk/<classes|pupils|teachers>/import/` (`file`, `commit`, `skipErrors`, `invite`).
  - New dependency: `openpyxl==3.1.5`.

---

## [0.21.0] — 2026-10-06

### Added
- **`School` model (multi-tenancy)** — every piece of data in ExamHub is now scoped to a `School` tenant.
  - Fields: `name`, `slug` (URL-safe unique identifier), `logo`, `primary_color`, `timezone`, `is_active`, `created_at`.
  - `School.is_active` gate: inactive schools return HTTP 403 on all requests.
  - Per-school branding (`primary_color`, `logo`) injected into templates via `request.school`.
- **`User.school` FK** — links every teacher/pupil to their school. Platform super-admins have `school=None`.
- **`User.is_school_admin` flag** — replaces the old `is_staff`-based class-admin pattern.
  - `can_manage_classes` property now returns `True` for superusers **or** users with `is_school_admin=True`.
  - Migration: `0021_school_user_is_school_admin_alter_schoolclass_name_and_more`.
- **`SchoolTenantMiddleware`** (`digitalexams/middleware.py`) — resolves `/s/<slug>/…` URLs.
  - Sets `request.school`, `request.school_url_prefix`, and rewrites `request.path_info` so all existing URL names / `reverse()` calls work unchanged.
  - Exempt paths: `/admin/`, `/superadmin/`, `/__debug__/`, `/static/`, `/media/` (and anything in `TENANT_EXEMPT_PATHS` setting).
  - Authenticated users hitting bare paths (e.g. `/classes/`) are auto-redirected to `/s/<slug>/classes/`.
  - Unknown slugs → 404. Inactive schools → 403. Cross-school access by authenticated users → 403.
- **Teacher-role management** (superuser-only):
  - `GET /roles/` — Roles page (only shown to superusers; `is_school_admin` admins are redirected to `/classes/`).
  - `GET /teachers/` — legacy alias, redirects to `/roles/`.
  - `GET /api/teachers/` — lists all teachers in the school (superuser only).
  - `POST /api/teachers/create/` — creates a new teacher or admin account with a random temp password.
    - Accepts `role: "admin" | "teacher"`. Rejects `"main_admin"`, `"pupil"`, and duplicate emails (case-insensitive).
    - Returns `{ isAdmin, tempPassword, … }`.
    - Logs `teacher_created` in `ActivityLogEntry`.
  - `POST /api/teachers/<id>/admin/` — promotes or demotes a teacher's admin flag (`isAdmin: true/false`).
    - Rejects changes to superusers and pupils (HTTP 400).
    - Logs `admin_granted` / `admin_revoked` in `ActivityLogEntry`.
- **Superadmin dashboard** (platform superuser only):
  - `GET /superadmin/` — lists all schools with stats.
  - `POST /superadmin/api/schools/create/` — creates a school + its first admin account in one call.
  - `POST /superadmin/api/schools/<id>/update/` — updates school metadata (name, slug, color, timezone).
  - `POST /superadmin/api/schools/<id>/toggle-active/` — toggles `is_active` on a school.
  - `GET /superadmin/api/schools/<id>/admins/` — lists admin accounts for a specific school.
  - `POST /superadmin/api/schools/<id>/create-admin/` — adds a new `is_school_admin` account to an existing school.
- **`School` FK added to existing models:**
  - `SchoolClass.school` (nullable FK, `related_name='classes'`).
  - `Assignment.school` (nullable FK, `related_name='assignments'`).
  - `AllowedBrowserExamKey.school` (nullable FK, `related_name='seb_keys'`).
- **Test suite expanded** — two new `TestCase` classes:
  - `MultiTenancyAndSuperAdminTests` — covers middleware routing, cross-school isolation, superadmin API, inactive-school lock-out, and per-school branding in templates.
  - `TeacherRoleTests` (existing class extended) — covers promote/demote, role-listing, and guard rails against changing superusers or pupils.
  - All existing test helpers updated to use `/s/<slug>/…` URL prefix.

---

## [0.20.0] — 2026-10-06

### Added
- **Guided tour** (`User.tour_completed_at`) — each role (main admin / admin / teacher / pupil) gets a tailored walkthrough on first login.
  - `POST /api/account/tour-done/` marks the tour complete and stops auto-start.
  - Manual replay available from the account menu (`data-start-tour` attribute).
  - Migration: `0020_user_tour_completed_at`.

---

## [0.19.0]

### Added
- **Activity log — class scoping** — `ActivityLogEntry` now carries a `school_class` FK so roster-change events can be filtered by class on the Activity page.
  - Migration: `0019_activitylogentry_school_class`.

---

## [0.18.0]

### Added
- **Submission exit-lock** for `Any browser with security` delivery mode.
  - `Submission` gains `locked_at`, `lock_reason`, `resume_code`, `lock_count`.
  - Server refuses saves/submits while locked.
  - `POST /api/submissions/<id>/lock/` — triggered automatically when a pupil leaves the exam window (tab switch, new tab, fullscreen exit).
  - `POST /api/submissions/<id>/resume/` — pupil enters a teacher-provided one-word code.
  - `POST /api/submissions/<id>/unlock/` — teacher unlocks directly from the Monitor tab.
  - `GET /api/submissions/<id>/lock-status/` — polling endpoint for the pupil's lock screen.
  - Migration: `0018_submission_exit_lock`.

---

## [0.17.0]

### Added
- **Broadcast messages** — teacher can send one message to every recipient of an assignment at once (`POST /api/assignments/<id>/broadcast-message/`). Messages are flagged `is_broadcast=True` in the chat thread.
- **Unread-dot tracking** — `Recipient` gains `teacher_last_read_at` and `pupil_last_read_at`; messages newer than the reader's timestamp show as unread.
  - Migration: `0017_message_is_broadcast_recipient_pupil_last_read_at_and_more`.

---

## [0.16.0]

### Added
- **Assignable pupils constraint** — `User.assignable_pupils()` classmethod returns only active, non-superuser, role=Pupil accounts.
- `SchoolClass.pupils` M2M now enforces `is_superuser=False` and `is_active=True` at the DB level.
- Migration: `0016_assignable_pupils`.

---

## [0.15.0]

### Added
- **Image items** — `AssignmentItem` gains an `image` file field (uploaded to `assignments/%Y/%m/`). Served at `GET /api/items/<id>/image/`.
- **Video items** — `AssignmentItem` gains a `video` file field. Served at `GET /api/items/<id>/video/`.
- **Section grouping** — `AssignmentItem.section` (optional, max 80 chars) groups consecutive items under a visual section header with a point-total badge in the pupil's resources panel.
- Migration: `0015_assignmentitem_image_assignmentitem_section_and_more`.

---

## [0.14.0]

### Added
- **Safe Exam Browser (SEB) integration** — lockdown delivery mode.
  - `AllowedBrowserExamKey` model stores one Browser Exam Key hash per SEB platform/version.
  - `GET /api/seb/config/` — downloads the shared `.seb` config file (teachers only).
  - `POST /api/seb/check/` — validates the SEB Browser Exam Key hash against stored rows.
  - `POST /api/seb/launch/` — generates a one-time token for the SEB launch flow.
  - `GET /seb/launch/<token>/config.seb` — serves a per-pupil `.seb` config.
  - `GET /seb/enter/<token>/` — SEB entry page that validates the token and begins the exam.
  - `examhub-lockdown.seb` config file added to project root.
  - Migration: `0014_allowedbrowserexamkey`.

---

## [0.13.0]

### Changed
- `ActivityLogEntry.id` migrated to `BigAutoField` for large-scale deployments.

### Added
- **1:1 messaging** — `Message` model scoped per `Recipient`.
  - `GET /api/recipients/<id>/messages/` — fetch thread.
  - `POST /api/recipients/<id>/messages/send/` — teacher or pupil sends a message.
  - Migration: `0013_alter_activitylogentry_id_and_more`.

---

## [0.12.0] — Phase 4 & 5

### Added
- **Grading** — `Submission` gains `item_scores` (JSON), `total_score`, `max_score`, `graded_at`.
  - Auto-grading on submit for MCQ single/multiple, True/False, and Fill-in-the-blank items.
  - `POST /api/submissions/<id>/grade/` — teacher saves scores and feedback.
  - `AssignmentItem.correct_answer` and `AssignmentItem.points` fields.
- **Grade publishing** — `Submission.published` flag; grades are hidden until explicitly published.
  - `POST /api/submissions/<id>/publish/` — publish one submission.
  - `POST /api/assignments/<id>/publish-grades/` — publish all submissions for an assignment.
- **Accommodations** — `Recipient` gains `extra_minutes` and `opens_override` for per-pupil timing overrides.
  - `POST /api/recipients/<id>/accommodations/` — update accommodations.
- **Activity log** — `ActivityLogEntry` model records assignment lifecycle, roster changes, submissions, and grading events.
  - `GET /api/activity-log/` — paginated, filterable by class and category.
  - Activity page at `/activity/`.
- Migration: `0012_phase4_phase5`.

---

## [0.11.0]

### Changed
- **AssignmentText replaced by AssignmentItem** — unified model supports PDF, Image, Video, Long answer, MCQ (single/multiple), True/False, Fill-in-the-blank, and Table item types.
  - `AssignmentItem.item_type` choices: `pdf`, `image`, `video`, `long_answer`, `mcq_single`, `mcq_multiple`, `true_false`, `fill_text`, `table`.
  - `AssignmentItem.options` (JSON) stores MCQ choices or Table headers/dimensions.
  - `AssignmentItem.prompt` for question text.
  - `AssignmentItem.order` controls display order.

### Added
- Media serving endpoints: `GET /api/items/<id>/pdf/`, `/image/`, `/video/`.
- Migration: `0011_assignment_items`.

---

## [0.10.0]

### Added
- **School classes** — `SchoolClass` model with M2M to teachers and pupils.
  - Admin teachers (staff flag or superuser) create, rename, and delete classes; assign teachers and pupils.
  - Regular teachers see only their assigned classes and can only assign work to those classes' pupils.
  - `GET /api/classes/` — list classes (filtered by role).
  - `POST /api/classes/create/` — admin only.
  - `POST /api/classes/<id>/update/` — admin only.
  - `POST /api/classes/<id>/delete/` — admin only.
  - `GET /api/pupils/` — list pupils (filtered by teacher's classes).
  - `POST /api/pupils/create/` — admin only.
  - Classes page at `/classes/`.
  - Migration: `0010_schoolclass`.

---

## [0.9.0]

### Added
- **Delivery modes** — `Assignment.delivery_mode` choices: `browser`, `browser_secure`, `lockdown`, `paper`.
- **Join code** — `Assignment.require_join_code` / `join_code` (8 chars, optional).
- **Calculator flags** — `Assignment.allow_calculator` and `allow_graphing_calculator`.
- Migration: `0009_assignment_delivery_settings`.

---

## [0.8.0]

### Changed
- `Submission.responses` migrated to a per-item JSON dict (`{"<AssignmentText id>": "<html>", ...}`) replacing the single `response_text` field.
- Migration: `0008_submission_responses_per_text`.

---

## [0.7.0]

### Removed
- `Assignment.source_pdf` field removed (replaced by `AssignmentText`).
- Migration: `0007_remove_assignment_source_pdf`.

---

## [0.6.0]

### Changed
- Existing source PDFs copied into `AssignmentText` rows (data migration).
- Migration: `0006_copy_source_pdfs_to_texts`.

---

## [0.5.0]

### Added
- `AssignmentText` model (predecessor to `AssignmentItem`) — stores PDF files and written responses.
- `Submission.response_text` field.
- Migration: `0005_assignmenttext_submission_response_text`.

---

## [0.4.0]

### Added
- `Assignment.duration_minutes` (default 60) — countdown starts when pupil opens the assignment.
- `Submission.started_at` — timestamp when the pupil first opens the assignment.
- Migration: `0004_assignment_duration_submission_started_at`.

---

## [0.3.0]

### Changed
- `User.role` default set to `PUPIL`.
- Migration: `0003_user_role_default`.

---

## [0.2.0]

### Changed
- User groups and superuser flag constraints adjusted.
- Superusers auto-assigned `role=TEACHER` on creation.
- Migration: `0002_alter_user_groups_alter_user_is_superuser`.

---

## [0.1.0] — Initial Release

### Added
- **Custom User model** (`digitalexams.User`) extending `AbstractUser`.
  - `role` field: `teacher` or `pupil`.
  - `can_manage_classes` property.
  - `teachable_pupils()` and `visible_classes()` helpers.
- **Assignment model** — title, instructions, teacher info, opens/due timestamps.
- **Recipient model** — links a pupil (by name + email) to an assignment; unique per assignment.
- **Submission model** — tracks submission lifecycle (`not_started` → `draft` → `submitted`), annotations, and outline HTML.
- **Core API endpoints:**
  - `POST /api/assignments/create/`
  - `GET /api/assignments/` — list
  - `GET /api/assignments/<id>/status/`
  - `PUT /api/assignments/<id>/update/`
  - `DELETE /api/assignments/<id>/delete/`
  - `POST /api/submissions/<id>/start/`
  - `POST /api/submissions/<id>/save/`
  - `POST /api/submissions/<id>/submit/`
- **Auth:** Login (`/login/`), logout (`/logout/`), password change (`/api/account/change-password/`).
- **Deployment:** Railway + PostgreSQL (via `psycopg2-binary`), Gunicorn, WhiteNoise for static files.
- Migration: `0001_initial`.

---

*Maintained by the ExamHub team. Update this file with every merged feature, fix, or breaking change.*
