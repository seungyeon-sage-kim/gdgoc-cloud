"""web: the public service. Event list and pages, signup API, tickets, door check-in, admin.

It never does slow work itself. A signup is saved (one transaction) and announced
on Pub/Sub; the ticket worker makes the ticket in the background.

This file puts the app together. Each area is its own module:
  common    templates, formatting, who is signed in, event helpers
  events    what anyone can reach: event pages, tickets, signing up
  auth      signing in, and the gates in front of staff pages
  account   마이페이지
  door      check-in, for staff
  admin     running events, for organizers
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from shared import store
from shared.runtime import setup
from web import account, admin, auth, door, events
from web.common import HERE, not_found


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init()
    yield


app = FastAPI(title="web", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
setup(app, "web", secrets=("TICKET_SECRET", "SESSION_SECRET"))
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
for area in (events, auth, account, door, admin):
    app.include_router(area.router)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    """A person who mistypes an address gets a page; scripts calling the API get JSON."""
    if exc.status_code == 404 and "text/html" in request.headers.get("accept", ""):
        return not_found(request)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
