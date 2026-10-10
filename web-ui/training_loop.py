#!/usr/bin/env python3
# =============================================================================
# DizerCore AI Assistant
# -----------------------------------------------------------------------------
# File:    web-ui/training_loop.py
# Purpose: Self-improvement loop extracted from app.py.
#
# Three responsibilities:
#   1. VERIFY   — check that tables / C++ symbols / constants / file paths
#                 cited in the model's output actually exist in the FTS
#                 index. Returns a list of issues the caller can act on.
#   2. FEEDBACK — persist 👍/👎/corrections to feedback.jsonl for later
#                 retrieval and for merging into the training dataset.
#   3. RETRIEVE — find similar past corrections for a new prompt and format
#                 them as a "PAST CORRECTIONS" block to prepend to the
#                 system prompt.
#
# No classes, no globals — pure functions. Callers import what they need.
#
# Imports by:
#   web-ui/app.py                  — verification + feedback + retrieval
#   training/dataset-builder.py    — merges feedback into the dataset
# =============================================================================

import json
import os
import re
import sqlite3
import time
import uuid


# =============================================================================
# Config (defaults can be overridden per-call)
# =============================================================================

DEFAULT_FEEDBACK_FILE = "/data/prompt-history/feedback.jsonl"
DEFAULT_INDEX_DB      = "/data/web-ui/dizercore-index.db"


# =============================================================================
# Verification
# =============================================================================

# Patterns for the four kinds of artifact we check. Deliberately conservative
# so we never flag free-form prose.
#
# Full paths: at least one directory separator. Dots are allowed inside
# segments so 'sql/old/12.x/world/foo.sql' matches as a whole (the earlier
# version's char class excluded '.', which truncated 'sql/old/12.x/...' down
# to 'x/...' and produced false positives).
_PATH_RE_FULL = re.compile(
    r"\b("
    r"[A-Za-z0-9_\-][A-Za-z0-9_.\-]*"
    r"(?:/[A-Za-z0-9_\-][A-Za-z0-9_.\-]*)+"
    r"\.(?:sql|cpp|cc|cxx|h|hpp|hh|py|lua)"
    r")\b"
)

# Bare filenames — no directory prefix. Only C/C++ header/source extensions,
# because a bare .sql reference is common and legitimate; a bare .cpp
# reference almost always means the model invented the path.
_PATH_RE_BARE = re.compile(
    r"(?<![A-Za-z0-9_./\-])"
    r"([A-Za-z_][A-Za-z0-9_\-]{2,}\.(?:cpp|cc|cxx|hpp|hh))"
    r"(?![A-Za-z0-9_.])"
)

_SYMBOL_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_]+::[A-Za-z_][A-Za-z0-9_]*)\b"
)
_CONST_RE = re.compile(
    r"\b((?:SPELL|SMART|CMSG|SMSG|MSG|QUEST|CONDITION|TEXT)_[A-Z0-9_]+)\b"
)
# snake_case identifiers of at least 8 chars with at least one underscore
_TABLE_RE = re.compile(
    r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+){1,4})\b"
)

# Words that look like tables but aren't. Skipped during verification.
_TABLE_STOPWORDS = {
    "select", "insert", "update", "delete", "where", "from", "join",
    "create", "drop", "alter", "table", "index", "values", "into",
    "entryorguid", "source_type", "action_type", "event_type",
    "target_type", "action_param", "event_param", "target_param",
    "phase_mask", "quest_id", "spell_id", "creature_id", "item_id",
    "gameobject_id", "entry_id", "guid", "smart_scripts",
}
# Note: smart_scripts IS a real table but it's such a common substring
# that flagging it wastes cycles. Same for the other obvious ones.

# Maximum candidates we'll check per output. Bounds the wall-clock cost of
# verification to ~1.5 s on a Pi 5 for the worst case.
_MAX_CANDIDATES = 12


