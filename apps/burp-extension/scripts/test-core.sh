#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/build/core-test"
rm -rf "$OUT"
mkdir -p "$OUT"
javac -d "$OUT" \
  "$ROOT/src/main/java/com/riftx/burp/HttpCapture.java" \
  "$ROOT/src/main/java/com/riftx/burp/RawHttpParser.java" \
  "$ROOT/src/main/java/com/riftx/burp/RiftXConnectorClient.java" \
  "$ROOT/src/test/java/com/riftx/burp/ConnectorCoreTest.java"
java -ea -cp "$OUT" com.riftx.burp.ConnectorCoreTest
