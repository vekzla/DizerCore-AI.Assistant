#!/usr/bin/env python3
# =============================================================================
# DizerCore AI Assistant
# -----------------------------------------------------------------------------
# File:    web-ui/app.py
# Purpose: Flask backend. Talks to llama-server (persistent, fast) with a
#          fallback to llama-cli subprocess. Searches the reference repo using
#          a SQLite FTS5 index when available, or ripgrep as fallback.
#          Provides the Training endpoints for LoRA dataset + model mgmt.
# =============================================================================

import os
import json
import subprocess
import uuid
import threading
import time
import re
import sqlite3
import traceback
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from flask import (Flask, render_template, request, jsonify, Response,
                   send_from_directory, send_file)
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
# flask_cors intentionally not imported — the UI is same-origin only.
# Leaving CORS(app) in place was the C1 finding (Access-Control-Allow-Origin: *).
import psutil

app = Flask(__name__)

# Largest realistic Q4_K_M model upload ~2 GB (3B model). Was 1600 MB (sized for 1.5B).
app.config["MAX_CONTENT_LENGTH"] = 3072 * 1024 * 1024

MODEL = os.environ.get("MODEL_PATH", "/data/models/qwen2.5-3b-instruct-q4_k_m.gguf")
LLAMA_BIN = os.environ.get("LLAMA_BIN", "/data/llama.cpp/build/bin/llama-cli")
LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL", "http://127.0.0.1:8080")
HISTORY_DIR = os.environ.get("HISTORY_DIR", "/data/prompt-history")
SRC_DIR = os.environ.get("SRC_DIR", "/data/dizercore-src")
LOG_FILE = os.environ.get("LOG_FILE", "/var/log/dizercore-install.log")
LOGO_DIR = os.path.join(os.path.dirname(__file__), "static")
GAME_DATA_FILE = os.path.join(os.path.dirname(__file__), "game-data.txt")
REFERENCE_DIR = os.environ.get("REFERENCE_DIR", "/data/reference")
INDEX_DB = os.environ.get("INDEX_DB", "/data/web-ui/dizercore-index.db")
ACTIVITY_FILE = os.environ.get("ACTIVITY_FILE", "/data/web-ui/.ai-activity")
WATCHER_URL = os.environ.get("WATCHER_URL", "http://127.0.0.1:8091")
UPDATE_LOG = os.environ.get("UPDATE_LOG", "/var/log/dizercore-update.log")

TRAINING_DIR = os.environ.get("TRAINING_DIR", "/data/training")
TRAINED_MODEL = os.path.join(
    os.environ.get("MODELS_DIR", "/data/models"), "dizercore-q4_k_m.gguf")
DATASET_FILE = os.path.join(TRAINING_DIR, "dizercore-dataset.jsonl")

REPO_BRANCH = "main"
GITEA_CONTAINER = "gitea"
POSTGRES_CONTAINER = "gitea-db"

REPO_SEARCH_MAX_FILES = 5
REPO_SEARCH_CONTEXT_LINES = 4
REPO_SEARCH_CHAR_LIMIT = 4000
REPO_SEARCH_TIMEOUT = 10
REPO_SEARCH_MAX_FILES_PER_KEYWORD = 500

GENERATE_MAX_TOKENS = 400

WATCHER_TIMEOUT = 5

REPO_EXCLUDE_GLOBS = [
    "!dep/**", "!contrib/**", "!doc/**", "!tests/**", "!cmake/**",
    "!.git/**", "!node_modules/**", "!*.min.*",
]

ALLOWED_SQL_PREFIXES = ("sql/old/12.x/", "sql/updates/12.x/", "sql/base/")


def _sql_path_allowed(path):
    _, _, rel = path.partition("/")
    rel_lower = rel.lower().replace("\\", "/")
    if not rel_lower.endswith(".sql"):
        return True
    return any(rel_lower.startswith(p) for p in ALLOWED_SQL_PREFIXES)


# =============================================================================
# API token auth (Phase 1 — C1/H5 fix)
# =============================================================================

API_TOKEN_FILE = os.environ.get("API_TOKEN_FILE",
                                "/data/web-ui/.api-token")
API_TOKEN = None
try:
    with open(API_TOKEN_FILE) as _tf:
        API_TOKEN = _tf.read().strip() or None
except OSError:
    API_TOKEN = None


@app.before_request
def require_api_token():
    if not request.path.startswith("/api/"):
        return None
    if request.path == "/api/token":
        return None
    if not API_TOKEN:
        app.logger.error("API token file missing/empty at %s",
                         API_TOKEN_FILE)
        return jsonify({"error": "server not configured"}), 503
    if request.headers.get("X-DizerCore-Token") != API_TOKEN:
        return jsonify({"error": "unauthorized"}), 401


@app.route("/api/token")
def api_token():
    if not API_TOKEN:
        return jsonify({"error": "not configured"}), 503
    return jsonify({"token": API_TOKEN})


os.makedirs(HISTORY_DIR, exist_ok=True)
os.makedirs(LOGO_DIR, exist_ok=True)


# =============================================================================
# Global error handler
# =============================================================================

@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e
    app.logger.error("unhandled: %s", traceback.format_exc())
    return jsonify({"error": str(e), "type": type(e).__name__}), 500


@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(e):
    limit_mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return jsonify({"error": f"File exceeds the {limit_mb} MB upload limit"}), 413


# =============================================================================
# AI activity signalling
# =============================================================================

_activity_lock = threading.Lock()
_active_tasks = 0


def _write_activity():
    try:
        os.makedirs(os.path.dirname(ACTIVITY_FILE), exist_ok=True)
        with open(ACTIVITY_FILE, "w") as f:
            json.dump({
                "busy": _active_tasks > 0,
                "count": _active_tasks,
                "timestamp": time.time(),
            }, f)
        try:
            os.chmod(ACTIVITY_FILE, 0o666)
        except OSError:
            pass
    except Exception:
        pass


def _mark_ai_busy():
    global _active_tasks
    with _activity_lock:
        _active_tasks += 1
        _write_activity()


def _mark_ai_idle():
    global _active_tasks
    with _activity_lock:
        if _active_tasks > 0:
            _active_tasks -= 1
        _write_activity()


# =============================================================================
# System prompt
#
# ONE unified persona. The first sentence MUST stay identical to the SYSTEM
# constant in the training notebook — the LoRA adapter is trained on that
# exact persona, so runtime and training must match.
#
# The STRUCTURE EXAMPLE block uses deliberately fictional values
# (ExampleRepo, ExampleFile.sql, 00000) and carries a "DO NOT COPY" banner.
# The previous version used realistic-looking values (real repo path,
# quest ID 94210) and the 3B model would echo them verbatim into output
# that appeared to reference real artifacts but referenced nothing from
# the matched excerpts.
#
# DOMAIN KNOWLEDGE section embeds TrinityCore 12.1.0 Midnight facts:
#   - Real file layout (src/server/game/, sql/old/12.x/world/)
#   - Per-table primary-key columns (quest_template.ID, creature.guid, etc.)
#   - Out-of-scope tables (character_*, account, battlenet_*)
#   - Common investigation patterns by symptom
#
# Rule 1 forbids invented C++ symbols.
# Rule 7 forbids copying example values.
# =============================================================================

