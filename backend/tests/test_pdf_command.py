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
