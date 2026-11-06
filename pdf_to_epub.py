"""Convert PDF books into clean, Kindle-ready EPUB files.

Command line:  python pdf_to_epub.py book.pdf [more.pdf | folder ...] [--title T] [--author A] [--cover image.jpg]
Web page:      index.html runs this same file in the browser through Pyodide (see worker.js).
"""
import sys, re, io, json, uuid, zipfile, argparse, collections
from datetime import datetime, timezone
from pathlib import Path
from html import escape
import pymupdf  # PyMuPDF

MARGIN = 0.08  # top/bottom strip of the page where running headers and footers live

STYLE = """
body { margin: 0 4%; }
p { text-indent: 1.5em; text-align: justify; margin: 0 0 0.3em 0; }
h2 { text-align: left; margin: 1.5em 0 1em 0; }
h3 { text-indent: 1.5em; margin: 1.5em 0 0.8em 0; font-weight: bold; }
p.note { font-size: 0.85em; text-indent: 0; }
p.toc { text-indent: 0; text-align: left; margin: 0 0 0.5em 0; }
sup a { text-decoration: none; }
div.img { text-align: center; text-indent: 0; margin: 1em 0; }
div.img img { max-width: 100%; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 0.9em; }
td { border: 1px solid #999; padding: 0.2em 0.4em; text-align: left; vertical-align: top; }
body.cover { margin: 0; text-align: center; }
body.cover img { max-width: 100%; max-height: 100%; }
"""

# Inline formatting travels inside the text as control characters until the HTML is written.
SUP, ESUP, ITA, EITA, BLD, EBLD = "\x01\x02\x03\x04\x05\x06"
MARKERS = re.compile(r"[\x01-\x06]")
INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f￾￿]")  # never valid in EPUB (and would clash with markers)
TAGS = {ITA: ("i", True), EITA: ("i", False), BLD: ("b", True), EBLD: ("b", False)}

LEADER = re.compile(r"(\s*[.…·_]\s*){3,}\d*\s*$")  # "Chapter ........ 13" in a printed table of contents
NOTE_KEY = re.compile(r"\s*\x01?([*†‡]+|\d{1,3})\x02?[.)]?\s+(.*)", re.S)  # "1 Text of the note" / "* Text"
LEGAL = re.compile(r"ISBN|©|copyright|dep[óo]sito legal|\bD\.\s?L\.|impreso en|printed in|todos los derechos|"
                   r"all rights reserved|rigurosamente prohibid|\bNIPO\b|reservados todos", re.I)

LABELS = {  # language: (notes heading, table of contents, credits)
    "es": ("Notas", "Índice", "Créditos"), "en": ("Notes", "Contents", "Credits"),
    "fr": ("Notes", "Table des matières", "Crédits"), "pt": ("Notas", "Índice", "Créditos"),
    "it": ("Note", "Indice", "Crediti"), "de": ("Anmerkungen", "Inhalt", "Impressum"),
}
STOPWORDS = {
    "es": "el la de que y en los las del se por con para una su al es lo como más pero".split(),
    "en": "the of and to in is that for it with as was on by this are be from which".split(),
    "fr": "le la les de des et est un une que dans pour qui au du sur pas avec".split(),
    "pt": "o a os as de que e do da em um uma para com não dos das ao".split(),
    "it": "il la di che e un una per non del della sono gli le nel alla".split(),
    "de": "der die und das ist nicht ein eine zu den mit von sich auf des dem".split(),
}

# ---------------------------------------------------------------- text helpers

def is_bold(span):
    return bool(span["flags"] & 16) or "bold" in span["font"].lower()

def span_text(span, line_bold):
    """Span text with its inline formatting encoded as markers."""
    t = INVALID.sub("", span["text"])
    if not t.strip():
        return t
    if re.search(r"SC\d|SC$|SmallCap", span["font"]):
        t = t.upper()                                   # small caps
    if span["flags"] & 1:
        return f"{SUP}{t.strip()}{ESUP}"                # superscript (footnote reference)
    lead, core, trail = re.match(r"(\s*)(.*?)(\s*)$", t, re.S).groups()
    font = span["font"].lower()
    if span["flags"] & 2 or "italic" in font or "oblique" in font:
        core = f"{ITA}{core}{EITA}"
    if is_bold(span) and not line_bold:                 # whole bold lines are headings, not emphasis
        core = f"{BLD}{core}{EBLD}"
    return lead + core + trail

