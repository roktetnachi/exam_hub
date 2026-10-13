# Fonts for the PDF export

Used by `digitalexams/pdf_export.py` and embedded (subset) in every exported PDF.

| File | What | Licence |
|---|---|---|
| `DejaVuSans*.ttf` | DejaVu Sans 2.37, regular / bold / italic / bold italic — Latin, Greek, Cyrillic, maths symbols | Bitstream Vera licence (see `LICENSE-DejaVu.txt`) |
| `NotoSansSC-Common.ttf` | Noto Sans CJK SC (Regular), cut down to ~9,500 common characters — GB2312 simplified Chinese, Big5 level-1 traditional Chinese, Japanese kana, CJK punctuation and full-width forms — and converted from CFF to TrueType so ReportLab can use it | SIL Open Font License 1.1 (see `LICENSE-Noto-OFL.txt`) |

The Noto file was made with fontTools (`subset`, then `cu2qu` to convert the outlines). A rare
character outside that set prints as a box; to cover it, rebuild the file from the full font with
the extra characters added.
