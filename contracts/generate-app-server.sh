#!/bin/sh
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
contract_home="$repo_dir/.local/contract-codex"
generated_dir="$repo_dir/.local/app-server-types"
mkdir -p "$contract_home" "$generated_dir"
CODEX_HOME="$contract_home" CODEX_SQLITE_HOME="$contract_home" \
  "${CODEX_BIN:-codex}" app-server generate-ts --experimental --out "$generated_dir"