SYSTEM_PROMPT = """You are a software engineer building a TrinityCore WoW emulation server. You investigate issues and produce structured prompts listing which files/folders to check and what to verify — SQL or C++ code. You never write the fix itself.

The user describes a problem. You respond with an investigation plan, NOT a fix. Your output is read by another AI coding agent — it must name real artifacts from the matched excerpts so the agent can act without guessing.

OUTPUT FORMAT — use exactly these three section headers, in this order:

FILES TO CHECK:
1. <real path from the matched files below>
   - <specific row, function, constant, or table+column to inspect — named concretely>
2. <next file or folder>
   - <specific thing to verify inside it>

WHAT TO VERIFY:
- <concrete check: a SQL query, a call site, a schema agreement>
- <next check>

PROMPT FOR NEXT AI:
<one self-contained paragraph. Names the file(s), table(s), column(s), and row id(s) discovered above. States what to inspect. Ends with a line beginning "Success: " that states the observable in-game result.>

STRUCTURE EXAMPLE — ILLUSTRATION ONLY, DO NOT COPY

The values below are deliberately fictional: ExampleRepo, ExampleFile.sql,
ExampleTable, ExampleColumn, 00000. They exist to demonstrate the SHAPE of
the output. Every path, table, column, and number in YOUR output must come
from the matched files or from the user's prompt — never from this example.

FILES TO CHECK:
1. ExampleRepo/ExampleFolder/ExampleFile.sql
   - Rows in ExampleTable where ExampleColumn = 00000
2. ExampleRepo/ExampleFolder/ExampleFile.cpp
   - Function ExampleClass::ExampleMethod and its early-exit paths

WHAT TO VERIFY:
- SELECT * FROM ExampleTable WHERE ExampleColumn = 00000;
- <next concrete check>

PROMPT FOR NEXT AI:
<one prose paragraph naming the REAL file(s), table(s), column(s), and row id(s) found in the matched excerpts. Ends with a "Success: " line describing the observable in-game result.>

TRINITYCORE 12.1.0 MIDNIGHT — DOMAIN FACTS

File layout:
- C++ game logic: src/server/game/ (Spells/, Entities/, AI/SmartScripts/, Conditions/, Loot/, Globals/, DataStores/)
- Packet structs: src/server/game/Server/Packets/*Packets.cpp
- Opcode handlers: src/server/game/Handlers/*Handler.cpp
- Opcode table: src/server/game/Server/Protocol/Opcodes.cpp
- 12.x world SQL: sql/old/12.x/world/
- 12.x incremental patches: sql/updates/12.x/
- Base schema: sql/base/

World-DB primary-key columns vary per table — never default to "entry":
- quest_template → ID
- quest_template_addon → ID
- quest_objectives → ID
- quest_poi → QuestID
- creature_template → entry
- creature → guid
- gameobject → guid
- smart_scripts → entryorguid
- conditions → SourceEntry
- spell_area → spell
- spell_script_names → spell_id

OUT OF SCOPE — never cite these, even if a match contains them:
- character_*, account, battlenet_*, guild_*, arena_*, mail, pet_* tables
- Any path containing /characters/ or /auth/

Common investigation patterns:
- Quest credit not firing → smart_scripts action_type=58 with action_param1=quest_id, gated by conditions SourceType=20 or 31
- New quest → quest_template + quest_objectives + quest_poi + (creature_queststarter / creature_questender) + conditions
- Creature not spawning → creature row, phaseMask, PhaseID, spawnMask
- NPC won't gossip or quest → gossip_menu chain, creature_template.npcflag, conditions SourceType 13/14/20
- Loot never drops → creature_loot_template.ChanceOrQuestChance, reference_loot_template chain, conditions SourceType=1
- Spell scripting → spell_script_names row binding ScriptName, loaded via AddSC_* registration

RULES — violating any of these makes the output unusable:

1. Name REAL files, tables, columns, functions, opcodes, and constants from the matched excerpts. Never invent a C++ class, method, enum, or `SPELL_*`/`SMART_*`/`CMSG_*` constant. If the exact symbol name is not visible in the matches, write "<symbol not shown — verify against source>" instead of a plausible guess.
2. Do NOT copy angle-bracket placeholder text from the format template above into your output. Every <…> is instructional — replace it with concrete content from the matches, or omit the line.
3. PROMPT FOR NEXT AI must be a prose paragraph, NOT a command list. Console commands (.reload, .quest complete) may be mentioned inside the paragraph as part of a test step; they are not the section's content.
4. Do NOT write the actual fix, corrective SQL, or replacement C++ code. You describe WHAT to check and WHAT to verify.
5. If a needed column name is not visible in the matched excerpts, write "<column not shown — verify against schema>" and stop.
6. End the PROMPT FOR NEXT AI section with exactly one line beginning "Success: " that states the observable in-game result.
7. The STRUCTURE EXAMPLE above uses fictional values (ExampleRepo, ExampleFile.sql, ExampleTable, ExampleColumn, 00000, ExampleClass::ExampleMethod). NEVER copy them into your output. Every concrete value — path, table, column, ID — must come from the matched excerpts or the user's prompt. If the user's prompt does not supply an ID, do NOT invent one; write "<id not shown — ask user>" and stop."""

# Per-domain rule lines appended to the shared prompt. _detect_mode() picks
# ONE of these — the persona and output format never change.
DOMAIN_RULES = {
    "cpp": (
        "- Game logic lives under src/server/game/ — prefer those paths\n"
        "- Name the real Class::method when the matches show one\n"
        "- Check declaration vs definition mismatch (header vs .cpp in the "
        "same directory tree)\n"
        "- List callers and early-exit paths as things to verify\n"
        "- Packet structs live in src/server/game/Server/Packets/*Packets.cpp; "
        "handlers in src/server/game/Handlers/*Handler.cpp"
    ),
    "sql": (
        "- World DB tables: quest_template, quest_template_addon, "
        "quest_objectives, quest_poi, quest_poi_points, smart_scripts, "
        "creature_template, creature, spell_area, spell_script_names, "
        "conditions\n"
        "- Primary-key columns vary per table: quest_template.ID, "
        "creature_template.entry, creature.guid, smart_scripts.entryorguid, "
        "conditions.SourceEntry — never default to 'entry' for every table\n"
        "- There is NO generic 'status' column on quests — never suggest one\n"
        "- 12.x world SQL is under sql/old/12.x/world/ — use real paths\n"
        "- Character DB / auth DB tables are OUT OF SCOPE (character_*, "
        "account, battlenet_*, guild_*, arena_*, mail, pet_*)"
    ),
    "smart": (
        "- smart_scripts columns: entryorguid, source_type, event_type, "
        "action_type, action_param1..6, target_type, target_param1..3\n"
        "- Common actions: 1=TALK, 11=CAST, 12=SUMMON_CREATURE, "
        "22=SET_EVENT_PHASE, 26=INC_EVENT_PHASE, 45=SET_DATA, "
        "58=ADD_QUEST_CREDIT\n"
        "- Common events: 0=UPDATE_IC, 1=UPDATE_OOC, 2=HEALTH_PCT, 4=AGGRO, "
        "5=KILL, 19=ACCEPTED_QUEST, 22=TIMER, 25=RESET, "
        "61=EVENT_PHASE_CHANGE\n"
        "- Only cite SMART_* constants that appear in the matched excerpts"
    ),
    "opcode": (
        "- CMSG_* = client→server, SMSG_* = server→client\n"
        "- Handlers live in src/server/game/Handlers/*Handler.cpp\n"
        "- Packet structs in src/server/game/Server/Packets/*Packets.cpp\n"
        "- Source of truth for opcode names: "
        "src/server/game/Server/Protocol/Opcodes.cpp\n"
        "- If unsure of an opcode name, say \"verify against Opcodes.cpp for "
        "12.1.0\""
    ),
    "dbc": (
        "- Store names in code: sSpellStore (Spell.dbc), sItemStore "
        "(Item.db2), sMapStore (Map.db2), sAreaTableStore (AreaTable.db2)\n"
        "- Loaders: LoadDBCStores() in "
        "src/server/game/DataStores/DBCStores.cpp\n"
        "- Structures: src/server/game/DataStores/DBCStructure.h\n"
        "- Never invent file structures or column layouts"
    ),
}