def tidy(text):
    text = re.sub(r"\x04(\s*)\x03", r"\1", text)        # merge neighbouring italic runs
    text = re.sub(r"\x06(\s*)\x05", r"\1", text)        # merge neighbouring bold runs
    return re.sub(r"\s+", " ", text).strip()

def plain(text):
    return MARKERS.sub("", re.sub(r"\s*\x01[^\x02]*\x02", "", text))

def join(lines):
    text = ""
    for l in lines:
        m = re.match(r"(.*\w)-([\x02\x04\x06]*)$", text, re.S)
        first = plain(l)[:1]
        if m and (first.islower() or (m.group(1)[-1:].isupper() and first.isupper())):
            text = m.group(1) + m.group(2) + l          # remove the hyphen of a word split across lines
        else:
            text = f"{text} {l}" if text else l
    return tidy(text)

def ends_sentence(text):
    """True if the text ends with closing punctuation (ignoring footnote references and formatting)."""
    text = re.sub(r"(\s*\x01[^\x02]*\x02)+$", "", text)
    return bool(re.search(r'[.!?:;»")\]”]$', re.sub(r"[\x02-\x06]+$", "", text)))

def starts_paragraph(text):
    c = plain(text)[:1]
    return c.isupper() or c.isdigit() or c in "«\"“¿¡-—–(*"

def markup(h):
    """Turn italic/bold markers into properly nested <i>/<b> tags, whatever state they arrive in."""
    out, stack = [], []
    for part in re.split(r"([\x03-\x06])", h):
        if part not in TAGS:
            out.append(part)
            continue
        tag, opening = TAGS[part]
        if opening:
            if tag not in stack:
                stack.append(tag)
                out.append(f"<{tag}>")
        elif tag in stack:
            reopen = []
            while stack:
                t = stack.pop()
                out.append(f"</{t}>")
                if t == tag:
                    break
                reopen.append(t)
            for t in reversed(reopen):
                stack.append(t)
                out.append(f"<{t}>")
    out.extend(f"</{t}>" for t in reversed(stack))
    return "".join(out)

def to_html(text, link=None):
    h = escape(text)
    h = re.sub(r"\x01([^\x02]*)\x02", (lambda m: link(m.group(1))) if link else r"<sup>\1</sup>", h)
    return markup(h.replace(SUP, "").replace(ESUP, ""))

def detect_language(sample):
    counts = collections.Counter(re.findall(r"[^\W\d_]+", sample.lower()))
    return max(STOPWORDS, key=lambda lang: sum(counts[w] for w in STOPWORDS[lang]))

# ---------------------------------------------------------------- reading the PDF

def split_paragraphs(lines):
    """PyMuPDF sometimes groups several paragraphs in one block: split on indents, short last lines and heading lines."""
    if not lines:
        return []
    left = min(l["x0"] for l in lines)
    right = max(l["x1"] for l in lines)
    groups = [[lines[0]]]
    for prev, l in zip(lines, lines[1:]):
        g = groups[-1]
        heading_end = prev["bold"] and not l["bold"] and all(x["bold"] for x in g)
        number_only = re.fullmatch(r"[\dIVXLC]+\.?", plain(" ".join(x["text"] for x in g)).strip())  # "6." before its title
        new_para = not number_only and ends_sentence(prev["text"]) and starts_paragraph(l["text"]) and (
            l["x0"] - left > 0.012 or right - prev["x1"] > 0.25 * (right - left))
        if heading_end or new_para:
            groups.append([l])
        else:
            g.append(l)
    return groups

def text_item(page, group, size):
    return {"kind": "text", "page": page, "lines": [l["text"] for l in group], "size": size,
            "bold": all(l["bold"] for l in group), "x0": min(l["x0"] for l in group),
            "x1": max(l["x1"] for l in group), "y0": group[0]["y0"], "y1": group[-1]["y1"]}

