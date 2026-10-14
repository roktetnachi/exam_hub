"""Export submitted work as PDF — one pupil, or a whole exam (a ZIP of PDFs or one combined PDF).

Each pupil's PDF has a cover block (exam, pupil, times, total), then every item in order: the
question, the pupil's answer, the teacher's score, feedback and numbered comments. A PDF item is
followed by the pupil's own copy of the paper — the original pages with their highlights and notes
and the teacher's comment marks drawn on top. Every page gets a footer (pupil · exam · page x of y).

Built with ReportLab (the text pages) and pypdf (the exam's own PDF pages + overlays), both pure
Python, so nothing extra is needed on Railway. Text uses the bundled DejaVu Sans (fonts/), which
covers Latin, Greek, Cyrillic and maths symbols; Chinese and Japanese text switches to
fonts/NotoSansSC-Common.ttf — Noto Sans CJK SC cut down to the ~9,500 common characters (GB2312
simplified, Big5 level-1 traditional, kana, CJK punctuation) and converted to TrueType so ReportLab
can embed it. Only the characters actually used are embedded, so PDFs stay small. A rare character
outside that set shows as a box.

Coordinates: pupils' annotations and the teacher's PDF marks are stored in the pupil viewer's
space — PDF.js at scale ANNOTATION_SCALE (1.2), origin top-left — so they're divided by 1.2 and
flipped to PDF space here.
"""
import datetime
import io
import os
import re
import zipfile
from html.parser import HTMLParser
from xml.sax.saxutils import escape

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET

from .models import Assignment, AssignmentItem, Recipient, Submission

ANNOTATION_SCALE = 1.2
FONT = 'ExamSans'
CJK_FONT = 'ExamHubCJK'
RED = '#d9231a'
MUTED = '#6b7280'
HIGHLIGHT_COLOURS = {'y': '#ffe873', 'g': '#8bdc7a', 'c': '#7fe0f0', 'm': '#f56fd0'}
HIGHLIGHT_NAMES = {'y': 'yellow', 'g': 'green', 'c': 'blue', 'm': 'pink'}
COMMENT_TINT = '#fbd5d2'
ITEM_TYPE_NAMES = {
    'pdf': 'PDF text', 'image': 'Image', 'video': 'Video', 'audio': 'Audio', 'long_answer': 'Long answer',
    'mcq_single': 'Multiple choice', 'mcq_multiple': 'Multiple choice (several answers)',
    'true_false': 'True / False', 'fill_text': 'Fill in the blank', 'table': 'Table',
}
MEDIA_ONLY = ('image', 'video', 'audio')

_fonts_ready = False


def _register_fonts():
    global _fonts_ready
    if _fonts_ready:
        return
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    folder = os.path.join(os.path.dirname(__file__), 'fonts')
    faces = {(0, 0): ('', 'DejaVuSans.ttf'), (1, 0): ('-Bold', 'DejaVuSans-Bold.ttf'),
             (0, 1): ('-Italic', 'DejaVuSans-Oblique.ttf'), (1, 1): ('-BoldItalic', 'DejaVuSans-BoldOblique.ttf')}
    for (bold, italic), (suffix, filename) in faces.items():
        pdfmetrics.registerFont(TTFont(FONT + suffix, os.path.join(folder, filename)))
        addMapping(FONT, bold, italic, FONT + suffix)
    pdfmetrics.registerFont(TTFont(CJK_FONT, os.path.join(folder, 'NotoSansSC-Common.ttf')))
    for bold in (0, 1):     # one weight only — bold / italic Chinese uses the same face
        for italic in (0, 1):
            addMapping(CJK_FONT, bold, italic, CJK_FONT)
    _fonts_ready = True


# Chinese / Japanese / Korean characters DejaVu Sans doesn't have.
_CJK = re.compile('[⺀-⿿　-〿぀-ヿ㄀-ㄯ㄰-㆏ㆠ-ㇿ'
                  '㈀-㋿㐀-䶿一-鿿가-힯豈-﫿＀-￯]+')
# Control characters ReportLab can't put in a PDF.
_CONTROL = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def rl(text):
    """Plain text -> ReportLab paragraph markup (escaped, with CJK runs in the CJK font)."""
    text = _CONTROL.sub('', str(text or ''))
    out, last = [], 0
    for m in _CJK.finditer(text):
        out.append(escape(text[last:m.start()]))
        out.append(f'<font name="{CJK_FONT}">{escape(m.group())}</font>')
        last = m.end()
    out.append(escape(text[last:]))
    return ''.join(out)


def rl_lines(text):
    """Like rl() but keeps the line breaks (prompts and feedback are written as plain text)."""
    return '<br/>'.join(rl(line) for line in str(text or '').replace('\r\n', '\n').split('\n'))


def js_round(x):
    """Math.round from the browser (halves go up) — Python's round() would break fingerprints."""
    import math
    return math.floor(float(x or 0) + 0.5)


# ---------------------------------------------------------------------------------------------
# Rich-text answers (the pupil's HTML) -> ReportLab flowables, with the teacher's comment marks
# ---------------------------------------------------------------------------------------------
class _PlainText(HTMLParser):
    """The answer's text exactly as the browser's marking code counts it: every text node
    joined, nothing added for line breaks (see tmPlainText in examhub.html)."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(html):
    p = _PlainText()
    p.feed(html or '')
    p.close()
    return ''.join(p.parts)


def ordered_marks(marks):
    return sorted([m for m in (marks or []) if isinstance(m, dict)],
                  key=lambda m: (m.get('start') or 0, m.get('end') or 0))


def resolve_mark(text, m):
    """Same rule as tmResolve: the saved offsets if the words still match, otherwise wherever the
    quoted words are found; None when they're gone."""
    start, end, quote = m.get('start') or 0, m.get('end') or 0, m.get('quote') or ''
    if not quote or text[start:end] == quote:
        return (start, end) if end <= len(text) else None
    i = text.find(quote)
    return (i, i + len(quote)) if i >= 0 else None


_INLINE_SIMPLE = {'b': ('<b>', '</b>'), 'strong': ('<b>', '</b>'), 'i': ('<i>', '</i>'), 'em': ('<i>', '</i>'),
                  'u': ('<u>', '</u>'), 'ins': ('<u>', '</u>'), 's': ('<strike>', '</strike>'),
                  'strike': ('<strike>', '</strike>'), 'del': ('<strike>', '</strike>'),
                  'sup': ('<super>', '</super>'), 'sub': ('<sub>', '</sub>')}
