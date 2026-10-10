"""A ticket or certificate as a canvas, layers and a page.

Ported from Muster's layout contract. The point of the design is that the
editor and the renderer run the same layout code against the same font
files, so what the canvas shows is what the attendee receives -- which the
wkhtmltopdf path never achieved, because a browser and a PDF engine lay
text out differently and neither side could measure the other's fonts.

Three modules, mirroring the TypeScript side one to one:

  model    the layer, canvas and page schemas and the colour helpers
  tokens   what a document can say, and the tiny template language
  engine   laying text out, validating a design, and the starters
  renderer drawing it, with reportlab for the PDF and pdfium for the PNG
"""
