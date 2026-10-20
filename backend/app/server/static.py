"""Static file serving for the built frontend.

``StaticFiles(html=True)`` serves ``index.html`` for ``/`` and for directory
URLs, but answers any other unknown path with a bare 404. For a single page app
that is wrong twice over: a deep link, a bookmark or a refresh on
``/whatever`` should render the app, and a typo in the API (``/api/helth``)
should *not* render an HTML page to a JSON client.

:class:`SpaStaticFiles` fixes the first without breaking the second.
"""

from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

#: Paths that must keep their own 404 instead of falling back to the SPA.
_API_PREFIXES = ("api/", "ws", "docs", "openapi.json", "redoc")


class SpaStaticFiles(StaticFiles):
    """Serve ``index.html`` for unknown *application* paths."""

    async def get_response(self, path: str, scope: Scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or not _is_spa_candidate(path, scope):
                raise
            # Let the browser render the app; it will show its own 404 screen.
            return await super().get_response("index.html", scope)


def _is_spa_candidate(path: str, scope: Scope) -> bool:
    """True when a missing file should be handed to the SPA instead of 404ing."""
    if scope.get("method") not in ("GET", "HEAD"):
        return False
    # `StaticFiles.get_path` runs the path through `os.path.normpath`, so on
    # Windows it arrives with backslashes ("api\\nope") - normalise before
    # matching prefixes, or every /api typo would render the SPA.
    normalised = path.replace("\\", "/").lstrip("/")
    if normalised.startswith(_API_PREFIXES):
        return False
    return _looks_like_navigation(scope)


def _looks_like_navigation(scope: Scope) -> bool:
    """Navigation requests get the app; asset requests get an honest 404.

    A browser navigation always advertises ``Accept: text/html,...``, while an
    ``<img>``, a stylesheet or a ``fetch()`` sends an Accept that has no
    ``text/html`` in it. So the header is the difference between "you opened a
    URL" and "something is missing" - and returning an HTML page with a 200 to
    the latter hides real bugs in the network tab.

    A *missing* Accept header is treated as a navigation: a client that sends
    none at all (curl, a monitoring probe, an uptime check) wants the page.
    """
    accept = ""
    for key, value in scope.get("headers") or ():
        if key == b"accept":
            accept = value.decode("latin-1")
            break
    return not accept.strip() or "text/html" in accept


__all__ = ["SpaStaticFiles"]
