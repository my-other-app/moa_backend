"""The image's two pip layers (backend/Dockerfile, requirements-base.txt,
requirements.txt) stay what the Dockerfile's comment says they are."""
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def _pins(name: str) -> dict[str, str]:
    out = {}
    for line in (BACKEND / name).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-r")):
            continue
        package = re.split(r"[=<>\[ ;]", line)[0]
        key = re.sub(r"[-_.]+", "-", package).lower()
        assert key not in out, f"{package} is pinned twice in {name}"
        out[key] = line
    return out


def test_requirements_include_the_base_and_pin_nothing_twice():
    base, rest = _pins("requirements-base.txt"), _pins("requirements.txt")
    assert "-r requirements-base.txt" in (BACKEND / "requirements.txt").read_text().splitlines()
    assert not set(base) & set(rest), f"pinned in both files: {sorted(set(base) & set(rest))}"
    assert all("==" in line for line in [*base.values(), *rest.values()]), "every requirement is pinned exactly"


def test_the_dockerfile_installs_the_base_layer_first_with_a_cache_mount():
    text = (BACKEND / "Dockerfile").read_text()
    assert text.startswith("# syntax=docker/dockerfile:1")
    base = text.index("COPY ./requirements-base.txt")
    rest = text.index("COPY ./requirements.txt")
    code = text.index("COPY ./ /backend/")
    assert base < rest < code
    installs = re.findall(r"RUN --mount=type=cache,target=/root/.cache/pip \\\n\s+pip install[^\n]*", text)
    assert len(installs) == 2
    assert "--no-cache-dir" not in "".join(installs)
