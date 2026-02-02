#!/usr/bin/env python3
"""ask-ai — Quick cloud/local AI queries from the command line.

Usage:
  ask-ai "What is the capital of France?"          # default: GPT-4o-mini
  ask-ai --gemini "Summarize this concept"          # Google Gemini
  ask-ai --claude "Review this approach"            # Anthropic Claude
  ask-ai --local "Hello"                            # Local Ollama
  ask-ai --remote --model qwen3:32b "Solve this"   # 3090 Ollama
  echo "some text" | ask-ai "Summarize this"        # stdin piping
  ask-ai --list-models                              # show providers
  ask-ai --model gpt-4o "Use a specific model"      # override model
  ask-ai --system "You are a poet" "Write about rain"
"""

import argparse
import json
import os
import sys
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ENV_FILE = os.path.expanduser("~/.env")
LOCAL_OLLAMA = "http://127.0.0.1:11434"
REMOTE_OLLAMA = "http://192.168.53.108:11434"

PROVIDERS = {
    "gpt": {
        "name": "OpenAI",
        "url": "https://api.openai.com/v1/chat/completions",
        "default_model": "gpt-4o-mini",
        "key_env": "OPENAI_API_KEY",
    },
    "claude": {
        "name": "Anthropic",
        "url": "https://api.anthropic.com/v1/messages",
        "default_model": "claude-sonnet-4-20250514",
        "key_env": "ANTHROPIC_API_KEY",
    },
    "gemini": {
        "name": "Google Gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/models",
        "default_model": "gemini-2.0-flash",
        "key_env": "GEMINI_API_KEY",
    },
    "local": {
        "name": "Ollama (local)",
        "url": f"{LOCAL_OLLAMA}/api/chat",
        "default_model": "qwen2.5:1.5b",
        "key_env": None,
    },
    "remote": {
        "name": "Ollama (3090)",
        "url": f"{REMOTE_OLLAMA}/api/chat",
        "default_model": "qwen3:32b",
        "key_env": None,
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_env():
    """Load key=value pairs from ~/.env."""
    env = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("'\"")
    return env


def get_key(env, key_name):
    return os.environ.get(key_name) or env.get(key_name)


def stream_openai(url, api_key, model, system, prompt):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {"model": model, "messages": messages, "stream": True}

    with requests.post(url, headers=headers, json=payload, stream=True, timeout=120) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            text = line.decode("utf-8")
            if text.startswith("data: "):
                data = text[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        sys.stdout.write(content)
                        sys.stdout.flush()
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
    print()


def stream_anthropic(url, api_key, model, system, prompt):
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 4096,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system

    with requests.post(url, headers=headers, json=payload, stream=True, timeout=120) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            text = line.decode("utf-8")
            if text.startswith("data: "):
                try:
                    event = json.loads(text[6:])
                    if event.get("type") == "content_block_delta":
                        content = event.get("delta", {}).get("text", "")
                        if content:
                            sys.stdout.write(content)
                            sys.stdout.flush()
                except json.JSONDecodeError:
                    continue
    print()


def stream_gemini(base_url, api_key, model, system, prompt):
    url = f"{base_url}/{model}:streamGenerateContent?alt=sse&key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    with requests.post(url, json=payload, stream=True, timeout=120) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            text = line.decode("utf-8")
            if text.startswith("data: "):
                try:
                    chunk = json.loads(text[6:])
                    parts = chunk.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                    for part in parts:
                        content = part.get("text", "")
                        if content:
                            sys.stdout.write(content)
                            sys.stdout.flush()
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
    print()


def stream_ollama(url, model, system, prompt):
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {"model": model, "messages": messages, "stream": True}

    try:
        in_think = False
        with requests.post(url, json=payload, stream=True, timeout=300) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        # Filter out <think>...</think> blocks from reasoning models
                        if "<think>" in content:
                            in_think = True
                            content = content[:content.index("<think>")]
                        if "</think>" in content:
                            in_think = False
                            content = content[content.index("</think>") + 8:]
                        if not in_think and content:
                            sys.stdout.write(content)
                            sys.stdout.flush()
                    if chunk.get("done"):
                        break
                except json.JSONDecodeError:
                    continue
        print()
    except requests.ConnectionError:
        print(f"Error: Cannot connect to Ollama at {url}", file=sys.stderr)
        sys.exit(1)


def list_models(env):
    print("Provider        Default Model            Status")
    print("─" * 56)
    for pid, prov in PROVIDERS.items():
        key_ok = "n/a"
        if prov["key_env"]:
            key = get_key(env, prov["key_env"])
            if not key:
                key_ok = "NO KEY"
            elif "your" in key.lower() or "here" in key.lower():
                key_ok = "placeholder"
            else:
                key_ok = "key set"
        else:
            # Check connectivity for Ollama
            try:
                r = requests.get(prov["url"].replace("/api/chat", "/api/tags"), timeout=3)
                models = [m["name"] for m in r.json().get("models", [])]
                key_ok = f"{len(models)} models"
            except Exception:
                key_ok = "offline"
        flag = f"--{pid}"
        print(f"{flag:<15} {prov['default_model']:<24} {key_ok}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Quick AI queries from the command line",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  ask-ai 'Explain monads simply'\n"
               "  ask-ai --gemini 'Summarize this'\n"
               "  ask-ai --claude 'Review this code'\n"
               "  ask-ai --local 'Hello'\n"
               "  ask-ai --remote --model qwen3:32b 'Solve this'\n"
               "  echo 'code' | ask-ai 'Review this'\n"
               "  ask-ai --list-models",
    )
    # Provider flags (mutually exclusive)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--gpt", action="store_const", const="gpt", dest="provider", help="OpenAI (default)")
    group.add_argument("--claude", action="store_const", const="claude", dest="provider", help="Anthropic Claude")
    group.add_argument("--gemini", action="store_const", const="gemini", dest="provider", help="Google Gemini")
    group.add_argument("--local", action="store_const", const="local", dest="provider", help="Local Ollama")
    group.add_argument("--remote", action="store_const", const="remote", dest="provider", help="3090 Ollama")

    parser.add_argument("--model", "-m", help="Override the default model")
    parser.add_argument("--system", "-s", help="System prompt")
    parser.add_argument("--list-models", action="store_true", help="Show available providers")
    parser.add_argument("prompt", nargs="*", help="The question/prompt")
    args = parser.parse_args()

    env = load_env()

    if args.list_models:
        list_models(env)
        return

    provider_id = args.provider or "gpt"
    provider = PROVIDERS[provider_id]

    # Build prompt: positional args + stdin
    parts = []
    if args.prompt:
        parts.append(" ".join(args.prompt))
    if not sys.stdin.isatty():
        stdin_text = sys.stdin.read().strip()
        if stdin_text:
            parts.append(stdin_text)
    if not parts:
        parser.print_help()
        sys.exit(1)

    prompt = "\n\n".join(parts)
    model = args.model or provider["default_model"]
    system = args.system

    # Print header to stderr so stdout stays clean for piping
    print(f"[{provider['name']} / {model}]", file=sys.stderr)

    def require_key(key_env, provider_name):
        key = get_key(env, key_env)
        if not key or "your" in key.lower() or "here" in key.lower():
            print(f"Error: {key_env} not set. Add a real key to ~/.env or environment.", file=sys.stderr)
            sys.exit(1)
        return key

    if provider_id == "gpt":
        api_key = require_key(provider["key_env"], provider["name"])
        stream_openai(provider["url"], api_key, model, system, prompt)

    elif provider_id == "claude":
        api_key = require_key(provider["key_env"], provider["name"])
        stream_anthropic(provider["url"], api_key, model, system, prompt)

    elif provider_id == "gemini":
        api_key = require_key(provider["key_env"], provider["name"])
        stream_gemini(provider["url"], api_key, model, system, prompt)

    elif provider_id in ("local", "remote"):
        stream_ollama(provider["url"], model, system, prompt)


if __name__ == "__main__":
    main()
