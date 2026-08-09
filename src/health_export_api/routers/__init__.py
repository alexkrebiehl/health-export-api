"""HTTP routers, split by responsibility.

``data`` serves JSON from storage; ``render`` serves HTML built from that JSON
and never reaches past :class:`~health_export_api.provider.DataProvider`. The
split is enforced by a test, not just convention.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from fastapi import HTTPException, status

from health_export_api.throttle import QueueFull


@contextmanager
def domain_errors() -> Iterator[None]:
    """Translate the provider's domain errors into HTTP status codes.

    The provider deliberately knows nothing about HTTP, so the mapping lives
    here — in one place rather than repeated in every handler that can hit it.
    """
    try:
        yield
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        )
    except QueueFull:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Too many coverage requests queued. Rendering is "
                "serialised because it is CPU-bound; retry shortly."
            ),
            headers={"Retry-After": "5"},
        )
