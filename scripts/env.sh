#!/usr/bin/env bash
# Source this script with the absolute physical workspace as its first argument.
if [[ $# -ne 1 || "$1" != /* ]]; then echo 'An absolute WorkspaceRoot is required.' >&2; return 2; fi
ip_root="$(cd "$1" && pwd)"
ip_source="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$ip_source" != "$ip_root" && "$ip_source" != "$ip_root/"* ]]; then echo 'Source is outside workspace.' >&2; return 2; fi
export INSIGHTPILOT_WORKSPACE_ROOT="$ip_root" INSIGHTPILOT_SOURCE_ROOT="$ip_source"
export UV_CACHE_DIR="$ip_root/.cache/uv" UV_PYTHON_INSTALL_DIR="$ip_root/.python"
export UV_PROJECT_ENVIRONMENT="$ip_source/.venv" UV_PYTHON_PREFERENCE=only-managed
export PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$ip_root/.cache/insightpilot-tmp"
mkdir -p "$TMPDIR" "$UV_CACHE_DIR" "$ip_root/.cache/insightpilot-verification" "$ip_root/.data/insightpilot"
if [[ -f "$ip_root/.env.insightpilot" ]]; then
 while IFS= read -r ip_line || [[ -n "$ip_line" ]]; do
  ip_line="${ip_line%$'\r'}"
  if [[ "$ip_line" =~ ^(INSIGHTPILOT_[A-Z0-9_]+)=(.*)$ ]]; then
   ip_name="${BASH_REMATCH[1]}"; ip_value="${BASH_REMATCH[2]}"
   if [[ "$ip_value" == \"*\" || "$ip_value" == \'*\' ]]; then ip_value="${ip_value:1:${#ip_value}-2}"; fi
   export "$ip_name=$ip_value"
  fi
 done < "$ip_root/.env.insightpilot"
fi
export PATH="$ip_root/.tools/uv:$PATH"

export LANGGRAPH_STRICT_MSGPACK=true