def page_items(page, textpage=None):
    """Text paragraphs and images of one page, with coordinates relative to the page size (0..1)."""
    w, h = page.rect.width, page.rect.height
    d = page.get_text("dict", textpage=textpage) if textpage else page.get_text("dict")
    items = []
    for b in d["blocks"]:
        x0, y0, x1, y1 = b["bbox"]
        if b["type"] == 1:
            items.append({"kind": "image", "page": page.number, "x0": x0 / w, "y0": y0 / h, "x1": x1 / w,
                          "y1": y1 / h, "image": b.get("image"), "ext": b.get("ext", ""),
                          "px": min(b.get("width", 0), b.get("height", 0))})
            continue
        lines, pending = [], ""
        for l in b["lines"]:
            normal = [s for s in l["spans"] if s["text"].strip() and not s["flags"] & 1]
            if not normal:                              # a line holding only a footnote reference
                t = "".join(span_text(s, True) for s in l["spans"]).strip()
                if lines:
                    lines[-1]["text"] += t
                else:
                    pending += t
                continue
            bold = all(is_bold(s) for s in normal)
            t = pending + "".join(span_text(s, bold) for s in l["spans"]).strip()
            pending = ""
            lx0, ly0, lx1, ly1 = l["bbox"]
            lines.append({"text": t, "x0": lx0 / w, "x1": lx1 / w, "y0": ly0 / h, "y1": ly1 / h,
                          "size": max(s["size"] for s in normal), "bold": bold})
        for group in split_paragraphs(lines):
            items.append(text_item(page.number, group, max(l["size"] for l in group)))
    return items

def page_tables(page):
    try:
        if len(page.get_cdrawings()) < 4:               # tables are drawn with lines; skip pages without any
            return []
        found = page.find_tables().tables
    except Exception:
        return []
    w, h = page.rect.width, page.rect.height
    out = []
    for t in found:
        rows = t.extract()
        cells = [c or "" for r in rows for c in r]
        if (len(rows) < 2 or max(len(r) for r in rows) < 2
                or sum(not c.strip() for c in cells) > 0.4 * len(cells)    # mostly empty: a layout grid, not a table
                or any(LEADER.search(c) for c in cells)):                  # dotted leaders: a table of contents
            continue
        x0, y0, x1, y1 = t.bbox
        out.append({"kind": "table", "page": page.number, "x0": x0 / w, "y0": y0 / h, "x1": x1 / w, "y1": y1 / h,
                    "rows": [[tidy(INVALID.sub("", c or "")) for c in r] for r in rows]})
    return out

def reading_order(items):
    """Pages in two columns: read the whole left column before the right one."""
    def in_column(i, side):
        width = i["x1"] - i["x0"]
        if not 0.2 <= width <= 0.55:
            return False
        return i["x1"] <= 0.56 if side == "left" else i["x0"] >= 0.44
    left = [i for i in items if in_column(i, "left")]
    right = [i for i in items if in_column(i, "right")]
    if len(left) < 2 or len(right) < 2:
        return items
    if min(max(i["y1"] for i in left), max(i["y1"] for i in right)) <= max(min(i["y0"] for i in left), min(i["y0"] for i in right)):
        return items                                    # side-by-side blocks that never overlap vertically
    top = min(i["y0"] for i in left + right)
    columns = {id(i) for i in left + right}
    rest = [i for i in items if id(i) not in columns]
    by_y = lambda i: i["y0"]
    above = [i for i in rest if i["y1"] <= top + 0.005]
    below = [i for i in rest if i["y1"] > top + 0.005]
    return sorted(above, key=by_y) + sorted(left, key=by_y) + sorted(right, key=by_y) + sorted(below, key=by_y)

