import os
import gradio as gr
from app.main import app as fastapi_app


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
