import hashlib
import random
import re
import string
import time
import unicodedata


def generate_ticket_id():
    """Generate a unique alphanumeric ticket ID."""
    timestamp = str(int(time.time() * 1000))  # Milliseconds for uniqueness
    random_str = "".join(random.choices(string.ascii_uppercase + string.digits, k=50))
    unique_hash = hashlib.sha1((timestamp + random_str).encode()).hexdigest()
    return unique_hash[:50]


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
