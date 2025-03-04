import io
import os
import tempfile
import jinja2
import pdfkit


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