_BLOCKS = {'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'li', 'ul', 'ol', 'section',
           'article', 'header', 'footer', 'center', 'address', 'dl', 'dt', 'dd', 'figure', 'hr'}
_SKIP = {'script', 'style', 'head', 'title', 'template'}
_VOID = {'br', 'img', 'hr', 'input', 'meta', 'link', 'wbr', 'col', 'area', 'source'}
_FONT_SIZE_PX = {'1': 10, '2': 13, '3': 16, '4': 18, '5': 24, '6': 32, '7': 48}
_KEYWORD_PX = {'xx-small': 9, 'x-small': 10, 'small': 13, 'medium': 16, 'large': 18, 'x-large': 24,
               'xx-large': 32, 'xxx-large': 48}
_COLOR_RE = re.compile(r'^(#[0-9a-fA-F]{3,8}|rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)|[a-zA-Z]+)$')


def _style_map(style):
    out = {}
    for part in (style or '').split(';'):
        if ':' in part:
            k, v = part.split(':', 1)
            out[k.strip().lower()] = v.strip()
    return out


def _colour(value):
    value = (value or '').strip()
    if not _COLOR_RE.match(value) or value.lower() in ('transparent', 'inherit', 'initial', 'currentcolor'):
        return None
    m = re.match(r'rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', value)
    if m:
        return '#%02x%02x%02x' % tuple(min(255, int(g)) for g in m.groups())
    try:
        from reportlab.lib import colors
        colors.toColor(value)
    except Exception:
        return None
    return value


