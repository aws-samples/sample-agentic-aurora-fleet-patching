#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Build Lambda deployment package with all dependencies
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$PROJECT_ROOT/.lambda-build"

echo "=== Building Lambda package ==="
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

# Install Python dependencies (Linux x86_64 for Lambda)
pip3 install --break-system-packages -q -t "$BUILD_DIR" \
    --platform manylinux2014_x86_64 --only-binary=:all: --implementation cp --python-version 3.12 \
    fastapi mangum pydantic boto3 psycopg2-binary 2>/dev/null || \
pip3 install -t "$BUILD_DIR" \
    --platform manylinux2014_x86_64 --only-binary=:all: --implementation cp --python-version 3.12 \
    fastapi mangum pydantic boto3 psycopg2-binary

# Copy application code
cp -r "$PROJECT_ROOT/api" "$BUILD_DIR/api"
cp -r "$PROJECT_ROOT/agents" "$BUILD_DIR/agents"
cp -r "$PROJECT_ROOT/shared" "$BUILD_DIR/shared"
cp -r "$PROJECT_ROOT/traffic-gen" "$BUILD_DIR/traffic_gen"

# Copy UI build if it exists
if [ -d "$PROJECT_ROOT/ui/dist" ]; then
    mkdir -p "$BUILD_DIR/ui"
    cp -r "$PROJECT_ROOT/ui/dist" "$BUILD_DIR/ui/dist"
fi

# Clean up unnecessary files
find "$BUILD_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -name "*.dist-info" -type d -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -name "tests" -type d -exec rm -rf {} + 2>/dev/null || true

echo "=== Build complete: $BUILD_DIR ==="
du -sh "$BUILD_DIR"
