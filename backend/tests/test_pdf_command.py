"""The wkhtmltopdf command (app/core/utils/pdf.py), built without pdfkit.

pdfkit read `<meta name="pdfkit-...">` tags out of the page and passed them
to wkhtmltopdf as options; a page carries a club's text, so none of it may
ever reach the command line. Only the caller's options do."""
import os
import stat

import pytest

from app.config import settings
from app.core.utils import pdf


@pytest.fixture
def fake_wkhtmltopdf(tmp_path, monkeypatch):
    """A wkhtmltopdf that writes its arguments and stdin into the 'PDF'."""
    fake = tmp_path / "wkhtmltopdf"
    fake.write_text('#!/bin/sh\nout="$(eval echo \\${$#})"\n{ printf "%s\\n" "$@"; echo ---; cat; } > "$out"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    pdf._wkhtmltopdf.cache_clear()
    yield fake
    pdf._wkhtmltopdf.cache_clear()


def test_the_command(fake_wkhtmltopdf):
    command = pdf.wkhtmltopdf_command(
        {"page-size": "A4", "margin-top": "0", "disable-javascript": None, "--Encoding": "UTF-8",
         "grayscale": True, "print-media-type": False, "title": ""},
        "/tmp/out.pdf",
    )
    assert command == [
        str(fake_wkhtmltopdf), "--quiet", "--page-size", "A4", "--margin-top", "0", "--disable-javascript",
        "--encoding", "UTF-8", "--grayscale", "-", "/tmp/out.pdf",
    ]


def test_options_in_the_page_never_reach_the_command_line(fake_wkhtmltopdf, tmp_path):
    (tmp_path / "page.html").write_text(
        '<html><head><meta name="pdfkit-enable-local-file-access" content="">'
        '<meta name="pdfkit-post-file" content="x /etc/passwd"></head><body>{{ name }}</body></html>'
    )
    out = pdf.generate_pdf_bytes("page.html", {"name": "Riya"}, template_dir=f"{tmp_path}/").getvalue().decode()
    args, _, page = out.partition("---\n")
    assert "--enable-local-file-access" not in args and "--post-file" not in args
    assert "--disable-local-file-access" in args and "--disable-javascript" in args
    assert "Riya" in page


def test_a_failure_is_raised_and_done_is_not(fake_wkhtmltopdf):
    pdf._check_exit(0, "")
    pdf._check_exit(1, "Loading pages\nDone\nExit with code 1 due to network error: ContentNotFoundError\n")
    with pytest.raises(IOError, match="code 2"):
        pdf._check_exit(2, "Error: Failed to load about:blank")


def test_no_wkhtmltopdf_is_said(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    pdf._wkhtmltopdf.cache_clear()
    try:
        with pytest.raises(IOError, match="No wkhtmltopdf"):
            pdf.wkhtmltopdf_command({}, "/tmp/x.pdf")
    finally:
        pdf._wkhtmltopdf.cache_clear()
    assert settings.RENDER_TIMEOUT > 0


# ─── No network but the fonts ───────────────────────────────────────────────


def test_the_renderer_reaches_nothing_but_the_fonts(fake_wkhtmltopdf, tmp_path):
    """A design's text reaches these pages: whatever it names, wkhtmltopdf
    goes through a proxy that is not there -- host lookups included -- and
    only Google Fonts bypasses it."""
    (tmp_path / "page.html").write_text("<html><body>{{ name }}</body></html>")
    out = pdf.generate_pdf_bytes("page.html", {"name": "Riya"}, template_dir=f"{tmp_path}/").getvalue().decode()
    args = out.partition("---\n")[0].splitlines()
    assert args[args.index("--proxy") + 1] == "http://127.0.0.1:9"
    assert "--proxy-hostname-lookup" in args
    bypassed = [args[i + 1] for i, a in enumerate(args) if a == "--bypass-proxy-for"]
    assert bypassed == ["fonts.googleapis.com", "fonts.gstatic.com"]
    # A resource that can't load is left out rather than failing the page.
    assert args[args.index("--load-error-handling") + 1] == "ignore"
    assert args[args.index("--load-media-error-handling") + 1] == "ignore"
    # And the PNG path is held to the same.
    assert pdf.network_args() == [
        "--proxy", "http://127.0.0.1:9", "--proxy-hostname-lookup", "--load-error-handling", "ignore",
        "--load-media-error-handling", "ignore", "--bypass-proxy-for", "fonts.googleapis.com", "--bypass-proxy-for", "fonts.gstatic.com",
    ]


def test_wkhtmltoimage_runs_without_the_network_too(tmp_path, monkeypatch):
    seen: list[list[str]] = []

    def run(args, **kwargs):
        seen.append(args)
        from PIL import Image

        Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(args[-1], format="PNG")
        # A blocked resource: wkhtmltoimage exits 1 after writing the whole image.
        return pdf.subprocess.CompletedProcess(args, 1, b"", b"Exit with code 1 due to network error: ConnectionRefusedError\n")

    monkeypatch.setattr(pdf.subprocess, "run", run)
    (tmp_path / "page.html").write_text("<html><body>x</body></html>")
    png = pdf.generate_image_bytes("page.html", {}, template_dir=f"{tmp_path}/").getvalue()
    assert png.startswith(b"\x89PNG")
    args = seen[0]
    assert args[0] == "wkhtmltoimage" and "--proxy" in args and "--proxy-hostname-lookup" in args
    assert args.index("--proxy") < args.index("-")


def test_a_document_missing_a_blocked_resource_still_counts():
    blocked = "QStandardPaths: XDG_RUNTIME_DIR not set\nExit with code 1 due to network error: ConnectionRefusedError\n"
    pdf._check_exit(1, blocked, wrote=True)
    with pytest.raises(IOError, match="code 1"):
        pdf._check_exit(1, blocked, wrote=False)
    with pytest.raises(IOError, match="code 1"):
        pdf._check_exit(1, "Error: something else\nfailed\n", wrote=True)


@pytest.mark.skipif(not __import__("shutil").which("wkhtmltopdf"), reason="needs the real wkhtmltopdf (backend/Dockerfile installs it)")
def test_the_real_renderer_fetches_nothing_a_page_names(tmp_path):
    """With the real binary: a page naming a server on this host, in CSS and
    in an image, gets a PDF, and the server is never asked."""
    import http.server
    import threading

    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        (tmp_path / "page.html").write_text(
            f'<html><head><style>.a {{ background: url(http://127.0.0.1:{port}/css) }}</style></head>'
            f'<body><div class="a">x</div><img src="http://localhost:{port}/img"></body></html>'
        )
        out = pdf.generate_pdf_bytes("page.html", {}, template_dir=f"{tmp_path}/").getvalue()
        assert out.startswith(b"%PDF")
        assert hits == []
    finally:
        server.shutdown()


def test_the_receipt_logo_is_embedded_not_fetched():
    from app.api.payments import background_tasks

    logo = background_tasks.receipt_logo_url()
    assert logo.startswith("data:image/png;base64,") and len(logo) > 1000
    html = pdf.render_html("payments/receipt.html", {"logo_url": logo})
    assert logo in html and "githubusercontent" not in html
