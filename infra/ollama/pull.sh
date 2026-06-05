#!/usr/bin/env bash
# Pull base models from ollama.com then build per-model context-capped derived
# tags via Modelfiles. The derived tags are what the orchestrator actually
# calls; the cap stops Ollama from allocating 256K-context KV cache on the 35B
# (would OOM the iGPU) or 40K cache on the 0.6B (waste of VRAM).
set -euo pipefail

base_models=(
  "qwen3.6:35b-a3b-q4_K_M"  # reasoner base (MoE, 35B/3B active, dense ~20GB Q4)
  "qwen3:8b"                # classifier base
  "qwen3:0.6b"              # narrative base
  "bge-m3"                  # embeddings (CPU fallback)
)

derived=(
  "qwen36-moe-128k"
  "qwen3-8b-8k"
  "qwen3-0.6b-4k"
)

for m in "${base_models[@]}"; do
  echo "Pulling $m..."
  docker compose exec -T ollama ollama pull "$m"
done

script_dir="$(cd "$(dirname "$0")" && pwd)"
for tag in "${derived[@]}"; do
  echo "Building derived tag: $tag..."
  docker compose cp "$script_dir/modelfiles/$tag.Modelfile" "ollama:/tmp/$tag.Modelfile"
  docker compose exec -T ollama ollama create "$tag" -f "/tmp/$tag.Modelfile"
done

echo "Done. Loaded tags:"
docker compose exec -T ollama ollama list
