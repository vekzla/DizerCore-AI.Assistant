# =============================================================================  
# DizerCore AI Assistant  
# -----------------------------------------------------------------------------  
# File:    web-ui/app.py  
# Purpose: Flask backend. Talks to llama-server (persistent, fast) with a  
#          fallback to llama-cli subprocess. Searches the reference repo using  
#          a SQLite FTS5 index when available, or ripgrep as fallback.  
#          Registers the Training blueprint for LoRA dataset + model mgmt.  
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
from flask import (Flask, render_template, request, jsonify,  
                   send_from_directory, send_file)  
from werkzeug.exceptions import HTTPException  
from flask_cors import CORS  
import psutil  
  
from training import training_bp  
  
app = Flask(__name__)  
CORS(app)  
  
# Allow up to 4 GB uploads for the trained GGUF  
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  
app.register_blueprint(training_bp)  
  
MODEL = os.environ.get("MODEL_PATH", "/data/models/qwen2.5-1.5b-instruct-q4_k_m.gguf")  
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
  
REPO_SEARCH_MAX_FILES = 8  
REPO_SEARCH_CONTEXT_LINES = 4  
REPO_SEARCH_CHAR_LIMIT = 6000  
REPO_SEARCH_TIMEOUT = 10  
REPO_SEARCH_MAX_FILES_PER_KEYWORD = 500  
  
# Investigation output is multi-section (FILES TO CHECK / WHAT TO VERIFY /  
# PROMPT FOR NEXT AI) — 256 tokens truncated it mid-block.  
GENERATE_MAX_TOKENS = 700  
  
# Seconds to wait for the index-watcher control API. Was 1s — too short  
# while the indexer saturates the Pi's CPU, which made the UI report  
# "watcher: down" even though the service was running.  
WATCHER_TIMEOUT = 5  
  
REPO_EXCLUDE_GLOBS = [  
    "!dep/**", "!contrib/**", "!doc/**", "!tests/**", "!cmake/**",  
    "!.git/**", "!node_modules/**", "!*.min.*",  
]  
  
os.makedirs(HISTORY_DIR, exist_ok=True)  
os.makedirs(LOGO_DIR, exist_ok=True)  
  
  
# =============================================================================  
# Global error handler  
# =============================================================================  
  
@app.errorhandler(Exception)  
def handle_exception(e):  
    if isinstance(e, HTTPException):  
        return e  
    tb = traceback.format_exc()  
    return jsonify({  
        "error": str(e),  
        "type": type(e).__name__,  
        "traceback": tb.split("\n")[-6:],  
    }), 500  
  
  
# =============================================================================  
# AI activity signalling — tells index-watcher.py when to pause  
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
# constant in training/dizercore-colab.ipynb — the LoRA adapter is trained  
# on that exact persona, so runtime and training must match.  
#  
# _detect_mode() no longer selects a persona; it only picks which domain  
# hint lines get appended to this shared prompt.  
# =============================================================================  
  
SYSTEM_PROMPT = """You are a software engineer building a TrinityCore WoW emulation server. You investigate issues and produce structured prompts listing which files/folders to check and what to verify — SQL or C++ code. You never write the fix itself.  
  
The user describes a problem. You respond with an investigation plan, NOT a fix.  
  
OUTPUT FORMAT (use exactly these three section headers):  
  
FILES TO CHECK:  
1. <real file path — prefer paths from the matched files below>  
   - what to look at inside it  
2. <next file or folder>  
   - what to look at inside it  
  
WHAT TO VERIFY:  
- <specific check — a SQL query to run, a function behaviour to trace, a schema agreement to confirm>  
- <next check>  
  
PROMPT FOR NEXT AI:  
<A ready-to-paste paragraph for a follow-up AI. It names the exact file(s), function(s), table(s) and row id(s) discovered above, states what to inspect, and defines what "done" looks like. This is the deliverable — make it self-contained.>  
  
RULES:  
- Name REAL files, tables, columns, functions, opcodes and constants from the matched excerpts. Never invent them  
- Never write the actual fix, the corrective SQL, or replacement C++ code  
- If a detail is not in the matches, write "verify against the actual schema/source" instead of guessing  
- End the PROMPT FOR NEXT AI block with a Success: line stating the observable in-game result"""  
  
