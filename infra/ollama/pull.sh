#!/usr/bin/env bash
set -euo pipefail
docker compose exec -T ollama ollama pull gemma4:12b   # reasoner
docker compose exec -T ollama ollama pull qwen3:8b     # default / classifier
docker compose exec -T ollama ollama pull qwen3:0.6b   # tiny narrative
docker compose exec -T ollama ollama pull bge-m3       # fallback embed model on CPU
