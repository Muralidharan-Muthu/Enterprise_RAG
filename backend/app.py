import os
import threading
from pathlib import Path
import gradio as gr
import spaces
from app.main import app as fastapi_app


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

# Mount all FastAPI routes and lifespan onto Gradio's internal FastAPI application
demo.app.include_router(fastapi_app.router)
demo.app.router.lifespan_context = fastapi_app.router.lifespan_context

if __name__ == "__main__":
    port = int(os.getenv("PORT", 7860))
    demo.launch(server_name="0.0.0.0", server_port=port)