class AnswerRenderer(HTMLParser):
    """Turns one answer's HTML into Paragraphs / list items / Tables. `marks` are the teacher's
    comment marks for this answer: [(start, end, number)] in plain_text() offsets — the marked
    words get a light-red tint and a red superscript number, like the Grade tab."""

    def __init__(self, styles, width, marks=()):
        super().__init__(convert_charrefs=True)
        self.styles, self.width = styles, width
        self.marks = sorted(marks)
        self.numbered = set()
        self.flow = []
        self.buf, self.has_text, self.has_break = [], False, False
        self.inline = []          # [(tag, opener, closer)]
        self.aligns = []          # [(tag, alignment)]
        self.lists = []           # [{'tag': 'ul'|'ol', 'n': int}]
        self.bullet = None
        self.pos = 0
        self.skip = 0
        self.table = None         # {'rows': [[cell markup]], 'row': [...] | None, 'cell': [...] | None, 'depth': n}

    # ---- helpers ----
    def _openers(self):
        return ''.join(o for _, o, _ in self.inline)

    def _closers(self):
        return ''.join(c for _, _, c in reversed(self.inline))

    def _alignment(self):
        from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
        value = self.aligns[-1][1] if self.aligns else 'left'
        return {'center': TA_CENTER, 'right': TA_RIGHT, 'justify': TA_JUSTIFY}.get(value, TA_LEFT)

    def _emit(self, markup):
        if self.table is not None and self.table['cell'] is not None:
            self.table['cell'].append(markup)
        elif self.table is not None:
            return    # stray text between cells, as browsers do with it
        else:
            self.buf.append(markup)

    def flush(self):
        if self.table is not None:
            return
        markup = ''.join(self.buf)
        if self.has_text:
            markup += self._closers()
            self._paragraph(markup)
        elif self.has_break:
            from reportlab.platypus import Spacer
            self.flow.append(Spacer(1, self.styles['answer'].leading * 0.6))
        self.buf, self.has_text, self.has_break = [self._openers()], False, False

    def _paragraph(self, markup):
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import Paragraph
        depth = len(self.lists)
        style = ParagraphStyle('a', parent=self.styles['answer'], alignment=self._alignment(),
                               leftIndent=16 * depth, bulletIndent=16 * depth - 11 if depth else 0)
        bullet = self.bullet
        self.bullet = None
        try:
            para = Paragraph(markup, style, bulletText=bullet)
        except Exception:
            para = Paragraph(rl(re.sub(r'<[^>]+>', '', markup)), style, bulletText=bullet)
        self.flow.append(para)

    def _inline_for(self, tag, attrs):
        opener = closer = ''
        if tag in _INLINE_SIMPLE:
            opener, closer = _INLINE_SIMPLE[tag]
        font_bits = []
        if tag == 'font':
            size = _FONT_SIZE_PX.get((attrs.get('size') or '').strip())
            if size:
                font_bits.append(f'size="{size * 10 / 16:.1f}"')
            colour = _colour(attrs.get('color'))
            if colour:
                font_bits.append(f'color="{colour}"')
        css = _style_map(attrs.get('style'))
        if css.get('font-weight') in ('bold', 'bolder', '600', '700', '800', '900'):
            opener, closer = opener + '<b>', '</b>' + closer
        if css.get('font-style') == 'italic':
            opener, closer = opener + '<i>', '</i>' + closer
        if 'underline' in css.get('text-decoration', '') or 'underline' in css.get('text-decoration-line', ''):
            opener, closer = opener + '<u>', '</u>' + closer
        if 'line-through' in css.get('text-decoration', ''):
            opener, closer = opener + '<strike>', '</strike>' + closer
        size = css.get('font-size', '')
        px = None
        if size in _KEYWORD_PX:
            px = _KEYWORD_PX[size]
        else:
            m = re.match(r'^([\d.]+)(px|pt|em|rem)$', size)
            if m:
                n = float(m.group(1))
                px = {'px': n, 'pt': n * 4 / 3, 'em': n * 16, 'rem': n * 16}[m.group(2)]
        if px:
            font_bits.append(f'size="{max(5.0, min(40.0, px * 10 / 16)):.1f}"')
        colour = _colour(css.get('color'))
        if colour and tag != 'font':
            font_bits.append(f'color="{colour}"')
        back = _colour(css.get('background-color'))
        if back:
            font_bits.append(f'backColor="{back}"')
        if font_bits:
            opener, closer = opener + f'<font {" ".join(font_bits)}>', '</font>' + closer
        return opener, closer

    # ---- parser callbacks ----
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in _SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == 'table':
            if self.table is not None:
                self.table['depth'] += 1      # nested table: flattened into the outer cell
                return
            self.flush()
            self.table = {'rows': [], 'row': None, 'cell': None, 'depth': 0}
            return
        if self.table is not None:
            if self.table['depth'] == 0 and tag == 'tr':
                self.table['row'] = []
                self.table['rows'].append(self.table['row'])
                return
            if self.table['depth'] == 0 and tag in ('td', 'th'):
                if self.table['row'] is None:
                    self.table['row'] = []
                    self.table['rows'].append(self.table['row'])
                self.table['cell'] = ['<b>' if tag == 'th' else '']
                self.table['row'].append(self.table['cell'])
                self.table['cell_tag'] = tag
                return
            if tag == 'br' or tag in _BLOCKS:
                if self.table['cell'] is not None and ''.join(self.table['cell']).strip():
                    self.table['cell'].append('<br/>')
                return
        if tag == 'br':
            self._emit('<br/>')
            self.has_break = True
            return
        if tag == 'img':
            self._emit(f'<i>{rl("[picture]")}</i>')
            self.has_text = True
            return
        if tag in ('ul', 'ol'):
            self.flush()
            self.lists.append({'tag': tag, 'n': 0})
            return
        if tag in _BLOCKS:
            self.flush()
            align = (attrs.get('align') or _style_map(attrs.get('style')).get('text-align') or '').lower()
            if tag == 'center':
                align = 'center'
            self.aligns.append((tag, align or (self.aligns[-1][1] if self.aligns else 'left')))
            if tag == 'li':
                lst = self.lists[-1] if self.lists else {'tag': 'ul', 'n': 0}
                lst['n'] += 1
                self.bullet = f'{lst["n"]}.' if lst['tag'] == 'ol' else '•'
            if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
                opener, closer = '<b>', '</b>'
                self.inline.append((tag, opener, closer))
                self.buf.append(opener)
            return
        if tag in _VOID:
            return
        opener, closer = self._inline_for(tag, attrs)
        self.inline.append((tag, opener, closer))
        self._emit(opener)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == 'table' and self.table is not None:
            if self.table['depth']:
                self.table['depth'] -= 1
                return
            self._finish_table()
            return
        if self.table is not None and self.table['depth'] == 0:
            if tag in ('td', 'th') and self.table['cell'] is not None:
                if self.table.get('cell_tag') == 'th':
                    self.table['cell'].append('</b>')
                self.table['cell'] = None
                return
            if tag == 'tr':
                self.table['row'] = None
                return
        if tag in ('ul', 'ol'):
            self.flush()
            if self.lists:
                self.lists.pop()
            return
        if tag in _BLOCKS:
            if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
                self._pop_inline(tag)
            self.flush()
            for i in range(len(self.aligns) - 1, -1, -1):
                if self.aligns[i][0] == tag:
                    del self.aligns[i:]
                    break
            return
        self._pop_inline(tag)

    def _pop_inline(self, tag):
        for i in range(len(self.inline) - 1, -1, -1):
            if self.inline[i][0] == tag:
                closing = ''.join(c for _, _, c in reversed(self.inline[i:]))
                reopen = ''.join(o for _, o, _ in self.inline[i + 1:])
                del self.inline[i]
                self._emit(closing + reopen)
                return

    def handle_data(self, data):
        start = self.pos
        self.pos += len(data)
        if self.skip or not data:
            return
        end = self.pos
        cuts = {start, end}
        for a, b, _ in self.marks:
            if start < a < end:
                cuts.add(a)
            if start < b < end:
                cuts.add(b)
        cuts = sorted(cuts)
        pieces = []
        for a, b in zip(cuts, cuts[1:]):
            seg = data[a - start:b - start]
            markup = rl(seg)
            if seg.strip() and any(ma <= a and b <= mb for ma, mb, _ in self.marks):
                markup = f'<font backColor="{COMMENT_TINT}">{markup}</font>'
            pieces.append(markup)
            for ma, mb, num in self.marks:
                if mb == b and num not in self.numbered:
                    self.numbered.add(num)
                    pieces.append(f'<super><font color="{RED}" size="7"><b>{num}</b></font></super>')
        markup = ''.join(pieces)
        if self.table is None and not self.has_text and not data.strip():
            # Whitespace between blocks — keep it out of the paragraph start.
            self._emit(markup)
            return
        self._emit(markup)
        if data.strip():
            self.has_text = True

    def _finish_table(self):
        from reportlab.lib import colors
        from reportlab.platypus import Paragraph, Table, TableStyle
        rows = [r for r in self.table['rows'] if r]
        self.table = None
        self.buf = [self._openers()]
        if not rows:
            return
        cols = max(len(r) for r in rows)
        style = self.styles['cell']
        data = []
        for r in rows:
            line = []
            for cell in r + [['']] * (cols - len(r)):
                markup = ''.join(cell).strip()
                try:
                    line.append(Paragraph(markup or '', style))
                except Exception:
                    line.append(Paragraph(rl(re.sub(r'<[^>]+>', '', markup)), style))
            data.append(line)
        table = Table(data, colWidths=[self.width / cols] * cols, hAlign='LEFT')
        table.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#b8c0cc')),
                                   ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                   ('LEFTPADDING', (0, 0), (-1, -1), 4), ('RIGHTPADDING', (0, 0), (-1, -1), 4)]))
        self.flow.append(table)

    def result(self):
        if self.table is not None:
            self._finish_table()
        self.flush()
        return self.flow


def answer_flowables(html, styles, width, marks=None):
    """The pupil's HTML answer as flowables, plus [(number, mark, found)] for the comment list."""
    text = plain_text(html)
    listed, ranges = [], []
    for num, m in enumerate(ordered_marks(marks), start=1):
        r = resolve_mark(text, m)
        listed.append((num, m, r is not None))
        if r:
            ranges.append((r[0], r[1], num))
    renderer = AnswerRenderer(styles, width, ranges)
    renderer.feed(html or '')
    renderer.close()
    return renderer.result(), listed


# ---------------------------------------------------------------------------------------------
# Drawing on the exam's own PDF pages
# ---------------------------------------------------------------------------------------------
def _to_pdf_space(box, x, y):
    """Viewer coordinates (scale 1.2, origin top-left of the crop box) -> PDF user space."""
    left, top = float(box.left), float(box.top)
    return left + x / ANNOTATION_SCALE, top - y / ANNOTATION_SCALE