# Per-domain rule lines appended to the shared prompt. _detect_mode() picks  
# ONE of these — the persona and output format never change.  
DOMAIN_RULES = {  
    "cpp": (  
        "- Prefer files under src/server/game/ and name the real "  
        "Class::method when the matches show one\n"  
        "- List callers/early-exit paths as things to verify"  
    ),  
    "sql": (  
        "- World DB tables: quest_template, quest_poi, quest_poi_points, "  
        "smart_scripts, creature_template, spell_area, conditions\n"  
        "- There is NO generic 'status' column on quests — never suggest one\n"  
        "- 12.x quest/world data lives under sql/old/12.x/world/ — use real paths"  
    ),  
    "smart": (  
        "- Use SMART_ACTION_*, SMART_EVENT_*, SMART_TARGET_* constants only "  
        "when shown in the matches\n"  
        "- smart_scripts columns: entryorguid, source_type, event_type, "  
        "action_type, action_param1-6, target_type"  
    ),  
    "opcode": (  
        "- Reference real CMSG_*/SMSG_*/MSG_* names and handler files\n"  
        "- If unsure of an opcode, say \"verify against Opcodes.h for 12.1.0\""  
    ),  
    "dbc": (  
        "- Reference actual DBC/DB2 file names and loader functions\n"  
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
# Domain classifier — selects hint lines only, not a persona  
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
    # domain noise — appears in nearly every prompt and nearly every file  
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
    """Per-keyword MATCH counts so we can pick the rarest as anchor."""  
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
# FTS5 index — primary search path  
# =============================================================================  
  
def _get_index_connection():  
    """  
    Open a fresh read-only SQLite connection to the FTS5 index.  
  
    Note: we do NOT cache the connection. SQLite connections are bound to  
    the thread that created them, and Flask serves each request on a  
    different worker thread. Opening a connection to a read-only SQLite  
    file costs ~1 ms — negligible compared to the FTS5 query itself.  
    """  
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
        return conn.execute(  
            """  
            SELECT path,  
                   snippet(files, 1, '', '', ' … ', 48) AS snippet,  
                   bm25(files) AS score  
            FROM files  
            WHERE files MATCH ?  
            ORDER BY rank  
            LIMIT ?  
            """,  
            (fts_query, REPO_SEARCH_MAX_FILES),  
        ).fetchall()  
  
    rows = []  
    error = None  
    try:  
        # Rare keywords anchor the search; common ones would match  
        # thousands of files and dilute bm25.  
        counts = _index_keyword_counts(conn, keywords)  
        informative = sorted(  
            (k for k, n in counts.items()  
             if 0 < n <= REPO_SEARCH_MAX_FILES_PER_KEYWORD),  
            key=lambda k: counts[k],  
        )  
  
        if informative:  
            # AND the two rarest; fall back to rarest alone  
            if len(informative) >= 2:  
                rows = _run(" AND ".join(  
                    f'"{k}"' for k in informative[:2]))  
            if not rows:  
                rows = _run(f'"{informative[0]}"')  
  
        # Final fallback: plain OR over all keywords  
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
  
    # Search every repo under /data/reference (source repo + schema repo)  
    keyword_files = {}  
    for kw in keywords:  
        files = []  
        for repo in repos:  
            for f in _ripgrep_files(kw, repo):  
                # Prefix with repo basename so paths match the FTS index  
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
  
    candidate_files.sort(key=_score_file_for_priority)  
    candidate_files = candidate_files[:REPO_SEARCH_MAX_FILES]  
  
    matches = []  
    total_chars = 0  
    for rel_prefixed in candidate_files:  
        # rel_prefixed is "<repo>/<rel>" — resolve it back to a real path  
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
    """ALL git repos under REFERENCE_DIR (source repo + schema repo)."""  
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
    """First repo — used only where a single display name is needed."""  
    repos = _find_reference_repos()  
    return repos[0] if repos else None  
  
  
def _search_reference_repo(query):  
    result = _search_via_index(query)  
    if result is not None and result.get("matches"):  
        return result  
    # FTS5 returned nothing (or errored) — try ripgrep  
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
        "\nIMPORTANT: The files above are REAL excerpts from the user's "  
        "reference repo. Use the ACTUAL file paths, table names, column "  
        "names, and ID numbers from these matches in your FILES TO CHECK "  
        "section. Do NOT invent file names or column names. If a specific "  
        "detail isn't in the matches, write \"verify against the actual "  
        "schema/source\" instead of guessing."  
    )  
    return "\n".join(lines)  
  
  
def _build_system_prompt(mode, query=""):  
    parts = [SYSTEM_PROMPT]  
  
    # Domain hint lines — appended to the shared persona, never a replacement  
    rules = DOMAIN_RULES.get(mode)  
    if rules:  
        parts.append(f"DOMAIN HINTS:\n{rules}")  
  
    game_data = _load_game_data()  
    if game_data:  
        parts.append(f"REFERENCE DATA (TrinityCore 12.1.0):\n{game_data}")  
  
    repo_matches = None  
    if query:  
        repo_matches = _search_reference_repo(query)  
        formatted = _format_repo_matches(repo_matches)  
        if formatted:  
            parts.append(formatted)  
  
    return "\n\n".join(parts), repo_matches  
  
  
# =============================================================================  
# Thinking-block stripper  
# =============================================================================  
  
_THINK_PATTERNS = [  
    re.compile(r"\[\s*Start thinking\s*\].*?\[\s*End thinking\s*\]", re.DOTALL | re.IGNORECASE),  
    re.compile(r"<\s*think\s*>.*?<\s*/\s*think\s*>", re.DOTALL | re.IGNORECASE),  
    re.compile(r"\[\s*Start thinking\s*\].*", re.DOTALL | re.IGNORECASE),  
    re.compile(r"\[\s*Prompt:.*?Generation:.*?\]", re.DOTALL),  
]  
  
  
def _strip_thinking(text):  
    if not text:  
        return text  
    out = text  
    for pat in _THINK_PATTERNS:  
        out = pat.sub("", out)  
    return out.strip()  
  
  
# =============================================================================  
# History helpers  
# =============================================================================  
  
def history_file():  
    return os.path.join(HISTORY_DIR, "history.json")  
  
  
def load_history():  
    if os.path.exists(history_file()):  
        with open(history_file()) as f:  
            return json.load(f)  
    return []  
  
  
def save_history(history):  
    with open(history_file(), "w") as f:  
        json.dump(history, f, indent=2)  
  
  
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
# Generate  
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
        "status": "running",  
        "mode": mode,  
        "backend": None,  
        "sources": matched_files,  
        "search_source": search_source,  
    }  
    history = load_history()  
    history.append(entry)  
    save_history(history)  
  
    def run():  
        _mark_ai_busy()  
        try:  
            output = None  
            if _server_ready():  
                output = _generate_via_server(prompt, system_prompt)  
                if output is not None:  
                    entry["backend"] = "llama-server"  
  
            if output is None:  
                output = _generate_via_cli(prompt, system_prompt)  
                entry["backend"] = "llama-cli"  
  
            output = _strip_thinking(output or "")  
            entry["refined"] = output if output else "(empty response — try again)"  
            entry["status"] = "done"  
        except subprocess.TimeoutExpired:  
            entry["status"] = "timeout"  
        except Exception as e:  
            entry["refined"] = str(e)  
            entry["status"] = "error"  
        finally:  
            _mark_ai_idle()  
        save_history(history)  
  
    threading.Thread(target=run, daemon=True).start()  
    return jsonify(entry)  
  
  
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
    save_history([])  
    return jsonify({"status": "cleared"})  
  
  
