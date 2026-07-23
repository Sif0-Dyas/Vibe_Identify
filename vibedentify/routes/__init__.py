"""HTTP routes, split by domain across this package.

A single Blueprint (``bp``, defined in ``_shared``) carries every route; importing
the domain modules below registers their handlers on it via their decorators. The
app factory imports ``bp`` from here exactly as it did when routes was one module.
``_artist_of`` / ``_second_style`` are re-exported for the tests that import them.
"""

from . import analysis, library, map, training  # noqa: F401  -- register routes on bp
from ._shared import _artist_of, _second_style, bp  # noqa: F401  -- re-exported

__all__ = ["bp"]
