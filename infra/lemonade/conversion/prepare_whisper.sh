#!/usr/bin/env bash
set -euo pipefail
# TODO: requires host-side `pip install lemonade-sdk amd-quark optimum` (not inside Docker).
# Steps:
# 1) optimum-cli export onnx --model distil-whisper/distil-small.en onnx_distil_whisper_small_en
# 2) quark-quantize --model onnx_distil_whisper_small_en --output infra/lemonade/models/distil-whisper-small.en-int8 --bits 8 --backend xdna
# 3) lemonade-cli test infra/lemonade/models/distil-whisper-small.en-int8
echo "Whisper model preparation — see TODO inside script."