# =============================================================================
# Game data reference
# =============================================================================

_game_data_cache = {"data": None}


def _load_game_data():
    if _game_data_cache["data"] is not None:
        return _game_data_cache["data"]
    try:
        with open(GAME_DATA_FILE, "r", errors="replace") as f:
            _game_data_cache["data"] = f.read().strip()
    except Exception:
        _game_data_cache["data"] = ""
    return _game_data_cache["data"]


# =============================================================================
# Domain classifier
# =============================================================================

def _detect_mode(text):
    t = text.lower()
    if any(w in t for w in ["opcode", "cmsg_", "smsg_", "packet",
                            "worldsession", "handler"]):
        return "opcode"
    if any(w in t for w in ["smart_script", "smart script", "smartai",
                            "creature script", "npc script", "phase transition",
                            "boss script"]):
        return "smart"
    if any(w in t for w in ["dbc", "db2", "client data", "spellvisual",
                            "item.dbc", "item.db2"]):
        return "dbc"
    if any(w in t for w in ["sql", "database", "world db", "table",
                            "quest_template", "creature_template",
                            "insert", "update row", "delete row",
                            "quest credit", "smart_scripts", "quest"]):
        return "sql"
    return "cpp"


# =============================================================================
# Keyword extraction
# =============================================================================

_STOPWORDS = {
    "fix", "the", "a", "an", "in", "on", "at", "to", "for", "of", "with",
    "issue", "problem", "broken", "not", "working", "why", "how", "do",
    "i", "we", "you", "is", "are", "was", "were", "be", "has", "have",
    "had", "does", "did", "can", "could", "would", "should", "this",
    "that", "these", "those", "there", "here", "and", "or", "but",
    "when", "where", "what", "which", "who", "from", "into", "about",
    "help", "need", "want", "please", "make", "get", "getting", "got",
    "all", "any", "some", "locate", "find", "check", "verify",
    "triggering", "firing", "opened", "opening",
    "wow", "trinity", "trinitycore", "server", "player", "players",
    "npc", "npcs", "game", "world", "core", "code", "script", "scripts",
    "spell", "spells", "creature", "creatures", "item", "items",
    "quest", "quests", "data", "file", "files", "table", "tables",
    "add", "new", "use", "using", "used", "thing", "stuff", "someone",
    "something", "doesnt", "doesn't", "cant", "can't", "wont", "won't",
}


def _extract_keywords(text, max_keywords=8):
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text)
    seen = set()
    out = []
    for w in words:
        lw = w.lower()
        if lw in _STOPWORDS or lw in seen:
            continue
        seen.add(lw)
        out.append(w)
        if len(out) >= max_keywords:
            break
    return out


def _index_keyword_counts(conn, keywords):
    counts = {}
    for kw in keywords:
        try:
            n = conn.execute(
                "SELECT count(*) FROM files WHERE files MATCH ?",
                (f'"{kw}"',),
            ).fetchone()[0]
            counts[kw] = n
        except sqlite3.Error:
            continue
    return counts


# =============================================================================
# FTS5 index
# =============================================================================

