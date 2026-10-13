"""Spell check on/off per exam, audio items with a play limit, and exporting work to PDF."""
import io
import json
import shutil
import tempfile
import zipfile

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from . import pdf_export
from .models import ActivityLogEntry, Assignment, AssignmentItem, Recipient, School, Subject, Submission, User

MEDIA = tempfile.mkdtemp(prefix='examhub-test-media-')
MP3_BYTES = b'ID3\x03\x00\x00\x00\x00\x00\x00' + b'\x00' * 64


def make_pdf(pages=2):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(pages):
        c.drawString(72, 720, f'Source text page {i + 1}')
        c.showPage()
    c.save()
    return buf.getvalue()


def pdf_text(data):
    from pypdf import PdfReader
    return '\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(data)).pages)


@override_settings(MEDIA_ROOT=MEDIA)
class SpellcheckAudioExportTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.school = School.objects.create(name='SSC', slug='ssc')
        self.t = User.objects.create_user('t', 't@x.edu', 'x', school=self.school, role='teacher', first_name='Tess')
        self.other_t = User.objects.create_user('o', 'o@x.edu', 'x', school=self.school, role='teacher')
        self.p = User.objects.create_user('p', 'p@x.edu', 'x', school=self.school, first_name='Pia')
        self.p2 = User.objects.create_user('p2', 'p2@x.edu', 'x', school=self.school, first_name='Ola')
        subj = Subject.objects.create(school=self.school, name='French')
        subj.teachers.add(self.t)
        subj.pupils.add(self.p, self.p2)

    # ---------- helpers ----------
    def create(self, items_meta, audios=(), **extra):
        data = {'title': 'Listening', 'items_meta': json.dumps(items_meta),
                'recipients': json.dumps([{'name': 'Pia', 'email': 'p@x.edu'}, {'name': 'Ola', 'email': 'p2@x.edu'}]),
                'audios': list(audios)}
        data.update(extra)
        return self.client.post('/s/ssc/api/assignments/create/', data)

    def audio_exam(self, max_plays=2, allow_pause=False):
        self.client.force_login(self.t)
        r = self.create([{'type': 'audio', 'label': 'Track 1', 'maxPlays': max_plays, 'allowPause': allow_pause, 'points': 3},
                         {'type': 'mcq_single', 'label': 'Q1', 'prompt': 'Where is Marie?',
                          'options': [{'id': 'a', 'text': 'Paris'}, {'id': 'b', 'text': 'Lyon'}],
                          'correctAnswer': {'optionId': 'a'}}],
                        [SimpleUploadedFile('track.mp3', MP3_BYTES, 'audio/mpeg')])
        self.assertEqual(r.status_code, 201, r.content)
        exam = Assignment.objects.get(id=r.json()['id'])
        return exam, exam.items.get(item_type='audio')

    def start(self, exam, user):
        self.client.force_login(user)
        rec = exam.recipients.get(pupil_email=user.email)
        r = self.client.post(f'/s/ssc/api/submissions/{rec.id}/start/', '{}', content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        return rec

    # ---------- spell check ----------
    def test_spellcheck_setting_is_saved_and_kept_when_left_out(self):
        self.client.force_login(self.t)
        r = self.create([{'type': 'long_answer', 'label': 'Q1', 'prompt': 'Écrivez.'}])
        exam = Assignment.objects.get(id=r.json()['id'])
        self.assertFalse(exam.allow_spellcheck)                       # off unless ticked
        r = self.create([{'type': 'long_answer', 'label': 'Q1', 'prompt': 'Write.'}], allow_spellcheck='true')
        self.assertTrue(Assignment.objects.get(id=r.json()['id']).allow_spellcheck)
        base = {'title': 'Listening', 'deliveryMode': 'browser'}
        r = self.client.post(f'/s/ssc/api/assignments/{exam.id}/update/', json.dumps(dict(base, allowSpellcheck=True)),
                             content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()['allowSpellcheck'])
        # An older page that doesn't send the field leaves it as it was.
        self.client.post(f'/s/ssc/api/assignments/{exam.id}/update/', json.dumps(base), content_type='application/json')
        exam.refresh_from_db()
        self.assertTrue(exam.allow_spellcheck)
        self.client.force_login(self.p)
        self.assertTrue(self.client.get('/s/ssc/api/assignments/').json()['assignments'][0]['allowSpellcheck'])

    def test_page_has_the_spellcheck_audio_and_export_controls(self):
        self.client.force_login(self.t)
        html = self.client.get('/s/ssc/').content.decode()
        for needle in ('id="aAllowSpellcheck"', 'id="editAssignmentSpellcheck"', 'function applyExamSpellcheck',
                       'data-add-type="audio"', 'class="item-audio-file"', 'function renderExamAudioPlayer',
                       'id="exportPdfBtn"', 'id="exportModal"', 'function downloadExport'):
            self.assertIn(needle, html)

    # ---------- audio ----------
    def test_audio_item_is_created_unmarked_with_its_play_rules(self):
        exam, item = self.audio_exam(max_plays=2, allow_pause=False)
        self.assertEqual((item.audio_max_plays, item.audio_allow_pause, item.points), (2, False, 0.0))
        self.client.force_login(self.p)
        data = self.client.get('/s/ssc/api/assignments/').json()['assignments'][0]['items'][0]
        self.assertEqual((data['type'], data['maxPlays'], data['allowPause']), ('audio', 2, False))
        self.assertTrue(data['audioUrl'].endswith(f'/api/items/{item.id}/audio/'))

    def test_audio_upload_checks(self):
        self.client.force_login(self.t)
        r = self.create([{'type': 'audio', 'label': 'T'}], [SimpleUploadedFile('notes.txt', b'hi', 'text/plain')])
        self.assertEqual(r.status_code, 400)
        r = self.create([{'type': 'audio', 'label': 'T'}])
        self.assertEqual(r.status_code, 400)
        r = self.create([{'type': 'audio', 'label': 'T', 'maxPlays': 50}], [SimpleUploadedFile('a.mp3', MP3_BYTES, 'audio/mpeg')])
        self.assertEqual(r.status_code, 400)
        # .m4a that the browser labels oddly is still accepted by its extension.
        r = self.create([{'type': 'audio', 'label': 'T'}], [SimpleUploadedFile('a.m4a', MP3_BYTES, 'application/octet-stream')])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertFalse(Assignment.objects.filter(title='Listening').count() > 1)

    def test_play_limit_is_counted_on_the_server(self):
        exam, item = self.audio_exam(max_plays=2)
        rec = self.start(exam, self.p)
        url = f'/s/ssc/api/submissions/{rec.id}/audio/{item.id}/play/'
        # A limited recording isn't sent until a play has been counted.
        r = self.client.get(f'/s/ssc/api/items/{item.id}/audio/')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['code'], 'play_not_started')
        self.assertEqual(self.client.post(url).json(), {'plays': 1, 'maxPlays': 2})
        r = self.client.get(f'/s/ssc/api/items/{item.id}/audio/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'audio/mpeg')
        self.assertEqual(self.client.post(url).json()['plays'], 2)
        r = self.client.post(url)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()['code'], 'no_plays_left')
        rec.submission.refresh_from_db()
        self.assertEqual(rec.submission.audio_plays, {str(item.id): 2})
        # The count reaches the pupil's and the teacher's views.
        mine = self.client.get('/s/ssc/api/assignments/').json()['assignments'][0]['recipients'][0]
        self.assertEqual(mine['audioPlays'], {str(item.id): 2})
        # Another pupil can't use up Pia's plays; teachers don't use plays and can always listen.
        self.client.force_login(self.p2)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.t)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.assertEqual(self.client.get(f'/s/ssc/api/items/{item.id}/audio/').status_code, 200)
        self.client.force_login(self.other_t)
        self.assertEqual(self.client.get(f'/s/ssc/api/items/{item.id}/audio/').status_code, 403)

    def test_a_save_that_started_before_a_play_does_not_undo_it(self):
        exam, item = self.audio_exam(max_plays=1)
        rec = self.start(exam, self.p)
        stale = Submission.objects.get(recipient=rec)                  # e.g. an autosave in flight
        self.client.post(f'/s/ssc/api/submissions/{rec.id}/audio/{item.id}/play/')
        stale.responses = {'x': 'typed'}
        stale.save()
        fresh = Submission.objects.get(recipient=rec)
        self.assertEqual(fresh.audio_plays, {str(item.id): 1})
        self.assertEqual(fresh.responses, {'x': 'typed'})
        r = self.client.post('/s/ssc/api/submissions/%d/save/' % rec.id,
                             json.dumps({'annotations': [], 'outlineHtml': '', 'responses': {}}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.post(f'/s/ssc/api/submissions/{rec.id}/audio/{item.id}/play/').status_code, 409)

    def test_unlimited_audio_is_served_straight_away_and_locked_pupils_cant_play(self):
        exam, item = self.audio_exam(max_plays=0, allow_pause=True)
        rec = self.start(exam, self.p)
        self.assertEqual(self.client.get(f'/s/ssc/api/items/{item.id}/audio/').status_code, 200)
        self.assertEqual(self.client.post(f'/s/ssc/api/submissions/{rec.id}/audio/{item.id}/play/').json()['plays'], 1)
        Submission.objects.filter(recipient=rec).update(locked_at=timezone.now())
        r = self.client.post(f'/s/ssc/api/submissions/{rec.id}/audio/{item.id}/play/')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['code'], 'exam_locked')

    # ---------- PDF export ----------
    def graded_exam(self):
        exam = Assignment.objects.create(school=self.school, title='Paper 1 — 中文', teacher_name='Tess', teacher_email='t@x.edu')
        pdf_item = AssignmentItem.objects.create(assignment=exam, item_type='pdf', label='Text 1', order=0, points=10)
        pdf_item.pdf.save('t1.pdf', ContentFile(make_pdf()))
        essay = AssignmentItem.objects.create(assignment=exam, item_type='long_answer', label='Q2', order=1, points=5,
                                              prompt='Discuss.')
        mcq = AssignmentItem.objects.create(assignment=exam, item_type='mcq_single', label='Q3', order=2, points=1,
                                            prompt='Pick', options=[{'id': 'a', 'text': 'Alpha'}, {'id': 'b', 'text': 'Beta'}],
                                            correct_answer={'optionId': 'b'})
        answer = '<div>The <b>lighthouse</b> means hope.</div><ul><li>point one</li></ul>'
        start = pdf_export.plain_text(answer).index('hope')
        recs = []
        for name, email, status in (('Pia', 'p@x.edu', 'submitted'), ('Ola', 'p2@x.edu', 'draft')):
            rec = Recipient.objects.create(assignment=exam, pupil_name=name, pupil_email=email)
            Submission.objects.create(
                recipient=rec, status=status, started_at=timezone.now(),
                submitted_at=timezone.now() if status == 'submitted' else None,
                responses={str(pdf_item.id): '<p>Response about contrast</p>', str(essay.id): answer, str(mcq.id): {'optionId': 'a'}},
                annotations=[{'textId': pdf_item.id, 'page': 1, 'type': 'highlight', 'color': 'y',
                              'rects': [{'x': 86, 'y': 100, 'w': 200, 'h': 16}], 'note': 'Key image here'}],
                item_scores={str(essay.id): {'score': 4, 'maxScore': 5, 'auto': False, 'feedback': 'Secret feedback'}},
                total_score=4, max_score=16, graded_at=timezone.now(),
                teacher_marks={'answers': {str(essay.id): [{'id': 'm1', 'start': start, 'end': start + 4, 'quote': 'hope',
                                                            'style': 'comment', 'comment': 'Teacher comment here'}]},
                               'pdf': {str(pdf_item.id): [{'id': 'p1', 'page': 2, 'shape': 'pin', 'x': 300, 'y': 200,
                                                           'w': 0, 'h': 0, 'comment': 'Pin comment'}]}})
            recs.append(rec)
        return exam, recs

    def test_teacher_exports_one_pupil_with_and_without_marks(self):
        exam, (pia, _) = self.graded_exam()
        self.client.force_login(self.t)
        r = self.client.get(f'/s/ssc/api/submissions/{pia.id}/export.pdf')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/pdf')
        self.assertIn('attachment;', r['Content-Disposition'])
        self.assertTrue(r.content.startswith(b'%PDF'))
        text = pdf_text(r.content)
        for needle in ('Pia', 'lighthouse', 'Teacher comment here', 'Secret feedback', 'Key image here', 'Pin comment',
                       'Source text page 2', 'Page 1 of'):
            self.assertIn(needle, text)
        clean = pdf_text(self.client.get(f'/s/ssc/api/submissions/{pia.id}/export.pdf?marks=0').content)
        self.assertIn('lighthouse', clean)
        self.assertIn('Key image here', clean)          # the pupil's own notes stay
        for needle in ('Teacher comment here', 'Secret feedback', 'Pin comment', 'correct answer'):
            self.assertNotIn(needle, clean)
        self.assertTrue(ActivityLogEntry.objects.filter(action='exported', assignment=exam).exists())

    def test_only_teachers_who_can_see_the_exam_can_export(self):
        exam, (pia, _) = self.graded_exam()
        for user in (self.p, self.other_t):
            self.client.force_login(user)
            self.assertEqual(self.client.get(f'/s/ssc/api/submissions/{pia.id}/export.pdf').status_code, 403)
            self.assertEqual(self.client.get(f'/s/ssc/api/assignments/{exam.id}/export/').status_code, 403)
        self.client.logout()
        self.assertNotEqual(self.client.get(f'/s/ssc/api/submissions/{pia.id}/export.pdf').status_code, 200)

    def test_whole_exam_as_zip_or_one_pdf(self):
        exam, _ = self.graded_exam()
        self.client.force_login(self.t)
        r = self.client.get(f'/s/ssc/api/assignments/{exam.id}/export/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/zip')
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        self.assertEqual(names, ['Pia.pdf'])                          # handed-in work only by default
        r = self.client.get(f'/s/ssc/api/assignments/{exam.id}/export/?who=started')
        self.assertEqual(sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist()), ['Ola.pdf', 'Pia.pdf'])
        r = self.client.get(f'/s/ssc/api/assignments/{exam.id}/export/?format=pdf&who=started')
        self.assertEqual(r['Content-Type'], 'application/pdf')
        text = pdf_text(r.content)
        self.assertIn('Pia', text)
        self.assertIn('Ola', text)
        self.assertEqual(self.client.get(f'/s/ssc/api/assignments/{exam.id}/export/?format=docx').status_code, 400)
        # pages=0 leaves the exam's own PDF pages out.
        r = self.client.get(f'/s/ssc/api/assignments/{exam.id}/export/?format=pdf&pages=0')
        text = pdf_text(r.content)
        self.assertNotIn('Source text page', text)
        self.assertIn('left out of this export', text)

    def test_nothing_to_export(self):
        exam = Assignment.objects.create(school=self.school, title='Empty', teacher_name='Tess', teacher_email='t@x.edu')
        Recipient.objects.create(assignment=exam, pupil_name='Pia', pupil_email='p@x.edu')
        self.client.force_login(self.t)
        r = self.client.get(f'/s/ssc/api/assignments/{exam.id}/export/')
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()['code'], 'nothing_to_export')

    def test_answer_html_conversion_and_comment_offsets(self):
        from reportlab.platypus import Paragraph, Table
        html = ('<div>One <b>two</b> three&nbsp;four</div><ol><li>a</li><li>b</li></ol>'
                '<table><tr><td>x</td><td>y</td></tr></table><div><br></div><div style="text-align:center">'
                '<span style="font-size: xx-large;">Big</span> <script>bad()</script>z</div>')
        text = pdf_export.plain_text(html)
        self.assertTrue(text.startswith('One two three\xa0four'))
        start = text.index('three')
        pdf_export._register_fonts()
        flow, listed = pdf_export.answer_flowables(html, pdf_export._styles(), 400, [{'start': start, 'end': start + 5, 'quote': 'three', 'comment': 'c'},
                                                         {'start': 0, 'end': 3, 'quote': 'gone', 'comment': 'lost'}])
        self.assertEqual([(n, found) for n, _, found in listed], [(1, False), (2, True)])
        paras = [f for f in flow if isinstance(f, Paragraph)]
        self.assertTrue(any(isinstance(f, Table) for f in flow))
        first = paras[0].text
        self.assertIn('three', first)
        joined = ' '.join(p.text for p in paras)
        self.assertNotIn('bad()', joined)
        self.assertIn('Big', joined)


