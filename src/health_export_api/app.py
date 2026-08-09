"""Application construction: shared dependencies, then mount the routers.

The HTTP surface is split in two, and the split is deliberate:

* ``/v1/...`` — JSON from storage (:mod:`health_export_api.routers.data`)
* ``/v1/render/...`` — HTML built from that JSON
  (:mod:`health_export_api.routers.render`)

The render router reaches the data layer only through
:class:`~health_export_api.provider.DataProvider`, so the two halves could be
separated into different processes by swapping that one object for an HTTP
client. They are not separated today because the render layer is pure
templating with no I/O, and a process boundary would cost an extra transfer of
the 1.4MB coverage payload, a second credential, and a second cache — for no
benefit at this size.
"""

import hashlib
import hmac
import os
import secrets
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Header, status
from fastapi.staticfiles import StaticFiles

from health_export_api.provider import DataProvider
from health_export_api.routers.data import build_data_router
from health_export_api.routers.render import build_render_router
from health_export_api.store import Store
from health_export_api.throttle import (
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_MAX_QUEUE,
    RequestGate,
    TTLCache,
)

_STATIC_DIR = Path(__file__).parent / "static"


def derive_embed_token(api_token: str) -> str:
    """A second, read-only token for the embeddable pages.

    The Home Assistant Webpage card cannot send an Authorization header, so the
    credential has to travel in the URL — where it ends up in the dashboard
    config and browser history. Putting the real token there would expose
    ingestion rights, so the embedded pages get their own, derived one:

    * nothing new to provision, and it cannot be reversed to the API token;
    * leaking it exposes the rendered dashboard pages and nothing else;
    * rotating HEALTH_EXPORT_API_TOKEN rotates it too.
    """
    return hmac.new(api_token.encode(), b"embed", hashlib.sha256).hexdigest()[:32]


def create_app(
    storage_dir: Path,
    api_token: str,
    summary_today: date | None = None,
    cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
    max_queue: int = DEFAULT_MAX_QUEUE,
) -> FastAPI:
    if not api_token:
        raise ValueError("api_token must not be empty")

    storage_dir.mkdir(parents=True, exist_ok=True)
    db_path = storage_dir / "health_export.db"
    store = Store(db_path)
    store.backfill(storage_dir)

    app = FastAPI(title="Health Export API", version="0.6.0")

    # Stock Leaflet, served same-origin so the map page has no CDN dependency.
    # Unauthenticated: it is open-source library code, not user data.
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    embed_token = derive_embed_token(api_token)

    # Coverage rendering is GIL-bound, so concurrent requests are far slower
    # than sequential ones. Serialise them and serve repeats from cache. Both
    # live on the provider: they protect the work, not the route.
    provider = DataProvider(
        store,
        cache=TTLCache(ttl=cache_ttl),
        gate=RequestGate(max_queue=max_queue),
        today=lambda: summary_today or date.today(),
    )

    def authorize(authorization: str | None) -> None:
        if authorization != f"Bearer {api_token}":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def authorize_embed(authorization: str | None, supplied: str | None) -> None:
        """Accept either the full bearer token or the derived embed token."""
        if supplied is not None and secrets.compare_digest(supplied, embed_token):
            return
        authorize(authorization)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/embed-token")
    def get_embed_token(
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        """The derived embed token. Requires the real bearer token."""
        authorize(authorization)
        return {"embed_token": embed_token}

    app.include_router(
        build_data_router(
            store=store,
            provider=provider,
            authorize=authorize,
            storage_dir=storage_dir,
        )
    )
    app.include_router(
        build_render_router(provider=provider, authorize_embed=authorize_embed)
    )
    return app


def create_app_from_env() -> FastAPI:
    api_token = os.environ.get("HEALTH_EXPORT_API_TOKEN")
    if not api_token:
        raise RuntimeError("HEALTH_EXPORT_API_TOKEN must be configured")

    raw_ttl = os.environ.get("HEALTH_EXPORT_CACHE_TTL")
    try:
        cache_ttl = DEFAULT_CACHE_TTL_SECONDS if raw_ttl is None else float(raw_ttl)
    except ValueError:
        raise RuntimeError(
            f"HEALTH_EXPORT_CACHE_TTL must be a number of seconds, got {raw_ttl!r}"
        )

    return create_app(
        storage_dir=Path(os.environ.get("HEALTH_EXPORT_STORAGE_DIR", "/data/exports")),
        api_token=api_token,
        cache_ttl=cache_ttl,
    )
