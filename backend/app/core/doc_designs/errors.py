"""What the document designs raise. All are ValueErrors: a caller that cannot
draw a design falls back to the classic renderer (contract, D9)."""


class DesignError(ValueError):
    """A design could not be filled."""


class UnknownDesign(DesignError):
    """No design has this id."""


class MissingValue(DesignError):
    """A value the design must print is empty: a required token, or an
    optional one outside any block that could hide it."""


class BadValue(DesignError):
    """A value of the wrong shape: an image that is not a data: URI, an
    accent that is not #RRGGBB."""


class LeftoverToken(DesignError):
    """The filled page still holds `{{` or a designer's placeholder: it must
    fail rather than print."""
