import os
import threading
from pathlib import Path
import gradio as gr
import spaces
from starlette.routing import Mount

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.api.routes import health, ingestion, documents, query, chats, graph as graph_routes, auth as auth_routes


@spaces.GPU
def zerogpu_handler(msg: str) -> str:
    """Registered @spaces.GPU function for Hugging Face ZeroGPU supervisor."""
    return f"ZeroGPU Hardware Allocated: {msg or 'OK'}"


def ensure_docling_models():
    """Ensure docling_models folder is downloaded and available on Hugging Face Spaces."""
    artifacts_dir = Path(__file__).resolve().parent / "docling_models"
    model_artifacts = artifacts_dir / "model_artifacts"
    if not (model_artifacts.exists() and any(model_artifacts.iterdir())):
        try:
            from huggingface_hub import snapshot_download
            print("[Docling] Pre-caching docling-models into docling_models/ ...")
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            snapshot_download(
                repo_id="ds4sd/docling-models",
                revision="v2.1.0",
                local_dir=str(artifacts_dir),
            )
            print(f"[Docling] Models successfully ready at {artifacts_dir}")
        except Exception as e:
            print(f"[Docling] Model setup note: {e}")


# Start docling models download in background thread so app startup completes without blocking
threading.Thread(target=ensure_docling_models, daemon=True).start()


def check_api_health(msg: str) -> str:
    """Instant CPU health check returning in <1ms without any queue delays."""
    return f"⚡ Enterprise RAG API is Healthy, Online & Operational! (Message: '{msg or 'ping'}')"


# ── Build the Gradio UI ──────────────────────────────────────────────────
with gr.Blocks(title="Enterprise RAG Backend API") as demo:
    gr.Markdown("# 🚀 Enterprise RAG Backend API")
    gr.Markdown("""
    FastAPI backend is live and serving all Enterprise RAG API endpoints.
    - **Interactive API Docs (Swagger)**: [`/api/docs`](./api/docs)
    - **Health Check Endpoint**: [`/api/v1/health`](./api/v1/health)
    - **Query API Endpoint**: `/api/v1/query`
    """)
    with gr.Group():
        inp = gr.Textbox(value="ping", label="Test Connection", placeholder="Enter text to ping the backend...")
        out = gr.Textbox(label="Backend Response", interactive=False)
        with gr.Row():
            btn_fast = gr.Button("⚡ Instant API Health Check", variant="primary")
            btn_gpu = gr.Button("🚀 Test ZeroGPU Allocation", variant="secondary")
    
    btn_fast.click(fn=check_api_health, inputs=inp, outputs=out, queue=False)
    btn_gpu.click(fn=zerogpu_handler, inputs=inp, outputs=out)


# ── Mount FastAPI as ASGI sub-application under /api ─────────────────────
#
# Gradio's SSR catches ALL unmatched routes and returns HTML. By mounting
# our FastAPI app at "/api" via Starlette Mount(), requests to /api/* are
# routed to FastAPI BEFORE Gradio's catch-all sees them.
#
# The mount strips the "/api" prefix before passing to FastAPI, so FastAPI
# routes are registered WITHOUT the /api prefix (e.g. /v1/health, /docs).
from app.main import lifespan
from app.core.logging import setup_logging
from app.core.tracing import setup_tracing

setup_logging()

api_app = FastAPI(
    title="Enterprise RAG API",
    description="Enterprise Agentic RAG — Ingestion & multi-store document intelligence platform",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

setup_tracing(api_app)

api_app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_app.include_router(health.router, prefix="/v1")
api_app.include_router(ingestion.router, prefix="/v1/ingest", tags=["ingestion"])
api_app.include_router(documents.router, prefix="/v1/documents", tags=["documents"])
api_app.include_router(query.router, prefix="/v1", tags=["query"])
api_app.include_router(chats.router, prefix="/v1/chats", tags=["chats"])
api_app.include_router(graph_routes.router, prefix="/v1/graph", tags=["graph"])
api_app.include_router(auth_routes.router, prefix="/v1/auth", tags=["auth"])


@api_app.get("/", include_in_schema=False)
def api_root():
    return {"service": "Enterprise RAG API", "version": "1.0.0", "docs": "/api/docs"}


# Insert FastAPI mount BEFORE Gradio's catch-all SSR routes
demo.app.routes.insert(0, Mount("/api", app=api_app))


if __name__ == "__main__":
    port = int(os.getenv("PORT", 7860))
    demo.launch(server_name="0.0.0.0", server_port=port)
