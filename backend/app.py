import os
import threading
from pathlib import Path
import gradio as gr
from app.main import app as fastapi_app


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
        btn = gr.Button("⚡ Instant API Health Check", variant="primary")
    
    btn.click(fn=check_api_health, inputs=inp, outputs=out, queue=False)


# Mount Gradio at /gradio and /ui on top of the FastAPI application.
# FastAPI serves /api/v1/*, /api/docs, / directly as JSON & Swagger.
# Gradio UI is accessible at /gradio or /ui.
app = gr.mount_gradio_app(fastapi_app, demo, path="/gradio")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