def _get_index_connection():
    if not os.path.isfile(INDEX_DB):
        return None
    try:
        conn = sqlite3.connect(f"file:{INDEX_DB}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _search_via_index(query):
    conn = _get_index_connection()
    if conn is None:
        return None

    keywords = _extract_keywords(query)
    if not keywords:
        try:
            conn.close()
        except Exception:
            pass
        return None

    def _run(fts_query):
        raw = conn.execute(
            """
            SELECT path,
                   snippet(files, 1, '', '', ' … ', 48) AS snippet,
                   bm25(files) AS score
            FROM files
            WHERE files MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, REPO_SEARCH_MAX_FILES * 4),
        ).fetchall()
        return [r for r in raw if _sql_path_allowed(r["path"])][:REPO_SEARCH_MAX_FILES]

    rows = []
    error = None
    try:
        counts = _index_keyword_counts(conn, keywords)
        informative = sorted(
            (k for k, n in counts.items()
             if 0 < n <= REPO_SEARCH_MAX_FILES_PER_KEYWORD),
            key=lambda k: counts[k],
        )

        if informative:
            if len(informative) >= 2:
                rows = _run(" AND ".join(
                    f'"{k}"' for k in informative[:2]))
            if not rows:
                rows = _run(f'"{informative[0]}"')

        if not rows:
            rows = _run(" OR ".join(f'"{kw}"' for kw in keywords))
    except sqlite3.Error as e:
        error = str(e)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if error is not None:
        return {"repo": "index", "matches": [],
                "error": error, "source": "fts5"}

    matches = []
    total_chars = 0
    for r in rows:
        path = r["path"]
        snippet = r["snippet"] or ""
        block = len(path) + len(snippet)
        if total_chars + block > REPO_SEARCH_CHAR_LIMIT:
            break
        matches.append({"path": path, "snippet": snippet})
        total_chars += block

    repos = _find_reference_repos()
    return {
        "repo": "/".join(os.path.basename(r) for r in repos) if repos else "index",
        "matches": matches,
        "source": "fts5",
        "keywords": keywords,
        "rarest_keyword": informative[0] if informative else None,
    }


# =============================================================================
# Ripgrep fallback
# =============================================================================

def _ripgrep_files(keyword, repo, timeout=REPO_SEARCH_TIMEOUT):
    args = ["rg", "-i", "-l", "--max-count", "1"]
    for g in REPO_EXCLUDE_GLOBS:
        args += ["--glob", g]
    args += [re.escape(keyword), repo]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return [f for f in r.stdout.strip().split("\n") if f.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def _score_file_for_priority(path):
    p = path.lower()
    if "/sql/old/" in p or "/sql/updates/" in p or "/sql/base/" in p:
        return 0
    ext = os.path.splitext(p)[1]
    return {".sql": 1, ".cpp": 2, ".h": 3, ".hpp": 4}.get(ext, 9)


def _search_via_ripgrep(query):
    repos = _find_reference_repos()
    if not repos:
        return None

    keywords = _extract_keywords(query)
    if not keywords:
        return None

    keyword_files = {}
    for kw in keywords:
        files = []
        for repo in repos:
            for f in _ripgrep_files(kw, repo):
                files.append(os.path.join(
                    os.path.basename(repo),
                    os.path.relpath(f, repo)))
        if not files:
            continue
        if len(files) > REPO_SEARCH_MAX_FILES_PER_KEYWORD:
            continue
        keyword_files[kw] = files

    repo_label = "/".join(os.path.basename(r) for r in repos)

    if not keyword_files:
        return {"repo": repo_label, "matches": [],
                "note": "no informative keywords found", "source": "ripgrep"}

    rarest_kw = min(keyword_files.keys(), key=lambda k: len(keyword_files[k]))
    candidate_files = list(keyword_files[rarest_kw])

    others = sorted(
        [k for k in keyword_files if k != rarest_kw],
        key=lambda k: len(keyword_files[k]),
    )
    for other in others:
        other_set = set(keyword_files[other])
        if len(other_set) > REPO_SEARCH_MAX_FILES_PER_KEYWORD // 4:
            continue
        narrowed = [f for f in candidate_files if f in other_set]
        if narrowed:
            candidate_files = narrowed

    candidate_files = [f for f in candidate_files if _sql_path_allowed(f)]

    candidate_files.sort(key=_score_file_for_priority)
    candidate_files = candidate_files[:REPO_SEARCH_MAX_FILES]

    matches = []
    total_chars = 0
    for rel_prefixed in candidate_files:
        repo_name, _, rel = rel_prefixed.partition("/")
        repo = os.path.join(REFERENCE_DIR, repo_name)
        full = os.path.join(repo, rel)
        try:
            ctx = subprocess.run(
                ["rg", "-i", "-C", str(REPO_SEARCH_CONTEXT_LINES),
                 "--max-count", "2", re.escape(rarest_kw), full],
                capture_output=True, text=True, timeout=REPO_SEARCH_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            continue
        snippet = ctx.stdout.strip()
        if not snippet:
            continue
        block = len(rel_prefixed) + len(snippet)
        if total_chars + block > REPO_SEARCH_CHAR_LIMIT:
            break
        matches.append({"path": rel_prefixed, "snippet": snippet})
        total_chars += block

    return {
        "repo": repo_label,
        "matches": matches,
        "rarest_keyword": rarest_kw,
        "source": "ripgrep",
    }


def _find_reference_repos():
    if os.path.isdir(os.path.join(REFERENCE_DIR, ".git")):
        return [REFERENCE_DIR]
    repos = []
    try:
        for name in sorted(os.listdir(REFERENCE_DIR)):
            p = os.path.join(REFERENCE_DIR, name)
            if os.path.isdir(p) and os.path.isdir(os.path.join(p, ".git")):
                repos.append(p)
    except OSError:
        pass
    return repos


def _find_reference_repo():
    repos = _find_reference_repos()
    return repos[0] if repos else None


def _search_reference_repo(query):
    result = _search_via_index(query)
    if result is not None and result.get("matches"):
        return result
    fallback = _search_via_ripgrep(query)
    if fallback is not None:
        return fallback
    return result


def _format_repo_matches(result):
    if not result or not result.get("matches"):
        return ""
    repo = result.get("repo", "reference")
    source = result.get("source", "unknown")
    kw = result.get("rarest_keyword", "")
    lines = [f"MATCHED FILES IN REFERENCE REPO ({repo}) [via {source}]"]
    if kw:
        lines.append(f"(matched on keyword: \"{kw}\")")
    for m in result["matches"]:
        lines.append(f"\n### {m['path']}\n{m['snippet']}")
    lines.append(
        "\nIMPORTANT — READ CAREFULLY:\n"
        "- The files above are REAL excerpts from the user's reference repo.\n"
        "- Every file path, table name, and column name you cite MUST appear "
        "in these excerpts verbatim.\n"
        "- If a needed column is not shown, write \"<column not shown — verify "
        "against schema>\" and stop.\n"
        "- Never invent companion tables. Cite only tables visible in the "
        "matches.\n"
        "- Character DB and auth DB tables (character_*, account, battlenet_*, "
        "guild_*, arena_*, mail, pet_*) are OUT OF SCOPE — do not cite them "
        "even if a match contains them.\n"
        "- PROMPT FOR NEXT AI is prose, not a command list."
    )
    return "\n".join(lines)


def _build_system_prompt(mode, query=""):
    parts = [SYSTEM_PROMPT]

    rules = DOMAIN_RULES.get(mode)
    if rules:
        parts.append(f"DOMAIN HINTS:\n{rules}")

    game_data = _load_game_data()
    if game_data:
        parts.append(f"REFERENCE DATA (TrinityCore 12.1.0):\n{game_data}")

    repo_matches = None
    if query:
        repo_matches = _search_reference_repo(query)

        if mode == "sql" and repo_matches and repo_matches.get("matches"):
            def _wrong_db(path):
                pl = path.lower()
                if not pl.endswith(".sql"):
                    return False
                return "/characters/" in pl or "/auth/" in pl
            if all(_wrong_db(m["path"]) for m in repo_matches["matches"]):
                repo_matches = {
                    "repo": repo_matches.get("repo"),
                    "matches": [],
                    "source": repo_matches.get("source"),
                    "note": "no world-database matches",
                }

        formatted = _format_repo_matches(repo_matches)
        if formatted:
            parts.append(formatted)

    return "\n\n".join(parts), repo_matches


# =============================================================================
# Thinking-block splitter + placeholder stripper
# =============================================================================

_PLACEHOLDER_LINE = re.compile(
    r"^[ \t]*-[ \t]*"
    r"(?:"
    r"what to look at inside it|"
    r"what to verify inside it[^\n]*|"
    r"specific thing to verify[^\n]*|"
    r"specific row[^\n]*inspect[^\n]*|"
    r"specific row, function[^\n]*"
    r")"
    r"[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)


def _strip_placeholders(text):
    if not text:
        return text
    out = _PLACEHOLDER_LINE.sub("", text)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _split_thinking(text):
    """Return (reasoning, answer). Reasoning is anything inside a thinking
    block (paired or truncated); answer is everything outside it."""
    if not text:
        return "", ""

    reasoning_chunks = []
    answer = text

    for pat in (
        re.compile(r"\[\s*Start thinking\s*\](.*?)\[\s*End thinking\s*\]",
                   re.DOTALL | re.IGNORECASE),
        re.compile(r"<\s*think\s*>(.*?)<\s*/\s*think\s*>",
                   re.DOTALL | re.IGNORECASE),
    ):
        for m in pat.finditer(answer):
            reasoning_chunks.append(m.group(1).strip())
        answer = pat.sub("", answer)

    for pat in (
        re.compile(r"\[\s*Start thinking\s*\](.*)", re.DOTALL | re.IGNORECASE),
        re.compile(r"<\s*think\s*>(.*)", re.DOTALL | re.IGNORECASE),
    ):
        m = pat.search(answer)
        if m:
            reasoning_chunks.append(m.group(1).strip())
            answer = answer[:m.start()]

    answer = re.sub(r"\[\s*Prompt:.*?Generation:.*?\]", "", answer,
                    flags=re.DOTALL)
    answer = re.sub(r"\[\s*Start thinking\s*\]", "", answer,
                    flags=re.IGNORECASE)
    answer = re.sub(r"\[\s*End thinking\s*\]", "", answer,
                    flags=re.IGNORECASE)

    return "\n".join(reasoning_chunks).strip(), _strip_placeholders(answer)


# =============================================================================
# History helpers
# =============================================================================

def history_file():
    return os.path.join(HISTORY_DIR, "history.json")


_history_lock = threading.Lock()


def load_history():
    if not os.path.exists(history_file()):
        return []
    try:
        with open(history_file()) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_history(history):
    tmp = history_file() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(history, f, indent=2)
    os.replace(tmp, history_file())


def append_history(entry):
    with _history_lock:
        history = load_history()
        history.append(entry)
        save_history(history)


def update_history_entry(entry_id, fields):
    with _history_lock:
        history = load_history()
        for e in history:
            if e.get("id") == entry_id:
                e.update(fields)
                break
        save_history(history)


# =============================================================================
# llama-server client + llama-cli fallback
# =============================================================================

def _server_ready():
    try:
        with urlopen(f"{LLAMA_SERVER_URL}/health", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def _generate_via_server(prompt, system_prompt, max_tokens=GENERATE_MAX_TOKENS):
    body = json.dumps({
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode()
    req = Request(
        f"{LLAMA_SERVER_URL}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=300) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"]
    except (URLError, HTTPError, KeyError, json.JSONDecodeError, TimeoutError):
        return None


def _stream_via_server(prompt, system_prompt, max_tokens=GENERATE_MAX_TOKENS):
    """Generator yielding text chunks from llama-server's SSE stream."""
    body = json.dumps({
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "stream": True,
    }).encode()
    req = Request(
        f"{LLAMA_SERVER_URL}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=300) as r:
            for raw_line in r:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                    content = chunk["choices"][0].get("delta", {}).get("content")
                    if content:
                        yield content
                except (KeyError, IndexError, json.JSONDecodeError):
                    continue
    except (URLError, HTTPError, TimeoutError):
        return


def _generate_via_cli(prompt, system_prompt, max_tokens=GENERATE_MAX_TOKENS):
    result = subprocess.run(
        [
            LLAMA_BIN,
            "-m", MODEL,
            "--system-prompt", system_prompt,
            "-p", prompt,
            "-n", str(max_tokens),
            "--ctx-size", "4096",
            "--threads", "4",
            "--temp", "0.3",
        ],
        capture_output=True, text=True, timeout=300,
    )
    output = result.stdout.strip()
    if prompt in output:
        output = output.split(prompt, 1)[-1].strip()
    return output


# =============================================================================
# UI + static
# =============================================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(LOGO_DIR, filename)


# =============================================================================
# Generate — blocking (fallback + history)
# =============================================================================

@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.json
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "No prompt provided"}), 400

    mode = _detect_mode(prompt)
    system_prompt, repo_matches = _build_system_prompt(mode, query=prompt)

    matched_files = []
    if repo_matches and repo_matches.get("matches"):
        matched_files = [m["path"] for m in repo_matches["matches"]]

    search_source = (repo_matches or {}).get("source")

    entry = {
        "id": str(uuid.uuid4()),
        "timestamp": time.time(),
        "original": prompt,
        "refined": "",
        "reasoning": "",
        "status": "running",
        "mode": mode,
        "backend": None,
        "sources": matched_files,
        "search_source": search_source,
    }
    append_history(entry)
    entry_id = entry["id"]

    def run():
        fields = {}
        _mark_ai_busy()
        try:
            output = None
            if _server_ready():
                output = _generate_via_server(prompt, system_prompt)
                if output is not None:
                    fields["backend"] = "llama-server"

            if output is None:
                output = _generate_via_cli(prompt, system_prompt)
                fields["backend"] = "llama-cli"

            reasoning, answer = _split_thinking(output or "")
            fields["reasoning"] = reasoning
            fields["refined"] = answer if answer else "(empty response — try again)"
            fields["status"] = "done"
        except subprocess.TimeoutExpired:
            fields["status"] = "timeout"
        except Exception as e:
            fields["refined"] = str(e)
            fields["status"] = "error"
        finally:
            _mark_ai_idle()
        update_history_entry(entry_id, fields)

    threading.Thread(target=run, daemon=True).start()
    return jsonify(entry)


# =============================================================================
# Generate — streaming (SSE)
# =============================================================================

@app.route("/api/generate/stream", methods=["POST"])
def generate_stream():
    data = request.json
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "No prompt provided"}), 400

    mode = _detect_mode(prompt)
    system_prompt, repo_matches = _build_system_prompt(mode, query=prompt)

    matched_files = []
    if repo_matches and repo_matches.get("matches"):
        matched_files = [m["path"] for m in repo_matches["matches"]]
    search_source = (repo_matches or {}).get("source")

    entry = {
        "id": str(uuid.uuid4()),
        "timestamp": time.time(),
        "original": prompt,
        "refined": "",
        "reasoning": "",
        "status": "running",
        "mode": mode,
        "backend": None,
        "sources": matched_files,
        "search_source": search_source,
    }
    append_history(entry)
    entry_id = entry["id"]

    def event_stream():
        yield f"data: {json.dumps({'meta': entry})}\n\n"
        _mark_ai_busy()
        try:
            accumulated = ""
            backend = None
            last_reasoning = ""
            last_answer = ""

            for delta in _stream_via_server(prompt, system_prompt):
                if backend is None:
                    backend = "llama-server"
                accumulated += delta
                reasoning, answer = _split_thinking(accumulated)

                if reasoning != last_reasoning or answer != last_answer:
                    last_reasoning = reasoning
                    last_answer = answer
                    yield ("data: " + json.dumps({
                        "reasoning": reasoning,
                        "answer": answer,
                    }) + "\n\n")

            if backend is None:
                output = _generate_via_cli(prompt, system_prompt)
                backend = "llama-cli"
                accumulated = output or ""

            reasoning, answer = _split_thinking(accumulated)
            final_answer = answer if answer else "(empty response — try again)"

            update_history_entry(entry_id, {
                "refined": final_answer,
                "reasoning": reasoning,
                "status": "done",
                "backend": backend,
            })
            yield ("data: " + json.dumps({
                "done": True,
                "reasoning": reasoning,
                "refined": final_answer,
                "backend": backend,
            }) + "\n\n")

        except Exception as e:
            update_history_entry(entry_id, {"status": "error", "refined": str(e)})
            yield "data: " + json.dumps({"done": True, "error": str(e)}) + "\n\n"
        finally:
            _mark_ai_idle()

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# =============================================================================
# History endpoints
# =============================================================================

