"""Fine-Tune Lab: Full FT, LoRA, and RAG for local LLMs on 3090.

Three approaches to the same training data:
  1. Full fine-tuning — all weights trained, 8-bit AdamW to fit 24GB VRAM
  2. LoRA — adapter-only training (~2% params), merged for GGUF export
  3. RAG — vector index of training data, injected as context at query time
"""

import json
import hashlib
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
import numpy as np
import torch
from datasets import Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, TrainerCallback
from trl import SFTTrainer, SFTConfig

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────

HF_BASE_MODEL = os.environ.get("HF_BASE_MODEL", "unsloth/Llama-3.2-3B-Instruct")
OLLAMA_BASE_MODEL = os.environ.get("BASE_MODEL", "llama3.2:3b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:8080")

CHECKPOINTS_DIR = Path("/data/checkpoints")
GGUF_DIR = Path("/data/gguf")
TRAINING_DIR = Path("/data/training")
RAG_DIR = Path("/data/rag")
LLAMA_CPP_DIR = Path("/opt/llama.cpp")

# Ollama model names for each approach
MODEL_NAMES = {
    "base": OLLAMA_BASE_MODEL,
    "full": "llama3.2-ft",
    "lora": "llama3.2-lora",
    "rag": "llama3.2-rag",
}

SYSTEM_PROMPT = (
    "You are a home networking expert specializing in UniFi, VLANs, "
    "firewall configuration, DNS, and WiFi troubleshooting. "
    "Give specific, actionable answers."
)

RAG_SYSTEM_PROMPT = (
    "You are a home networking expert. You will be given relevant reference "
    "examples before each question. Use them to inform your answer, but adapt "
    "to the specific question asked. Give specific, actionable answers about "
    "UniFi, VLANs, firewall configuration, DNS, and WiFi troubleshooting."
)


@dataclass
class TrainingStatus:
    running: bool = False
    method: str = ""  # "full", "lora", "rag-index"
    current_round: int = 0
    current_epoch: float = 0.0
    total_epochs: int = 3
    loss: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    error: Optional[str] = None
    stage: str = "idle"  # idle, downloading, training, merging, converting, registering, indexing, done


