"""FastAPI APIRouter modules for SSEManager's REST/SSE surface (SSE-01).

Each module builds one APIRouter grouped by concern, taking the owning
SSEManager instance so endpoint closures can reach its state
(message_router, classifier, storage) exactly as they did before the split.
"""
