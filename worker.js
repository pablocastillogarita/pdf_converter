// Runs pdf_to_epub.py inside the browser (Pyodide) so the page stays responsive.
importScripts("https://cdn.jsdelivr.net/pyodide/v0.28.3/full/pyodide.js");

const ready = (async () => {
  const py = await loadPyodide();
  await py.loadPackage("pymupdf");
  const code = await (await fetch("pdf_to_epub.py", { cache: "no-cache" })).text();
  py.FS.writeFile("/home/pyodide/pdf_to_epub.py", code);
  py.runPython("import sys, json; sys.path.insert(0, '/home/pyodide'); import pdf_to_epub as P");
  return py;
})();

ready.then(
  () => postMessage({ type: "ready" }),
  (e) => postMessage({ type: "fatal", message: String(e) })
);

const CONVERT = `
a = json.loads(args_json)
data = open("/tmp/in.pdf", "rb").read()
cover = open("/tmp/cover.img", "rb").read() if a["cover"] else None
try:
    epub, info, cover_out = P.convert_bytes(data, a["name"], a["title"], a["author"], cover, a["ocr"])
    open("/tmp/out.epub", "wb").write(epub)
    open("/tmp/cover.out", "wb").write(cover_out or b"")
    result = {"ok": True, "info": info}
except ValueError as e:
    if "scanned" not in str(e):
        raise
    result = {"ok": False, "scanned": True, "pages": P.open_pdf(data)}
json.dumps(result)`;

onmessage = async ({ data: msg }) => {
  const reply = (body, transfer = []) => postMessage({ rid: msg.rid, ...body }, transfer);
  try {
    const py = await ready;
    if (msg.type === "convert") {
      py.FS.writeFile("/tmp/in.pdf", new Uint8Array(msg.pdf));
      if (msg.cover) py.FS.writeFile("/tmp/cover.img", new Uint8Array(msg.cover));
      py.globals.set("args_json", JSON.stringify({
        name: msg.name, title: msg.title ?? null, author: msg.author ?? null, cover: !!msg.cover, ocr: msg.ocr ?? null,
      }));
      const result = JSON.parse(py.runPython(CONVERT));
      if (!result.ok) return reply(result);
      const epub = py.FS.readFile("/tmp/out.epub");
      const cover = py.FS.readFile("/tmp/cover.out");
      reply({ ok: true, info: result.info, epub, cover }, [epub.buffer, cover.buffer]);
    } else if (msg.type === "render") {
      py.runPython(`open("/tmp/page.png", "wb").write(P.render_page(${Number(msg.page)}))`);
      const png = py.FS.readFile("/tmp/page.png");
      reply({ ok: true, png }, [png.buffer]);
    }
  } catch (e) {
    reply({ ok: false, message: String(e.message || e).trim().split("\n").pop() });
  }
};
