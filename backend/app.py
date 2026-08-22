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


def health_ping(msg: str) -> str:
    """Simple status check for Gradio UI."""
    return f"Enterprise RAG Backend Active: {msg or 'OK'}"


with gr.Blocks(title="Enterprise RAG Backend API") as demo:
    gr.Markdown("# 🚀 Enterprise RAG Backend API")
    gr.Markdown("""
    FastAPI backend is live and serving all Enterprise RAG API endpoints.
    - **Interactive API Docs (Swagger)**: [`/api/docs`](./api/docs)
    - **Health Check Endpoint**: [`/api/v1/health`](./api/v1/health)
    - **Query API Endpoint**: `/api/v1/query`
    """)
    inp = gr.Textbox(value="ping", label="Test Connection")
    out = gr.Textbox(label="Result")
    btn = gr.Button("Check Service Health", variant="primary")
    btn.click(fn=health_ping, inputs=inp, outputs=out)

# Mount Gradio once onto the FastAPI application at /ui
app = gr.mount_gradio_app(fastapi_app, demo, path="/ui")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
