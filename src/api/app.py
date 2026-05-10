"""
Gift Concierge Agent — FastAPI backend

Serves the chat frontend and exposes a single SSE endpoint that streams
step events then the final reply.

Run from the project root:
    uvicorn src.api.app:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import uuid
from pathlib import Path

# Ensure `src/` is on the path so sibling packages (agents, memory, …) resolve
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

# ---------------------------------------------------------------------------
# Lazy orchestrator init (imported here so the module loads without error
# even if env vars are missing at import time)
# ---------------------------------------------------------------------------

_orch = None

def _get_orch():
    global _orch
    if _orch is None:
        from agents.orchestrator import GiftOrchestrator
        from memory.embedder import OpenRouterEmbedder
        from memory.profile_store import SupabaseProfileStore
        from memory.rag_store import QdrantRAGStore
        from memory.st_store import SupabaseSTStore

        _orch = GiftOrchestrator(
            st_store=SupabaseSTStore(),
            profile_store=SupabaseProfileStore(),
            rag_store=QdrantRAGStore(embedder=OpenRouterEmbedder()),
        )
    return _orch


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

ROOT     = Path(__file__).parent.parent.parent
FRONTEND = ROOT / "frontend"

app = FastAPI(title="Gift Concierge Agent")

# Serve static files from /frontend (CSS, images if any)
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message:    str
    user_id:    str = "default_user"
    session_id: str = ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(str(FRONTEND / "index.html"))


@app.post("/chat")
async def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    user_id    = req.user_id or "default_user"

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def run_sync():
        try:
            for event in _get_orch().chat_stream(user_id, session_id, req.message):
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"type": "error", "text": str(exc)},
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel

    thread = threading.Thread(target=run_sync, daemon=True)
    thread.start()

    async def event_stream():
        while True:
            event = await queue.get()
            if event is None:
                break
            data = json.dumps(event, ensure_ascii=False)
            yield f"data: {data}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
