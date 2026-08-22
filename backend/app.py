import os
import gradio as gr
import spaces
from app.main import app as fastapi_app

@spaces.GPU
def gpu_ping(msg: str):
    """Registered @spaces.GPU function for Hugging Face ZeroGPU supervisor."""
    return f"ZeroGPU Active: {msg}"

with gr.Blocks(title="Enterprise RAG Backend API") as demo:
    gr.Markdown("# 🚀 Enterprise RAG Backend API")
    gr.Markdown("""
    The FastAPI backend is operational on Hugging Face ZeroGPU.
    - **Swagger UI**: [`/api/docs`](./api/docs)
    - **Health Check**: [`/api/v1/health`](./api/v1/health)
    - **Query API**: `/api/v1/query`
    """)
    inp = gr.Textbox(value="ping", label="Test Connection")
    out = gr.Textbox(label="Result")
    btn = gr.Button("Check ZeroGPU Status")
    btn.click(fn=gpu_ping, inputs=inp, outputs=out)

# Mount Gradio UI at /gradio and /ui while keeping all FastAPI routes at root
app = gr.mount_gradio_app(fastapi_app, demo, path="/gradio")
gr.mount_gradio_app(fastapi_app, demo, path="/ui")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7860))
    uvicorn.run(fastapi_app, host="0.0.0.0", port=port)
