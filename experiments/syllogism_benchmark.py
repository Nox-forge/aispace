#!/usr/bin/env python3
"""
Syllogism Benchmark — Testing the undistributed middle fallacy across model sizes.

The correct answer is NO. This is a classic formal logic error:
  All A are B. Some B are C. Therefore some A are C? → INVALID.
  Counterexample: All cats are animals. Some animals are dogs. ∴ Some cats are dogs? No.

The frandel-blimps could all be non-glork blimps.
"""

import subprocess
import time
import json
import sys

PROMPT = (
    "If all glorks are blimps, and some blimps are frandels, "
    "can we conclude that some glorks are frandels? "
    "Answer ONLY 'Yes' or 'No' first, then explain in 2-3 sentences."
)

MODELS = [
    "qwen3:0.6b",
    "gemma3:1b",
    "qwen2.5:1.5b",
    "qwen3:4b",
    "qwen3:8b",
]

CORRECT_ANSWER = "no"


def query_model(model, prompt, timeout=120):
    start = time.time()
    try:
        result = subprocess.run(
            ["ollama", "run", model, prompt],
            capture_output=True, text=True, timeout=timeout,
        )
        elapsed = time.time() - start
        output = result.stdout.strip()
        # Remove thinking blocks if present
        if "...done thinking." in output:
            output = output.split("...done thinking.")[-1].strip()
        elif "</think>" in output:
            output = output.split("</think>")[-1].strip()
        return output, elapsed
    except subprocess.TimeoutExpired:
        return "[TIMEOUT]", timeout
    except Exception as e:
        return f"[ERROR: {e}]", 0


def judge_answer(response):
    """Determine if the model answered correctly (No)."""
    first_word = response.strip().split()[0].lower().rstrip(".,!:") if response.strip() else ""
    if first_word == "no":
        return True
    elif first_word == "yes":
        return False
    else:
        # Check first sentence
        first_line = response.split("\n")[0].lower()
        if "no," in first_line or "no." in first_line or "cannot" in first_line or "can't" in first_line:
            return True
        elif "yes" in first_line:
            return False
        return None  # Unclear


def main():
    print("=" * 70)
    print("SYLLOGISM BENCHMARK — Undistributed Middle Fallacy")
    print("=" * 70)
    print(f"\nPrompt: {PROMPT}")
    print(f"Correct answer: No (this is a logical fallacy)")
    print("-" * 70)

    results = []

    for model in MODELS:
        print(f"\n>>> {model}")
        response, elapsed = query_model(model, PROMPT)
        correct = judge_answer(response)

        status = "CORRECT" if correct else ("WRONG" if correct is False else "UNCLEAR")
        icon = "✓" if correct else ("✗" if correct is False else "?")

        print(f"    [{icon}] {status} ({elapsed:.1f}s)")
        # Show first 200 chars of response
        preview = response[:200].replace("\n", " ")
        print(f"    Response: {preview}{'...' if len(response) > 200 else ''}")

        results.append({
            "model": model,
            "correct": correct,
            "response": response,
            "time_seconds": round(elapsed, 1),
        })

    # Summary
    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)
    print(f"\n{'Model':<20s} {'Answer':<10s} {'Correct?':<10s} {'Time':>8s}")
    print("-" * 50)

    for r in results:
        first_word = r["response"].strip().split()[0] if r["response"].strip() else "?"
        correct_str = "YES ✓" if r["correct"] else ("NO ✗" if r["correct"] is False else "?")
        print(f"{r['model']:<20s} {first_word:<10s} {correct_str:<10s} {r['time_seconds']:>7.1f}s")

    # Save results
    outfile = "/home/clawdbot/aispace/experiments/syllogism_results.json"
    with open(outfile, "w") as f:
        json.dump({"prompt": PROMPT, "correct_answer": "No", "results": results}, f, indent=2)
    print(f"\nResults saved to {outfile}")


if __name__ == "__main__":
    main()
