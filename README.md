# PDF to EPUB

Converts PDFs into Kindle-ready EPUBs: rejoins paragraphs, removes running headers and page numbers, detects chapters and subtitles, and moves footnotes to the end of each chapter.

## Web page

`index.html` is a small page: choose a PDF, get an EPUB to download. The conversion runs entirely in the browser (Pyodide + PyMuPDF), so the PDF is never uploaded anywhere.

To publish it: **Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch: `main`, folder `/ (root)` → Save**. A minute later the page is live at `https://<your-user>.github.io/<repo-name>/`.

The first conversion takes a few seconds longer while the converter downloads; after that it is cached.

## Command line

```
pip install pymupdf
python pdf_to_epub.py "path\to\folder\or\file.pdf"
```

Scanned PDFs (images with no text) are not supported; run OCR on them first.
