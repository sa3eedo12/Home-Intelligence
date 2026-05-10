#!/usr/bin/env bash
set -euo pipefail
# TODO: requires host-side `pip install lemonade-sdk amd-quark optimum` (not inside Docker).
# Steps:
# 1) optimum-cli export onnx --model Qwen/Qwen3-1.7B onnx_qwen3_1.7b
# 2) quark-quantize --model onnx_qwen3_1.7b --output infra/lemonade/models/qwen3-1.7b-int4 --bits 4 --backend xdna
# 3) lemonade-cli test infra/lemonade/models/qwen3-1.7b-int4
echo "Router model preparation — see TODO inside script."
