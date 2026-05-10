#!/usr/bin/env bash
set -euo pipefail
docker compose exec -T ollama ollama pull qwen3:8b
docker compose exec -T ollama ollama pull qwen3.6:35b-a3b
docker compose exec -T ollama ollama pull bge-m3   # fallback embed model on CPU