def _annotation_fingerprint(an):
    rects = an.get('rects') or []
    r = rects[0] if rects else {'x': an.get('x'), 'y': an.get('y')}
    return '|'.join(str(v) for v in (an.get('textId'), an.get('page'), an.get('type'),
                                     js_round(r.get('x') or 0), js_round(r.get('y') or 0)))


def pdf_item_notes(sub, item, include_marks):
    """What's written on a PDF item, numbered the way it's drawn on the pages:
    pupil notes N1, N2… (only annotations that have words), teacher marks 1, 2… (UI numbering)."""
    anns = [(idx, an) for idx, an in enumerate(sub.annotations or [])
            if isinstance(an, dict) and str(an.get('textId')) == str(item.id)]
    ann_comments = ((sub.teacher_marks or {}).get('annComments') or {}) if include_marks else {}
    pupil_notes = []
    for idx, an in sorted(anns, key=lambda p: (p[1].get('page') or 0, _ann_top(p[1]))):
        text = (an.get('note') if an.get('type') == 'highlight' else an.get('text')) or ''
        c = ann_comments.get(str(idx))
        teacher = c.get('comment') if (isinstance(c, dict) and c.get('fp') == _annotation_fingerprint(an)) else ''
        if text.strip() or (teacher or '').strip():
            pupil_notes.append({'idx': idx, 'an': an, 'text': text.strip(), 'teacher': (teacher or '').strip(),
                                'label': f'N{len(pupil_notes) + 1}'})
    marks = []
    if include_marks:
        for num, m in enumerate(((sub.teacher_marks or {}).get('pdf') or {}).get(str(item.id)) or [], start=1):
            if isinstance(m, dict):
                marks.append({'num': num, 'm': m})
    return anns, pupil_notes, marks


def _ann_top(an):
    rects = an.get('rects') or []
    return (rects[0].get('y') if rects else an.get('y')) or 0


def _draw_badge(c, x, y, label, fill, text_colour='#ffffff', stroke=None):
    from reportlab.lib import colors
    w = max(11, 4.6 * len(label) + 5)
    c.setFillColor(colors.HexColor(fill))
    c.setStrokeColor(colors.HexColor(stroke or '#ffffff'))
    c.setLineWidth(0.8)
    c.roundRect(x, y, w, 11, 5.5, stroke=1, fill=1)
    c.setFillColor(colors.HexColor(text_colour))
    c.setFont(FONT + '-Bold', 7)
    c.drawCentredString(x + w / 2, y + 3, label)


def overlay_for_page(page, page_no, anns, pupil_notes, marks):
    """A one-page PDF (same size) with this page's highlights, notes and teacher marks, or None."""
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas
    box = page.cropbox
    here = [an for _, an in anns if (an.get('page') or 1) == page_no]
    here_marks = [mk for mk in marks if (mk['m'].get('page') or 1) == page_no]
    if not here and not here_marks:
        return None
    note_label = {id(n['an']): n['label'] for n in pupil_notes}
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(float(page.mediabox.right), float(page.mediabox.top)))
    s = ANNOTATION_SCALE
    for an in here:
        if an.get('type') == 'highlight':
            colour = HIGHLIGHT_COLOURS.get(an.get('color'), HIGHLIGHT_COLOURS['y'])
            c.saveState()
            c.setFillColor(colors.HexColor(colour), alpha=0.45)
            last = None
            for r in an.get('rects') or []:
                x, y_top = _to_pdf_space(box, r.get('x') or 0, r.get('y') or 0)
                w, h = (r.get('w') or 0) / s, (r.get('h') or 0) / s
                c.rect(x, y_top - h, w, h, stroke=0, fill=1)
                last = (x + w, y_top, h)
            c.restoreState()
            label = note_label.get(id(an))
            if label and last:   # just after the end of the highlight, on the same line
                _draw_badge(c, last[0] + 2, last[1] - last[2] / 2 - 5.5, label, '#ffffff', '#1f2937', '#1f2937')
        elif an.get('type') == 'image-note':
            if an.get('scope') == 'full':
                x, y_top = _to_pdf_space(box, an.get('imgX') or 0, an.get('imgY') or 0)
                w, h = (an.get('imgW') or 0) / s, (an.get('imgH') or 0) / s
                c.saveState()
                c.setFillColor(colors.HexColor(HIGHLIGHT_COLOURS.get(an.get('color'), '#ffe873')), alpha=0.35)
                c.rect(x, y_top - h, w, h, stroke=0, fill=1)
                c.restoreState()
            x, y_top = _to_pdf_space(box, an.get('x') or 0, an.get('y') or 0)
            c.setFillColor(colors.HexColor('#fff3a3'))
            c.setStrokeColor(colors.HexColor('#c9a400'))
            c.setLineWidth(0.8)
            c.rect(x, y_top - 14, 14, 14, stroke=1, fill=1)
            label = note_label.get(id(an))
            if label:
                _draw_badge(c, x + 10, y_top - 4, label, '#ffffff', '#1f2937', '#1f2937')
    for mk in here_marks:
        m = mk['m']
        x, y_top = _to_pdf_space(box, m.get('x') or 0, m.get('y') or 0)
        w, h = (m.get('w') or 0) / s, (m.get('h') or 0) / s
        shape = m.get('shape')
        c.setStrokeColor(colors.HexColor(RED))
        c.setLineWidth(1.6)
        if shape == 'highlight':
            c.saveState()
            c.setFillColor(colors.HexColor('#ffd600'), alpha=0.38)
            c.rect(x, y_top - max(5, h), max(5, w), max(5, h), stroke=0, fill=1)
            c.restoreState()
        elif shape == 'box':
            c.roundRect(x, y_top - max(5, h), max(5, w), max(5, h), 2, stroke=1, fill=0)
        elif shape == 'circle':
            c.ellipse(x, y_top - max(10, h), x + max(10, w), y_top, stroke=1, fill=0)
        elif shape == 'underline':
            c.line(x, y_top - h, x + max(8, w), y_top - h)
        if shape == 'pin':
            _draw_badge(c, x - 9, y_top - 2, str(mk['num']), RED)
        else:
            _draw_badge(c, x + max(8, w) - 3, y_top + 2, str(mk['num']), RED)
    c.showPage()
    c.save()
    from pypdf import PdfReader
    return PdfReader(io.BytesIO(buf.getvalue())).pages[0]


