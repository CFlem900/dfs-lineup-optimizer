"""Chat endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.rate_limiter import limiter
from app.api.auth import require_api_key
from app.api.dependencies import get_services
from app.api.sse_helpers import sse_stream, format_data_event
from app.models.ai import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
@limiter.limit("30/minute")
def chat_endpoint(request: Request, _auth=Depends(require_api_key), chat_request: ChatRequest = ...):
    """Send a message to the AI assistant.

    Returns a response with optional structured actions (e.g. lock_player,
    generate_lineups) that the frontend can execute.
    """
    try:
        svc = get_services()
        return svc.chat_agent.chat(chat_request)
    except Exception as e:
        logger.error(f"Chat failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat/stream")
@limiter.limit("30/minute")
async def stream_chat_endpoint(request: Request, _auth=Depends(require_api_key), chat_request: ChatRequest = ...):
    """Stream chat response via Server-Sent Events.

    SSE data format (one JSON per ``data:`` line):
    - ``{"chunk": "text"}``  -- partial text token
    - ``{"session_id": "...", "actions": [...]}``  -- final event with actions
    - ``{"error": "msg"}``  -- error event
    """
    svc = get_services()
    session_id = chat_request.session_id or ""

    def _run_chat(put, cancelled):
        try:
            full_text = ""
            for chunk in svc.chat_agent.stream_chat(chat_request):
                if cancelled.is_set():
                    break
                full_text += chunk
                put({"chunk": chunk})

            if not cancelled.is_set():
                # Extract actions from the completed response
                _, actions = svc.chat_agent._extract_actions(full_text)
                put({
                    "session_id": session_id or svc.chat_agent._last_session_id,
                    "actions": [
                        a if isinstance(a, dict) else a.model_dump()
                        for a in (actions or [])
                    ],
                    "done": True,
                })
        except Exception as e:
            logger.error(f"Chat stream failed: {e}")
            put({"error": str(e), "done": True})

    return await sse_stream(
        request,
        _run_chat,
        timeout_s=60.0,
        format_event=format_data_event,
        is_terminal=lambda m: m.get("done") is True or m.get("error") is not None,
    )
