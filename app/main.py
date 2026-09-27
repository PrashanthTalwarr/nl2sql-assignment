"""
HTTP API.
  GET  /api/health        liveness check (used by App Runner)
  GET  /api/roles         the three mock logins
  POST /api/login         {user_id} -> signed session token
  POST /api/chat          {message, history} -> final JSON result (tests / non-streaming clients)
  POST /api/chat/stream   {message, history} -> NDJSON event stream (the UI)
  GET  /                  chat UI

Auth: mock login (pick one of 3 roles), but the session is a SIGNED token.
The browser cannot change who it is: any edit breaks the signature.
"""
import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field

from . import db, pipeline

load_dotenv()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("nl2sql.api")

# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
SECRET = os.getenv("SESSION_SECRET")
if not SECRET:
    SECRET = secrets.token_urlsafe(32)
    log.warning("SESSION_SECRET not set: using a random one; sessions reset on restart.")
SESSION_MAX_AGE_S = 8 * 3600
signer = URLSafeTimedSerializer(SECRET, salt="nl2sql-session")
bearer = HTTPBearer(auto_error=False)

MOCK_LOGINS = [
    {"user_id": "U001", "role": "exec", "label": "Executive",
     "detail": "Sarah Chen · all territories · pricing visible"},
    {"user_id": "U003", "role": "director", "label": "Director of Sales",
     "detail": "Jennifer Walsh · Northeast region · no pricing"},
    {"user_id": "U009", "role": "ram", "label": "Regional Account Manager",
     "detail": "Amy Nguyen · New York Metro territory · no pricing"},
]
ALLOWED_LOGINS = {m["user_id"] for m in MOCK_LOGINS}

# ---------------------------------------------------------------------------
# Rate limiting (per user, sliding 60s window)
# ---------------------------------------------------------------------------
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "20"))
_hits = defaultdict(deque)


def rate_limit(user_id):
    now = time.time()
    q = _hits[user_id]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "Too many questions in a short time. Please wait a minute.")
    q.append(now)


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if not creds:
        raise HTTPException(401, "Not signed in.")
    try:
        user_id = signer.loads(creds.credentials, max_age=SESSION_MAX_AGE_S)
    except SignatureExpired:
        raise HTTPException(401, "Session expired. Please sign in again.")
    except BadSignature:
        raise HTTPException(401, "Invalid session.")
    user = db.get_user(user_id)
    if not user:
        raise HTTPException(401, "Unknown user.")
    return user


def _scope_label(user):
    if user["role"] == "exec":
        return "All territories · pricing visible"
    if user["role"] == "director":
        return f"{user['region_name']} region · no pricing"
    return f"{user['territory_name']} territory · no pricing"


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    user_id: str


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    history: list[Turn] = Field(default_factory=list, max_length=40)


def _clean_history(turns):
    """Last 10 turns, starting with a user turn (Anthropic requires that)."""
    history = [t.model_dump() for t in turns][-10:]
    while history and history[0]["role"] != "user":
        history.pop(0)
    return history


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="NovaPharma NL-to-SQL Assistant")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/roles")
def roles():
    return MOCK_LOGINS


@app.post("/api/login")
def login(req: LoginRequest):
    if req.user_id not in ALLOWED_LOGINS:
        raise HTTPException(403, "Unknown login.")
    user = db.get_user(req.user_id)
    return {
        "token": signer.dumps(user["user_id"]),
        "user": {"name": user["full_name"], "role": user["role"], "scope": _scope_label(user)},
    }


@app.post("/api/chat")
def chat(req: ChatRequest, user=Depends(current_user)):
    rate_limit(user["user_id"])
    return pipeline.ask(user, req.message.strip(), _clean_history(req.history))


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest, user=Depends(current_user)):
    rate_limit(user["user_id"])
    history = _clean_history(req.history)
    question = req.message.strip()

    def events():
        try:
            for event in pipeline.ask_stream(user, question, history):
                yield json.dumps(event, default=str) + "\n"
        except Exception as e:                       # never leave the client hanging
            log.exception("stream failed: %s", e)
            yield json.dumps({"type": "done", "result": {
                "answer": "Something went wrong while answering. Please try again.",
                "kind": "error", "sql": None, "verified": None, "summary": "",
                "corrected": False, "columns": [], "rows": [], "truncated": False}}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/")
def index():
    page = STATIC_DIR / "index.html"
    if page.exists():
        return FileResponse(page)
    return JSONResponse({"message": "API is running. See /docs."})