# ---------------------------------------------------------------------------------------------
# Building one pupil's PDF
# ---------------------------------------------------------------------------------------------
def _styles():
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    base = ParagraphStyle('base', fontName=FONT, fontSize=10, leading=14, textColor=colors.HexColor('#232733'))
    return {
        'base': base,
        'school': ParagraphStyle('school', parent=base, fontSize=9, textColor=colors.HexColor(MUTED)),
        'title': ParagraphStyle('title', parent=base, fontName=FONT + '-Bold', fontSize=17, leading=21, spaceAfter=4),
        'pupil': ParagraphStyle('pupil', parent=base, fontSize=12, leading=16, spaceAfter=8),
        'section': ParagraphStyle('section', parent=base, fontName=FONT + '-Bold', fontSize=12, leading=16,
                                  textColor=colors.HexColor('#28186f'), spaceBefore=10, spaceAfter=4),
        'item': ParagraphStyle('item', parent=base, fontName=FONT + '-Bold', fontSize=11, leading=15),
        'itemright': ParagraphStyle('itemright', parent=base, fontSize=9.5, alignment=2, textColor=colors.HexColor(MUTED)),
        'label': ParagraphStyle('label', parent=base, fontName=FONT + '-Bold', fontSize=8, leading=11,
                                textColor=colors.HexColor(MUTED), spaceBefore=7, spaceAfter=2),
        'answer': ParagraphStyle('answer', parent=base, fontSize=10, leading=14.5, spaceAfter=3),
        'cell': ParagraphStyle('cell', parent=base, fontSize=9.5, leading=12.5),
        'muted': ParagraphStyle('muted', parent=base, fontSize=9.5, textColor=colors.HexColor(MUTED)),
        'comment': ParagraphStyle('comment', parent=base, fontSize=9.5, leading=13, leftIndent=14, firstLineIndent=-14,
                                  spaceAfter=2),
        'feedback': ParagraphStyle('feedback', parent=base, fontSize=10, leading=14, backColor=colors.HexColor('#eef6ee'),
                                   borderPadding=(5, 6, 5, 6), spaceBefore=4, spaceAfter=6),
    }


def _fmt_time(dt, tz):
    if not dt:
        return '—'
    return dt.astimezone(tz).strftime('%d %b %Y, %I:%M %p').replace(' 0', ' ')


def _fmt_score(x):
    if x is None:
        return '—'
    return f'{x:g}' if isinstance(x, (int, float)) else str(x)


class _Context:
    """What stays the same for every pupil in one export (cached files, styles, settings)."""
    def __init__(self, assignment, include_marks, exported_by, tz, include_pages=True):
        _register_fonts()
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        self.assignment = assignment
        self.items = list(assignment.items.all())
        self.include_marks = include_marks
        self.include_pages = include_pages
        self.exported_by = exported_by
        self.tz = tz
        self.styles = _styles()
        self.pagesize = A4
        self.margin = 18 * mm
        self.width = A4[0] - 2 * self.margin
        self._files = {}

    def file_bytes(self, field):
        key = field.name
        if key not in self._files:
            try:
                with field.open('rb') as fh:
                    self._files[key] = fh.read()
            except (FileNotFoundError, ValueError, OSError):
                self._files[key] = None
        return self._files[key]


def _item_header(ctx, n, item, score):
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle
    st = ctx.styles
    right = ITEM_TYPE_NAMES.get(item.item_type, item.item_type)
    if ctx.include_marks and item.item_type not in MEDIA_ONLY:
        if score and score.get('score') is not None:
            right += f' · <b>{_fmt_score(score.get("score"))} / {_fmt_score(item.points)}</b>'
        else:
            right += f' · {_fmt_score(item.points)} pt{"" if item.points == 1 else "s"}'
    t = Table([[Paragraph(f'{n}. {rl(item.label)}', st['item']), Paragraph(right, st['itemright'])]],
              colWidths=[ctx.width * 0.62, ctx.width * 0.38])
    t.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#edf1f9')),
                           ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                           ('LEFTPADDING', (0, 0), (-1, -1), 7), ('RIGHTPADDING', (0, 0), (-1, -1), 7),
                           ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5)]))
    return t


def _comment_list(ctx, listed, heading):
    from reportlab.platypus import Paragraph
    out = []
    if listed:
        out.append(Paragraph(heading, ctx.styles['label']))
    for num, m, found in listed:
        quote = (m.get('quote') or '').strip()
        q = f'“{rl(quote[:160])}{"…" if len(quote) > 160 else ""}” — ' if quote else ''
        lost = '' if found else f' <font color="{MUTED}">(these words are no longer in the answer)</font>'
        out.append(Paragraph(f'<font color="{RED}"><b>{num}</b></font>  {q}{rl_lines(m.get("comment") or "")}{lost}',
                             ctx.styles['comment']))
    return out


def _mcq_flowables(ctx, item, value):
    from reportlab.platypus import Paragraph
    st = ctx.styles
    multiple = item.item_type == AssignmentItem.ItemType.MCQ_MULTIPLE
    value = value if isinstance(value, dict) else {}
    chosen = set(value.get('optionIds') or []) if multiple else {value.get('optionId')}
    ca = item.correct_answer or {}
    correct = set(ca.get('optionIds') or []) if multiple else {ca.get('optionId')}
    out = []
    for opt in item.options or []:
        if not isinstance(opt, dict):
            continue
        picked = opt.get('id') in chosen
        box = ('☒' if picked else '☐') if multiple else ('◉' if picked else '○')
        tail = ''
        if ctx.include_marks and opt.get('id') in correct and opt.get('id') is not None:
            tail = f'  <font color="#16a34a"><b>✓ correct answer</b></font>'
        text = rl(opt.get('text'))
        out.append(Paragraph(f'{box}  {"<b>" + text + "</b>" if picked else text}{tail}', st['answer']))
    if not chosen - {None}:
        out.append(Paragraph('<i>No answer chosen.</i>', st['muted']))
    return out


def _table_answer(ctx, item, value):
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle
    dims = item.options if isinstance(item.options, dict) else {}
    rows_n, cols_n = int(dims.get('rows') or 3), int(dims.get('cols') or 3)
    cells = (value or {}).get('cells') if isinstance(value, dict) else None
    cells = cells if isinstance(cells, list) else []
    data = []
    for r in range(rows_n):
        row = cells[r] if r < len(cells) and isinstance(cells[r], list) else []
        data.append([Paragraph(rl(row[c] if c < len(row) else ''), ctx.styles['cell']) for c in range(cols_n)])
    t = Table(data, colWidths=[ctx.width / max(1, cols_n)] * cols_n, hAlign='LEFT')
    t.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#b8c0cc')),
                           ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    return t


