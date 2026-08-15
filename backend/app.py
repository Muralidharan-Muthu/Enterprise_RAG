import os
import uvicorn
from app.main import app

try:
    import spaces

    @spaces.GPU
    def _zero_gpu_init():
        """Satisfies ZeroGPU startup check for Hugging Face Spaces."""
        return True
except ImportError:
    pass

if __name__ == "__main__":
    port = int(os.getenv("PORT", 7860))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, log_level="info")
