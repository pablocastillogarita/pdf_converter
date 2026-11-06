import sys, re, io, uuid, zipfile, collections
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
"""

LEADER = re.compile(r"(\s*[.…·_]\s*){3,}\d*\s*$")  # "Chapter ........ 13" in a printed table of contents

def blocks(doc):
    for page in doc:
        h = page.rect.height
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            lines, sizes, bolds = [], [], []
            for l in b["lines"]:
                t = ""
                for s in l["spans"]:
                    st = s["text"]
                    if "SC" in s["font"]:
                        st = st.upper()              # small caps
                    if s["flags"] & 1 and st.strip():
                        st = f"\x01{st.strip()}\x02"  # superscript (footnote reference)
                    t += st
                t = t.strip()
                if t:
                    lines.append(t)
                    for s in l["spans"]:
                        if s["text"].strip() and not s["flags"] & 1:
                            sizes.append(s["size"])
                            bolds.append(bool(s["flags"] & 16) or "bold" in s["font"].lower())
            if lines and sizes:
                yield b["bbox"][1] / h, b["bbox"][3] / h, lines, max(sizes), all(bolds), page.number

def join(lines):
    text = ""
    for l in lines:
        if text.endswith("-") and (l[:1].islower() or (text[-2:-1].isupper() and l[:1].isupper())):
            text = text[:-1] + l            # remove the hyphen of a word split across lines
        else:
            text = f"{text} {l}" if text else l
    text = re.sub(r"[\x00\x03-\x08\x0b\x0c\x0e-\x1f\x7f￾￿]", "", text)  # characters invalid in EPUB
    return re.sub(r"\s+", " ", text)

def ends_sentence(text):
    """True if the text ends with closing punctuation (ignoring footnote references)."""
    return bool(re.search(r'[.!?:;»")\]]$', re.sub(r"(\s*\x01[^\x02]*\x02)+$", "", text)))

def plain(text):
    return re.sub(r"\s*\x01[^\x02]*\x02", "", text).replace("\x01", "").replace("\x02", "")

def to_html(text):
    return escape(text).replace("\x01", "<sup>").replace("\x02", "</sup>")

def extract(doc, default_title):
    """Turn the PDF into [title, paragraphs, footnotes] chapters."""
    all_blocks = list(blocks(doc))
    if not all_blocks:
        raise ValueError("no text found (scanned PDF, needs OCR)")
    norm = lambda s: re.sub(r"\d+", "#", s.lower())
    counts = collections.Counter(norm(join(b[2])) for b in all_blocks)
    repeated = {t for t, n in counts.items() if n > max(3, len(doc) * 0.2)}
    body = collections.Counter(round(b[3]) for b in all_blocks).most_common(1)[0][0]

    chapters = [[default_title, [], []]]     # [title, paragraphs, footnotes]
    prev_title = None                        # (page, y1) of the last title block
    for y0, y1, lines, size, bold, page in all_blocks:
        text = join(lines)
        if re.fullmatch(r"[\d\W_]+", text):
            continue                         # page number (also "15 ■")
        if norm(text) in repeated and (y0 < MARGIN or y1 > 1 - MARGIN):
            continue                         # repeated header or footer
        if chapters[-1][0].endswith("-") and not chapters[-1][1]:
            chapters[-1][0] = join([chapters[-1][0], text])   # title split by a hyphen
            continue
        if any(LEADER.search(l) for l in lines):
            entry = []                       # printed table of contents: one line per entry, no dots or page numbers
            for l in lines:
                if re.fullmatch(r"\d+", l):
                    continue                 # stray page number
                entry.append(l)
                if LEADER.search(l):
                    chapters[-1][1].append(("toc", LEADER.sub("", join(entry))))
                    entry = []
            if entry:
                chapters[-1][1].append(("toc", join(entry)))
            prev_title = None
            continue
        if size >= body * 1.2 and len(text) < 200:
            if prev_title and prev_title[0] == page and y0 - prev_title[1] < 0.03:
                chapters[-1][0] = join([chapters[-1][0], text])   # title split across several blocks
            else:
                chapters.append([text, [], []])    # large title -> new chapter
            prev_title = (page, y1)
            continue
        prev_title = None
        if size < body * 0.9 and y0 > 0.6:
            notes = chapters[-1][2]          # footnote: small text at the bottom of the page
            if notes and not ends_sentence(notes[-1]):
                notes[-1] = join([notes[-1], text])
            else:
                notes.append(text)
            continue
        paras = chapters[-1][1]
        if len(text) < 150 and not text.endswith(".") and (bold or size >= body * 1.1):
            if re.match(r"\d+\.\s", text) and plain(text).upper() == plain(text):
                chapters.append([text, [], []])    # "3. ALL-CAPS TITLE" -> chapter
            else:
                paras.append(("sub", text))  # subtitle (bold or slightly larger text)
            continue
        if paras and isinstance(paras[-1], str) and not ends_sentence(paras[-1]):
            paras[-1] = join([paras[-1], text])   # paragraph split across pages: merge
        else:
            paras.append(text)
    return [c for c in chapters if c[1]]

XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="es" xml:lang="es">
<head><meta charset="utf-8"/><title>{title}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
{body}
</body>
</html>"""