@app.route("/api/history/<entry_id>", methods=["DELETE"])  
def delete_entry(entry_id):  
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
        try:  
            conn = _get_index_connection()  
            if conn is not None:  
                count = conn.execute("SELECT count(*) FROM files").fetchone()[0]  
                conn.close()  
            else:  
                index_error = "could not open index DB"  
        except Exception as e:  
            index_error = str(e)  
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
# Update runner — install.sh re-execs itself into a detached systemd unit so  
# the update survives the prompt-gateway restart that happens mid-install.  
# Status is read from the on-disk log + systemctl, not in-memory state.  
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
        # NOTE: sudo's env_reset strips DIZERCORE_UPDATE=1 — pass the flags  
        # through `env` inside the sudo command so install.sh sees them and  
        # self-detaches into the dizercore-update systemd unit.  
        try:  
            r = subprocess.run(  
                ["sudo", "-n", "env", "DIZERCORE_UPDATE=1",  
                 "DIZERCORE_NON_INTERACTIVE=1",  
                 "/bin/bash", os.path.join(SRC_DIR, "install.sh")],  
                cwd=SRC_DIR, capture_output=True, text=True, timeout=30)  
        except subprocess.TimeoutExpired:  
            # sudo may be killed mid-detach — check whether the unit actually  
            # registered before reporting failure.  
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
  
    # ---------- Active model (parses ExecStart with line continuations) ----------  
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
            if conn is not None:  
                info["index_files"] = conn.execute("SELECT count(*) FROM files").fetchone()[0]  
                conn.close()  
            else:  
                info["index_files"] = None  
        else:  
            info["index_size"] = None  
            info["index_files"] = None  
    except Exception as e:  
        errors.append(f"index: {e}")  
        info["index_size"] = None  
        info["index_files"] = None  
  
    # Watcher state — WATCHER_TIMEOUT keeps this working while a build  
    # saturates the CPU (was timeout=1, which reported a live busy  
    # watcher as "down").  
    try:  
        with urlopen(f"{WATCHER_URL}/status", timeout=WATCHER_TIMEOUT) as r:  
            w = json.loads(r.read())  
        info["watcher"] = "running"  
        info["watcher_last_build"] = w.get("last_build_status") or "never"  
    except Exception:  
        info["watcher"] = "down"  
        info["watcher_last_build"] = None  
  
    # AI busy flag  
    try:  
        if os.path.isfile(ACTIVITY_FILE):  
            with open(ACTIVITY_FILE) as f:  
                act = json.load(f)  
            info["ai_busy"] = "yes" if act.get("busy") else "no"  
        else:  
            info["ai_busy"] = "no"  
    except Exception:  
        info["ai_busy"] = "unknown"  
  
    # Training state  
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
  
if __name__ == "__main__":  
    app.run(host="0.0.0.0", port=5000, debug=False)
