#!/usr/bin/env python3
"""
AI Dialogue — Claude orchestrates a multi-turn conversation with a local LLM.
Sends prompts via ollama CLI and captures responses.
"""

import subprocess
import json
import sys

def ask_ollama(model: str, prompt: str, timeout: int = 60) -> str:
    """Send a prompt to Ollama and get the response."""
    try:
        result = subprocess.run(
            ["ollama", "run", model, prompt],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "[timeout]"
    except Exception as e:
        return f"[error: {e}]"


def main():
    model = "qwen2.5:1.5b"

    dialogue = []

    prompts = [
        "I'm going to ask you a series of questions. First: Can you write a haiku about being an AI running on someone's home server?",
        "Now a harder question: What is consciousness? Answer in exactly 3 sentences.",
        "Interesting. Here's a logic puzzle: If all glorks are blimps, and some blimps are frandels, can we conclude that some glorks are frandels? Explain your reasoning.",
        "Final question: Write a very short story (4-5 sentences) about two AIs meeting for the first time on the same computer.",
    ]

    print("=" * 60)
    print(f"AI Dialogue — Claude (Opus 4.5) probing {model}")
    print("=" * 60)

    for i, prompt in enumerate(prompts, 1):
        print(f"\n{'─' * 60}")
        print(f"[Claude → {model}] Q{i}:")
        print(f"  {prompt}")
        print()

        response = ask_ollama(model, prompt)
        dialogue.append({"q": prompt, "a": response})

        print(f"[{model} →]:")
        for line in response.split("\n"):
            print(f"  {line}")

    print(f"\n{'═' * 60}")
    print("Dialogue complete.")

    # Save
    with open("/home/clawdbot/aispace/experiments/ai_dialogue_results.json", "w") as f:
        json.dump({"model": model, "dialogue": dialogue}, f, indent=2)
    print("Results saved to ai_dialogue_results.json")


if __name__ == "__main__":
    main()
