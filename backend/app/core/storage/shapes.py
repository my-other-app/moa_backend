"""The one shape each club picture has, and the sizes made of it.

Agreed with the owner in Oct 2026, and the same in the app, on the public
site and on both dashboards:

    picture                              shape     upload asked for
    club logo, partner logo, people      1:1       1080 x 1080  (an Instagram profile picture)
    event poster                         4:5       1080 x 1350  (an Instagram portrait post)
    club banner (`cover`)                3:1       1500 x 500   (an X header)

Each size below is a box the picture is fitted inside (fields._fit): its
shape is kept, it is never cropped and never enlarged. So a poster that is
not 4:5 still has every size, and the clients show it whole (on a blurred
copy of itself) inside their 4:5 frame. The names are the ones the clients
already read -- thumbnail, medium, large -- and every size a column has ever
made is still made, so a picture stored before has the same names.
"""
from __future__ import annotations

from typing import Iterable

from app.config import settings
from app.core.storage.fields import S3ImageField

#: (width, height) of the picture each kind asks clubs to upload.
LOGO = (1080, 1080)
POSTER = (1080, 1350)
COVER = (1500, 500)

#: A club's logo: a 40-px row at 3x, a 160-px header at 3x, the full upload.
LOGO_SIZES = {
    "thumbnail": {"width": 150, "height": 150},
    "medium": {"width": 500, "height": 500},
    "large": {"width": LOGO[0], "height": LOGO[1]},
}

#: An event's poster, all 4:5: a list row's thumbnail, a feed or discover
#: card (about 270 px wide at 2x), and the event page.
POSTER_SIZES = {
    "thumbnail": {"width": 240, "height": 300},
    "medium": {"width": 540, "height": 675},
    "large": {"width": POSTER[0], "height": POSTER[1]},
}

#: A club's banner, 3:1: the asked-for size, and twice it for a wide
#: screen at 2x. A 1500 x 500 upload makes both at 1500 x 500 (never
#: enlarged); only a bigger upload has a sharper `large`.
COVER_SIZES = {
    "medium": {"width": COVER[0], "height": COVER[1]},
    "large": {"width": COVER[0] * 2, "height": COVER[1] * 2},
}


class MovedImageField(S3ImageField):
    """An image column whose folder changed: what was stored in one of the
    `earlier` folders keeps its sizes.

    S3ImageField decides from the key alone whether a picture's sizes exist
    (has_variants: only in the column's own folder). Event posters were
    stored with the club logos, in clubs/logos/, until Oct 2026; without
    this, every one of them would point each size at its original.
    """

    cache_ok = True

    def __init__(self, *args, earlier: Iterable[str] = (), **kwargs):
        super().__init__(*args, **kwargs)
        self.earlier = tuple(f.strip("/") for f in earlier)

    def has_variants(self, key: str) -> bool:
        if super().has_variants(key):
            return True
        # As S3ImageField.has_variants: a key under the base path, never an address.
        if not self.variations or not key or "://" in key.replace(settings.S3_BASE_PATH, "", 1):
            return False
        folder = key.rpartition("/")[0].strip("/")
        return any(folder == f or folder.endswith("/" + f) for f in self.earlier)
