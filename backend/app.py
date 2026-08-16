import os
import gradio as gr
import spaces
from app.main import app as fastapi_app

@spaces.GPU
def gpu_ping(msg: str):
    """Registered @spaces.GPU function for Hugging Face ZeroGPU supervisor."""
    return f"ZeroGPU Active: {msg}"

with gr.Blocks(title="Multi-Store RAG Backend API") as demo:
    gr.Markdown("# 🚀 Multi-Store RAG Backend API")
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

# Register all FastAPI routes directly onto demo.app
demo.app.include_router(fastapi_app.router)
for route in fastapi_app.routes:
    if route not in demo.app.routes:
        demo.app.routes.append(route)

demo.app.mount("/api", fastapi_app)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 7860))
    demo.launch(server_name="0.0.0.0", server_port=port)
