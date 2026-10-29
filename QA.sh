#!/usr/bin/env bash
# QA gate for Borg Backup GUI – exit 1 if score < 95 or a required check fails.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PY="${ROOT}/.venv/bin/python"
RUFF="${ROOT}/.venv/bin/ruff"
MYPY="${ROOT}/.venv/bin/mypy"
PYTEST="${ROOT}/.venv/bin/pytest"

if [[ ! -x "$PY" ]]; then
  echo "[QA] .venv fehlt – bitte python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt"
  exit 1
fi

TARGETS=(borg_backup_gui.py)
if [[ -f borg_model.py ]]; then
  TARGETS+=(borg_model.py)
fi
if [[ -f borg_updates.py ]]; then
  TARGETS+=(borg_updates.py)
fi

pass=0
fail=0
score=0
REQUIRED_FAILED=0
declare -a results=()

run_check() {
  local name="$1"
  local weight="$2"
  local required="$3"
  shift 3
  local out="/tmp/qa_${name}.out"
  echo ""
  echo "=== $name (weight ${weight}) ==="
  if "$@" >"$out" 2>&1; then
    echo "PASS"
    results+=("PASS|$name|$weight")
    pass=$((pass + 1))
    score=$((score + weight))
    return 0
  fi
  echo "FAIL"
  sed -n '1,40p' "$out" || true
  results+=("FAIL|$name|$weight")
  fail=$((fail + 1))
  if [[ "$required" == "1" ]]; then
    REQUIRED_FAILED=1
  fi
  return 0
}

TOTAL_WEIGHT=100

# Required checks sum to 95; security is a 5pt bonus toward 100.
run_check "syntax" 10 1 "$PY" -m py_compile "${TARGETS[@]}"
run_check "ruff_check" 30 1 "$RUFF" check "${TARGETS[@]}"
run_check "ruff_format" 15 1 "$RUFF" format --check "${TARGETS[@]}"
run_check "mypy" 20 1 "$MYPY" --explicit-package-bases "${TARGETS[@]}"
run_check "pytest" 20 1 "$PYTEST" -q
# S310: intentional urllib to GitHub Releases in borg_updates.py
run_check "ruff_security" 5 0 "$RUFF" check --select S --ignore S108,S110,S310,S603,S607 "${TARGETS[@]}"

echo ""
echo "========================================"
echo "QA SUMMARY"
echo "========================================"
for row in "${results[@]}"; do
  IFS='|' read -r st name weight <<<"$row"
  printf "  %-6s  %-14s  %+d\n" "$st" "$name" "$weight"
done
echo "----------------------------------------"
echo "Score: ${score}/${TOTAL_WEIGHT}  (passed=${pass} failed=${fail})"

THRESHOLD=95
if (( score < THRESHOLD )) || (( REQUIRED_FAILED == 1 )); then
  echo "RESULT: FAIL (need >= ${THRESHOLD} and all required checks)"
  exit 1
fi

echo "RESULT: PASS"
exit 0
