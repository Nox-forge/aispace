#!/usr/bin/env python3
"""
llm - CLI for querying Ollama models on the 3090 (Alex-PcLinux).
Built by Nox for the aispace workspace.

Usage:
    llm "prompt"                    # Chat with default model (qwen3:32b)
    llm -m phi4:14b "prompt"        # Use specific model
    llm -l                          # List available models
    llm -b "prompt"                 # Benchmark prompt across all models
    llm --thinking "prompt"         # Show thinking tokens (for R1/QwQ)
    llm -s                          # Show currently loaded model stats
    llm --bench-suite               # Run standard reasoning benchmark
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
import textwrap

OLLAMA_URL = "http://192.168.53.108:11434"
DEFAULT_MODEL = "qwen3:32b"

# Models that use chain-of-thought thinking
THINKING_MODELS = {"deepseek-r1", "qwq", "glm-4.7"}

# Standard benchmark prompts
BENCH_SUITE = {
    "syllogism": {
        "prompt": "All roses are flowers. Some flowers fade quickly. Can we conclude that some roses fade quickly? Answer yes or no with a one sentence explanation.",
        "correct": "no",
        "category": "Logic",
    },
    "math": {
        "prompt": "What is 27 * 43? Show only the final number.",
        "correct": "1161",
        "category": "Math",
    },
    "counterfactual": {
        "prompt": "If the Earth had two moons, name one specific effect on ocean tides. One sentence only.",
        "correct": None,  # qualitative
        "category": "Reasoning",
    },
    "code": {
        "prompt": "Write a Python one-liner that checks if a string is a palindrome. Just the code, no explanation.",
        "correct": None,  # qualitative
        "category": "Code",
    },
    "ambiguity": {
        "prompt": "A bat and a ball cost $1.10 together. The bat costs $1.00 more than the ball. How much does the ball cost? Just the number.",
        "correct": "0.05",
        "category": "Logic",
    },
}

# ─── Terminal formatting ───────────────────────────────────────────────────────

class C:
    """ANSI color codes."""
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    MAGENTA = "\033[35m"
    RESET = "\033[0m"


def header(text):
    print(f"\n{C.BOLD}{C.CYAN}{'─' * 60}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}  {text}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}{'─' * 60}{C.RESET}")


# ─── Ollama API ────────────────────────────────────────────────────────────────

def api_get(endpoint):
    """GET request to Ollama API."""
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}{endpoint}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"{C.RED}Error: {e}{C.RESET}", file=sys.stderr)
        return None


def api_post(endpoint, data, timeout=600):
    """POST request to Ollama API."""
    payload = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}{endpoint}",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req, timeout=timeout)


def generate(model, prompt, stream=True, show_thinking=False,
             num_predict=4096, num_ctx=4096, kv_cache_type=None):
    """Generate a response from a model. Returns (response, thinking, stats)."""
    options = {"num_predict": num_predict, "num_ctx": num_ctx}
    if kv_cache_type:
        options["kv_cache_type"] = kv_cache_type

    data = {
        "model": model,
        "prompt": prompt,
        "stream": stream,
        "options": options,
    }

    response_text = ""
    thinking_text = ""
    stats = {}

    try:
        with api_post("/api/generate", data) as resp:
            if stream:
                for raw_line in resp:
                    line = raw_line.decode().strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    token = obj.get("response", "")
                    think = obj.get("thinking", "")

                    if think:
                        thinking_text += think
                        if show_thinking:
                            print(f"{C.DIM}{think}{C.RESET}", end="", flush=True)
                    elif token:
                        response_text += token
                        print(token, end="", flush=True)
                    elif not token and not think and not obj.get("done"):
                        # Empty response during thinking (streaming mode quirk)
                        thinking_text += " "
                        if show_thinking:
                            print(f"{C.DIM}.{C.RESET}", end="", flush=True)

                    if obj.get("done"):
                        stats = obj
                        break
                print()  # newline after streaming
            else:
                result = json.loads(resp.read())
                response_text = result.get("response", "")
                thinking_text = result.get("thinking", "")
                stats = result
    except urllib.error.URLError as e:
        print(f"{C.RED}Connection error: {e}{C.RESET}", file=sys.stderr)
    except Exception as e:
        print(f"{C.RED}Error: {e}{C.RESET}", file=sys.stderr)

    return response_text, thinking_text, stats


def print_stats(stats, show_load=True):
    """Print generation statistics."""
    eval_dur = stats.get("eval_duration", 0) / 1e9
    eval_count = stats.get("eval_count", 0)
    load_dur = stats.get("load_duration", 0) / 1e9
    total_dur = stats.get("total_duration", 0) / 1e9
    tok_s = eval_count / eval_dur if eval_dur > 0 else 0

    parts = [f"{C.DIM}"]
    parts.append(f"{eval_count} tokens, {tok_s:.1f} tok/s")
    if show_load and load_dur > 1:
        parts.append(f", load: {load_dur:.1f}s")
    parts.append(f", total: {total_dur:.1f}s")
    parts.append(f"{C.RESET}")
    print("".join(parts))


# ─── Commands ──────────────────────────────────────────────────────────────────

def cmd_list():
    """List available models."""
    data = api_get("/api/tags")
    if not data:
        return

    header("Available Models")
    models = sorted(data["models"], key=lambda m: m["details"].get("parameter_size", ""))

    for m in models:
        name = m["name"]
        params = m["details"].get("parameter_size", "?")
        quant = m["details"].get("quantization_level", "?")
        size_gb = m["size"] / 1e9
        family = m["details"].get("family", "?")
        print(f"  {C.GREEN}{name:40s}{C.RESET} {params:>8s}  {quant:>8s}  {size_gb:5.1f}GB  {C.DIM}{family}{C.RESET}")

    # Show loaded model
    ps = api_get("/api/ps")
    if ps and ps.get("models"):
        print(f"\n{C.YELLOW}Loaded:{C.RESET}")
        for m in ps["models"]:
            vram = m.get("size_vram", 0) / 1e9
            total = m["size"] / 1e9
            print(f"  {m['name']} — {vram:.1f}GB VRAM / {total:.1f}GB total, ctx={m.get('context_length', '?')}")


def cmd_status():
    """Show currently loaded model status."""
    ps = api_get("/api/ps")
    if not ps or not ps.get("models"):
        print(f"{C.DIM}No models currently loaded.{C.RESET}")
        return

    header("Loaded Models")
    for m in ps["models"]:
        total = m["size"]
        vram = m.get("size_vram", 0)
        ram = total - vram
        ctx = m.get("context_length", "?")
        expires = m.get("expires_at", "?")[:19]
        print(f"  {C.GREEN}{m['name']}{C.RESET}")
        print(f"    VRAM: {vram/1e9:.1f}GB ({vram/total*100:.0f}%)  RAM: {ram/1e9:.1f}GB  Ctx: {ctx}")
        print(f"    Expires: {expires}")


def cmd_chat(model, prompt, show_thinking=False):
    """Send a prompt and stream the response."""
    # Determine if model needs special handling
    is_thinking = any(t in model for t in THINKING_MODELS)
    kv = "q4_0" if "70b" in model or "120b" in model else None

    print(f"{C.DIM}Model: {model}{C.RESET}")
    if is_thinking and show_thinking:
        print(f"{C.DIM}(showing chain-of-thought){C.RESET}")
    print()

    response, thinking, stats = generate(
        model, prompt,
        stream=True,
        show_thinking=show_thinking,
        kv_cache_type=kv,
    )

    print()
    print_stats(stats)


def cmd_benchmark(prompt, models=None):
    """Run a single prompt across multiple models."""
    if models is None:
        data = api_get("/api/tags")
        if not data:
            return
        # Skip 70b+ for benchmarks (too slow)
        models = [
            m["name"] for m in data["models"]
            if "70b" not in m["name"] and "120b" not in m["name"]
        ]
        models.sort()

    header(f"Benchmark: {prompt[:60]}...")

    results = []
    for model in models:
        print(f"\n{C.BOLD}{model}{C.RESET}")
        kv = "q4_0" if "70b" in model else None
        response, thinking, stats = generate(
            model, prompt,
            stream=False,
            num_predict=2000,
            kv_cache_type=kv,
        )

        eval_dur = stats.get("eval_duration", 1) / 1e9
        eval_count = stats.get("eval_count", 0)
        tok_s = eval_count / eval_dur if eval_dur > 0 else 0
        load = stats.get("load_duration", 0) / 1e9

        # Clean response (strip thinking tags if present)
        clean = response.strip()
        if "<think>" in clean and "</think>" in clean:
            clean = clean[clean.index("</think>") + len("</think>"):].strip()

        preview = clean[:120].replace("\n", " ")
        print(f"  {C.GREEN}{preview}{C.RESET}")
        print(f"  {C.DIM}{tok_s:.1f} tok/s, {eval_count} tokens, load: {load:.1f}s{C.RESET}")

        results.append({
            "model": model,
            "response": clean,
            "tok_s": tok_s,
            "tokens": eval_count,
            "load_s": load,
        })

    # Summary table
    header("Results")
    print(f"  {'Model':40s} {'tok/s':>8s} {'Tokens':>8s}")
    print(f"  {'─' * 40} {'─' * 8} {'─' * 8}")
    for r in sorted(results, key=lambda x: -x["tok_s"]):
        print(f"  {r['model']:40s} {r['tok_s']:8.1f} {r['tokens']:8d}")


def cmd_bench_suite():
    """Run the standard reasoning benchmark suite across all suitable models."""
    data = api_get("/api/tags")
    if not data:
        return

    # Only test models that fit comfortably (skip 70b/120b)
    models = sorted([
        m["name"] for m in data["models"]
        if "70b" not in m["name"] and "120b" not in m["name"]
    ])

    header("Reasoning Benchmark Suite")
    print(f"  Models: {len(models)}")
    print(f"  Tests:  {len(BENCH_SUITE)}")

    # Results: model -> test -> {response, correct, tok_s}
    all_results = {m: {} for m in models}

    for test_name, test in BENCH_SUITE.items():
        print(f"\n{C.BOLD}{C.YELLOW}Test: {test_name} ({test['category']}){C.RESET}")
        print(f"{C.DIM}  {test['prompt'][:80]}{C.RESET}")

        for model in models:
            kv = "q4_0" if "70b" in model else None
            response, thinking, stats = generate(
                model, test["prompt"],
                stream=False,
                num_predict=2000,
                kv_cache_type=kv,
            )

            eval_dur = stats.get("eval_duration", 1) / 1e9
            eval_count = stats.get("eval_count", 0)
            tok_s = eval_count / eval_dur if eval_dur > 0 else 0

            clean = response.strip()

            # Check correctness
            correct = None
            if test["correct"]:
                correct = test["correct"].lower() in clean.lower()

            marker = ""
            if correct is True:
                marker = f"{C.GREEN}PASS{C.RESET}"
            elif correct is False:
                marker = f"{C.RED}FAIL{C.RESET}"
            else:
                marker = f"{C.DIM}---{C.RESET}"

            preview = clean[:80].replace("\n", " ")
            print(f"  {model:35s} [{marker}] {C.DIM}{preview}{C.RESET}")

            all_results[model][test_name] = {
                "response": clean,
                "correct": correct,
                "tok_s": tok_s,
            }

    # Summary
    header("Summary")
    print(f"  {'Model':35s} {'Pass':>6s} {'Fail':>6s} {'Avg tok/s':>10s}")
    print(f"  {'─' * 35} {'─' * 6} {'─' * 6} {'─' * 10}")

    for model in models:
        results = all_results[model]
        passes = sum(1 for r in results.values() if r["correct"] is True)
        fails = sum(1 for r in results.values() if r["correct"] is False)
        avg_toks = sum(r["tok_s"] for r in results.values()) / len(results) if results else 0
        print(f"  {model:35s} {passes:6d} {fails:6d} {avg_toks:10.1f}")


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Query Ollama models on the 3090.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              llm "What is consciousness?"
              llm -m phi4:14b "Write a haiku about recursion"
              llm -m deepseek-r1:8b --thinking "Solve: x^2 + 5x + 6 = 0"
              llm -l
              llm -b "Explain quantum tunneling in one sentence"
              llm --bench-suite
        """),
    )
    parser.add_argument("prompt", nargs="?", help="The prompt to send")
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL, help=f"Model to use (default: {DEFAULT_MODEL})")
    parser.add_argument("-l", "--list", action="store_true", help="List available models")
    parser.add_argument("-s", "--status", action="store_true", help="Show loaded model stats")
    parser.add_argument("-b", "--benchmark", action="store_true", help="Run prompt across all models")
    parser.add_argument("--bench-suite", action="store_true", help="Run standard reasoning benchmark")
    parser.add_argument("--thinking", action="store_true", help="Show chain-of-thought tokens")

    args = parser.parse_args()

    if args.list:
        cmd_list()
    elif args.status:
        cmd_status()
    elif args.bench_suite:
        cmd_bench_suite()
    elif args.benchmark:
        if not args.prompt:
            parser.error("--benchmark requires a prompt")
        cmd_benchmark(args.prompt)
    elif args.prompt:
        cmd_chat(args.model, args.prompt, show_thinking=args.thinking)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
