import os
import threading
from pathlib import Path
import gradio as gr
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.api.routes import health, ingestion, documents, query, chats, graph as graph_routes, auth as auth_routes
from app.main import lifespan

try:
    import spaces
    gpu_decorator = spaces.GPU
except Exception:
    def gpu_decorator(*args, **kwargs):
        def wrapper(fn):
            return fn
        return wrapper if not args or not callable(args[0]) else args[0]


@gpu_decorator(duration=10)
def zerogpu_handler(msg: str) -> str:
    """Registered @spaces.GPU function for ZeroGPU supervisor compliance."""
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
    """Instant health check returning in <1ms without any queue delays."""
    return f"⚡ Enterprise RAG API is Healthy, Online & Operational! (Message: '{msg or 'ping'}')"


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


# Add CORS middleware to demo.app
demo.app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register all API routes onto demo.app
demo.app.include_router(health.router, prefix="/api/v1")
demo.app.include_router(ingestion.router, prefix="/api/v1/ingest", tags=["ingestion"])
demo.app.include_router(documents.router, prefix="/api/v1/documents", tags=["documents"])
demo.app.include_router(query.router, prefix="/api/v1", tags=["query"])
demo.app.include_router(chats.router, prefix="/api/v1/chats", tags=["chats"])
demo.app.include_router(graph_routes.router, prefix="/api/v1/graph", tags=["graph"])
demo.app.include_router(auth_routes.router, prefix="/api/v1/auth", tags=["auth"])

# Wire lifespan into demo.app
demo.app.router.lifespan_context = lifespan

if __name__ == "__main__":
    port = int(os.getenv("PORT", 7860))
    demo.launch(server_name="0.0.0.0", server_port=port)
