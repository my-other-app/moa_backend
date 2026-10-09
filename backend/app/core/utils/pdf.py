import io
import os
import tempfile
import jinja2
import logging
import pdfkit


logger = logging.getLogger(__name__)


def generate_pdf_bytes(
    template_path: str, context: dict, options: dict = None, template_dir: str = None
) -> io.BytesIO:
    """
    Generate PDF as BytesIO object from Jinja2 template

    :param template_path: Path to the Jinja2 HTML template
    :param context: Dictionary of variables to pass to the template
    :param options: Optional PDFKit configuration options
    :param template_dir: Optional custom template directory (defaults to './')
    :return: PDF content as BytesIO object
    """
    try:
        # Set template loader directory
        template_loader_path = template_dir or "templates/pdf/"

        # Create Jinja2 environment
        template_loader = jinja2.FileSystemLoader(searchpath=template_loader_path)
        template_env = jinja2.Environment(
            loader=template_loader,
            autoescape=True,  # Protect against XSS in templates
            trim_blocks=True,
            lstrip_blocks=True,
        )

        # Load the template
        template = template_env.get_template(template_path)

        # Render the template with context
        html_out = template.render(context)

        # Default PDFKit options
        default_options = {
            "page-size": "A4",
            "margin-top": "0",
            "margin-right": "0",
            "margin-bottom": "0",
            "margin-left": "0",
            "encoding": "UTF-8",
            # pdfkit/wkhtmltopdf has a known, unpatched vulnerability
            # (PYSEC-2026-2860) where JavaScript in the rendered HTML can
            # read and exfiltrate local files. These templates never need
            # JS, so disable it outright rather than rely solely on Jinja2
            # autoescape to keep script content out.
            "disable-javascript": None,
        }

        # Merge default options with user-provided options
        pdf_options = {**default_options, **(options or {})}

        # Use a temporary file as an intermediary
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_output:
            temp_output_path = temp_output.name

        try:
            # Generate PDF using the temporary file
            pdfkit.from_string(html_out, temp_output_path, options=pdf_options)

            # Read the PDF into BytesIO
            with open(temp_output_path, "rb") as pdf_file:
                pdf_buffer = io.BytesIO(pdf_file.read())

            # Reset buffer position
            pdf_buffer.seek(0)

            return pdf_buffer

        finally:
            # Clean up the temporary file
            if os.path.exists(temp_output_path):
                os.unlink(temp_output_path)

    except jinja2.TemplateError as e:
        raise ValueError(f"Jinja2 Template Error: {e}")
    except pdfkit.errors.PDFKitError as e:
        raise RuntimeError(f"PDFKit Generation Error: {e}")
    except IOError as e:
        raise IOError(f"File Operation Error: {e}")
    except Exception as e:
        raise RuntimeError(f"Unexpected error generating PDF: {e}")


def save_pdf_to_file(pdf_bytes, filename="output.pdf"):
    """
    Save PDF bytes to a file
    """
    with open(filename, "wb") as f:
        f.write(pdf_bytes.getvalue())

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
    import subprocess
    import tempfile

    template_loader_path = template_dir or "templates/pdf/"
    template_env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(searchpath=template_loader_path),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    html_out = template_env.get_template(template_path).render(context)

    with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w") as src:
        src.write(html_out)
        source_path = src.name
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as out:
        output_path = out.name

    try:
        subprocess.run(
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
                source_path,
                output_path,
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        with open(output_path, "rb") as rendered:
            return _tighten_png(rendered.read())
    except subprocess.CalledProcessError as e:
        logger.error(f"wkhtmltoimage failed: {e.stderr[:500]}")
        raise
    finally:
        for path in (source_path, output_path):
            try:
                os.unlink(path)
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
