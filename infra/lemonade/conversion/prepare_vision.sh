#!/usr/bin/env bash
set -euo pipefail
# TODO: requires host-side `pip install lemonade-sdk amd-quark ultralytics` (not inside Docker).
# Steps:
# 1) yolo export model=yolov8n.pt format=onnx
# 2) quark-quantize --model yolov8n.onnx --output infra/lemonade/models/yolov8n-int8 --bits 8 --backend xdna
# 3) lemonade-cli test infra/lemonade/models/yolov8n-int8
echo "Vision model preparation — see TODO inside script."
