"""Fine-tuning logic for Qwen3-0.6B using transformers + trl."""

import json
import hashlib
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
import torch
from datasets import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    TrainingArguments,
)
from trl import SFTTrainer, SFTConfig

logger = logging.getLogger(__name__)

BASE_MODEL = "Qwen/Qwen3-0.6B"
OLLAMA_URL = "http://localhost:8080"  # overridden by env var
CHECKPOINTS_DIR = Path("/data/checkpoints")
GGUF_DIR = Path("/data/gguf")
TRAINING_DIR = Path("/data/training")
LLAMA_CPP_DIR = Path("/opt/llama.cpp")


@dataclass
class TrainingStatus:
    running: bool = False
    current_round: int = 0
    total_rounds: int = 0
    current_epoch: float = 0.0
    total_epochs: int = 3
    loss: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    error: Optional[str] = None
    stage: str = "idle"  # idle, downloading, training, converting, registering, done


class FineTuner:
    def __init__(self, ollama_url: str = OLLAMA_URL):
        self.ollama_url = ollama_url
        self.status = TrainingStatus()
        self.model = None
        self.tokenizer = None

    def get_current_round(self) -> int:
        """Determine the next round number from existing checkpoints."""
        CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
        existing = sorted(CHECKPOINTS_DIR.glob("round_*"))
        if not existing:
            return 1
        last = existing[-1].name  # round_3
        return int(last.split("_")[1]) + 1

    def list_snapshots(self) -> list[dict]:
        """List all saved training snapshots."""
        CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
        snapshots = []
        for d in sorted(CHECKPOINTS_DIR.glob("round_*")):
            meta_file = d / "training_meta.json"
            meta = {}
            if meta_file.exists():
                meta = json.loads(meta_file.read_text())
            snapshots.append({
                "name": d.name,
                "path": str(d),
                "has_gguf": (GGUF_DIR / d.name / "model.gguf").exists(),
                **meta,
            })
        return snapshots

    def load_training_data(self, extra_data: Optional[list[dict]] = None) -> Dataset:
        """Load training data from disk + optional extra data."""
        all_data = []

        # Load from training directory
        TRAINING_DIR.mkdir(parents=True, exist_ok=True)
        for f in sorted(TRAINING_DIR.glob("*.json")):
            data = json.loads(f.read_text())
            if isinstance(data, list):
                all_data.extend(data)

        # Add extra data from request
        if extra_data:
            all_data.extend(extra_data)

        if not all_data:
            raise ValueError("No training data found")

        logger.info(f"Loaded {len(all_data)} training examples")
        return Dataset.from_list(all_data)

    def format_chat(self, example: dict) -> str:
        """Format a chat example into a training string using the tokenizer's chat template."""
        messages = example["messages"]
        return self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=False
        )

    def train(
        self,
        extra_data: Optional[list[dict]] = None,
        epochs: int = 3,
        learning_rate: float = 2e-5,
        batch_size: int = 8,
    ):
        """Run a full training round."""
        try:
            self.status = TrainingStatus(
                running=True,
                started_at=time.time(),
                stage="downloading",
            )
            round_num = self.get_current_round()
            self.status.current_round = round_num
            self.status.total_epochs = epochs

            logger.info(f"=== Starting training round {round_num} ===")

            # Load model and tokenizer
            logger.info(f"Loading model {BASE_MODEL}...")
            self.tokenizer = AutoTokenizer.from_pretrained(
                BASE_MODEL, trust_remote_code=True
            )
            if self.tokenizer.pad_token is None:
                self.tokenizer.pad_token = self.tokenizer.eos_token

            self.model = AutoModelForCausalLM.from_pretrained(
                BASE_MODEL,
                torch_dtype=torch.bfloat16,
                device_map="auto",
                trust_remote_code=True,
            )

            # Load latest checkpoint if continuing
            latest_checkpoint = self._get_latest_checkpoint()
            if latest_checkpoint:
                logger.info(f"Loading weights from {latest_checkpoint}")
                self.model = AutoModelForCausalLM.from_pretrained(
                    str(latest_checkpoint),
                    torch_dtype=torch.bfloat16,
                    device_map="auto",
                    trust_remote_code=True,
                )

            # Load data
            self.status.stage = "training"
            dataset = self.load_training_data(extra_data)

            # Format dataset
            formatted = dataset.map(
                lambda x: {"text": self.format_chat(x)},
                remove_columns=dataset.column_names,
            )

            # Training config
            output_dir = CHECKPOINTS_DIR / f"round_{round_num}"
            output_dir.mkdir(parents=True, exist_ok=True)

            training_args = SFTConfig(
                output_dir=str(output_dir),
                num_train_epochs=epochs,
                per_device_train_batch_size=batch_size,
                gradient_accumulation_steps=1,
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

            # Custom callback to update status
            from transformers import TrainerCallback

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

            logger.info("Starting training...")
            trainer.train()

            # Save the full model
            logger.info(f"Saving model to {output_dir}")
            trainer.save_model(str(output_dir))
            self.tokenizer.save_pretrained(str(output_dir))

            # Save metadata
            meta = {
                "round": round_num,
                "epochs": epochs,
                "learning_rate": learning_rate,
                "batch_size": batch_size,
                "num_examples": len(dataset),
                "final_loss": self.status.loss,
                "timestamp": time.time(),
                "base_model": BASE_MODEL,
            }
            (output_dir / "training_meta.json").write_text(json.dumps(meta, indent=2))

            # Convert to GGUF
            self.status.stage = "converting"
            logger.info("Converting to GGUF...")
            gguf_path = self._convert_to_gguf(output_dir, round_num)

            # Register with Ollama
            if gguf_path:
                self.status.stage = "registering"
                logger.info("Registering with Ollama...")
                self._register_with_ollama(gguf_path, round_num)

            # Cleanup GPU memory
            del self.model
            del trainer
            torch.cuda.empty_cache()
            self.model = None

            self.status.stage = "done"
            self.status.finished_at = time.time()
            self.status.running = False
            logger.info(f"=== Round {round_num} complete ===")

        except Exception as e:
            logger.exception("Training failed")
            self.status.error = str(e)
            self.status.running = False
            self.status.stage = "idle"
            # Cleanup
            if self.model is not None:
                del self.model
                self.model = None
                torch.cuda.empty_cache()
            raise

    def _get_latest_checkpoint(self) -> Optional[Path]:
        """Get the latest checkpoint directory."""
        existing = sorted(CHECKPOINTS_DIR.glob("round_*"))
        if not existing:
            return None
        latest = existing[-1]
        if (latest / "config.json").exists():
            return latest
        return None

    def _convert_to_gguf(self, checkpoint_dir: Path, round_num: int) -> Optional[Path]:
        """Convert HuggingFace checkpoint to GGUF format."""
        gguf_out_dir = GGUF_DIR / f"round_{round_num}"
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
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                logger.error(f"GGUF conversion failed: {result.stderr}")
                return None
            logger.info(f"GGUF saved to {gguf_path}")
            return gguf_path
        except Exception as e:
            logger.error(f"GGUF conversion error: {e}")
            return None

    def _register_with_ollama(self, gguf_path: Path, round_num: int):
        """Upload GGUF to Ollama and create a model."""
        model_name = f"qwen3:tuned-r{round_num}"

        try:
            # Calculate digest
            sha256 = hashlib.sha256()
            with open(gguf_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    sha256.update(chunk)
            digest = f"sha256:{sha256.hexdigest()}"

            # Upload blob
            with httpx.Client(timeout=600) as client:
                # Check if blob exists
                resp = client.head(f"{self.ollama_url}/api/blobs/{digest}")
                if resp.status_code != 200:
                    logger.info(f"Uploading GGUF blob ({gguf_path.stat().st_size / 1e6:.0f}MB)...")
                    with open(gguf_path, "rb") as f:
                        resp = client.post(
                            f"{self.ollama_url}/api/blobs/{digest}",
                            content=f.read(),
                        )
                        resp.raise_for_status()

                # Create model
                modelfile = f"""FROM @{digest}
SYSTEM You are a home networking expert specializing in UniFi, VLANs, firewall configuration, DNS, and WiFi troubleshooting. Give specific, actionable answers."""

                logger.info(f"Creating Ollama model {model_name}...")
                resp = client.post(
                    f"{self.ollama_url}/api/create",
                    json={"name": model_name, "modelfile": modelfile},
                    timeout=120,
                )
                resp.raise_for_status()

            logger.info(f"Model {model_name} registered with Ollama")

        except Exception as e:
            logger.error(f"Ollama registration failed: {e}")
            # Non-fatal - the checkpoint is still saved

    def load_snapshot(self, round_name: str) -> bool:
        """Load a specific snapshot and register it as the active tuned model."""
        checkpoint_dir = CHECKPOINTS_DIR / round_name
        if not checkpoint_dir.exists():
            return False

        round_num = int(round_name.split("_")[1])
        gguf_path = GGUF_DIR / round_name / "model.gguf"

        if not gguf_path.exists():
            # Need to convert first
            gguf_path = self._convert_to_gguf(checkpoint_dir, round_num)
            if not gguf_path:
                return False

        # Register as "qwen3:tuned" (the active model)
        self._register_with_ollama(gguf_path, round_num)
        return True