def _open_index(index_db):
    if not os.path.isfile(index_db):
        return None
    try:
        conn = sqlite3.connect(f"file:{index_db}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _content_contains(conn, needle):
    """True if `needle` appears as a literal substring in any indexed file
    content. Uses LIKE, not FTS MATCH — FTS tokenization splits on '_' and
    would give false positives on names like 'creature_questender_conditional'.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM files WHERE content LIKE ? LIMIT 1",
            (f"%{needle}%",),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return None  # unknown — caller must not flag


def _path_contains(conn, path):
    """True if the exact path appears in the indexed path column.
    Matches on suffix so 'sql/old/12.x/world/foo.sql' matches either
    'DizerCore-WoW/sql/old/12.x/world/foo.sql' or 'sql/old/12.x/world/foo.sql'.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM files WHERE path = ? OR path LIKE ? LIMIT 1",
            (path, f"%/{path}"),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return None


def _basename_exists(conn, name):
    """True if any indexed path ends with `/name` or equals `name`.
    Used for bare-filename checks: 'SpellScripts.cpp' is real if the
    repo has it anywhere, even though the model didn't give a directory.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM files WHERE path = ? OR path LIKE ? LIMIT 1",
            (name, f"%/{name}"),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return None


def _dedupe_preserve_order(seq):
    seen = set()
    out = []
    for x in seq:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def verify_output(output, index_db=DEFAULT_INDEX_DB):
    """Scan the model's output for cited artifacts and check each one exists
    in the FTS index.

    Returns:
        {
          "ok": bool,
          "issues": [
            {"kind": "path"|"symbol"|"constant"|"table",
             "name": str,
             "detail": str},
            ...
          ],
          "checked": int,     # how many artifacts were inspected
        }
    """
    if not output:
        return {"ok": True, "issues": [], "checked": 0}

    conn = _open_index(index_db)
    if conn is None:
        # No index → can't verify anything. Report success so caller doesn't
        # block on a missing index.
        return {"ok": True, "issues": [], "checked": 0}

    issues = []
    checked = 0

    try:
        # 1a. Full paths with directory separators
        paths = _dedupe_preserve_order(_PATH_RE_FULL.findall(output))[:_MAX_CANDIDATES]
        for p in paths:
            checked += 1
            if _path_contains(conn, p) is False:
                issues.append({
                    "kind": "path",
                    "name": p,
                    "detail": f"file path not in reference index: {p}",
                })

        # 1b. Bare filenames — check any indexed path ends with this name
        bare = _dedupe_preserve_order(_PATH_RE_BARE.findall(output))[:_MAX_CANDIDATES]
        for name in bare:
            checked += 1
            if _basename_exists(conn, name) is False:
                issues.append({
                    "kind": "path",
                    "name": name,
                    "detail": f"filename not found anywhere in reference index: {name}",
                })

        # 2. Class::method symbols
        symbols = _dedupe_preserve_order(_SYMBOL_RE.findall(output))[:_MAX_CANDIDATES]
        for s in symbols:
            checked += 1
            if _content_contains(conn, s) is False:
                issues.append({
                    "kind": "symbol",
                    "name": s,
                    "detail": f"symbol not found in any indexed source: {s}",
                })

        # 3. ALL_CAPS_CONSTANT names
        consts = _dedupe_preserve_order(_CONST_RE.findall(output))[:_MAX_CANDIDATES]
        for c in consts:
            checked += 1
            if _content_contains(conn, c) is False:
                issues.append({
                    "kind": "constant",
                    "name": c,
                    "detail": f"constant not found in any indexed source: {c}",
                })

        # 4. snake_case table names
        candidates = _TABLE_RE.findall(output)
        candidates = [
            c for c in candidates
            if c not in _TABLE_STOPWORDS and len(c) >= 8
        ]
        candidates = _dedupe_preserve_order(candidates)[:_MAX_CANDIDATES]
        for t in candidates:
            # Skip names that appear as part of a longer identifier
            # (avoid re-flagging a fragment of a path we already checked).
            checked += 1
            if _content_contains(conn, t) is False:
                issues.append({
                    "kind": "table",
                    "name": t,
                    "detail": f"table name not found in any indexed source: {t}",
                })
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {"ok": not issues, "issues": issues, "checked": checked}


def format_verification_retry(issues):
    """Build the retry-instruction appended to the system prompt when
    verification failed. Caller can use this to ask the model to rewrite."""
    if not issues:
        return ""
    lines = [
        "VERIFICATION FAILED — the previous draft cited artifacts that do "
        "not exist in the reference repo. Rewrite the investigation using "
        "only real artifacts. Removed items:",
    ]
    for i in issues[:8]:
        lines.append(f"  - [{i['kind']}] {i['name']}")
    lines.append(
        "If you cannot find a real replacement for a removed item, omit "
        "that line rather than inventing a new one."
    )
    return "\n".join(lines)


# =============================================================================
# Feedback storage
# =============================================================================

def _ensure_feedback_dir(feedback_file):
    d = os.path.dirname(feedback_file)
    if d:
        os.makedirs(d, exist_ok=True)


def save_feedback(prompt, output, verdict, correction="",
                  feedback_file=DEFAULT_FEEDBACK_FILE, entry_id=None):
    """Append a feedback record to feedback.jsonl.

    verdict:  "good" | "bad"
    correction: user's free-text correction when verdict == "bad".
                Empty for "good" verdicts.

    Returns the record that was written.
    """
    _ensure_feedback_dir(feedback_file)
    record = {
        "id": entry_id or str(uuid.uuid4()),
        "ts": time.time(),
        "prompt": prompt or "",
        "output": output or "",
        "verdict": verdict,
        "correction": correction or "",
    }
    with open(feedback_file, "a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def load_feedback(feedback_file=DEFAULT_FEEDBACK_FILE):
    """Return all feedback records as a list. Missing file → empty list.
    Corrupt lines are skipped."""
    if not os.path.isfile(feedback_file):
        return []
    out = []
    try:
        with open(feedback_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


# =============================================================================
# Retrieval of similar past corrections
# =============================================================================

# Very small tokenizer for the overlap score — lowercase alnum+underscore.
_TOKEN_RE = re.compile(r"[a-z_][a-z0-9_]{2,}")


def _keyword_overlap(a, b):
    ta = set(_TOKEN_RE.findall(a.lower()))
    tb = set(_TOKEN_RE.findall(b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def retrieve_corrections(query, feedback_file=DEFAULT_FEEDBACK_FILE,
                         top_k=3, min_score=0.08):
    """Return the top-K past corrections with non-empty text, ranked by
    keyword overlap with the current query. Only 'bad'-verdict entries with
    a correction are candidates."""
    if not query:
        return []

    all_fb = load_feedback(feedback_file)
    candidates = []
    for r in all_fb:
        if r.get("verdict") != "bad":
            continue
        if not r.get("correction"):
            continue
        score = _keyword_overlap(query, r.get("prompt", ""))
        if score >= min_score:
            candidates.append((score, r))
    candidates.sort(key=lambda x: -x[0])
    return [r for _, r in candidates[:top_k]]


def format_corrections_block(corrections):
    """Format retrieved corrections as a system-prompt block. Empty input
    returns empty string (caller appends nothing)."""
    if not corrections:
        return ""
    lines = [
        "PAST CORRECTIONS — apply these to the current task. The user has "
        "previously flagged these mistakes on similar prompts:",
    ]
    for c in corrections:
        p = c.get("prompt", "")[:80].replace("\n", " ")
        fix = c.get("correction", "").strip()
        if not fix:
            continue
        lines.append(f'- On "{p}": {fix}')
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


# =============================================================================
# Merge feedback into the training dataset
# =============================================================================

def feedback_to_training_examples(feedback_file=DEFAULT_FEEDBACK_FILE):
    """Convert 'bad'-verdict feedback entries into instruction/response
    training pairs.

    The correction text is treated as the desired response for the original
    prompt. This is a coarse policy — a proper correction would need to be
    re-run through the model to produce a full FILES TO CHECK / WHAT TO
    VERIFY / PROMPT FOR NEXT AI output, which is a manual step.

    Returns a list of {"instruction", "input", "output", "source"} dicts.
    """
    out = []
    for r in load_feedback(feedback_file):
        if r.get("verdict") != "bad":
            continue
        prompt = r.get("prompt", "").strip()
        correction = r.get("correction", "").strip()
        if not prompt or not correction:
            continue
        out.append({
            "instruction": prompt,
            "input": "",
            "output": correction,
            "source": "feedback",
        })
    return out
