"""FastAPI service for the Fine-Tune Lab — 3-way showdown edition.

Endpoints:
  POST /train/full      — Full weight fine-tuning (8-bit AdamW)
  POST /train/lora      — LoRA adapter training
  POST /train/rag-index — Build RAG vector index from training data
  POST /chat            — Chat with any model
  POST /chat/rag        — RAG-augmented chat (retrieve + generate)
  POST /compare         — Compare base vs any trained model
  POST /benchmark       — Run prompts through all 4 models
  GET  /status          — Training status
  GET  /snapshots       — List all training snapshots
  POST /load/{method}/{version} — Load specific snapshot
  GET  /gguf/{method}/{version} — Download GGUF file
"""

import asyncio
import json
import logging
import multiprocessing
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from trainer import FineTuner, MODEL_NAMES, OLLAMA_BASE_MODEL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:8080")
BASE_MODEL = os.environ.get("BASE_MODEL", "llama3.2:3b")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Fine-Tune Lab v2 starting — Ollama: {OLLAMA_URL}, Base: {BASE_MODEL}")
    logger.info(f"Models: {MODEL_NAMES}")
    yield
    logger.info("Fine-Tune Lab shutting down")


app = FastAPI(title="Fine-Tune Lab", version="2.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

tuner = FineTuner(ollama_url=OLLAMA_URL)

# Status file shared between parent and training subprocess
STATUS_FILE = "/data/training_status.json"


def _write_status(status_dict: dict):
    """Write status to shared file (called from subprocess)."""
    import json as _json
    with open(STATUS_FILE, "w") as f:
        _json.dump(status_dict, f)


def _read_status() -> dict:
    """Read status from shared file (called from parent)."""
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _run_training_subprocess(method: str, kwargs: dict):
    """Run training in a subprocess that fully releases GPU on exit."""
    from trainer import FineTuner
    t = FineTuner(ollama_url=os.environ.get("OLLAMA_URL", "http://localhost:8080"))

    # Hook into status updates — write to file instead of in-memory
    original_init = t._init_status
    original_finish = t._finish_status

    def patched_init(m):
        original_init(m)
        _write_status({
            "running": True, "method": m, "stage": t.status.stage,
            "current_round": t.status.current_round, "current_epoch": 0.0,
            "total_epochs": t.status.total_epochs, "loss": None, "error": None,
            "started_at": t.status.started_at,
        })

    def patched_finish(error=None):
        original_finish(error)
        _write_status({
            "running": False, "method": t.status.method, "stage": t.status.stage,
            "current_round": t.status.current_round,
            "current_epoch": t.status.current_epoch,
            "total_epochs": t.status.total_epochs,
            "loss": t.status.loss, "error": t.status.error,
            "started_at": t.status.started_at, "finished_at": t.status.finished_at,
        })

    t._init_status = patched_init
    t._finish_status = patched_finish

    # Periodic status writer (piggyback on trainer callback via a thread)
    import threading
    stop_event = threading.Event()

    def status_writer():
        while not stop_event.is_set():
            if t.status.running:
                _write_status({
                    "running": True, "method": t.status.method, "stage": t.status.stage,
                    "current_round": t.status.current_round,
                    "current_epoch": round(t.status.current_epoch, 2),
                    "total_epochs": t.status.total_epochs,
                    "loss": round(t.status.loss, 4) if t.status.loss else None,
                    "error": None, "started_at": t.status.started_at,
                })
            stop_event.wait(2)

    writer = threading.Thread(target=status_writer, daemon=True)
    writer.start()

    try:
        if method == "full":
            t.train_full(**kwargs)
        elif method == "lora":
            t.train_lora(**kwargs)
        elif method == "rag-index":
            t.build_rag_index(extra_data=kwargs.get("extra_data"))
    except Exception as e:
        _write_status({
            "running": False, "method": method, "stage": "idle",
            "current_round": t.status.current_round,
            "current_epoch": t.status.current_epoch,
            "total_epochs": t.status.total_epochs,
            "loss": t.status.loss, "error": str(e),
            "started_at": t.status.started_at, "finished_at": time.time(),
        })
    finally:
        stop_event.set()
    # Process exits here → ALL GPU memory released


_training_process: Optional[multiprocessing.Process] = None


def _start_training(method: str, kwargs: dict):
    global _training_process
    _training_process = multiprocessing.Process(
        target=_run_training_subprocess, args=(method, kwargs), daemon=True
    )
    _training_process.start()


def _is_training_running() -> bool:
    global _training_process
    if _training_process is not None and _training_process.is_alive():
        return True
    # Check status file as fallback
    status = _read_status()
    return status.get("running", False)


# ── Request/Response models ───────────────────────────────────

class TrainFullRequest(BaseModel):
    extra_data: Optional[list[dict]] = None
    epochs: int = 3
    learning_rate: float = 2e-5


class TrainLoraRequest(BaseModel):
    extra_data: Optional[list[dict]] = None
    epochs: int = 3
    learning_rate: float = 2e-4
    lora_rank: int = 16
    lora_alpha: int = 32


class ChatRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 512


class RagChatRequest(BaseModel):
    prompt: str
    top_k: int = 3
    temperature: float = 0.7
    max_tokens: int = 512


class CompareRequest(BaseModel):
    prompt: str
    models: Optional[list[str]] = None  # defaults to [base, latest trained]
    temperature: float = 0.7
    max_tokens: int = 512


class BenchmarkRequest(BaseModel):
    prompts: list[str]
    temperature: float = 0.7
    max_tokens: int = 512


# ── Helpers ───────────────────────────────────────────────────

async def ollama_generate(model: str, prompt: str, temperature: float = 0.7, max_tokens: int = 512) -> str:
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            },
        )
        resp.raise_for_status()
        return resp.json().get("response", "")


