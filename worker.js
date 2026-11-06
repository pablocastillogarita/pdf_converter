// Runs pdf_to_epub.py inside the browser (Pyodide) so the page stays responsive.
importScripts("https://cdn.jsdelivr.net/pyodide/v0.28.3/full/pyodide.js");

const ready = (async () => {
  const py = await loadPyodide();
  await py.loadPackage("pymupdf");
  const code = await (await fetch("pdf_to_epub.py", { cache: "no-cache" })).text();
  py.FS.writeFile("/home/pyodide/pdf_to_epub.py", code);
  py.runPython("import sys; sys.path.insert(0, '/home/pyodide'); import pdf_to_epub");
  return py;
})();

ready.then(
  () => postMessage({ type: "ready" }),
  (e) => postMessage({ type: "error", message: String(e) })
);

onmessage = async ({ data }) => {
  try {
    const py = await ready;
    py.FS.writeFile("/tmp/in.pdf", new Uint8Array(data.buffer));
    py.globals.set("pdf_name", data.name);
    const chapters = py.runPython(`
epub, count = pdf_to_epub.convert_bytes(open("/tmp/in.pdf", "rb").read(), pdf_name)
open("/tmp/out.epub", "wb").write(epub)
count`);
    const epub = py.FS.readFile("/tmp/out.epub");
    postMessage({ type: "done", epub, chapters }, [epub.buffer]);
  } catch (e) {
    const msg = String(e.message || e);
    postMessage({ type: "error", message: msg.includes("OCR") ? "scanned" : msg.split("\n").filter(Boolean).pop() });
  }
};
