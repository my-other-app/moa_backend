"""The quiz's name filter (a round's `players.filter_names`): a player's
name goes up on the big screen in front of the room, so a round may refuse
names with words on this list.

A name is compared after folding what is used to dodge a list: case,
accents, repeated letters, digits and symbols standing in for letters, and
the spaces or dots put between letters. A word on the list matches as a
whole word, or anywhere inside the name for the longer ones (short ones
inside other words are ordinary: "Scunthorpe", "Dickens").

The list is short and plain on purpose; it is a courtesy for the room, not
a moderation system. Organisers who want names they can vouch for use
registered names (`players.names: registered`).
"""

from __future__ import annotations

import re
import unicodedata

#: Whole words only.
_WORDS = frozenset(
    {
        "anal",
        "anus",
        "arse",
        "ass",
        "bitch",
        "boob",
        "boobs",
        "cock",
        "crap",
        "cum",
        "cunt",
        "dick",
        "dildo",
        "fag",
        "fuck",
        "fucker",
        "hoe",
        "jizz",
        "kike",
        "nazi",
        "nigga",
        "nigger",
        "penis",
        "porn",
        "prick",
        "pussy",
        "rape",
        "rapist",
        "retard",
        "sex",
        "shit",
        "slut",
        "spic",
        "tit",
        "tits",
        "twat",
        "vagina",
        "wank",
        "wanker",
        "whore",
        # Common transliterated abuse in the clubs' languages.
        "bhosdi",
        "bhosdike",
        "chutiya",
        "chutiye",
        "gandu",
        "harami",
        "lavda",
        "lauda",
        "madarchod",
        "behenchod",
        "bhenchod",
        "randi",
        "thayoli",
        "pooru",
        "myre",
        "kunna",
        "punda",
        "thevidiya",
    }
)
#: Long enough to be refused inside another word too.
_INSIDE = frozenset(
    {
        "fuck",
        "nigger",
        "nigga",
        "bitch",
        "whore",
        "slut",
        "rapist",
        "madarchod",
        "behenchod",
        "bhenchod",
        "bhosdike",
        "chutiya",
        "thayoli",
        "thevidiya",
        "wanker",
        "dildo",
        "pussy",
    }
)

_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "9": "g", "@": "a", "$": "s", "!": "i", "|": "i", "+": "t"})


def _fold(text: str) -> str:
    bare = "".join(ch for ch in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(ch))
    return bare.casefold().translate(_LEET)


def _squeeze(word: str) -> str:
    return re.sub(r"(.)\1+", r"\1", word)


def refused(name: str) -> bool:
    """Whether the filter refuses `name`."""
    folded = _fold(name)
    words = [w for w in re.split(r"[^a-z]+", folded) if w]
    joined = "".join(words)
    for w in words:
        if w in _WORDS or _squeeze(w) in _WORDS:
            return True
    for bad in _INSIDE:
        if bad in joined or bad in _squeeze(joined):
            return True
    # Letters spaced out to dodge it: "f u c k", "s.h.i.t".
    singles = "".join(w for w in words if len(w) == 1)
    if len(singles) >= 3 and (singles in _WORDS or any(bad in singles for bad in _INSIDE)):
        return True
    return False
