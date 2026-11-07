import io
import logging
import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from typing import Iterable, Optional

import jinja2

from app.config import settings


logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _environment(template_dir: str) -> jinja2.Environment:
    """One Jinja environment per template folder, so compiled templates are kept."""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(searchpath=template_dir),
        autoescape=True,  # Protect against XSS in templates
        trim_blocks=True,
        lstrip_blocks=True,
    )


@lru_cache(maxsize=1)
def _wkhtmltopdf() -> str:
    """Where wkhtmltopdf is, looked up once."""
    path = shutil.which("wkhtmltopdf")
    if not path:
        raise IOError("No wkhtmltopdf executable found: install wkhtmltopdf (backend/Dockerfile does)")
    return path


def wkhtmltopdf_command(options: dict, output_path: str, extra: Iterable[str] = ()) -> list[str]:
    """wkhtmltopdf's argument list: each option as --name [value] (a value of
    None or True is a bare flag, False or "" leaves it out), then "-" (the
    page on stdin) and the output file.

    This used to be pdfkit's. pdfkit (unmaintained, an unpatched advisory:
    GHSA-9g3x-6x24-vf9f) also read `<meta name="pdfkit-...">` tags out of the
    page and passed them on as command-line options -- options from the
    document being rendered, which may carry a club's text. Here only the
    caller's options ever reach the command line.
    """
    args = [_wkhtmltopdf(), "--quiet"]
    for key, value in options.items():
        name = key.lower() if key.startswith("--") else f"--{key.lower()}"
        if value is False or value == "":
            continue
        args.append(name)
        if value is not None and value is not True:
            args.append(str(value))
    # `extra`: arguments as given, for an option that repeats (--bypass-proxy-for).
    return [*args, *extra, "-", output_path]


#: The only hosts wkhtmltopdf and wkhtmltoimage may reach: the fonts a
#: design names (Google Fonts' stylesheet and its font files).
FONT_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")

#: Everything else goes to a proxy that is not there, so nothing a page names
#: -- a design's text, a value that slipped past app.api.templates_style -- can
#: make the renderer fetch an address: not the cloud's metadata service, not
#: a service on this host, not the internet. Host names are looked up through
#: the proxy too, so not even a DNS query leaves. Pictures arrive as data:
#: URIs (app.api.templates_images.embedded) and need no network; a resource
#: that cannot load is left out of the page rather than failing it.
NETWORK_OPTIONS: dict[str, Optional[str]] = {
    "proxy": "http://127.0.0.1:9",
    "proxy-hostname-lookup": None,
    "load-error-handling": "ignore",
    "load-media-error-handling": "ignore",
}


def network_args() -> list[str]:
    """NETWORK_OPTIONS as command-line arguments, with a bypass per font host."""
    args: list[str] = []
    for name, value in NETWORK_OPTIONS.items():
        args.append(f"--{name}")
        if value is not None:
            args.append(value)
    for host in FONT_HOSTS:
        args += ["--bypass-proxy-for", host]
    return args


def _check_exit(returncode: int, stderr: str, wrote: bool = False) -> None:
    """Raise unless wkhtmltopdf finished: it sometimes exits non-zero after
    writing the PDF, and then says "Done" on its second-last line. A
    resource the page names that could not load -- refused by the proxy
    above, or a font host that did not answer -- makes it exit 1 "due to
    network error" after writing the whole document without it: that is a
    finished document too."""
    if returncode == 0:
        return
    lines = stderr.splitlines()
    if len(lines) > 1 and lines[-2].strip() == "Done":
        return
    if wrote and returncode == 1 and "due to network error" in stderr:
        logger.info("a document left out a resource it could not load: %s", stderr.strip().splitlines()[-1][:200])
        return
    raise IOError(f"wkhtmltopdf exited with code {returncode}: {stderr[-1000:] or 'no output'}")


def render_html(template_path: str, context: dict, template_dir: str = None) -> str:
    """The template, rendered. A design's `style` is made safe first
    (app.api.templates_style.safe): its colours, fonts, border and layer
    geometry go into CSS, and designs saved before they were checked exist."""
    if isinstance(context, dict) and isinstance(context.get("style"), dict):
        from app.api.templates_style import safe

        context = {**context, "style": safe(context["style"])}
    return _environment(template_dir or "templates/pdf/").get_template(template_path).render(context)


