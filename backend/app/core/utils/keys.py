import hashlib
import random
import re
import string
import time
import unicodedata
import uuid


def generate_ticket_id():
    """Generate a short, user-friendly, and unique ticket ID."""
    timestamp = format(int(time.time() * 1000) % (36**5), "X")
    characters = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    random_part = "".join(random.choices(characters, k=5))
    return f"MOA-{timestamp}-{random_part}"


def generate_slug(name):
    """Generate a unique slug from a given name."""
    # Normalize the name (remove accents, convert to lowercase)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("utf-8")
    name = re.sub(r"[^a-zA-Z0-9\s-]", "", name).strip().lower()  # Remove special chars
    name = re.sub(
        r"[\s-]+", "-", name
    )  # Replace spaces and multiple dashes with a single dash

    # Create a unique hash from timestamp
    unique_hash = hashlib.sha1(str(time.time()).encode()).hexdigest()[:6]

    return f"{name}-{unique_hash}"