def pdf_items(doc):
    """Every item of the book in reading order. Pages without text are OCR'd when Tesseract is installed."""
    items, ocr, ocr_pages = [], None, 0                 # ocr: None = untested, False = unavailable
    for page in doc:
        its = page_items(page)
        if ocr is not False and not any(i["kind"] == "text" for i in its) and page.get_images():
            try:
                its = page_items(page, page.get_textpage_ocr(language="spa+eng", dpi=300, full=True))
                ocr, ocr_pages = True, ocr_pages + 1
            except Exception:
                ocr = False
        tables = page_tables(page)
        if tables:
            inside = lambda i, t: t["x0"] <= (i["x0"] + i["x1"]) / 2 <= t["x1"] and t["y0"] <= (i["y0"] + i["y1"]) / 2 <= t["y1"]
            its = [i for i in its if i["kind"] != "text" or not any(inside(i, t) for t in tables)] + tables
            its.sort(key=lambda i: i["y0"])
        items.extend(reading_order(its))
    return items, ocr_pages

def ocr_items(pages):
    """Items from OCR results made in the browser: [{w, h, paragraphs: [{lines: [{text, x0, y0, x1, y1}]}]}]."""
    items = []
    for n, pg in enumerate(pages):
        w, h, its = pg["w"], pg["h"], []
        for para in pg["paragraphs"]:
            lines = [{"text": INVALID.sub("", l["text"]).strip(), "x0": l["x0"] / w, "x1": l["x1"] / w,
                      "y0": l["y0"] / h, "y1": l["y1"] / h, "size": (l["y1"] - l["y0"]) / h * 1000, "bold": False}
                     for l in para["lines"] if l["text"].strip()]
            for group in split_paragraphs(lines):
                sizes = sorted(l["size"] for l in group)
                its.append(text_item(n, group, sizes[len(sizes) // 2]))
        items.extend(reading_order(its))
    return items

def image_file(data, ext):
    ext = ext.lower()
    if ext in ("jpeg", "jpg", "png", "gif"):
        return data, "jpg" if ext == "jpeg" else ext
    try:                                                # JPEG 2000, JBIG2, TIFF... -> PNG
        pix = pymupdf.Pixmap(data)
        if pix.colorspace and pix.colorspace.n > 3:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        return pix.tobytes("png"), "png"
    except Exception:
        return None, None

# ---------------------------------------------------------------- building chapters

def extract(items, n_pages, default_title):
    """Turn the items into chapters [title, paragraphs, footnotes], plus credits and image files."""
    texts = [i for i in items if i["kind"] == "text"]
    if not texts:
        raise ValueError("no text found (scanned PDF): install Tesseract for OCR, or use the web page")
    norm = lambda s: re.sub(r"\d+", "#", plain(s).lower())
    top = collections.Counter(norm(join(i["lines"])) for i in texts if i["y0"] < MARGIN)
    bottom = collections.Counter(norm(join(i["lines"])) for i in texts if i["y1"] > 1 - MARGIN)
    headers = {t for t, n in top.items() if n >= 3}     # running heads (book title, chapter title) at the top
    footers = {t for t, n in bottom.items() if n > max(3, n_pages * 0.2)}   # stricter: short footnotes live down there too
    body = collections.Counter(round(i["size"]) for i in texts).most_common(1)[0][0]
    page_chars = collections.Counter()
    for i in texts:
        page_chars[i["page"]] += len(plain(join(i["lines"])))
    front = max(5, n_pages * 0.05)
    legal_pages = {i["page"] for i in texts if i["page"] < front and LEGAL.search(plain(join(i["lines"])))}
    image_uses = collections.Counter(hash(i["image"]) for i in items if i["kind"] == "image" and i["image"])
    footnotes = set()                                   # small text that closes a page, however tall the notes area is
    by_page = collections.defaultdict(list)
    for i in texts:
        by_page[i["page"]].append(i)
    for page_texts in by_page.values():
        tail = []
        for i in reversed(page_texts):
            if i["size"] >= body * 0.9:
                break
            if not re.fullmatch(r"[\d\W_]+", plain(join(i["lines"]))):   # skip the page number at the bottom
                tail.append(i)
        if tail and min(i["y0"] for i in tail) > 0.3:
            footnotes.update(id(i) for i in tail)

    chapters = [[default_title, [], []]]               # [title, paragraphs, footnotes]
    starts = [0]                                        # first page of each chapter
    credits, images = [], {}
    prev_title = None                                   # (page, y1) of the last title block
    prev_head = None                                    # (page, y1) of the last subtitle-sized heading
    last_notes = []                                     # notes list that got the latest footnote

    def new_chapter(text):
        if plain(text).casefold() != plain(chapters[-1][0]).casefold():   # same title again: keep going
            chapters.append([text, [], []])
            starts.append(page)

    for it in items:
        paras, page = chapters[-1][1], it["page"]
        if it["kind"] == "image":
            area = (it["x1"] - it["x0"]) * (it["y1"] - it["y0"])
            if (not it["image"] or area < 0.03 or it["px"] < 80
                    or image_uses[hash(it["image"])] > 3                    # logo or ornament on every page
                    or (area > 0.8 and page_chars[page] > 200)              # scanned page behind its own text
                    or (page == 0 and area > 0.5)):                         # the cover itself
                continue
            data, ext = image_file(it["image"], it["ext"])
            if data:
                name = f"images/img{len(images)}.{ext}"
                images[name] = data
                paras.append(("img", name))
            continue
        if it["kind"] == "table":
            paras.append(("table", it["rows"]))
            prev_title = None
            continue

        lines, size = it["lines"], it["size"]
        text = join(lines)
        flat = plain(text)
        if not flat.strip() or re.fullmatch(r"[\d\W_]+", flat):
            continue                                    # page number (also "15 ■")
        if (it["y0"] < MARGIN and norm(text) in headers) or (it["y1"] > 1 - MARGIN and norm(text) in footers):
            continue                                    # repeated header or footer
        if page in legal_pages:
            credits.append(text)                        # copyright page: moved to the end
            continue
        if plain(chapters[-1][0]).endswith("-") and not paras:
            chapters[-1][0] = join([chapters[-1][0], text])   # title split by a hyphen
            continue
        if any(LEADER.search(plain(l)) for l in lines):
            entry = []                                  # printed table of contents: one line per entry, no dots or page numbers
            if (len(paras) >= 2 and isinstance(paras[-2], tuple) and paras[-2][0] == "toc"
                    and (isinstance(paras[-1], str) or paras[-1][0] == "sub")):
                prev = paras.pop()                      # first line of a two-line entry, read as a separate block
                entry.append(plain(prev if isinstance(prev, str) else prev[1]))
            for l in lines:
                if re.fullmatch(r"\d+", plain(l)):
                    continue                            # stray page number
                entry.append(plain(l))
                if LEADER.search(plain(l)):
                    paras.append(("toc", LEADER.sub("", join(entry))))
                    entry = []
            if entry:
                paras.append(("toc", join(entry)))
            prev_title = None
            continue
        if size >= body * 1.2 and len(flat) < 200:
            if prev_title and prev_title[0] == page and it["y0"] - prev_title[1] < 0.03:
                chapters[-1][0] = join([chapters[-1][0], text])   # title split across several blocks
            else:
                new_chapter(text)                       # large title -> new chapter
            prev_title, prev_head = (page, it["y1"]), None
            continue
        prev_title = None
        if id(it) in footnotes:
            notes = chapters[-1][2]                     # footnotes: one per "N text", continuations joined
            for l in lines:
                m = NOTE_KEY.match(l)
                target = notes or (last_notes if not m else [])   # a note continued from the previous page
                last = NOTE_KEY.match(target[-1]) if target else None
                follows = (m and last and m.group(1).isdigit() and last.group(1).isdigit()
                           and int(m.group(1)) == int(last.group(1)) + 1)
                if target and not (m and (follows or ends_sentence(target[-1]))):
                    target[-1] = join([target[-1], l])
                else:
                    notes.append(join([l]))
                    last_notes = notes
            continue
        if len(flat) < 150 and not flat.endswith(".") and (it["bold"] or size >= body * 1.1):
            close = (prev_head and prev_head[0] == page and it["y0"] - prev_head[1] < 0.03
                     and len(flat) > 2 and not re.match(r"\d+\.\s", flat))   # not index letters "K", "M"
            if close and not paras and flat.upper() == flat:
                chapters[-1][0] = join([chapters[-1][0], text])   # second line of a numbered title
            elif close and paras and isinstance(paras[-1], tuple) and paras[-1][0] == "sub":
                paras[-1] = ("sub", join([paras[-1][1], text]))  # subtitle on two lines
            elif re.match(r"\d+\.\s", flat) and flat.upper() == flat:
                new_chapter(text)                       # "3. ALL-CAPS TITLE" -> chapter
            else:
                paras.append(("sub", text))             # subtitle (bold or slightly larger text)
            prev_head = (page, it["y1"])
            continue
        prev_head = None
        last = next((k for k in range(len(paras) - 1, -1, -1) if not (isinstance(paras[k], tuple) and paras[k][0] == "img")), None)
        if last is not None and isinstance(paras[last], str) and not ends_sentence(paras[last]):
            paras[last] = join([paras[last], text])     # paragraph split across pages (or around an image): merge
        else:
            paras.append(text)
    result, result_starts = [], []
    tiny = lambda c: not c[2] and sum(len(plain(p if isinstance(p, str) else p[1])) for p in c[1]
                                      if isinstance(p, str) or p[0] in ("sub", "toc")) < 300
    for c, start in zip(chapters, starts):
        if not c[1]:
            continue
        if result and plain(result[-1][0]).casefold() == plain(c[0]).casefold():
            result[-1][1] += c[1]                       # same title twice (cover, half-title...): one chapter
            result[-1][2] += c[2]
        elif result and start < front and result_starts[-1] < front and tiny(result[-1]) and tiny(c):
            result[-1][1] += [("sub", c[0])] + c[1]     # title pages (author, title, publisher): one section
        else:
            result.append(c)
            result_starts.append(start)
    move_notes(result)
    return result, credits, images

REF = re.compile(r"\x01\s*([^\x02]+?)\s*\x02")
PARENT = re.compile(r"(cap[ií]tulo|chapter|chapitre|kapitel|parte?|part|libro|book|livre|t[ií]tulo)\b", re.I)

def move_notes(chapters):
    """A section that starts mid-page gets that page's footnotes: give back the ones referenced before it."""
    note_key = lambda n: (NOTE_KEY.match(n) or [None, None])[1]
    refs = [collections.Counter(k.strip() for p in c[1] if isinstance(p, str) for k in REF.findall(p)) for c in chapters]
    for i in range(1, len(chapters)):
        prev, cur = chapters[i - 1], chapters[i]
        want = refs[i - 1] - collections.Counter(note_key(n) for n in prev[2])
        spare = collections.Counter(note_key(n) for n in cur[2]) - refs[i]
        keep = []
        for n in cur[2]:
            k = note_key(n)
            if k and want[k] > 0 and spare[k] > 0:
                prev[2].append(n)
                want[k] -= 1
                spare[k] -= 1
            else:
                keep.append(n)
        cur[2][:] = keep

# ---------------------------------------------------------------- writing the EPUB

XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">
<head><meta charset="utf-8"/><title>{title}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body{body_class}>
{body}
</body>
</html>"""

MEDIA = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif"}

def chapter_html(heading, paras, notes, notes_label):
    parsed, available, used = [], collections.defaultdict(collections.deque), set()
    for k, n in enumerate(notes):
        m = NOTE_KEY.match(n)
        parsed.append((m.group(1), m.group(2)) if m else (None, n))
        if m:
            available[m.group(1)].append(k)

    def link(key):                                      # n-th "1" in the text -> n-th note "1" of the chapter
        queue = available.get(key.strip())
        if not queue:
            return f"<sup>{key}</sup>"
        k = queue.popleft()
        used.add(k)
        return f'<sup><a epub:type="noteref" id="r{k}" href="#n{k}">{key}</a></sup>'

    # in the title only "*"-style notes are linked (stray digits there are usually layout artefacts)
    html = f"<h2>{to_html(heading, lambda key: link(key) if not key.strip().isdigit() else f'<sup>{key}</sup>')}</h2>\n"
    for p in paras:
        if isinstance(p, str):
            html += f"<p>{to_html(p, link)}</p>\n"
        elif p[0] == "sub":
            html += f"<h3>{to_html(p[1])}</h3>\n"
        elif p[0] == "toc":
            html += f'<p class="toc">{to_html(p[1])}</p>\n'
        elif p[0] == "img":
            html += f'<div class="img"><img src="{p[1]}" alt=""/></div>\n'
        elif p[0] == "table":
            html += "<table>\n" + "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in row) + "</tr>\n" for row in p[1]) + "</table>\n"
    if notes:
        html += f"<hr/><h3>{notes_label}</h3>\n"
        for k, (key, rest) in enumerate(parsed):
            if k in used:
                html += (f'<aside epub:type="footnote" id="n{k}"><p class="note">'
                         f'<a href="#r{k}">{escape(key)}</a> {to_html(rest)}</p></aside>\n')
            else:
                html += f'<p class="note">{to_html(notes[k])}</p>\n'
    return html

def build_epub(title, author, lang, chapters, credits, images, cover):
    """Write an EPUB 3 (with an NCX for older readers) and return its bytes."""
    notes_label, toc_label, credits_label = LABELS.get(lang, LABELS["en"])
    if credits:
        chapters = chapters + [[credits_label, credits, []]]
    uid = f"urn:uuid:{uuid.uuid4()}"
    page = lambda t, body, cls="": XHTML.format(lang=lang, title=t, body=body, body_class=f' class="{cls}"' if cls else "")
    files, toc, spine = {"OEBPS/style.css": STYLE}, [], []
    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
                '<item id="css" href="style.css" media-type="text/css"/>']
    cover_meta = ""
    if cover:
        data, ext = cover
        files[f"OEBPS/images/cover.{ext}"] = data
        files["OEBPS/cover.xhtml"] = page(escape(title), f'<div><img src="images/cover.{ext}" alt="{escape(title)}"/></div>', "cover")
        manifest += [f'<item id="cover-image" href="images/cover.{ext}" media-type="{MEDIA[ext]}" properties="cover-image"/>',
                     '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>']
        spine.append("cover")
        cover_meta = '<meta name="cover" content="cover-image"/>\n'
    for i, (name, data) in enumerate(images.items()):
        files[f"OEBPS/{name}"] = data
        manifest.append(f'<item id="im{i}" href="{name}" media-type="{MEDIA[name.rsplit(".", 1)[1]]}"/>')
    for i, (heading, paras, notes) in enumerate(chapters):
        name = f"chap{i}.xhtml"
        files[f"OEBPS/{name}"] = page(escape(plain(heading)), chapter_html(heading, paras, notes, notes_label))
        manifest.append(f'<item id="c{i}" href="{name}" media-type="application/xhtml+xml"/>')
        spine.append(f"c{i}")
        toc.append((name, escape(plain(heading))))

    tree, parent = [], False                            # "1. Section" entries go inside the "Chapter I" before them
    for n, t in toc:
        if PARENT.match(t):
            parent = True
        elif not (parent and re.match(r"\d+\.\s", t)):
            parent = False
        if parent and re.match(r"\d+\.\s", t) and tree:
            tree[-1][2].append((n, t, []))
        else:
            tree.append((n, t, []))
    nav = lambda nodes: "<ol>\n" + "".join(f'<li><a href="{n}">{t}</a>' + (nav(kids) if kids else "") + "</li>\n"
                                           for n, t, kids in nodes) + "</ol>"
    order = iter(range(1, len(toc) + 1))
    ncx = lambda nodes: "".join(f'<navPoint id="n{(o := next(order))}" playOrder="{o}"><navLabel><text>{t}</text></navLabel>'
                                f'<content src="{n}"/>{ncx(kids)}</navPoint>\n' for n, t, kids in nodes)
    files["OEBPS/nav.xhtml"] = page(toc_label, f'<nav epub:type="toc" id="toc"><h1>{toc_label}</h1>{nav(tree)}</nav>')
    files["OEBPS/toc.ncx"] = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'<head><meta name="dtb:uid" content="{uid}"/></head>\n'
        f"<docTitle><text>{escape(title)}</text></docTitle>\n<navMap>\n"
        + ncx(tree) + "</navMap>\n</ncx>")
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files["OEBPS/content.opf"] = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id" xml:lang="{lang}">\n'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'<dc:identifier id="id">{uid}</dc:identifier>\n'
        f"<dc:title>{escape(title)}</dc:title>\n<dc:language>{lang}</dc:language>\n"
        + (f"<dc:creator>{escape(author)}</dc:creator>\n" if author else "")
        + cover_meta
        + f'<meta property="dcterms:modified">{modified}</meta>\n</metadata>\n<manifest>\n'
        + "\n".join(manifest) + '\n</manifest>\n<spine toc="ncx">\n'
        + "".join(f'<itemref idref="{s}"/>\n' for s in spine)
        + "</spine>\n</package>")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)  # must be first, uncompressed
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0" encoding="utf-8"?>\n'
                   '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   "</rootfiles></container>", compress_type=zipfile.ZIP_DEFLATED)
        for name, content in files.items():
            z.writestr(name, content, compress_type=zipfile.ZIP_DEFLATED)
    return out.getvalue()

# ---------------------------------------------------------------- entry points

def make_cover(doc, cover):
    """The given image, or the first page of the PDF rendered as a picture."""
    if cover:
        return cover, "png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
    if not len(doc):
        return None
    first = doc[0]
    zoom = min(3.0, 1200 / max(first.rect.width, 1))
    return first.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("jpg", jpg_quality=85), "jpg"

def default_title(doc, filename):
    title = (doc.metadata or {}).get("title") or ""
    if not title.strip() or re.search(r"\.[a-z0-9]{2,5}\s*$|^microsoft|untitled|sin título", title.strip(), re.I):
        title = Path(filename).stem                     # metadata is empty or a file name ("doc1.docx", "99243_book.indb")
    return title.strip()

def convert_bytes(data, filename, title=None, author=None, cover=None, ocr=None):
    """PDF bytes -> (EPUB bytes, info, cover bytes). `ocr` = page results from the browser for scanned PDFs."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    title = (title or "").strip() or default_title(doc, filename)
    if author is None:
        author = (doc.metadata or {}).get("author") or ""
    if ocr:
        items, ocr_pages = ocr_items(json.loads(ocr) if isinstance(ocr, str) else ocr), len(doc)
    else:
        items, ocr_pages = pdf_items(doc)
    chapters, credits, images = extract(items, len(doc), title)
    sample = " ".join(plain(p) for c in chapters[:20] for p in c[1][:30] if isinstance(p, str))
    lang = detect_language(sample)
    cover_file = make_cover(doc, cover)
    epub = build_epub(title, author.strip(), lang, chapters, credits, images, cover_file)
    info = {"title": title, "author": author.strip(), "chapters": len(chapters), "images": len(images),
            "lang": lang, "ocr_pages": ocr_pages}
    return epub, info, cover_file[0] if cover_file else None

_doc = None

def open_pdf(data):
    """Keep a PDF open so the web page can render its pages for OCR. Returns the page count."""
    global _doc
    _doc = pymupdf.open(stream=data, filetype="pdf")
    return len(_doc)

def render_page(n, width=1600):
    page = _doc[n]
    zoom = width / max(page.rect.width, 1)
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY).tobytes("png")

def convert(path, title=None, author=None, cover=None):
    try:
        epub, info, _ = convert_bytes(path.read_bytes(), path.name, title, author, cover)
    except ValueError as e:
        print(f"{path.name}: {e}")
        return
    output = path.with_suffix(".epub")
    output.write_bytes(epub)
    ocr = f", OCR on {info['ocr_pages']} pages" if info["ocr_pages"] else ""
    print(f"Done: {output.name} ({info['chapters']} chapters, {info['images']} images, language: {info['lang']}{ocr})")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert PDF books into Kindle-ready EPUB files.")
    parser.add_argument("paths", nargs="+", help="PDF files or folders with PDFs")
    parser.add_argument("--title", help="book title (default: from the PDF)")
    parser.add_argument("--author", help="book author (default: from the PDF)")
    parser.add_argument("--cover", type=Path, help="cover image, JPG or PNG (default: first page of the PDF)")
    args = parser.parse_args()
    cover = args.cover.read_bytes() if args.cover else None
    for arg in args.paths:
        p = Path(arg)
        for pdf in (sorted(p.glob("*.pdf")) if p.is_dir() else [p]):
            convert(pdf, args.title, args.author, cover)