def build_epub(title, author, chapters):
    """Write a minimal EPUB 3 (with an NCX for older readers) and return its bytes."""
    uid = f"urn:uuid:{uuid.uuid4()}"
    files = {"OEBPS/style.css": STYLE}
    toc = []
    for i, (heading, paras, notes) in enumerate(chapters):
        html = f"<h2>{to_html(heading)}</h2>"
        for p in paras:
            if isinstance(p, str):
                html += f"<p>{to_html(p)}</p>\n"
            elif p[0] == "sub":
                html += f"<h3>{to_html(p[1])}</h3>\n"
            else:
                html += f'<p class="toc">{to_html(p[1])}</p>\n'
        if notes:
            html += "<hr/><h3>Notas</h3>\n" + "".join(f'<p class="note">{to_html(n)}</p>\n' for n in notes)
        name = f"chap{i}.xhtml"
        files[f"OEBPS/{name}"] = XHTML.format(title=escape(plain(heading)), body=html)
        toc.append((name, escape(plain(heading))))

    files["OEBPS/nav.xhtml"] = XHTML.format(title="Índice", body=(
        '<nav epub:type="toc" id="toc"><h1>Índice</h1><ol>\n'
        + "".join(f'<li><a href="{n}">{t}</a></li>\n' for n, t in toc) + "</ol></nav>"))
    files["OEBPS/toc.ncx"] = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'<head><meta name="dtb:uid" content="{uid}"/></head>\n'
        f"<docTitle><text>{escape(title)}</text></docTitle>\n<navMap>\n"
        + "".join(f'<navPoint id="n{i}" playOrder="{i + 1}"><navLabel><text>{t}</text></navLabel>'
                  f'<content src="{n}"/></navPoint>\n' for i, (n, t) in enumerate(toc))
        + "</navMap>\n</ncx>")
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files["OEBPS/content.opf"] = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id" xml:lang="es">\n'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'<dc:identifier id="id">{uid}</dc:identifier>\n'
        f"<dc:title>{escape(title)}</dc:title>\n<dc:language>es</dc:language>\n"
        + (f"<dc:creator>{escape(author)}</dc:creator>\n" if author else "")
        + f'<meta property="dcterms:modified">{modified}</meta>\n</metadata>\n<manifest>\n'
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
        '<item id="css" href="style.css" media-type="text/css"/>\n'
        + "".join(f'<item id="c{i}" href="{n}" media-type="application/xhtml+xml"/>\n' for i, (n, _) in enumerate(toc))
        + '</manifest>\n<spine toc="ncx">\n'
        + "".join(f'<itemref idref="c{i}"/>\n' for i in range(len(toc)))
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

def convert_bytes(data, filename):
    """PDF bytes -> (EPUB bytes, number of chapters). Used by the command line and the web page."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    title = doc.metadata.get("title") or Path(filename).stem
    chapters = extract(doc, title)
    return build_epub(title, doc.metadata.get("author"), chapters), len(chapters)

def convert(path):
    try:
        epub, count = convert_bytes(path.read_bytes(), path.name)
    except ValueError as e:
        print(f"{path.name}: {e}")
        return
    output = path.with_suffix(".epub")
    output.write_bytes(epub)
    print(f"Done: {output.name} ({count} chapters)")

if __name__ == "__main__":
    for arg in sys.argv[1:]:
        p = Path(arg)
        for pdf in (sorted(p.glob("*.pdf")) if p.is_dir() else [p]):
            convert(pdf)
