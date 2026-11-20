""""I'm attending" posters (feature-register/posters/CONTRACT.md).

A guest whose ticket was sent makes a poster to share: their photo in the
design's frame, their name from the registration, the organiser's phrase for
the moment, and their own link back to the event page. The design is a
fourth document kind on the layout v2 board (`poster`), one layout per
format (square, portrait, story), with variants by role and ticket type.

Off for every event until its organiser switches it on (poster_settings), and
then touching nothing else: check-in, payments, approvals and ticket printing
do not read any of it. Renders go through the jobs queue at a low priority,
one at a time per runner, so card printing is never behind them.

- models.py: settings, designs (a layout per format per variant), each guest's poster
- moments.py: which moment applies to a guest, and why not
- designs.py: variants and their resolution, the starters, the board's DTO
- values.py: the poster's tokens for a guest (talk details from the agenda)
- photos.py: the guest's photo, cleaned, kept privately, cropped to the frame
- render.py: the PNG through the jobs queue, kept by fingerprint
- links.py: each guest's share link (a tracking link folded into one row)
- service.py, router.py: the guest's endpoints; organiser.py: the organiser's
- sweep.py: photos and renders removed 30 days after the event ends
"""
