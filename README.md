# PDF to EPUB

Converts PDF books into clean, Kindle-ready EPUBs:

- Rejoins lines and paragraphs, removes running headers, footers and page numbers.
- Detects chapters and subtitles, and builds a navigable table of contents.
- Keeps italics and bold, images and tables; reads two-column pages in the right order.
- Footnotes go to the end of each chapter, with tappable references (pop-up notes on Kindle).
- Copyright and ISBN pages are moved to a "Credits" section at the end.
- Detects the language (Spanish, English, French, Portuguese, Italian, German).
- Uses the first page as the cover, or an image you choose.
- Scanned PDFs are read with OCR.

## Web page

`index.html` is a small page: choose one or more PDFs, adjust title, author and cover, and download the EPUBs.
Everything runs in the browser (Pyodide + PyMuPDF, Tesseract.js for OCR), so the PDFs are never uploaded anywhere.

To publish it: **Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch: `main`, folder `/ (root)` → Save**.
A minute later the page is live at `https://<your-user>.github.io/<repo-name>/`.

## Command line

```
pip install pymupdf
python pdf_to_epub.py book.pdf
python pdf_to_epub.py "C:\folder\with\pdfs"
python pdf_to_epub.py book.pdf --title "Title" --author "Author" --cover cover.jpg
```

OCR on the command line needs [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki) installed (with the Spanish language pack); without it, scanned PDFs are skipped with a message. The web page does not need it.
