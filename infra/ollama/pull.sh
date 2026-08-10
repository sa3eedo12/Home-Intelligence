#!/usr/bin/env bash
# Pull base models from ollama.com then build per-model context-capped derived
# tags via Modelfiles. The derived tags are what the orchestrator actually
# calls; the cap stops Ollama from allocating 256K-context KV cache on the 35B
# (would OOM the iGPU) or 40K cache on the 0.6B (waste of VRAM).
#
# Context sizes are chosen from measured cost, not guesswork. On this iGPU the
# MoE's KV cache is cheap (32k -> 64k costs only +0.7 GB) while the dense 8B's
# is not (16k -> 32k costs +5.1 GB), which is why only the MoE gets a large
# window. The MoE needs it: OpenClaw's system prompt alone is ~27.5k tokens.
set -euo pipefail

base_models=(
  "qwen3.6:35b-a3b-q4_K_M"  # reasoner base (MoE, 35B/3B active, dense ~20GB Q4)
  "qwen3:8b"                # classifier base
  "qwen3:0.6b"              # narrative base
  "bge-m3"                  # embeddings (CPU fallback)
)

# One derived tag per base that needs a context cap. bge-m3 is used as-is.
derived=(
  "qwen36-moe-64k"
  "qwen3-8b-16k"
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
