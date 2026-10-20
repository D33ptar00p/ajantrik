#!/usr/bin/env bash
# Secret scan for a factory job: the branch's commits and/or files that leave the machine.
#
#   .factory/security/secret_scan.sh <out.json> [--git <worktree> <base>] [--file <path>]...
#
# Always runs the built-in scanner (policy.py: token patterns + this repo's secret .env values).
# When gitleaks is on PATH it runs too, for its much larger rule set. Findings from both land in
# <out.json> as {tool, findings: [{file, line, commit?, rule, fingerprint}]}; values are never
# written. Exit 0 clean, 1 findings, 2 usage/setup error.
# A reviewed false positive: add its fingerprint to allow.txt next to this script (humans only).
set -uo pipefail

SEC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SEC_DIR/../.." && pwd)"
[[ $# -ge 1 ]] || { echo "usage: $0 <out.json> [--git <worktree> <base>] [--file <path>]..." >&2; exit 2; }
OUT="$1"
shift

ARGS=(--out "$OUT")
WORKTREE="" MERGE_BASE=""
FILES=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --git)
            WORKTREE="$(cd "$2" && pwd)" || exit 2
            MERGE_BASE="$(git -C "$WORKTREE" merge-base "$3" HEAD 2>/dev/null || git -C "$WORKTREE" merge-base "origin/$3" HEAD)" || {
                echo "secret-scan: no merge-base with $3" >&2; exit 2; }
            ARGS+=(--git "$WORKTREE" "$MERGE_BASE")
            shift 3 ;;
        --file)
            [[ -f "$2" ]] && { FILES+=("$2"); ARGS+=(--file "$2"); }
            shift 2 ;;
        *) echo "secret-scan: unknown argument $1" >&2; exit 2 ;;
    esac
done

TOOL=builtin
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT  # gitleaks reports hold the raw values; they never leave this dir
if command -v gitleaks >/dev/null; then
    TOOL=gitleaks+builtin
    # v8.19+ has `git`/`dir`; older releases only `detect`.
    if gitleaks git --help >/dev/null 2>&1; then NEW=1; else NEW=0; fi
    if [[ -n $WORKTREE ]]; then
        if [[ $NEW == 1 ]]; then
            gitleaks git --no-banner --exit-code 0 --report-format json --report-path "$TMP/git.json" \
                --log-opts "$MERGE_BASE..HEAD" "$WORKTREE" >/dev/null 2>&1
        else
            gitleaks detect --no-banner --exit-code 0 --report-format json --report-path "$TMP/git.json" \
                --log-opts "$MERGE_BASE..HEAD" --source "$WORKTREE" >/dev/null 2>&1
        fi
        ARGS+=(--gitleaks-report "$TMP/git.json")
    fi
    for i in "${!FILES[@]}"; do
        if [[ $NEW == 1 ]]; then
            gitleaks dir --no-banner --exit-code 0 --report-format json --report-path "$TMP/f$i.json" "${FILES[$i]}" >/dev/null 2>&1
        else
            gitleaks detect --no-git --no-banner --exit-code 0 --report-format json --report-path "$TMP/f$i.json" \
                --source "${FILES[$i]}" >/dev/null 2>&1
        fi
        ARGS+=(--gitleaks-report "$TMP/f$i.json")
    done
fi

CLAUDE_PROJECT_DIR="$REPO" python3 "$SEC_DIR/policy.py" scan --tool "$TOOL" "${ARGS[@]}"
STATUS=$?
[[ $STATUS == 0 ]] && echo "secret-scan: clean ($TOOL)" || echo "secret-scan: findings in $OUT ($TOOL)"
exit $STATUS