@override_settings(MEDIA_ROOT=MEDIA)
class AccessibilityAndOfflineTests(TestCase):
    """Read-aloud setting, the pupil Accessibility menu, offline-backup support, character count."""

    def setUp(self):
        self.school = School.objects.create(name='SSC', slug='ssc')
        self.t = User.objects.create_user('t', 't@x.edu', 'x', school=self.school, role='teacher', first_name='Tess')
        self.p = User.objects.create_user('p', 'p@x.edu', 'x', school=self.school, first_name='Pia')
        subj = Subject.objects.create(school=self.school, name='Chinese')
        subj.teachers.add(self.t)
        subj.pupils.add(self.p)

    def create(self, **extra):
        data = {'title': 'Reading', 'items_meta': json.dumps([{'type': 'long_answer', 'label': 'Q1', 'prompt': '写一段话。'}]),
                'recipients': json.dumps([{'name': 'Pia', 'email': 'p@x.edu'}])}
        data.update(extra)
        r = self.client.post('/s/ssc/api/assignments/create/', data)
        self.assertEqual(r.status_code, 201, r.content)
        return Assignment.objects.get(id=r.json()['id'])

    def test_read_aloud_is_on_unless_the_teacher_turns_it_off(self):
        self.client.force_login(self.t)
        self.assertTrue(self.create().allow_text_to_speech)
        exam = self.create(allow_text_to_speech='false')
        self.assertFalse(exam.allow_text_to_speech)
        base = {'title': 'Reading', 'deliveryMode': 'browser'}
        r = self.client.post(f'/s/ssc/api/assignments/{exam.id}/update/', json.dumps(dict(base, allowTextToSpeech=True)),
                             content_type='application/json')
        self.assertTrue(r.json()['allowTextToSpeech'])
        self.client.post(f'/s/ssc/api/assignments/{exam.id}/update/', json.dumps(base), content_type='application/json')
        exam.refresh_from_db()
        self.assertTrue(exam.allow_text_to_speech)                    # left out = unchanged

    def test_pupils_get_the_accessibility_menu_teachers_do_not(self):
        self.client.force_login(self.p)
        html = self.client.get('/s/ssc/').content.decode()
        self.assertIn('<div style="position:relative;" id="a11yWrap">', html)
        for needle in ('id="a11yPanel"', 'data-a11y-key="font" data-a11y-value="dyslexic"', 'data-a11y-key="theme" data-a11y-value="night"',
                       'data-a11y-key="overlay"', 'id="readAloudBtn"', 'function startReadAloud', 'id="offlineBanner"',
                       'function restoreOfflineBackup', 'function countText', '/static/digitalexams/a11y/opendyslexic-latin-400-normal.woff2',
                       'id="a11yOverlay"'):
            self.assertIn(needle, html)
        self.assertNotIn('(?<', html)                                 # no look-behind regex (older iPad Safari)
        self.client.force_login(self.t)
        html = self.client.get('/s/ssc/').content.decode()
        self.assertIn('class="hidden" id="a11yWrap"', html)
        self.assertIn('id="aAllowTextToSpeech" checked', html)

    def test_font_files_are_shipped(self):
        from django.contrib.staticfiles import finders
        for name in ('opendyslexic-latin-400-normal.woff2', 'opendyslexic-latin-700-normal.woff2',
                     'atkinson-hyperlegible-latin-400-normal.woff2', 'atkinson-hyperlegible-latin-ext-700-italic.woff2'):
            self.assertTrue(finders.find('digitalexams/a11y/' + name), name)

    def test_updated_at_is_sent_and_a_play_does_not_change_it(self):
        self.client.force_login(self.t)
        exam = self.create()
        item = AssignmentItem.objects.create(assignment=exam, item_type='audio', label='A', order=5, points=0, audio_max_plays=2)
        item.audio.save('a.mp3', ContentFile(MP3_BYTES))
        self.client.force_login(self.p)
        rec = exam.recipients.get()
        self.client.post(f'/s/ssc/api/submissions/{rec.id}/start/', '{}', content_type='application/json')
        saved = self.client.post(f'/s/ssc/api/submissions/{rec.id}/save/',
                                 json.dumps({'annotations': [], 'outlineHtml': '', 'responses': {'1': '你好'}}),
                                 content_type='application/json').json()['updatedAt']
        mine = self.client.get('/s/ssc/api/assignments/').json()['assignments'][0]['recipients'][0]
        self.assertEqual(mine['updatedAt'], saved)
        self.client.post(f'/s/ssc/api/submissions/{rec.id}/audio/{item.id}/play/')
        mine = self.client.get('/s/ssc/api/assignments/').json()['assignments'][0]['recipients'][0]
        self.assertEqual(mine['updatedAt'], saved)                     # still "when the work last reached the server"
        self.assertEqual(mine['responses'], {'1': '你好'})