async def ollama_model_exists(model: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/show",
                json={"model": model},
            )
            return resp.status_code == 200
    except Exception:
        return False


# ── Endpoints ─────────────────────────────────────────────────

@app.get("/")
async def root():
    available = {}
    for name, model in MODEL_NAMES.items():
        available[name] = await ollama_model_exists(model)
    return {
        "service": "Fine-Tune Lab",
        "version": app.version,
        "ollama_url": OLLAMA_URL,
        "base_model": BASE_MODEL,
        "models": MODEL_NAMES,
        "available": available,
        "snapshots": len(tuner.list_snapshots()),
    }


@app.post("/train/full")
async def start_full_training(req: TrainFullRequest):
    if _is_training_running():
        raise HTTPException(409, "Training already in progress")

    _start_training("full", {
        "extra_data": req.extra_data, "epochs": req.epochs,
        "learning_rate": req.learning_rate,
    })
    return {
        "message": "Full fine-tuning started (subprocess — GPU released on completion)",
        "method": "full",
        "target_model": MODEL_NAMES["full"],
        "epochs": req.epochs,
    }


@app.post("/train/lora")
async def start_lora_training(req: TrainLoraRequest):
    if _is_training_running():
        raise HTTPException(409, "Training already in progress")

    _start_training("lora", {
        "extra_data": req.extra_data, "epochs": req.epochs,
        "learning_rate": req.learning_rate, "lora_rank": req.lora_rank,
        "lora_alpha": req.lora_alpha,
    })
    return {
        "message": "LoRA fine-tuning started (subprocess — GPU released on completion)",
        "method": "lora",
        "target_model": MODEL_NAMES["lora"],
        "epochs": req.epochs,
        "lora_rank": req.lora_rank,
        "lora_alpha": req.lora_alpha,
    }


class RagIndexRequest(BaseModel):
    extra_data: Optional[list[dict]] = None


@app.post("/train/rag-index")
async def build_rag_index(req: Optional[RagIndexRequest] = None):
    if _is_training_running():
        raise HTTPException(409, "Training already in progress")

    extra_data = req.extra_data if req else None
    _start_training("rag-index", {"extra_data": extra_data})
    return {
        "message": "RAG index build started (subprocess)",
        "method": "rag",
        "target_model": MODEL_NAMES["rag"],
    }


class UploadDataRequest(BaseModel):
    data: list[dict]
    filename: str = "training_data.json"


@app.post("/data/upload")
async def upload_training_data(req: UploadDataRequest):
    """Upload training data to the container's /data/training/ directory."""
    from pathlib import Path
    training_dir = Path("/data/training")
    training_dir.mkdir(parents=True, exist_ok=True)
    path = training_dir / req.filename
    path.write_text(json.dumps(req.data, indent=2))
    return {"message": f"Saved {len(req.data)} examples to {path}", "path": str(path)}


@app.get("/data/list")
async def list_training_data():
    """List training data files."""
    from pathlib import Path
    training_dir = Path("/data/training")
    training_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for f in sorted(training_dir.glob("*.json")):
        data = json.loads(f.read_text())
        count = len(data) if isinstance(data, list) else 1
        files.append({"name": f.name, "examples": count, "size_kb": round(f.stat().st_size / 1024, 1)})
    return {"files": files}


@app.post("/train")
async def start_training_legacy(req: TrainFullRequest):
    """Legacy endpoint — routes to full fine-tuning."""
    return await start_full_training(req)