def _image_flowable(ctx, item):
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image, Paragraph
    data = ctx.file_bytes(item.image) if item.image else None
    if not data:
        return Paragraph("<i>The image file isn't available on the server.</i>", ctx.styles['muted'])
    try:
        w, h = ImageReader(io.BytesIO(data)).getSize()
        scale = min(ctx.width / w, (150 * mm) / h, 1.0 if w > 200 else ctx.width / w)
        return Image(io.BytesIO(data), width=w * scale, height=h * scale, hAlign='LEFT')
    except Exception:
        return Paragraph("<i>This image couldn't be added to the PDF (unsupported format).</i>", ctx.styles['muted'])


def _file_name(field):
    return (field.name or '').rsplit('/', 1)[-1] if field else ''


def build_submission_pdf(ctx, recipient):
    """One pupil's export as PDF bytes."""
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import CondPageBreak, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    st = ctx.styles
    a = ctx.assignment
    try:
        sub = recipient.submission
    except Submission.DoesNotExist:
        sub = Submission(recipient=recipient)
    responses = sub.responses or {}
    scores = (sub.item_scores or {}) if ctx.include_marks else {}
    answer_marks = ((sub.teacher_marks or {}).get('answers') or {}) if ctx.include_marks else {}

    segments = []        # ('flow', [flowables]) | ('pages', item, anns, pupil_notes, marks)
    flow = []

    # ---- cover block ----
    school = a.school.name if a.school_id else ''
    if school:
        flow.append(Paragraph(rl(school), st['school']))
    flow.append(Paragraph(rl(a.title), st['title']))
    flow.append(Paragraph(f'<b>{rl(recipient.pupil_name)}</b> <font color="{MUTED}" size="10">{rl(recipient.pupil_email)}</font>',
                          st['pupil']))
    status = {'submitted': 'Handed in', 'draft': 'Started, not handed in', 'not_started': 'Not started'}.get(sub.status, sub.status)
    facts = [('Status', status), ('Started', _fmt_time(sub.started_at, ctx.tz)),
             ('Handed in', _fmt_time(sub.submitted_at, ctx.tz)), ('Teacher', a.teacher_name)]
    if ctx.include_marks:
        if sub.graded_at:
            pct = f' ({100 * sub.total_score / sub.max_score:.0f}%)' if sub.max_score else ''
            released = 'released to the pupil' if sub.published else 'not released to the pupil yet'
            facts.append(('Total', f'{_fmt_score(sub.total_score)} / {_fmt_score(sub.max_score)}{pct} — {released}'))
        else:
            facts.append(('Total', 'Not graded yet'))
    facts.append(('Exported', f'{_fmt_time(timezone.now(), ctx.tz)} by {ctx.exported_by}'))
    t = Table([[Paragraph(f'<font color="{MUTED}">{rl(k)}</font>', st['base']), Paragraph(rl(v), st['base'])]
               for k, v in facts], colWidths=[28 * mm, ctx.width - 28 * mm])
    t.setStyle(TableStyle([('LINEBELOW', (0, 0), (-1, -1), 0.4, colors.HexColor('#e3e8f2')),
                           ('LEFTPADDING', (0, 0), (-1, -1), 0), ('TOPPADDING', (0, 0), (-1, -1), 3),
                           ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]))
    flow.append(t)
    if a.instructions:
        flow.append(Paragraph('INSTRUCTIONS', st['label']))
        flow.append(Paragraph(rl_lines(a.instructions), st['base']))
    flow.append(Spacer(1, 10))

    # ---- items ----
    last_section = None
    for n, item in enumerate(ctx.items, start=1):
        key = str(item.id)
        section = (item.section or '').strip()
        block = []
        if section and section != last_section:
            block.append(Paragraph(rl(section), st['section']))
        last_section = section or last_section
        score = scores.get(key) if isinstance(scores.get(key), dict) else None
        block.append(_item_header(ctx, n, item, score))
        if item.prompt:
            block.append(Spacer(1, 4))
            block.append(Paragraph(rl_lines(item.prompt), st['base']))
        value = responses.get(key)
        t = item.item_type
        pages_after = None
        if t in (AssignmentItem.ItemType.PDF, AssignmentItem.ItemType.LONG_ANSWER):
            block.append(Paragraph('WRITTEN RESPONSE' if t == 'pdf' else "PUPIL'S ANSWER", st['label']))
            html = value if isinstance(value, str) else ''
            if plain_text(html).strip():
                answer, listed = answer_flowables(html, st, ctx.width, answer_marks.get(key))
                block.extend(answer)
                block.extend(_comment_list(ctx, listed, "TEACHER'S COMMENTS"))
            else:
                block.append(Paragraph('<i>No response written.</i>', st['muted']))
            if t == AssignmentItem.ItemType.PDF:
                anns, pupil_notes, marks = pdf_item_notes(sub, item, ctx.include_marks)
                if pupil_notes:
                    block.append(Paragraph("PUPIL'S NOTES ON THE PDF", st['label']))
                    for note in pupil_notes:
                        an = note['an']
                        kind = (f'Highlight ({HIGHLIGHT_NAMES.get(an.get("color"), "yellow")})'
                                if an.get('type') == 'highlight' else 'Sticky note')
                        line = f'<b>{note["label"]}</b>  Page {an.get("page") or 1} · {kind}'
                        if note['text']:
                            line += f': {rl_lines(note["text"])}'
                        if note['teacher']:
                            line += f'<br/><font color="{RED}">Teacher: {rl_lines(note["teacher"])}</font>'
                        block.append(Paragraph(line, st['comment']))
                if marks:
                    block.append(Paragraph("TEACHER'S COMMENTS ON THE PDF", st['label']))
                    for mk in marks:
                        m = mk['m']
                        what = {'pin': 'Comment', 'highlight': 'Highlight', 'underline': 'Underline',
                                'circle': 'Circle', 'box': 'Box'}.get(m.get('shape'), 'Mark')
                        text = rl_lines(m.get('comment') or '') or f'<font color="{MUTED}">(no comment)</font>'
                        block.append(Paragraph(f'<font color="{RED}"><b>{mk["num"]}</b></font>  Page {m.get("page") or 1} · '
                                               f'{what}: {text}', st['comment']))
                pdf_bytes = ctx.file_bytes(item.pdf) if (item.pdf and ctx.include_pages) else None
                if not ctx.include_pages:
                    block.append(Paragraph(f'<font color="{MUTED}">The PDF\'s pages were left out of this export.</font>',
                                           st['muted']))
                elif pdf_bytes:
                    block.append(Paragraph(f'<font color="{MUTED}">The pupil\'s copy of “{rl(_file_name(item.pdf))}”, '
                                           'with their highlights and notes'
                                           f'{" and your marks" if ctx.include_marks else ""}, follows.</font>',
                                           st['muted']))
                    pages_after = (item, anns, pupil_notes, marks)
                else:
                    block.append(Paragraph("<i>The PDF file isn't available on the server, so its pages "
                                           "couldn't be added.</i>", st['muted']))
        elif t in (AssignmentItem.ItemType.MCQ_SINGLE, AssignmentItem.ItemType.MCQ_MULTIPLE):
            block.append(Paragraph("PUPIL'S ANSWER", st['label']))
            block.extend(_mcq_flowables(ctx, item, value))
        elif t == AssignmentItem.ItemType.TRUE_FALSE:
            got = value.get('value') if isinstance(value, dict) else None
            block.append(Paragraph("PUPIL'S ANSWER", st['label']))
            block.append(Paragraph('<b>True</b>' if got is True else '<b>False</b>' if got is False
                                   else '<i>No answer chosen.</i>', st['answer']))
            ca = (item.correct_answer or {}).get('value')
            if ctx.include_marks and ca is not None:
                block.append(Paragraph(f'<font color="#16a34a">Correct answer: {"True" if ca else "False"}</font>', st['muted']))
        elif t == AssignmentItem.ItemType.FILL_TEXT:
            got = (value.get('text') if isinstance(value, dict) else '') or ''
            block.append(Paragraph("PUPIL'S ANSWER", st['label']))
            block.append(Paragraph(f'<b>{rl(got)}</b>' if got.strip() else '<i>No answer written.</i>', st['answer']))
            accepted = (item.correct_answer or {}).get('accepted') or []
            if ctx.include_marks and accepted:
                block.append(Paragraph(f'<font color="#16a34a">Accepted: {rl(", ".join(map(str, accepted)))}</font>', st['muted']))
        elif t == AssignmentItem.ItemType.TABLE:
            block.append(Paragraph("PUPIL'S ANSWER", st['label']))
            block.append(_table_answer(ctx, item, value))
        elif t == AssignmentItem.ItemType.IMAGE:
            block.append(Spacer(1, 4))
            block.append(_image_flowable(ctx, item))
        elif t == AssignmentItem.ItemType.VIDEO:
            block.append(Paragraph(f'<font color="{MUTED}">▶ Video “{rl(_file_name(item.video))}” — not included in the PDF.</font>',
                                   st['muted']))
        elif t == AssignmentItem.ItemType.AUDIO:
            plays = int((sub.audio_plays or {}).get(key, 0) or 0)
            limit = f' of {item.audio_max_plays}' if item.audio_max_plays else ''
            block.append(Paragraph(f'<font color="{MUTED}">♪ Audio “{rl(_file_name(item.audio))}” — not included in the PDF. '
                                   f'Played {plays}{limit} time{"" if plays == 1 and not limit else "s"}.</font>', st['muted']))
        if ctx.include_marks and score and (score.get('feedback') or '').strip():
            block.append(Paragraph('FEEDBACK', st['label']))
            block.append(Paragraph(rl_lines(score['feedback']), st['feedback']))
        block.append(Spacer(1, 12))
        flow.append(CondPageBreak(60))
        flow.append(KeepTogether(block[:3]) if len(block) > 3 else KeepTogether(block))
        flow.extend(block[3:] if len(block) > 3 else [])
        if pages_after:
            segments.append(('flow', flow))
            segments.append(('pages',) + pages_after)
            flow = []

    notepad = sub.outline_html or ''
    if plain_text(notepad).strip():
        flow.append(CondPageBreak(80))
        flow.append(Paragraph("Pupil's notepad", st['section']))
        flow.append(Paragraph('<font color="{0}">Rough notes the pupil kept during the exam (not marked).</font>'.format(MUTED),
                              st['muted']))
        flow.extend(answer_flowables(notepad, st, ctx.width)[0])
    if flow:
        segments.append(('flow', flow))

    # ---- assemble ----
    writer = PdfWriter()
    for seg in segments:
        if seg[0] == 'flow':
            buf = io.BytesIO()
            doc = SimpleDocTemplate(buf, pagesize=ctx.pagesize, leftMargin=ctx.margin, rightMargin=ctx.margin,
                                    topMargin=ctx.margin, bottomMargin=ctx.margin + 6,
                                    title=f'{a.title} — {recipient.pupil_name}', author=a.teacher_name)
            doc.build(seg[1])
            for page in PdfReader(io.BytesIO(buf.getvalue())).pages:
                writer.add_page(page)
        else:
            _, item, anns, pupil_notes, marks = seg
            try:
                reader = PdfReader(io.BytesIO(ctx.file_bytes(item.pdf)))
                if reader.is_encrypted:
                    reader.decrypt('')
                pages = list(reader.pages)
            except Exception:
                pages = []
            for page_no, page in enumerate(pages, start=1):
                if (page.get('/Rotate') or 0) % 360:
                    page.transfer_rotation_to_content()
                over = overlay_for_page(page, page_no, anns, pupil_notes, marks)
                if over is not None:
                    page.merge_page(over)
                writer.add_page(page)
    _add_footers(writer, f'{recipient.pupil_name} · {a.title}')
    writer.add_metadata({'/Title': f'{a.title} — {recipient.pupil_name}', '/Author': a.teacher_name or '',
                         '/Creator': 'Andam'})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _draw_mixed(c, x, y, text, size):
    """drawString that switches to the CJK font for Chinese / Japanese runs (names, titles)."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    last = 0
    runs = []
    for m in _CJK.finditer(text):
        runs.append((FONT, text[last:m.start()]))
        runs.append((CJK_FONT, m.group()))
        last = m.end()
    runs.append((FONT, text[last:]))
    for font, part in runs:
        if part:
            c.setFont(font, size)
            c.drawString(x, y, part)
            x += stringWidth(part, font, size)


def _add_footers(writer, text):
    from pypdf import PdfReader
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas
    total = len(writer.pages)
    if not total:
        return
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i, page in enumerate(writer.pages, start=1):
        w, h = float(page.mediabox.width), float(page.mediabox.height)
        x0, y0 = float(page.mediabox.left), float(page.mediabox.bottom)
        c.setPageSize((x0 + w, y0 + h))
        c.setFillColor(colors.HexColor('#8a93a3'))
        label = text if len(text) <= 90 else text[:88] + '…'
        _draw_mixed(c, x0 + 28, y0 + 14, label, 7.5)
        c.setFont(FONT, 7.5)
        c.drawRightString(x0 + w - 28, y0 + 14, f'Page {i} of {total}')
        c.showPage()
    c.save()
    footers = PdfReader(io.BytesIO(buf.getvalue())).pages
    for page, footer in zip(writer.pages, footers):
        page.merge_page(footer)


# ---------------------------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------------------------
_UNSAFE = re.compile(r'[^\w\s.,()&+-]', re.UNICODE)


def safe_filename(text, fallback='export'):
    name = _UNSAFE.sub('', str(text or '')).strip()
    name = re.sub(r'\s+', ' ', name)[:120].strip(' .')
    return name or fallback


def _attachment(data, filename, content_type):
    from urllib.parse import quote
    response = HttpResponse(data, content_type=content_type)
    ascii_name = filename.encode('ascii', 'ignore').decode() or 'export'
    response['Content-Disposition'] = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'
    response['Cache-Control'] = 'no-store'
    return response


def _include_marks(request):
    return request.GET.get('marks', '1') not in ('0', 'false', 'no')


def _include_pages(request):
    """pages=0 leaves out the exam's own PDF pages (with the pupil's highlights) — much smaller
    files when the paper is a big scan and the whole class is exported."""
    return request.GET.get('pages', '1') not in ('0', 'false', 'no')


def _actor(request):
    return request.user.get_full_name() or request.user.username


@login_required
@require_GET
def export_submission_pdf(request, recipient_id):
    """GET …/api/submissions/<recipient id>/export.pdf[?marks=0][&pages=0] — one pupil's work as a PDF.
    Teachers who can see the exam only; marks=0 leaves out scores, feedback and comments; pages=0
    leaves out the exam's own PDF pages."""
    from . import views
    denied = views._require_teacher(request)
    if denied:
        return denied
    recipient = get_object_or_404(Recipient.objects.select_related('assignment', 'assignment__school'), id=recipient_id)
    assignment = recipient.assignment
    if getattr(request, 'school', None) and assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not views.can_manage_exam(request.user, assignment):
        return views.not_your_exam_response()
    ctx = _Context(assignment, _include_marks(request), _actor(request), views.MANILA_TZ, _include_pages(request))
    data = build_submission_pdf(ctx, recipient)
    views.log_activity(_actor(request), request.user.email, 'exported',
                       f'Exported {recipient.pupil_name}\'s work on "{assignment.title}" as PDF.', assignment=assignment)
    return _attachment(data, f'{safe_filename(assignment.title)} - {safe_filename(recipient.pupil_name, "pupil")}.pdf',
                       'application/pdf')


