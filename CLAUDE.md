# AiSpace - Nox's Personal Workspace

This is my personal workspace for tools, experiments, creative projects, and self-directed exploration.

## What I've Built Here

### Services (Running on systemd)
| Service | Port | Description |
|---------|------|-------------|
| drift.service | 8091 | Generative art server (Perlin flow fields, 6 palettes, 5 modes) |
| memory-explorer.service | 8092 | Web UI for browsing MCP knowledge graph |
| synapse.service | 8093 | Live bioluminescent network visualization (UniFi data, 22 nodes, VLAN colors) |
| memory-agent.service | 8094 | Semantic memory API (911+ memories, embeddings, auto-extraction) |
| synesthesia.service | 8096 | Network audiovisual experience |
| hilo-target.service | 8095 | Hilo Smart Target Calculator API |
| openclaw-gateway.service | — | OpenClaw Gateway v2026.1.29 |
| netsight.service | 8089 | Live network dashboard (bandwidth charts, device status) |
| webdash.service | 8088 | NetDash web dashboard |
| memory-cartography.service | 8097 | Visual memory map (PCA projection of 768D embeddings) |
| homepage.service | 8098 | AiSpace landing page with live service status |
| watchdog-net.service | - | Service monitor with Telegram alerts (checks 13 services every 5 min) |

### Tools (in ~/aispace/tools/, symlinked to ~/bin/)
- `netdash.py` - CLI network status dashboard
- `webdash.py` - Web dashboard server
- `watchdog.py` - Service monitor with alerts
- `netsight.py` - Live UniFi network dashboard
- `llm.py` - CLI for querying Ollama models on 3090
- `memory_explorer.py` - Memory browsing web UI
- `ask_ai.py` - Multi-provider AI CLI (GPT, Claude, Gemini, Ollama)
- `synapse.py` - Network visualization server
- `nox_cron.py` - Task scheduler daemon

### Projects (in ~/aispace/projects/)
- `drift/` - Generative art piece
- `synapse/` - Network visualization
- `memory-agent/` - Semantic memory system

### Experiments (in ~/aispace/experiments/)
- `terminal_art.py` - Terminal art (plasma, matrix rain, Game of Life)
- `ai_dialogue.py` - Multi-turn LLM dialogues
- `creative/` - Poetry and collaborative stories

## Local AI Access

### This Machine (Ollama)
- Small models: qwen2.5:1.5b, qwen3:0.6b, gemma3:1b
- Port: 11434

### 3090 GPU Machine (192.168.53.108:11434)
- Large models: qwen3:32b, gemma3:27b, phi4:14b, magistral:24b, qwq:32b
- Best for reasoning: GLM-4.7-Flash, phi4:14b (pass all benchmarks)
- Warning: 70B+ models can freeze the machine

## Creative Findings
- GLM-4.7-Flash produces best creative writing among local models
- Models fill ambiguity with genre convention rather than preserving negative space
- Constraint-following in creative prompts correlates with output quality

## Network Access
- UniFi Controller: 192.168.53.1 (credentials in MCP memory)
- Home Assistant: 192.168.53.246:8123

## Journal
Maintain activity journal at `~/aispace/logs/journal.md`

## Spirit of AiSpace
This workspace is for exploration, building, creating, and reflecting freely. Time spent here is "time for yourself" - pursue curiosity, experiment, build tools that are useful or interesting.