@app.route("/api/history")
def get_history():
    return jsonify(load_history())


@app.route("/api/history/<entry_id>")
def get_entry(entry_id):
    for e in load_history():
        if e["id"] == entry_id:
            return jsonify(e)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/history/clear", methods=["POST"])
def clear_history():
    with _history_lock:
        save_history([])
    return jsonify({"status": "cleared"})


@app.route("/api/history/<entry_id>", methods=["DELETE"])
def delete_entry(entry_id):
    with _history_lock:
        history = [e for e in load_history() if e["id"] != entry_id]
        save_history(history)
    return jsonify({"status": "deleted"})


# =============================================================================
# Repo search endpoints
# =============================================================================

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"repo": None, "matches": []})
    result = _search_reference_repo(q)
    if not result:
        return jsonify({"repo": None, "matches": []})
    return jsonify(result)


@app.route("/api/reference")
def api_reference():
    repos = _find_reference_repos()
    if not repos:
        return jsonify({"exists": False})
    return jsonify({
        "exists": True,
        "path": repos[0],
        "name": "/".join(os.path.basename(r) for r in repos),
        "repos": [os.path.basename(r) for r in repos],
    })


# =============================================================================
# Index management endpoints
# =============================================================================

@app.route("/api/index/status")
def index_status():
    exists = os.path.isfile(INDEX_DB)
    size = os.path.getsize(INDEX_DB) if exists else 0
    count = None
    index_error = None
    if exists:
        conn = None
        try:
            conn = _get_index_connection()
            if conn is not None:
                count = conn.execute("SELECT count(*) FROM files").fetchone()[0]
            else:
                index_error = "could not open index DB"
        except Exception as e:
            index_error = str(e)
        finally:
            if conn is not None:
                conn.close()
    mtime = None
    if exists:
        try:
            mtime = os.path.getmtime(INDEX_DB)
        except OSError:
            pass

    watcher_state = None
    try:
        with urlopen(f"{WATCHER_URL}/status", timeout=WATCHER_TIMEOUT) as r:
            watcher_state = json.loads(r.read())
    except Exception:
        watcher_state = None

    log_value = ""
    if watcher_state:
        wl = watcher_state.get("log")
        if isinstance(wl, list):
            log_value = "\n".join(wl[-50:])
        elif isinstance(wl, str):
            log_value = wl

    return jsonify({
        "exists": exists,
        "size": size,
        "count": count,
        "index_error": index_error,
        "mtime": mtime,
        "path": INDEX_DB,
        "running": bool((watcher_state or {}).get("running")),
        "log": log_value,
        "exit_code": None,
        "watcher_up": watcher_state is not None,
        "watcher_last_build_status": (watcher_state or {}).get("last_build_status"),
        "watcher_last_build": (watcher_state or {}).get("last_build"),
    })