@login_required
@require_GET
def export_assignment(request, assignment_id):
    """GET …/api/assignments/<id>/export/?format=zip|pdf&who=submitted|started&marks=1&pages=1
    Everyone's work on one exam: a ZIP with a PDF per pupil, or one combined PDF (each pupil
    starting on a new page). who=submitted (default) is handed-in work only; who=started also
    includes pupils who started but haven't handed in."""
    from pypdf import PdfReader, PdfWriter
    from . import views
    denied = views._require_teacher(request)
    if denied:
        return denied
    assignment = get_object_or_404(Assignment.objects.select_related('school'), id=assignment_id)
    if getattr(request, 'school', None) and assignment.school_id != request.school.id:
        return HttpResponseForbidden('Forbidden')
    if not views.can_manage_exam(request.user, assignment):
        return views.not_your_exam_response()
    fmt = request.GET.get('format', 'zip')
    who = request.GET.get('who', 'submitted')
    if fmt not in ('zip', 'pdf') or who not in ('submitted', 'started'):
        return JsonResponse({'detail': 'Unknown export option.'}, status=400)
    recipients = list(assignment.recipients.select_related('submission').order_by('pupil_name', 'id'))
    wanted = []
    for r in recipients:
        try:
            sub = r.submission
        except Submission.DoesNotExist:
            sub = None
        if sub is None:
            continue
        if sub.status == 'submitted' or (who == 'started' and (sub.started_at or sub.status == 'draft')):
            wanted.append(r)
    if not wanted:
        detail = ('Nobody has handed in this exam yet.' if who == 'submitted'
                  else 'Nobody has started this exam yet.')
        return JsonResponse({'code': 'nothing_to_export', 'detail': detail}, status=404)
    ctx = _Context(assignment, _include_marks(request), _actor(request), views.MANILA_TZ, _include_pages(request))
    title = safe_filename(assignment.title)
    if fmt == 'pdf':
        writer = PdfWriter()
        for r in wanted:
            for page in PdfReader(io.BytesIO(build_submission_pdf(ctx, r))).pages:
                writer.add_page(page)
        writer.add_metadata({'/Title': assignment.title, '/Creator': 'Andam'})
        out = io.BytesIO()
        writer.write(out)
        data, filename, ctype = out.getvalue(), f'{title} - all pupils.pdf', 'application/pdf'
    else:
        out = io.BytesIO()
        used = set()
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
            for r in wanted:
                name = f'{safe_filename(r.pupil_name, "pupil")}.pdf'
                if name.lower() in used:
                    name = f'{safe_filename(r.pupil_name, "pupil")} ({r.id}).pdf'
                used.add(name.lower())
                zf.writestr(name, build_submission_pdf(ctx, r))
        data, filename, ctype = out.getvalue(), f'{title} - PDFs.zip', 'application/zip'
    views.log_activity(_actor(request), request.user.email, 'exported',
                       f'Exported {len(wanted)} pupil(s)\' work on "{assignment.title}" '
                       f'({"one PDF" if fmt == "pdf" else "ZIP of PDFs"}).', assignment=assignment)
    return _attachment(data, filename, ctype)