class FineTuner:
    def __init__(self, ollama_url: str = OLLAMA_URL):
        self.ollama_url = ollama_url
        self.status = TrainingStatus()
        self.model = None
        self.tokenizer = None
        # RAG state (loaded lazily)
        self._rag_index = None
        self._rag_data = None
        self._rag_embedder = None

    # ── Helpers ────────────────────────────────────────────────

    def get_next_round(self, method: str) -> int:
        base_dir = CHECKPOINTS_DIR / method
        base_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(base_dir.glob("round_*"))
        if not existing:
            return 1
        return int(existing[-1].name.split("_")[1]) + 1

    def list_snapshots(self) -> list[dict]:
        CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
        snapshots = []
        for method_dir in sorted(CHECKPOINTS_DIR.iterdir()):
            if not method_dir.is_dir():
                continue
            method = method_dir.name
            for d in sorted(method_dir.glob("round_*")):
                meta_file = d / "training_meta.json"
                meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
                gguf_path = GGUF_DIR / method / d.name / "model.gguf"
                snapshots.append({
                    "method": method,
                    "name": d.name,
                    "path": str(d),
                    "has_gguf": gguf_path.exists(),
                    **meta,
                })
        # Also list RAG index if it exists
        rag_meta = RAG_DIR / "rag_meta.json"
        if rag_meta.exists():
            meta = json.loads(rag_meta.read_text())
            snapshots.append({"method": "rag", "name": "index", **meta})
        return snapshots

    def load_training_data(self, extra_data: Optional[list[dict]] = None) -> Dataset:
        all_data = self._load_raw_training_data()
        if extra_data:
            all_data.extend(extra_data)
        if not all_data:
            raise ValueError("No training data found in /data/training/")
        logger.info(f"Loaded {len(all_data)} training examples")
        return Dataset.from_list(all_data)

    def _load_raw_training_data(self) -> list[dict]:
        all_data = []
        TRAINING_DIR.mkdir(parents=True, exist_ok=True)
        for f in sorted(TRAINING_DIR.glob("*.json")):
            data = json.loads(f.read_text())
            if isinstance(data, list):
                all_data.extend(data)
        return all_data

    def format_chat(self, example: dict) -> str:
        return self.tokenizer.apply_chat_template(
            example["messages"], tokenize=False, add_generation_prompt=False
        )

    def _cleanup_gpu(self):
        if self.model is not None:
            del self.model
            self.model = None
        if self.tokenizer is not None:
            del self.tokenizer
            self.tokenizer = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def _convert_to_gguf(self, checkpoint_dir: Path, method: str, round_num: int) -> Optional[Path]:
        gguf_out_dir = GGUF_DIR / method / f"round_{round_num}"
        gguf_out_dir.mkdir(parents=True, exist_ok=True)
        gguf_path = gguf_out_dir / "model.gguf"

        convert_script = LLAMA_CPP_DIR / "convert_hf_to_gguf.py"
        if not convert_script.exists():
            logger.warning("llama.cpp convert script not found, skipping GGUF conversion")
            return None

        try:
            result = subprocess.run(
                [
                    "python3", str(convert_script),
                    str(checkpoint_dir),
                    "--outtype", "f16",
                    "--outfile", str(gguf_path),
                ],
                capture_output=True, text=True, timeout=600,
            )
            if result.returncode != 0:
                logger.error(f"GGUF conversion failed: {result.stderr}")
                return None
            logger.info(f"GGUF saved to {gguf_path} ({gguf_path.stat().st_size / 1e9:.1f}GB)")
            return gguf_path
        except Exception as e:
            logger.error(f"GGUF conversion error: {e}")
            return None

    def _register_gguf_with_ollama(self, gguf_path: Path, model_name: str):
        try:
            sha256 = hashlib.sha256()
            with open(gguf_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    sha256.update(chunk)
            digest = f"sha256:{sha256.hexdigest()}"

            with httpx.Client(timeout=600) as client:
                resp = client.head(f"{self.ollama_url}/api/blobs/{digest}")
                if resp.status_code != 200:
                    size_mb = gguf_path.stat().st_size / 1e6
                    logger.info(f"Uploading GGUF blob ({size_mb:.0f}MB)...")
                    with open(gguf_path, "rb") as f:
                        resp = client.post(
                            f"{self.ollama_url}/api/blobs/{digest}",
                            content=f,
                        )
                        resp.raise_for_status()
                    logger.info("Blob upload complete")
                else:
                    logger.info("Blob already exists in Ollama")

                logger.info(f"Creating Ollama model {model_name}...")
                resp = client.post(
                    f"{self.ollama_url}/api/create",
                    json={
                        "model": model_name,
                        "files": {gguf_path.name: digest},
                        "system": SYSTEM_PROMPT,
                        "stream": False,
                    },
                    timeout=300,
                )
                resp.raise_for_status()
            logger.info(f"Model {model_name} registered with Ollama")
        except Exception as e:
            logger.error(f"Ollama registration failed: {e}")

    def _register_ollama_alias(self, model_name: str, from_model: str, system: str):
        try:
            with httpx.Client(timeout=120) as client:
                logger.info(f"Creating Ollama model {model_name} from {from_model}...")
                resp = client.post(
                    f"{self.ollama_url}/api/create",
                    json={
                        "model": model_name,
                        "from": from_model,
                        "stream": False,
                        "system": system,
                    },
                    timeout=120,
                )
                resp.raise_for_status()
            logger.info(f"Model {model_name} registered")
        except Exception as e:
            logger.error(f"Ollama alias creation failed: {e}")

    def _unload_ollama_models(self):
        """Unload all models from Ollama VRAM to free GPU memory for training."""
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.get(f"{self.ollama_url}/api/ps")
                if resp.status_code == 200:
                    running = resp.json().get("models", [])
                    for m in running:
                        name = m.get("name", "")
                        logger.info(f"Unloading Ollama model {name} from VRAM...")
                        client.post(
                            f"{self.ollama_url}/api/generate",
                            json={"model": name, "keep_alive": 0},
                            timeout=30,
                        )
                    if running:
                        logger.info(f"Unloaded {len(running)} models from Ollama VRAM")
                    else:
                        logger.info("No Ollama models loaded in VRAM")
        except Exception as e:
            logger.warning(f"Could not unload Ollama models: {e}")

    def _init_status(self, method: str):
        self.status = TrainingStatus(
            running=True,
            method=method,
            started_at=time.time(),
            stage="downloading",
        )

    def _finish_status(self, error: Optional[str] = None):
        self.status.finished_at = time.time()
        self.status.running = False
        self.status.error = error
        self.status.stage = "done" if not error else "idle"

    # ── Full Fine-Tuning ──────────────────────────────────────

    def train_full(
        self,
        extra_data: Optional[list[dict]] = None,
        epochs: int = 3,
        learning_rate: float = 2e-5,
    ):
        """Full weight fine-tuning with 8-bit AdamW + gradient checkpointing.

        Memory budget for 3B bf16:
          Model: ~6GB + Gradients: ~6GB + 8-bit Adam: ~6GB + Activations: ~2GB = ~20GB
        """
        try:
            self._init_status("full")
            self._unload_ollama_models()
            round_num = self.get_next_round("full")
            self.status.current_round = round_num
            self.status.total_epochs = epochs

            logger.info(f"=== Full FT round {round_num} — {HF_BASE_MODEL} ===")

            # Load model
            self.tokenizer = AutoTokenizer.from_pretrained(HF_BASE_MODEL, trust_remote_code=True)
            if self.tokenizer.pad_token is None:
                self.tokenizer.pad_token = self.tokenizer.eos_token

            # Check available GPU memory and set limit (leave 1GB headroom for other processes)
            free_mem, total_mem = torch.cuda.mem_get_info(0)
            gpu_limit = f"{int((free_mem / 1e9) - 1)}GiB"
            logger.info(f"GPU memory: {free_mem/1e9:.1f}GB free / {total_mem/1e9:.1f}GB total — capping at {gpu_limit}")

            self.model = AutoModelForCausalLM.from_pretrained(
                HF_BASE_MODEL,
                torch_dtype=torch.bfloat16,
                device_map="auto",
                max_memory={0: gpu_limit, "cpu": "40GiB"},
                trust_remote_code=True,
            )

            # Load and format data
            self.status.stage = "training"
            dataset = self.load_training_data(extra_data)
            formatted = dataset.map(
                lambda x: {"text": self.format_chat(x)},
                remove_columns=dataset.column_names,
            )

            output_dir = CHECKPOINTS_DIR / "full" / f"round_{round_num}"
            output_dir.mkdir(parents=True, exist_ok=True)

            # Set CUDA memory config before training
            os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

            training_args = SFTConfig(
                output_dir=str(output_dir),
                num_train_epochs=epochs,
                per_device_train_batch_size=1,
                gradient_accumulation_steps=8,
                learning_rate=learning_rate,
                weight_decay=0.01,
                warmup_ratio=0.1,
                logging_steps=5,
                save_strategy="epoch",
                bf16=True,
                max_length=512,
                dataset_text_field="text",
                report_to="none",
                optim="adamw_bnb_8bit",
                gradient_checkpointing=True,
                gradient_checkpointing_kwargs={"use_reentrant": False},
            )

            status_ref = self.status

            class StatusCallback(TrainerCallback):
                def on_log(self, args, state, control, logs=None, **kwargs):
                    if logs:
                        status_ref.loss = logs.get("loss", status_ref.loss)
                        status_ref.current_epoch = state.epoch or 0.0

            trainer = SFTTrainer(
                model=self.model,
                args=training_args,
                train_dataset=formatted,
                processing_class=self.tokenizer,
                callbacks=[StatusCallback()],
            )

            logger.info("Training (full)...")
            trainer.train()

            trainer.save_model(str(output_dir))
            self.tokenizer.save_pretrained(str(output_dir))

            meta = {
                "method": "full",
                "round": round_num,
                "epochs": epochs,
                "learning_rate": learning_rate,
                "batch_size": "1x8 (grad accum)",
                "optimizer": "adamw_bnb_8bit",
                "num_examples": len(dataset),
                "final_loss": self.status.loss,
                "timestamp": time.time(),
                "hf_model": HF_BASE_MODEL,
            }
            (output_dir / "training_meta.json").write_text(json.dumps(meta, indent=2))

            # Convert to GGUF
            self.status.stage = "converting"
            gguf_path = self._convert_to_gguf(output_dir, "full", round_num)

            if gguf_path:
                self.status.stage = "registering"
                self._register_gguf_with_ollama(gguf_path, MODEL_NAMES["full"])

            del trainer
            self._cleanup_gpu()
            self._finish_status()
            logger.info(f"=== Full FT round {round_num} complete ===")

        except Exception as e:
            logger.exception("Full fine-tuning failed")
            self._cleanup_gpu()
            self._finish_status(str(e))
            raise

    # ── LoRA Fine-Tuning ──────────────────────────────────────

    def train_lora(
        self,
        extra_data: Optional[list[dict]] = None,
        epochs: int = 3,
        learning_rate: float = 2e-4,
        lora_rank: int = 16,
        lora_alpha: int = 32,
    ):
        """LoRA fine-tuning — adapter-only training on ~2% of parameters.

        Memory budget: Model (bf16): ~6GB + LoRA grads/optim: ~0.5GB = ~7GB total.
        Much faster and lighter than full FT. After training, adapter is merged
        into base model for GGUF export.
        """
        try:
            from peft import LoraConfig, get_peft_model, TaskType

            self._init_status("lora")
            self._unload_ollama_models()
            round_num = self.get_next_round("lora")
            self.status.current_round = round_num
            self.status.total_epochs = epochs

            logger.info(f"=== LoRA round {round_num} — r={lora_rank}, alpha={lora_alpha} ===")

            self.tokenizer = AutoTokenizer.from_pretrained(HF_BASE_MODEL, trust_remote_code=True)
            if self.tokenizer.pad_token is None:
                self.tokenizer.pad_token = self.tokenizer.eos_token

            self.model = AutoModelForCausalLM.from_pretrained(
                HF_BASE_MODEL,
                torch_dtype=torch.bfloat16,
                device_map="auto",
                trust_remote_code=True,
            )

            # Apply LoRA adapters
            lora_config = LoraConfig(
                r=lora_rank,
                lora_alpha=lora_alpha,
                lora_dropout=0.05,
                target_modules=[
                    "q_proj", "k_proj", "v_proj", "o_proj",
                    "gate_proj", "up_proj", "down_proj",
                ],
                task_type=TaskType.CAUSAL_LM,
                bias="none",
            )
            self.model = get_peft_model(self.model, lora_config)
            trainable, total = self.model.get_nb_trainable_parameters()
            logger.info(f"LoRA: {trainable:,} trainable / {total:,} total ({100*trainable/total:.1f}%)")

            self.status.stage = "training"
            dataset = self.load_training_data(extra_data)
            formatted = dataset.map(
                lambda x: {"text": self.format_chat(x)},
                remove_columns=dataset.column_names,
            )

            output_dir = CHECKPOINTS_DIR / "lora" / f"round_{round_num}"
            output_dir.mkdir(parents=True, exist_ok=True)

            training_args = SFTConfig(
                output_dir=str(output_dir),
                num_train_epochs=epochs,
                per_device_train_batch_size=2,
                gradient_accumulation_steps=4,
                learning_rate=learning_rate,
                weight_decay=0.01,
                warmup_ratio=0.1,
                logging_steps=5,
                save_strategy="epoch",
                bf16=True,
                max_length=2048,
                dataset_text_field="text",
                report_to="none",
            )

            status_ref = self.status

            class StatusCallback(TrainerCallback):
                def on_log(self, args, state, control, logs=None, **kwargs):
                    if logs:
                        status_ref.loss = logs.get("loss", status_ref.loss)
                        status_ref.current_epoch = state.epoch or 0.0

            trainer = SFTTrainer(
                model=self.model,
                args=training_args,
                train_dataset=formatted,
                processing_class=self.tokenizer,
                callbacks=[StatusCallback()],
            )

            logger.info("Training (LoRA)...")
            trainer.train()

            # Save adapter
            self.model.save_pretrained(str(output_dir))
            self.tokenizer.save_pretrained(str(output_dir))

            # Merge adapter into base for GGUF export
            self.status.stage = "merging"
            logger.info("Merging LoRA adapter into base model...")
            merged_dir = output_dir / "merged"
            merged_dir.mkdir(exist_ok=True)
            merged_model = self.model.merge_and_unload()
            merged_model.save_pretrained(str(merged_dir))
            self.tokenizer.save_pretrained(str(merged_dir))

            meta = {
                "method": "lora",
                "round": round_num,
                "epochs": epochs,
                "learning_rate": learning_rate,
                "lora_rank": lora_rank,
                "lora_alpha": lora_alpha,
                "trainable_params": trainable,
                "total_params": total,
                "trainable_pct": round(100 * trainable / total, 2),
                "num_examples": len(dataset),
                "final_loss": self.status.loss,
                "timestamp": time.time(),
                "hf_model": HF_BASE_MODEL,
            }
            (output_dir / "training_meta.json").write_text(json.dumps(meta, indent=2))

            self.status.stage = "converting"
            gguf_path = self._convert_to_gguf(merged_dir, "lora", round_num)

            if gguf_path:
                self.status.stage = "registering"
                self._register_gguf_with_ollama(gguf_path, MODEL_NAMES["lora"])

            del trainer, merged_model
            self._cleanup_gpu()
            self._finish_status()
            logger.info(f"=== LoRA round {round_num} complete ===")

        except Exception as e:
            logger.exception("LoRA fine-tuning failed")
            self._cleanup_gpu()
            self._finish_status(str(e))
            raise

    # ── RAG Index Builder ─────────────────────────────────────

    def build_rag_index(self, extra_data: Optional[list[dict]] = None):
        """Build FAISS vector index from training data for retrieval-augmented generation.

        Embeds all training questions with all-MiniLM-L6-v2 (384d), stores in FAISS.
        At query time, user question is embedded and top-3 similar Q&A pairs are
        injected into the prompt as context.
        """
        try:
            from sentence_transformers import SentenceTransformer
            import faiss

            self._init_status("rag-index")
            self.status.stage = "indexing"

            logger.info("=== Building RAG index ===")

            raw_data = self._load_raw_training_data()
            if extra_data:
                raw_data.extend(extra_data)
                # Also save to disk for future use
                TRAINING_DIR.mkdir(parents=True, exist_ok=True)
                import json as _json
                (TRAINING_DIR / "training_data.json").write_text(_json.dumps(extra_data, indent=2))
                logger.info(f"Saved {len(extra_data)} examples to {TRAINING_DIR}/training_data.json")
            if not raw_data:
                raise ValueError("No training data found")

            # Extract Q&A pairs
            qa_pairs = []
            questions = []
            for item in raw_data:
                msgs = item.get("messages", [])
                q = next((m["content"] for m in msgs if m["role"] == "user"), None)
                a = next((m["content"] for m in msgs if m["role"] == "assistant"), None)
                if q and a:
                    questions.append(q)
                    qa_pairs.append({"question": q, "answer": a})

            logger.info(f"Embedding {len(questions)} questions...")
            embedder = SentenceTransformer("all-MiniLM-L6-v2")
            embeddings = embedder.encode(questions, show_progress_bar=True, normalize_embeddings=True)
            embeddings = np.array(embeddings, dtype=np.float32)

            # FAISS inner-product index (cosine similarity since embeddings are L2-normalized)
            dim = embeddings.shape[1]
            index = faiss.IndexFlatIP(dim)
            index.add(embeddings)

            RAG_DIR.mkdir(parents=True, exist_ok=True)
            faiss.write_index(index, str(RAG_DIR / "index.faiss"))
            with open(RAG_DIR / "qa_pairs.json", "w") as f:
                json.dump(qa_pairs, f, indent=2)

            logger.info(f"RAG index saved: {len(qa_pairs)} pairs, {dim}d embeddings")

            # Register Ollama model (base model + RAG system prompt)
            self.status.stage = "registering"
            self._register_ollama_alias(MODEL_NAMES["rag"], OLLAMA_BASE_MODEL, RAG_SYSTEM_PROMPT)

            # Reset cached state
            self._rag_index = None
            self._rag_data = None
            self._rag_embedder = None

            meta = {
                "method": "rag",
                "num_pairs": len(qa_pairs),
                "embedding_model": "all-MiniLM-L6-v2",
                "embedding_dim": dim,
                "timestamp": time.time(),
            }
            (RAG_DIR / "rag_meta.json").write_text(json.dumps(meta, indent=2))

            self._finish_status()
            logger.info("=== RAG index complete ===")

        except Exception as e:
            logger.exception("RAG index build failed")
            self._finish_status(str(e))
            raise

    # ── RAG Query ─────────────────────────────────────────────

    def _ensure_rag_loaded(self):
        if self._rag_index is not None:
            return

        import faiss
        from sentence_transformers import SentenceTransformer

        index_path = RAG_DIR / "index.faiss"
        data_path = RAG_DIR / "qa_pairs.json"
        if not index_path.exists() or not data_path.exists():
            raise FileNotFoundError("RAG index not built. Run POST /train/rag-index first.")

        self._rag_index = faiss.read_index(str(index_path))
        with open(data_path) as f:
            self._rag_data = json.load(f)
        self._rag_embedder = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info(f"RAG loaded: {len(self._rag_data)} pairs")

    def rag_retrieve(self, query: str, top_k: int = 3) -> list[dict]:
        self._ensure_rag_loaded()
        query_emb = self._rag_embedder.encode([query], normalize_embeddings=True)
        query_emb = np.array(query_emb, dtype=np.float32)
        scores, indices = self._rag_index.search(query_emb, top_k)
        results = []
        for i, (score, idx) in enumerate(zip(scores[0], indices[0])):
            if idx < len(self._rag_data):
                results.append({"rank": i + 1, "score": float(score), **self._rag_data[idx]})
        return results

    def format_rag_prompt(self, query: str, examples: list[dict]) -> str:
        context = "\n\n".join(
            f"Example Q: {ex['question']}\nExample A: {ex['answer']}"
            for ex in examples
        )
        return f"Here are some relevant reference examples:\n\n{context}\n\nNow answer this question:\n{query}"

    # ── Legacy ────────────────────────────────────────────────

    def train(self, extra_data=None, epochs=3, learning_rate=2e-5, batch_size=8):
        """Legacy /train endpoint — routes to train_full."""
        self.train_full(extra_data=extra_data, epochs=epochs, learning_rate=learning_rate)

    def load_snapshot(self, method: str, round_name: str) -> bool:
        checkpoint_dir = CHECKPOINTS_DIR / method / round_name
        if not checkpoint_dir.exists():
            return False
        round_num = int(round_name.split("_")[1])
        if method == "lora" and (checkpoint_dir / "merged").exists():
            checkpoint_dir = checkpoint_dir / "merged"
        gguf_path = GGUF_DIR / method / round_name / "model.gguf"
        if not gguf_path.exists():
            gguf_path = self._convert_to_gguf(checkpoint_dir, method, round_num)
            if not gguf_path:
                return False
        self._register_gguf_with_ollama(gguf_path, MODEL_NAMES[method])
        return True