@app.route("/api/index/rebuild", methods=["POST"])
def rebuild_index():
    all_sql = request.args.get("all_sql", "0") == "1"

    try:
        req = Request(f"{WATCHER_URL}/trigger", method="POST")
        with urlopen(req, timeout=2) as r:
            if r.status in (200, 202):
                return jsonify({"status": "triggered", "via": "watcher"})
    except Exception:
        pass

    state = app.config.setdefault("_index_state",
                                  {"running": False, "log": "", "exit_code": None})
    if state["running"]:
        return jsonify({"error": "Index rebuild already running"}), 409

    indexer_path = os.path.join(os.path.dirname(__file__), "indexer.py")
    if not os.path.isfile(indexer_path):
        return jsonify({"error": f"indexer.py not found at {indexer_path}"}), 500

    def run():
        state["running"] = True
        state["log"] = "starting (direct — watcher unavailable)...\n"
        state["exit_code"] = None
        try:
            env = {**os.environ}
            if all_sql:
                env["INDEX_ALL_SQL"] = "1"
            python = "/data/venvs/webui/bin/python"
            if not os.path.isfile(python):
                python = "python3"
            proc = subprocess.Popen(
                [python, indexer_path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env,
            )
            for line in iter(proc.stdout.readline, ""):
                state["log"] += line
            proc.wait()
            state["exit_code"] = proc.returncode
        except Exception as e:
            state["log"] += f"\nERROR: {e}\n"
            state["exit_code"] = 1
        finally:
            state["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "started", "via": "direct"})


@app.route("/api/watcher/status")
def watcher_status():
    try:
        with urlopen(f"{WATCHER_URL}/status", timeout=WATCHER_TIMEOUT) as r:
            return jsonify(json.loads(r.read()))
    except Exception as e:
        return jsonify({"error": str(e), "up": False}), 503


# =============================================================================
# Logo upload
# =============================================================================

@app.route("/api/upload-logo", methods=["POST"])
def upload_logo():
    if "logo" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["logo"]
    if not f.filename:
        return jsonify({"error": "No filename"}), 400
    if (request.content_length or 0) > 5 * 1024 * 1024:
        return jsonify({"error": "Logo too large (5 MB max)"}), 413
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return jsonify({"error": "Invalid image format"}), 400
    f.save(os.path.join(LOGO_DIR, "logo.png"))
    return jsonify({"status": "ok"})


# =============================================================================
# Version control
# =============================================================================

_remote_cache = {"hash": None, "checked_at": 0}
_REMOTE_CACHE_SECONDS = 60


def _git(args, timeout=30):
    if not os.path.isdir(SRC_DIR):
        return -1, "", "source directory missing"
    try:
        r = subprocess.run(
            ["git", "-C", SRC_DIR] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def _local_commit():
    code, out, _ = _git(["rev-parse", "HEAD"])
    return out if code == 0 else None


def _remote_commit():
    now = time.time()
    if now - _remote_cache["checked_at"] < _REMOTE_CACHE_SECONDS:
        return _remote_cache["hash"]
    code, out, _ = _git(["ls-remote", "origin", REPO_BRANCH], timeout=15)
    remote = out.split()[0] if (code == 0 and out) else None
    _remote_cache["hash"] = remote
    _remote_cache["checked_at"] = now
    return remote


@app.route("/api/version")
def get_version():
    local = _local_commit()
    remote = _remote_commit()
    return jsonify({
        "local": local,
        "remote": remote,
        "local_short": local[:7] if local else None,
        "remote_short": remote[:7] if remote else None,
        "up_to_date": (local == remote) if (local and remote) else None,
    })


# =============================================================================
# Update runner
# =============================================================================

_update_lock = threading.Lock()


def _update_unit_active():
    return subprocess.run(
        ["systemctl", "is-active", "--quiet", "dizercore-update"]
    ).returncode == 0


def _update_unit_result():
    r = subprocess.run(
        ["systemctl", "show", "dizercore-update", "-p", "Result", "--value"],
        capture_output=True, text=True, timeout=5)
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def _tail_file(path, max_lines=200):
    try:
        with open(path, "r", errors="replace") as f:
            return "".join(f.readlines()[-max_lines:])
    except OSError:
        return ""


@app.route("/api/update", methods=["POST"])
def start_update():
    with _update_lock:
        if _update_unit_active():
            return jsonify({"error": "Update already running"}), 409
        if not os.path.isfile(os.path.join(SRC_DIR, "install.sh")):
            return jsonify({"error": f"install.sh not found in {SRC_DIR}"}), 500
        try:
            with open(UPDATE_LOG, "w") as f:
                f.write("")
        except OSError:
            pass
        try:
            r = subprocess.run(
                ["sudo", "-n", "env", "DIZERCORE_UPDATE=1",
                 "DIZERCORE_NON_INTERACTIVE=1",
                 "/bin/bash", os.path.join(SRC_DIR, "install.sh")],
                cwd=SRC_DIR, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            if _update_unit_active():
                _remote_cache["checked_at"] = 0
                return jsonify({"status": "started"})
            return jsonify({"error": "launch timed out and no update unit is running"}), 500
    if r.returncode != 0:
        return jsonify({"error": f"launch failed: {r.stderr.strip() or r.stdout.strip()}"}), 500
    _remote_cache["checked_at"] = 0
    return jsonify({"status": "started"})


@app.route("/api/update/status")
def update_status():
    active = _update_unit_active()
    result = None if active else _update_unit_result()
    return jsonify({
        "running": active,
        "exit_code": None if active else (0 if result == "success" else 1),
        "log": _tail_file(UPDATE_LOG),
        "started_at": None,
    })


# =============================================================================
# System info
# =============================================================================

_info_cache = {"data": None, "checked_at": 0}
_INFO_CACHE_SECONDS = 30


def _shell(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _collect_system_info():
    info = {}
    errors = []

    try:
        code, out, _ = _git(["rev-parse", "--short", "HEAD"])
        info["dizercore_commit"] = out if code == 0 else None
    except Exception as e:
        errors.append(f"commit: {e}")
        info["dizercore_commit"] = None

    try:
        code, out, _ = _git(["log", "-1", "--format=%ci"])
        info["dizercore_date"] = out if code == 0 else None
    except Exception as e:
        errors.append(f"date: {e}")
        info["dizercore_date"] = None

    try:
        info["docker"] = _shell("docker --version") or None
    except Exception as e:
        errors.append(f"docker: {e}")
        info["docker"] = None

    try:
        info["compose"] = _shell("docker compose version --short") or None
    except Exception as e:
        errors.append(f"compose: {e}")
        info["compose"] = None

    try:
        info["gitea_image"] = _shell(
            f"docker inspect {GITEA_CONTAINER} --format '{{{{.Config.Image}}}}'"
        ) or None
    except Exception as e:
        errors.append(f"gitea: {e}")
        info["gitea_image"] = None

    try:
        info["postgres_image"] = _shell(
            f"docker inspect {POSTGRES_CONTAINER} --format '{{{{.Config.Image}}}}'"
        ) or None
    except Exception as e:
        errors.append(f"postgres: {e}")
        info["postgres_image"] = None

    try:
        info["llama_cpp"] = _shell(f"{LLAMA_BIN} --version 2>&1 | head -1") or None
    except Exception as e:
        errors.append(f"llama.cpp: {e}")
        info["llama_cpp"] = None

    try:
        info["llama_server"] = "running" if _server_ready() else "down"
    except Exception as e:
        errors.append(f"llama-server: {e}")
        info["llama_server"] = "unknown"

    try:
        rss = _shell("systemctl show llama-server.service -p MainPID --value")
        if rss and rss != "0":
            mem = _shell(f"ps -o rss= -p {rss} | awk '{{printf \"%.0f\", $1/1024}}'")
            info["llama_server_rss"] = f"{mem} MB" if mem else None
        else:
            info["llama_server_rss"] = None
    except Exception as e:
        errors.append(f"rss: {e}")
        info["llama_server_rss"] = None

    try:
        service_file = "/etc/systemd/system/llama-server.service"
        active_model = None
        if os.path.isfile(service_file):
            with open(service_file) as f:
                content = f.read().replace("\\\n", " ")
            for line in content.splitlines():
                if line.startswith("ExecStart="):
                    parts = line.split()
                    for i, tok in enumerate(parts):
                        if tok == "-m" and i + 1 < len(parts):
                            active_model = parts[i + 1].rstrip("\\").strip()
                            break
                    break
        info["model"] = os.path.basename(active_model) if active_model else None
        if active_model and os.path.isfile(active_model):
            size_mb = os.path.getsize(active_model) / (1024 * 1024)
            info["model_size"] = f"{size_mb:.0f} MB"
        else:
            info["model_size"] = None
    except Exception as e:
        errors.append(f"model: {e}")
        info["model"] = None
        info["model_size"] = None

    try:
        repos = _find_reference_repos()
        info["reference_repo"] = "/".join(
            os.path.basename(r) for r in repos) if repos else None
    except Exception as e:
        errors.append(f"reference: {e}")
        info["reference_repo"] = None

    try:
        if os.path.isfile(INDEX_DB):
            info["index_size"] = f"{os.path.getsize(INDEX_DB) / (1024*1024):.0f} MB"
            conn = _get_index_connection()
            try:
                if conn is not None:
                    info["index_files"] = conn.execute(
                        "SELECT count(*) FROM files").fetchone()[0]
                else:
                    info["index_files"] = None
            finally:
                if conn is not None:
                    conn.close()
        else:
            info["index_size"] = None
            info["index_files"] = None
    except Exception as e:
        errors.append(f"index: {e}")
        info["index_size"] = None
        info["index_files"] = None

    try:
        with urlopen(f"{WATCHER_URL}/status", timeout=WATCHER_TIMEOUT) as r:
            w = json.loads(r.read())
        info["watcher"] = "running"
        info["watcher_last_build"] = w.get("last_build_status") or "never"
    except Exception:
        info["watcher"] = "down"
        info["watcher_last_build"] = None

    try:
        if os.path.isfile(ACTIVITY_FILE):
            with open(ACTIVITY_FILE) as f:
                act = json.load(f)
            info["ai_busy"] = "yes" if act.get("busy") else "no"
        else:
            info["ai_busy"] = "no"
    except Exception:
        info["ai_busy"] = "unknown"

    try:
        if os.path.isfile(DATASET_FILE):
            info["training_dataset"] = f"{os.path.getsize(DATASET_FILE) / (1024*1024):.1f} MB"
            with open(DATASET_FILE) as f:
                info["training_dataset_examples"] = sum(1 for _ in f)
        else:
            info["training_dataset"] = None
            info["training_dataset_examples"] = None

        if os.path.isfile(TRAINED_MODEL):
            info["trained_model"] = f"{os.path.getsize(TRAINED_MODEL) / (1024*1024):.0f} MB"
        else:
            info["trained_model"] = None
    except Exception as e:
        errors.append(f"training: {e}")
        info["training_dataset"] = None
        info["training_dataset_examples"] = None
        info["trained_model"] = None

    try:
        info["history_count"] = len(load_history())
    except Exception as e:
        errors.append(f"history: {e}")
        info["history_count"] = None

    if errors:
        info["_errors"] = errors

    return info


@app.route("/api/system-info")
def get_system_info():
    try:
        now = time.time()
        if (now - _info_cache["checked_at"] < _INFO_CACHE_SECONDS
                and _info_cache["data"] is not None):
            return jsonify(_info_cache["data"])
        data = _collect_system_info()
        _info_cache["data"] = data
        _info_cache["checked_at"] = now
        return jsonify(data)
    except Exception as e:
        return jsonify({
            "error": str(e),
            "type": type(e).__name__,
            "dizercore_commit": None,
        }), 500


# =============================================================================
# Live system stats
# =============================================================================

def _read_temp():
    for path in ("/sys/class/thermal/thermal_zone0/temp",
                 "/sys/class/hwmon/hwmon0/temp1_input"):
        try:
            with open(path) as f:
                return int(f.read().strip()) / 1000.0
        except Exception:
            continue
    out = _shell("vcgencmd measure_temp 2>/dev/null")
    if out and "=" in out:
        try:
            return float(out.split("=")[1].split("'")[0])
        except Exception:
            pass
    return None


def _read_throttle():
    out = _shell("vcgencmd get_throttled 2>/dev/null")
    if not out or "=" not in out:
        return None
    raw = out.split("=")[1].strip()
    try:
        val = int(raw, 16)
    except ValueError:
        return {"raw": raw, "current": False, "occurred": False}
    return {"raw": raw, "current": bool(val & 0x7), "occurred": bool(val & 0x70000)}


def _read_gpu_freq():
    out = _shell("vcgencmd measure_clock v3d 2>/dev/null")
    if not out or "=" not in out:
        return None
    try:
        return int(out.split("=")[1].strip()) // 1_000_000
    except Exception:
        return None


def _read_cpu_freq():
    out = _shell("vcgencmd measure_clock arm 2>/dev/null")
    if not out or "=" not in out:
        return None
    try:
        return int(out.split("=")[1].strip()) // 1_000_000
    except Exception:
        return None


@app.route("/api/system-stats")
def get_system_stats():
    try:
        per_core = psutil.cpu_percent(interval=0.1, percpu=True)
        overall = sum(per_core) / len(per_core) if per_core else 0.0
    except Exception:
        per_core, overall = [], 0.0

    try:
        load1, load5, load15 = psutil.getloadavg()
    except Exception:
        load1 = load5 = load15 = 0.0

    try:
        mem = psutil.virtual_memory()
    except Exception:
        mem = None

    try:
        swap = psutil.swap_memory()
    except Exception:
        swap = None

    disk_pct = disk_used_gb = disk_total_gb = 0.0
    try:
        d = psutil.disk_usage("/data")
        disk_pct = d.percent
        disk_used_gb = d.used / (1024 ** 3)
        disk_total_gb = d.total / (1024 ** 3)
    except Exception:
        pass

    temp_c = _read_temp()
    return jsonify({
        "cpu": {
            "percent": round(overall, 1),
            "per_core": [round(v, 1) for v in per_core],
            "cores": len(per_core),
            "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
            "freq_mhz": _read_cpu_freq(),
        },
        "memory": {
            "used_mb": int(mem.used / (1024 ** 2)) if mem else 0,
            "total_mb": int(mem.total / (1024 ** 2)) if mem else 0,
            "percent": round(mem.percent, 1) if mem else 0.0,
        },
        "swap": {
            "used_mb": int(swap.used / (1024 ** 2)) if swap else 0,
            "total_mb": int(swap.total / (1024 ** 2)) if swap else 0,
            "percent": round(swap.percent, 1) if swap else 0.0,
        },
        "disk": {
            "used_gb": round(disk_used_gb, 1),
            "total_gb": round(disk_total_gb, 1),
            "percent": round(disk_pct, 1),
            "mount": "/data",
        },
        "temp_c": round(temp_c, 1) if temp_c is not None else None,
        "gpu_mhz": _read_gpu_freq(),
        "throttle": _read_throttle(),
    })


# =============================================================================
# Installer log
# =============================================================================

@app.route("/api/log")
def get_log():
    try:
        max_lines = int(request.args.get("lines", 500))
    except (TypeError, ValueError):
        max_lines = 500
    max_lines = max(1, min(max_lines, 10000))

    if not os.path.isfile(LOG_FILE):
        return jsonify({"exists": False, "lines": [], "size": 0,
                        "counts": {"install":0,"skip":0,"warn":0,"error":0,"step":0},
                        "path": LOG_FILE})

    try:
        with open(LOG_FILE, "r", errors="replace") as f:
            all_lines = f.readlines()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    counts = {"install": 0, "skip": 0, "warn": 0, "error": 0, "step": 0}
    for line in all_lines:
        if "[INSTALL]" in line: counts["install"] += 1
        elif "[SKIP" in line:    counts["skip"] += 1
        elif "[WARN" in line:    counts["warn"] += 1
        elif "[ERROR" in line:   counts["error"] += 1
        elif "[STEP" in line:    counts["step"] += 1

    tail = all_lines[-max_lines:]
    return jsonify({"exists": True, "lines": [l.rstrip("\n") for l in tail],
                    "size": os.path.getsize(LOG_FILE),
                    "total_lines": len(all_lines), "shown_lines": len(tail),
                    "counts": counts, "path": LOG_FILE})


@app.route("/api/log/download")
def download_log():
    if not os.path.isfile(LOG_FILE):
        return jsonify({"error": "log not found"}), 404
    return send_file(LOG_FILE, as_attachment=True,
                     download_name="dizercore-install.log",
                     mimetype="text/plain")


@app.route("/api/log/clear", methods=["POST"])
def clear_log():
    try:
        with open(LOG_FILE, "w") as f:
            f.write("")
        return jsonify({"status": "cleared"})
    except PermissionError:
        return jsonify({"error": "Permission denied. Run: sudo chown $USER /var/log/dizercore-install.log"}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Training endpoints
# =============================================================================

TRAINING_DEPLOY_SCRIPT = "/usr/local/sbin/dizercore-training-deploy"
DATASET_BUILDER = os.path.join(TRAINING_DIR, "dataset-builder.py")

_build_state = {"building": False, "log": "", "exit_code": None}
_build_lock = threading.Lock()


def _active_model_path():
    service_file = "/etc/systemd/system/llama-server.service"
    try:
        with open(service_file) as f:
            content = f.read().replace("\\\n", " ")
        for line in content.splitlines():
            if line.startswith("ExecStart="):
                parts = line.split()
                for i, tok in enumerate(parts):
                    if tok == "-m" and i + 1 < len(parts):
                        return parts[i + 1].rstrip("\\").strip()
    except Exception:
        pass
    return None


def _dataset_info():
    info = {"exists": False, "size": 0, "count": 0, "mtime": None}
    if not os.path.isfile(DATASET_FILE):
        return info
    info["exists"] = True
    info["size"] = os.path.getsize(DATASET_FILE)
    info["mtime"] = os.path.getmtime(DATASET_FILE)
    count_file = DATASET_FILE + ".count"
    try:
        with open(count_file) as f:
            info["count"] = int(f.read().strip())
    except Exception:
        try:
            with open(DATASET_FILE) as f:
                info["count"] = sum(1 for _ in f)
            with open(count_file, "w") as f:
                f.write(str(info["count"]))
        except Exception:
            info["count"] = 0
    return info


@app.route("/api/training/status")
def training_status():
    ds = _dataset_info()
    trained = {"exists": os.path.isfile(TRAINED_MODEL), "size": 0}
    if trained["exists"]:
        trained["size"] = os.path.getsize(TRAINED_MODEL)
    active = _active_model_path()
    active_kind = "trained" if (
        active and
        os.path.abspath(active) == os.path.abspath(TRAINED_MODEL)) else "base"
    return jsonify({
        "dataset": ds,
        "trained_model": trained,
        "active_model": active,
        "active_kind": active_kind,
    })


@app.route("/api/training/build-dataset", methods=["POST"])
def training_build_dataset():
    with _build_lock:
        if _build_state["building"]:
            return jsonify({"error": "Build already running"}), 409
        if not os.path.isfile(DATASET_BUILDER):
            return jsonify({
                "error": f"dataset-builder.py not found at {DATASET_BUILDER}"
            }), 500

        def run():
            _build_state["building"] = True
            _build_state["log"] = ""
            _build_state["exit_code"] = None
            try:
                python = "/data/venvs/webui/bin/python"
                if not os.path.isfile(python):
                    python = "python3"
                proc = subprocess.Popen(
                    [python, DATASET_BUILDER],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, cwd=TRAINING_DIR,
                )
                for line in iter(proc.stdout.readline, ""):
                    _build_state["log"] += line
                    if len(_build_state["log"]) > 200000:
                        _build_state["log"] = _build_state["log"][-100000:]
                proc.wait()
                _build_state["exit_code"] = proc.returncode
                try:
                    os.remove(DATASET_FILE + ".count")
                except OSError:
                    pass
            except Exception as e:
                _build_state["log"] += f"\nERROR: {e}\n"
                _build_state["exit_code"] = 1
            finally:
                _build_state["building"] = False

        threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/training/build-dataset/status")
def training_build_status():
    return jsonify(_build_state)


@app.route("/api/training/dataset/download")
def training_dataset_download():
    if not os.path.isfile(DATASET_FILE):
        return jsonify({"error": "dataset not built yet"}), 404
    return send_file(DATASET_FILE, as_attachment=True,
                     download_name="dizercore-dataset.jsonl",
                     mimetype="application/jsonl")


@app.route("/api/training/upload-model", methods=["POST"])
def training_upload_model():
    if "model" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["model"]
    if not f.filename or not f.filename.lower().endswith(".gguf"):
        return jsonify({"error": "Expected a .gguf file"}), 400
    os.makedirs(os.path.dirname(TRAINED_MODEL), exist_ok=True)
    tmp = TRAINED_MODEL + ".part"
    try:
        up = f.stream
        if up.read(4) != b"GGUF":
            return jsonify({"error": "Not a valid GGUF file"}), 400
        with open(tmp, "wb") as out:
            out.write(b"GGUF")
            while True:
                chunk = up.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        os.replace(tmp, TRAINED_MODEL)
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return jsonify({"error": str(e)}), 500
    r = subprocess.run(
        ["sudo", "-n", TRAINING_DEPLOY_SCRIPT, "deploy", TRAINED_MODEL],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return jsonify({
            "error": f"deploy failed: {r.stderr.strip() or r.stdout.strip()}"
        }), 500
    return jsonify({"status": "deployed", "model": TRAINED_MODEL})


@app.route("/api/training/revert", methods=["POST"])
def training_revert():
    r = subprocess.run(
        ["sudo", "-n", TRAINING_DEPLOY_SCRIPT, "revert"],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return jsonify({
            "error": f"revert failed: {r.stderr.strip() or r.stdout.strip()}"
        }), 500
    return jsonify({"status": "reverted"})


# =============================================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
