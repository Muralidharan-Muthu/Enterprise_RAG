import os
import gradio as gr
from app.main import app as fastapi_app

# Simple dashboard UI for the Space root view
with gr.Blocks(title="Multi-Store RAG Backend API") as demo:
    gr.Markdown("# 🚀 Multi-Store RAG Backend API is Running!")
    gr.Markdown("""
    The FastAPI backend is operational on Hugging Face ZeroGPU.
    
    - **API Documentation (Swagger UI)**: [`/docs`](./docs)
    - **OpenAPI Schema**: [`/openapi.json`](./openapi.json)
    - **Health Check**: [`/health`](./health)
    - **Query Endpoints**: `/api/v1/query` & `/api/v1/query/stream`
    - **Ingestion Endpoints**: `/api/v1/ingest/upload`
    """)

# Mount Gradio dashboard on root, preserving all FastAPI API endpoints
app = gr.mount_gradio_app(fastapi_app, demo, path="/")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
