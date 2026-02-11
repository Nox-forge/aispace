"""FastAPI service for the Fine-Tune Lab."""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from trainer import FineTuner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:8080")
BASE_MODEL = os.environ.get("BASE_MODEL", "qwen3:0.6b")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Fine-Tune Lab starting — Ollama: {OLLAMA_URL}, Base: {BASE_MODEL}")
    yield
    logger.info("Fine-Tune Lab shutting down")


app = FastAPI(title="Fine-Tune Lab", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

tuner = FineTuner(ollama_url=OLLAMA_URL)


# --- Request/Response models ---

class TrainRequest(BaseModel):
    extra_data: Optional[list[dict]] = None
    epochs: int = 3
    learning_rate: float = 2e-5
    batch_size: int = 8


class ChatRequest(BaseModel):
    prompt: str
    model: Optional[str] = None  # defaults to latest tuned
    temperature: float = 0.7
    max_tokens: int = 512


class CompareRequest(BaseModel):
    prompt: str
    temperature: float = 0.7
    max_tokens: int = 512


# --- Helper ---

async def ollama_generate(model: str, prompt: str, temperature: float = 0.7, max_tokens: int = 512) -> str:
    """Query Ollama for a completion."""
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                },
            },
        )
        resp.raise_for_status()
        return resp.json().get("response", "")


def get_latest_tuned_model() -> Optional[str]:
    """Get the name of the latest fine-tuned model in Ollama."""
    snapshots = tuner.list_snapshots()
    if not snapshots:
        return None
    latest = snapshots[-1]
    round_num = latest["name"].split("_")[1]
    return f"qwen3:tuned-r{round_num}"


# --- Endpoints ---

@app.get("/")
async def root():
    return {
        "service": "Fine-Tune Lab",
        "status": "running",
        "ollama_url": OLLAMA_URL,
        "base_model": BASE_MODEL,
        "latest_tuned": get_latest_tuned_model(),
        "snapshots": len(tuner.list_snapshots()),
    }


@app.post("/train")
async def start_training(req: TrainRequest, background_tasks: BackgroundTasks):
    """Start a new training round."""
    if tuner.status.running:
        raise HTTPException(409, "Training already in progress")

    def run_training():
        tuner.train(
            extra_data=req.extra_data,
            epochs=req.epochs,
            learning_rate=req.learning_rate,
            batch_size=req.batch_size,
        )

    background_tasks.add_task(run_training)
    return {
        "message": "Training started",
        "round": tuner.get_current_round(),
        "epochs": req.epochs,
    }


@app.get("/status")
async def training_status():
    """Get current training status."""
    s = tuner.status
    result = {
        "running": s.running,
        "stage": s.stage,
        "current_round": s.current_round,
        "current_epoch": round(s.current_epoch, 2),
        "total_epochs": s.total_epochs,
        "loss": round(s.loss, 4) if s.loss else None,
        "error": s.error,
    }
    if s.started_at:
        elapsed = (s.finished_at or time.time()) - s.started_at
        result["elapsed_seconds"] = round(elapsed, 1)
    return result


@app.post("/chat")
async def chat(req: ChatRequest):
    """Chat with the fine-tuned model (or specify a model)."""
    model = req.model or get_latest_tuned_model()
    if not model:
        raise HTTPException(404, "No fine-tuned model available yet. Run /train first.")

    try:
        response = await ollama_generate(model, req.prompt, req.temperature, req.max_tokens)
        return {"model": model, "prompt": req.prompt, "response": response}
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Ollama error: {e.response.text}")


@app.post("/compare")
async def compare(req: CompareRequest):
    """Send the same prompt to base and tuned models, return both responses."""
    tuned_model = get_latest_tuned_model()
    if not tuned_model:
        raise HTTPException(404, "No fine-tuned model available yet. Run /train first.")

    # Query both in parallel
    base_task = ollama_generate(BASE_MODEL, req.prompt, req.temperature, req.max_tokens)
    tuned_task = ollama_generate(tuned_model, req.prompt, req.temperature, req.max_tokens)

    try:
        base_resp, tuned_resp = await asyncio.gather(base_task, tuned_task)
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Ollama error: {e.response.text}")

    return {
        "prompt": req.prompt,
        "base": {"model": BASE_MODEL, "response": base_resp},
        "tuned": {"model": tuned_model, "response": tuned_resp},
    }


@app.get("/snapshots")
async def list_snapshots():
    """List all training snapshots."""
    return {"snapshots": tuner.list_snapshots()}


@app.post("/load/{version}")
async def load_snapshot(version: str):
    """Load a specific snapshot as the active tuned model."""
    if tuner.status.running:
        raise HTTPException(409, "Cannot load snapshot while training is in progress")

    if not version.startswith("round_"):
        version = f"round_{version}"

    success = tuner.load_snapshot(version)
    if not success:
        raise HTTPException(404, f"Snapshot {version} not found or conversion failed")

    return {"message": f"Loaded snapshot {version}", "model": get_latest_tuned_model()}


@app.get("/gguf/{version}")
async def download_gguf(version: str):
    """Download the GGUF file for a specific round."""
    if not version.startswith("round_"):
        version = f"round_{version}"
    from pathlib import Path
    gguf_path = Path("/data/gguf") / version / "model.gguf"
    if not gguf_path.exists():
        raise HTTPException(404, f"GGUF not found for {version}")
    return FileResponse(str(gguf_path), media_type="application/octet-stream", filename=f"{version}.gguf")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8881)
