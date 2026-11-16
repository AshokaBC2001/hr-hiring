"""Local read-only mock TalentHub REST API backed by the sample MongoDB.

Implements Sithuli's TalentHub Intern Data API specification:
- GET /api/interns         -> list of active intern records
- GET /api/interns/{id}    -> single intern by Trainee_ID or MongoDB _id

Run directly:
    .venv/bin/python -m hiring.talent_hub_mock
"""
from __future__ import annotations

import hmac
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import config, talent_hub


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    talent_hub.close()


app = FastAPI(title="SLT TalentHub Mock API", version="1.0", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.exception_handler(talent_hub.TalentHubUnavailable)
async def mongo_unavailable(request: Request, exc: talent_hub.TalentHubUnavailable):
    return JSONResponse({"detail": str(exc)}, status_code=500)


def require_bearer_token(authorization: str | None = Header(default=None)) -> None:
    expected = (config.TALENT_HUB_API_TOKEN or "").strip()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    token = authorization[len("Bearer "):].strip()
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid bearer token.")


@app.get("/api/interns", dependencies=[Depends(require_bearer_token)])
def get_all_interns():
    return talent_hub.mongo_list_interns(limit=0)


@app.get("/api/interns/{identifier}", dependencies=[Depends(require_bearer_token)])
def get_intern_by_id(identifier: str):
    doc = talent_hub.mongo_get_intern(identifier)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"No intern matching {identifier} was found.")
    return doc


if __name__ == "__main__":
    h = talent_hub.mongo_health()
    print("\nSLT TalentHub Mock API (read-only)")
    print(f"  Listening on: http://{config.TALENT_HUB_MOCK_HOST}:{config.TALENT_HUB_MOCK_PORT}/api")
    print(f"  MongoDB ({config.TALENT_HUB_DB_NAME}.{config.TALENT_HUB_COLLECTION}): {h['detail']}")
    print("  Stop with Control+C\n")
    uvicorn.run(
        "hiring.talent_hub_mock:app",
        host=config.TALENT_HUB_MOCK_HOST,
        port=config.TALENT_HUB_MOCK_PORT,
        log_level="warning",
    )
