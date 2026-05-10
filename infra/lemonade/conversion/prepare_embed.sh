#!/usr/bin/env bash
set -euo pipefail
# TODO: requires host-side `pip install lemonade-sdk amd-quark optimum` (not inside Docker).
# Steps:
# 1) optimum-cli export onnx --model BAAI/bge-m3 onnx_bge_m3
# 2) quark-quantize --model onnx_bge_m3 --output infra/lemonade/models/bge-m3-int8 --bits 8 --backend xdna
# 3) lemonade-cli test infra/lemonade/models/bge-m3-int8
echo "Embedding model preparation — see TODO inside script."