def generate_pdf_bytes(
    template_path: str, context: dict, options: dict = None, template_dir: str = None
) -> io.BytesIO:
    """
    Generate PDF as BytesIO object from Jinja2 template

    :param template_path: Path to the Jinja2 HTML template
    :param context: Dictionary of variables to pass to the template
    :param options: Optional wkhtmltopdf options (name -> value; None for a flag)
    :param template_dir: Optional custom template directory (defaults to './')
    :return: PDF content as BytesIO object

    wkhtmltopdf runs for at most RENDER_TIMEOUT seconds (pdfkit, which this
    used, waited with no limit at all, so a page that never finished loading
    held its thread for good); it is killed when it overruns. Blocks while it runs: async code calls this through
    app.core.layout.pool.
    """
    try:
        html_out = render_html(template_path, context, template_dir)

        # Default wkhtmltopdf options
        default_options: dict[str, Optional[str]] = {
            "page-size": "A4",
            "margin-top": "0",
            "margin-right": "0",
            "margin-bottom": "0",
            "margin-left": "0",
            "encoding": "UTF-8",
            # wkhtmltopdf has a known, unpatched vulnerability (the pdfkit
            # advisory PYSEC-2026-2860) where JavaScript in the rendered HTML can
            # read and exfiltrate local files. These templates never need
            # JS, so disable it outright rather than rely solely on Jinja2
            # autoescape to keep script content out.
            "disable-javascript": None,
            # CVE-2025-26240, also unpatched: path traversal through
            # `from_string`. That one does not need JavaScript -- a
            # `file://` in an img src or a CSS url() is enough -- so
            # disabling JS above does not cover it.
            #
            # Club-supplied text reaches these templates, which is exactly
            # the input the advisory is about. Nothing here loads a local
            # resource: artwork, logos and QR codes arrive as data: URIs and
            # the styles are inline, so this costs nothing.
            "disable-local-file-access": None,
            # And no network but the fonts (NETWORK_OPTIONS above).
            **NETWORK_OPTIONS,
        }

        # Merge default options with user-provided options
        pdf_options = {**default_options, **(options or {})}

        # Use a temporary file as an intermediary
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_output:
            temp_output_path = temp_output.name

        try:
            # The font hosts' bypasses repeat one option, which the dict cannot hold.
            bypass = [a for host in FONT_HOSTS for a in ("--bypass-proxy-for", host)]
            done = subprocess.run(
                wkhtmltopdf_command(pdf_options, temp_output_path, bypass),
                input=html_out.encode("utf-8"),
                capture_output=True,
                timeout=settings.RENDER_TIMEOUT,
            )
            stderr = (done.stderr or done.stdout or b"").decode("utf-8", errors="replace")
            _check_exit(done.returncode, stderr, wrote=os.path.getsize(temp_output_path) > 0)

            # Read the PDF into BytesIO
            with open(temp_output_path, "rb") as pdf_file:
                pdf_buffer = io.BytesIO(pdf_file.read())
            if not pdf_buffer.getbuffer().nbytes:
                raise IOError("wkhtmltopdf wrote nothing")

            # Reset buffer position
            pdf_buffer.seek(0)

            return pdf_buffer

        finally:
            # Clean up the temporary file
            if os.path.exists(temp_output_path):
                os.unlink(temp_output_path)

    except jinja2.TemplateError as e:
        raise ValueError(f"Jinja2 Template Error: {e}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"PDF Generation Error: wkhtmltopdf ran longer than {settings.RENDER_TIMEOUT:g}s and was stopped"
        )
    except IOError as e:
        raise IOError(f"File Operation Error: {e}")
    except Exception as e:
        raise RuntimeError(f"Unexpected error generating PDF: {e}")


def generate_image_bytes(
    template_path: str,
    context: dict,
    width: int = 1400,
    template_dir: str = None,
) -> io.BytesIO:
    """Render the same Jinja2 template to a PNG instead of a PDF.

    The app shows a certificate preview beside a download button, and the
    two have to be the same document. Rendering the PDF in a webview put
    Apple's PDF viewer chrome on screen -- a page counter, a page frame, and
    the certificate shrunk inside a sheet of white -- which no styling can
    remove. An image has no viewer, so it crops to the certificate exactly.

    wkhtmltoimage is the same engine wkhtmltopdf uses, so the output is the
    same rendering of the same template, not a second implementation that
    can drift from it.
    """
    html_out = render_html(template_path, context, template_dir)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as out:
        output_path = out.name

    try:
        done = subprocess.run(
            [
                "wkhtmltoimage",
                "--format", "png",
                "--width", str(width),
                # Transparent so the card behind supplies the colour, and
                # cropped to the content rather than padded to a page.
                "--transparent",
                "--quality", "90",
                # No JavaScript, for the same reason the PDF path disables
                # it: these templates never need it, and it is the vector
                # for the unpatched local-file read in wkhtmltopdf.
                "--disable-javascript",
                # And no local files at all, for CVE-2025-26240 -- which
                # needs no JavaScript, only a file:// in a src or a url().
                "--disable-local-file-access",
                # And no network but the fonts, as for the PDF.
                *network_args(),
                # "-" reads the document from stdin. With local file access
                # off, a local input path is the one thing wkhtmltoimage
                # might still have had to open; this way it opens nothing.
                "-",
                output_path,
            ],
            input=html_out.encode("utf-8"),
            capture_output=True,
            timeout=settings.RENDER_TIMEOUT,
        )
        if done.returncode != 0:
            stderr = (done.stderr or b"").decode("utf-8", errors="replace")
            wrote = os.path.exists(output_path) and os.path.getsize(output_path) > 0
            if not (wrote and done.returncode == 1 and "due to network error" in stderr):
                raise subprocess.CalledProcessError(done.returncode, "wkhtmltoimage", output=done.stdout, stderr=done.stderr)
        with open(output_path, "rb") as rendered:
            return _tighten_png(rendered.read())
    except subprocess.CalledProcessError as e:
        logger.error(f"wkhtmltoimage failed: {e.stderr[:500]}")
        raise
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass

def _tighten_png(raw: bytes) -> io.BytesIO:
    """Crop a rendered page to its content and compress it.

    wkhtmltoimage writes the full page at the requested width and does not
    compress: a certificate came back 1400x559 and 2.99MB, which is exactly
    its uncompressed size. Most of that was transparent margin either side
    of a centred card, and an app showing four of them in a list would pull
    12MB to draw four thumbnails.

    Cropping to the alpha bounding box also fixes the shape -- the image
    becomes the certificate rather than a wide page with a certificate in
    the middle -- so it scales into a panel without being letterboxed.
    """
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as image:
        image = image.convert("RGBA")
        box = image.getbbox()
        if box:
            image = image.crop(box)

        out = io.BytesIO()
        # Flat artwork with few colours, so a palette costs nothing visually
        # and saves most of the bytes. Transparency is kept: the card behind
        # it in the app supplies the colour around the certificate.
        image.save(out, format="PNG", optimize=True)
        out.seek(0)
        return out
