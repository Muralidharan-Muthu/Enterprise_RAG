"""
Hugging Face Spaces entrypoint for Enterprise RAG Backend.

With sdk: docker, this file is NOT auto-executed — the Dockerfile CMD runs
uvicorn app.main:app directly. This module is kept for optional manual use
and for the docling_models pre-caching utility.
"""
import threading
from pathlib import Path


def ensure_docling_models():
    """Ensure docling_models folder is downloaded and available."""
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


# Start docling models download in background thread on import
threading.Thread(target=ensure_docling_models, daemon=True).start()


if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("PORT", 7860))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)