@app.get("/status")
async def training_status():
    # Read from shared status file (written by training subprocess)
    s = _read_status()
    if not s:
        return {"running": False, "method": "", "stage": "idle"}

    # Check if subprocess actually died (process gone but status says running)
    if s.get("running") and _training_process is not None and not _training_process.is_alive():
        s["running"] = False
        s["stage"] = "idle"
        s["error"] = s.get("error") or "Training process exited unexpectedly"
        _write_status(s)

    result = {
        "running": s.get("running", False),
        "method": s.get("method", ""),
        "stage": s.get("stage", "idle"),
        "current_round": s.get("current_round", 0),
        "current_epoch": round(s.get("current_epoch", 0), 2),
        "total_epochs": s.get("total_epochs", 3),
        "loss": round(s["loss"], 4) if s.get("loss") else None,
        "error": s.get("error"),
    }
    started_at = s.get("started_at")
    if started_at:
        finished_at = s.get("finished_at") or (time.time() if s.get("running") else started_at)
        result["elapsed_seconds"] = round(finished_at - started_at, 1)
    return result


@app.post("/chat")
async def chat(req: ChatRequest):
    model = req.model or BASE_MODEL
    try:
        response = await ollama_generate(model, req.prompt, req.temperature, req.max_tokens)
        return {"model": model, "prompt": req.prompt, "response": response}
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Ollama error: {e.response.text}")


@app.post("/chat/rag")
async def chat_rag(req: RagChatRequest):
    """RAG-augmented chat: retrieve similar examples, inject as context, query model."""
    try:
        examples = tuner.rag_retrieve(req.prompt, top_k=req.top_k)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

    rag_prompt = tuner.format_rag_prompt(req.prompt, examples)

    try:
        response = await ollama_generate(
            MODEL_NAMES["rag"], rag_prompt, req.temperature, req.max_tokens
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Ollama error: {e.response.text}")

    return {
        "model": MODEL_NAMES["rag"],
        "prompt": req.prompt,
        "retrieved_examples": examples,
        "response": response,
    }


@app.post("/compare")
async def compare(req: CompareRequest):
    """Compare responses from multiple models side-by-side."""
    models_to_compare = req.models or [BASE_MODEL, MODEL_NAMES["full"]]

    tasks = {m: ollama_generate(m, req.prompt, req.temperature, req.max_tokens) for m in models_to_compare}

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    responses = {}
    for model, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            responses[model] = {"error": str(result)}
        else:
            responses[model] = {"response": result}

    return {"prompt": req.prompt, "responses": responses}


@app.post("/benchmark")
async def benchmark(req: BenchmarkRequest):
    """Run the same prompts through all 4 models. Returns structured comparison."""
    all_results = []

    for prompt in req.prompts:
        result = {"prompt": prompt, "responses": {}}

        # Build tasks for all models
        tasks = {}
        for name, model in MODEL_NAMES.items():
            if name == "rag":
                # RAG needs retrieval first
                try:
                    examples = tuner.rag_retrieve(prompt, top_k=3)
                    rag_prompt = tuner.format_rag_prompt(prompt, examples)
                    tasks[name] = ollama_generate(model, rag_prompt, req.temperature, req.max_tokens)
                    result.setdefault("rag_examples", examples)
                except Exception as e:
                    result["responses"][name] = {"error": str(e)}
                    continue
            else:
                tasks[name] = ollama_generate(model, prompt, req.temperature, req.max_tokens)

        if tasks:
            responses = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for name, resp in zip(tasks.keys(), responses):
                if isinstance(resp, Exception):
                    result["responses"][name] = {"error": str(resp)}
                else:
                    result["responses"][name] = {"text": resp}

        all_results.append(result)

    return {"results": all_results, "models": MODEL_NAMES}


@app.get("/snapshots")
async def list_snapshots():
    return {"snapshots": tuner.list_snapshots()}


@app.post("/load/{method}/{version}")
async def load_snapshot(method: str, version: str):
    if tuner.status.running:
        raise HTTPException(409, "Cannot load snapshot while training is in progress")
    if method not in ("full", "lora"):
        raise HTTPException(400, "Method must be 'full' or 'lora'")
    if not version.startswith("round_"):
        version = f"round_{version}"

    success = tuner.load_snapshot(method, version)
    if not success:
        raise HTTPException(404, f"Snapshot {method}/{version} not found or conversion failed")
    return {"message": f"Loaded {method}/{version}", "model": MODEL_NAMES[method]}


@app.get("/gguf/{method}/{version}")
async def download_gguf(method: str, version: str):
    if not version.startswith("round_"):
        version = f"round_{version}"
    from pathlib import Path
    gguf_path = Path("/data/gguf") / method / version / "model.gguf"
    if not gguf_path.exists():
        raise HTTPException(404, f"GGUF not found for {method}/{version}")
    return FileResponse(str(gguf_path), media_type="application/octet-stream", filename=f"{method}-{version}.gguf")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8881)
