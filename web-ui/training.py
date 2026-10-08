#!/usr/bin/env python3  
# =============================================================================  
# DizerCore AI Assistant  
# -----------------------------------------------------------------------------  
# File:    training/dataset-builder.py  
# Purpose: Walk ALL reference repos under /data/reference (TrinityCore mirror  
#          + per-table schema dump repos) and produce training examples that  
#          teach the model how the codebase is structured — file roles,  
#          function inventories, SQL table usage, and investigation prompts.  
#  
#          Tables are NOT hardcoded: a schema pass reads each repo's own SQL  
#          files (CREATE TABLE / INSERT INTO) to discover the real table set,  
#          which IDs exist in which tables, and which files reference each  
#          table. Investigation examples therefore name real files and  
#          sibling tables and emit a ready-to-use prompt for a follow-up AI.  
#  
#          Paths written into examples are "<RepoName>/<relpath>" — identical  
#          to what the FTS5 index stores, so runtime matches line up with  
#          what the adapter was trained on.  
#  
# Output:  /data/training/dizercore-dataset.jsonl  
#          Line 1 is a metadata record carrying the base model repo so the  
#          training notebook automatically trains whatever model the user  
#          installed on the Pi.  
# =============================================================================  
  
import hashlib  
import json  
import os  
import re  
import sys  
from collections import defaultdict  
  
REFERENCE_DIR = os.environ.get("REFERENCE_DIR", "/data/reference")  
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "/data/training/dizercore-dataset.jsonl")  
  
INCLUDE_EXTENSIONS = {".cpp", ".h", ".hpp", ".sql", ".cs", ".inc", ".lua"}  
MAX_WHOLE_FILE_BYTES = 4000  
MAX_CHUNK_BYTES = 2500  
MAX_FILE_BYTES = 500 * 1024  
MAX_TABLE_EXAMPLES = 500  
MAX_IDS_PER_TABLE = 40  
MAX_FILES_PER_TABLE = 20  
  
# Investigation examples: up to this many phrasing variants emitted per file  
# during the walk, then upsampled until they reach this fraction of the  
# dataset (they're what teaches the runtime output format).  
INV_VARIANTS_PER_FILE = 2  
INV_TARGET_FRACTION = 0.15  
INV_POOL_CAP = 6000  
  
SKIP_DIRS = {".git", "dep", "contrib", "doc", "docs", "tests", "cmake",  
             "build", "bin", "node_modules"}  
  
TABLE_REF_RE = re.compile(  
    r"\b(?:from|into|update|join|table)\s+`?(\w+)`?",  
    re.IGNORECASE,  
)  
  
CREATE_TABLE_RE = re.compile(  
    r"\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?`?(\w+)`?",  
    re.IGNORECASE,  
)  
  
INSERT_ID_RE = re.compile(  
    r"\binsert\s+into\s+`?(\w+)`?[^;]*?\bvalues\s*\(\s*(\d{1,10})\s*,",  
    re.IGNORECASE | re.DOTALL,  
)  
  
TC_CLASSES = [  
    "Player", "Unit", "Creature", "Spell", "SpellInfo", "SpellMgr",  
    "WorldSession", "World", "ObjectMgr", "Map", "MapManager", "InstanceScript",  
    "CreatureAI", "ScriptedAI", "SmartAI", "SmartScript", "Quest", "Loot",  
    "Item", "GameObject", "TempSummon", "Pet", "Battleground",  
]  
  
SQL_STOP_WORDS = {  
    "select", "set", "where", "values", "dual", "table", "tables",  
    "if", "not", "exists", "temporary", "like", "as", "on", "using",  
}  
  
MODEL_KEY_FILE = "/data/.dizercore-model"  
BASE_MAP = {  
    "0.5b":      "Qwen/Qwen2.5-Coder-0.5B-Instruct",  
    "0.5b-base": "Qwen/Qwen2.5-0.5B-Instruct",  
    "1.5b":      "Qwen/Qwen2.5-Coder-1.5B-Instruct",  
    "1.5b-base": "Qwen/Qwen2.5-1.5B-Instruct",  
    "3b":        "Qwen/Qwen2.5-Coder-3B-Instruct",  
    "3b-base":   "Qwen/Qwen2.5-3B-Instruct",  
}  
DEFAULT_BASE = "Qwen/Qwen2.5-1.5B-Instruct"  
  
KNOWN_TABLES = set()  
TABLE_IDS = defaultdict(set)  
TABLE_FILES = defaultdict(set)  
  
# ---------------------------------------------------------------------------  
# Varied problem phrasings — one formulaic template taught the model to key  
# off a fixed string instead of real user language. variant selection is  
# hash-stable so a rebuild produces deterministic output.  
# ---------------------------------------------------------------------------  
  
PROBLEM_SQL_TEMPLATES = [  
    "{table} entry {id} is not behaving as expected (quest/item/spell not working).",  
    "Players report the content tied to {table} entry {id} is broken.",  
    "After the latest update, {table} row {id} stopped working.",  
    "{table} entry {id}: worked on the old core, broken now — regression?",  
    "Something is wrong with {table} entry {id}; in-game behaviour is off.",  
    "Need to troubleshoot {table} entry {id} — nothing happens when it should.",  
    "Quest/NPC/item from {table} (entry {id}) does nothing when triggered.",  
    "Investigate why {table} entry {id} does not work in-game.",  
]  
  
PROBLEM_SQL_CONTENT_TEMPLATES = [  
    "I want to add a new quest/NPC/item using {table} — what has to be wired up?",  
    "New content task: create an entry in {table} and make it work in-game.",  
    "Walk me through adding a row to {table} so the content actually functions.",  
]  
  
PROBLEM_CPP_TEMPLATES = [  
    "{cls}::{method} in {rel} is not behaving correctly.",  
    "Regression: {cls}::{method} ({rel}) used to work, now it misbehaves.",  
    "Bug report: triggering {cls}::{method} produces wrong results.",  
    "{cls}::{method} in {rel} returns early / does nothing in some cases.",  
    "Crash or fault suspected somewhere around {cls}::{method} in {rel}.",  
    "Troubleshoot {cls}::{method} ({rel}) — the in-game effect is missing.",  
    "{cls}::{method} in {rel} silently fails under certain conditions.",  
    "Figure out why {cls}::{method} ({rel}) is not being reached.",  
]  
  
  
def _pick(templates, *keys):  
    h = hashlib.md5("|".join(str(k) for k in keys).encode()).hexdigest()  
    return templates[int(h, 16) % len(templates)]  
  
  
def installed_base_model():  
    try:  
        with open(MODEL_KEY_FILE) as f:  
            key = f.read().strip()  
    except OSError:  
        key = ""  
    return BASE_MAP.get(key, DEFAULT_BASE), key or "(none)"  
  
  
def find_reference_repos():  
    """Every git repo under REFERENCE_DIR — mirrors indexer.py so the schema  
    dump repo contributes tables and examples instead of being ignored."""  
    if os.path.isdir(os.path.join(REFERENCE_DIR, ".git")):  
        return [REFERENCE_DIR]  
    repos = []  
    if not os.path.isdir(REFERENCE_DIR):  
        return repos  
    for name in sorted(os.listdir(REFERENCE_DIR)):  
        full = os.path.join(REFERENCE_DIR, name)  
        if os.path.isdir(full) and os.path.isdir(os.path.join(full, ".git")):  
            repos.append(full)  
    return repos  
  
  
def repo_rel(repo, rel):  
    """Prefix a repo-relative path with the repo basename — matches what the  
    indexer stores in the FTS5 path column."""  
    return f"{os.path.basename(repo)}/{rel}"  
  
  
# =============================================================================  
# Schema discovery — the repos tell us their own tables  
# =============================================================================  
  
def iter_repo_files(repo, exts=None):  
    for root, dirs, files in os.walk(repo):  
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]  
        for fname in files:  
            ext = os.path.splitext(fname)[1].lower()  
            if exts is not None and ext not in exts:  
                continue  
            if ext not in INCLUDE_EXTENSIONS:  
                continue  
            yield os.path.join(root, fname), os.path.relpath(  
                os.path.join(root, fname), repo)  
  
  
def read_file(full):  
    try:  
        if os.path.getsize(full) > MAX_FILE_BYTES:  
            return None  
        with open(full, "r", errors="ignore") as f:  
            content = f.read()  
        return content if len(content.strip()) >= 50 else None  
    except Exception:  
        return None  
  
  
def discover_schema(repo):  
    """First pass over ONE repo: learn its tables, row ids, and file map."""  
    repo_name = os.path.basename(repo)  
    print(f"Pass 1 ({repo_name}): discovering schema ...", flush=True)  
  
    for full, rel in iter_repo_files(repo, exts={".sql"}):  
        content = read_file(full)  
        if content is None:  
            continue  
        rel_prefixed = repo_rel(repo, rel)  
        for m in CREATE_TABLE_RE.finditer(content):  
            KNOWN_TABLES.add(m.group(1).lower())  
        for m in INSERT_ID_RE.finditer(content):  
            table = m.group(1).lower()  
            KNOWN_TABLES.add(table)  
            if len(TABLE_IDS[table]) < MAX_IDS_PER_TABLE:  
                TABLE_IDS[table].add(m.group(2))  
            if len(TABLE_FILES[table]) < MAX_FILES_PER_TABLE:  
                TABLE_FILES[table].add(rel_prefixed)  
  
    for full, rel in iter_repo_files(repo):  
        if rel.endswith(".sql"):  
            continue  
        content = read_file(full)  
        if content is None:  
            continue  
        for m in TABLE_REF_RE.finditer(content):  
            name = m.group(1).lower()  
            if name in KNOWN_TABLES and \  
                    len(TABLE_FILES[name]) < MAX_FILES_PER_TABLE:  
                TABLE_FILES[name].add(repo_rel(repo, rel))  
  
    if not KNOWN_TABLES:  
        for full, rel in iter_repo_files(repo, exts={".sql"}):  
            content = read_file(full)  
            if content is None:  
                continue  
            for m in TABLE_REF_RE.finditer(content):  
                name = m.group(1).lower()  
                if name not in SQL_STOP_WORDS:  
                    KNOWN_TABLES.add(name)  
  
  
def sibling_tables(table, limit=6):  
    if "_" not in table:  
        return []  
    prefix = table.split("_", 1)[0] + "_"  
    sibs = sorted(t for t in KNOWN_TABLES  
                  if t.startswith(prefix) and t != table)  
    return sibs[:limit]  
  
  
# =============================================================================  
# Content extraction helpers  
# =============================================================================  
  
def extract_functions(content):  
    pattern = re.compile(  
        r"^([A-Za-z_][A-Za-z0-9_:<>\*&,\s]+?)\s+"  
        r"([A-Za-z_][A-Za-z0-9_]*)::([A-Za-z_~][A-Za-z0-9_]*)\s*"  
        r"\(([^)]*)\)\s*"  
        r"(?:const\s*)?\{",  
        re.MULTILINE,  
    )  
    out = []  
    for m in pattern.finditer(content):  
        start = m.start()  
        depth = 0  
        end = None  
        for i in range(m.end() - 1, min(m.end() + 12000, len(content))):  
            ch = content[i]  
            if ch == "{":  
                depth += 1  
            elif ch == "}":  
                depth -= 1  
                if depth == 0:  
                    end = i + 1  
                    break  
        if end and (end - start) <= MAX_CHUNK_BYTES:  
            line_start = content.count("\n", 0, start) + 1  
            cls = m.group(2)  
            method = m.group(3)  
            sig = f"{m.group(1).strip()} {cls}::{method}({m.group(4).strip()})"  
            out.append((cls, method, sig, content[start:end], line_start))  
    return out  
  
  
def find_called_functions(body):  
    calls = set()  
    for m in re.finditer(r"\b([A-Z][A-Za-z0-9_]+)::([A-Za-z_][A-Za-z0-9_]*)\s*\(", body):  
        calls.add(f"{m.group(1)}::{m.group(2)}")  
    for m in re.finditer(r"\b([a-z_][A-Za-z0-9_]+)\s*\(", body):  
        name = m.group(1)  
        if name in {"if", "while", "for", "switch", "return", "sizeof", "assert"}:  
            continue  
        if len(name) < 3:  
            continue  
        calls.add(name)  
    return sorted(calls)[:8]  
  
  
def find_opcodes(text):  
    return sorted(set(re.findall(r"\b(?:CMSG|SMSG|MSG)_[A-Z0-9_]+\b", text)))  
  
  
def find_tables(text):  
    found = set()  
    for m in TABLE_REF_RE.finditer(text):  
        name = m.group(1).lower()  
        if name in KNOWN_TABLES:  
            found.add(name)  
    return sorted(found)  
  
  
def find_related_ids(text):  
    ids = set()  
    for m in re.finditer(r"\b(?:ID|Id|id|entry|Entry)\s*[=:]\s*(\d{3,8})\b", text):  
        ids.add(m.group(1))  
    for m in re.finditer(r"\bVALUES\s*\(\s*(\d{3,8})\s*,", text, re.IGNORECASE):  
        ids.add(m.group(1))  
    return sorted(ids)[:10]  
  
  
def make_example(instruction, output):  
    return {  
        "instruction": instruction,  
        "input": "",  
        "output": output.strip(),  
    }  
  
  
# =============================================================================  
# Example generators  
# =============================================================================  
  
def example_file_role(rel_path, content, category, fns=None, tables=None,  
                      opcodes=None):  
    if tables is None:  
        tables = find_tables(content)  
    if fns is None:  
        fns = extract_functions(content) if category in ("cpp", "header") else []  
    if opcodes is None:  
        opcodes = find_opcodes(content)  
  
    lines = [f"File: {rel_path}", f"Category: {category}", ""]  
    if category == "cpp":  
        lines.append("Purpose: C++ source. Defines classes and functions that implement game logic.")  
        if fns:  
            lines.append("")  
            lines.append(f"Functions defined ({len(fns)}):")  
            for cls, method, sig, _, ln in fns[:15]:  
                lines.append(f"  - {cls}::{method}  (line {ln})")  
    elif category == "header":  
        lines.append("Purpose: C++ header. Declares classes and interfaces used by source files.")  
    elif category == "sql":  
        lines.append("Purpose: World database update. Contains INSERT/UPDATE/DELETE rows for game content.")  
        if tables:  
            lines.append("")  
            lines.append(f"Tables modified: {', '.join(tables)}")  
            ids_here = find_related_ids(content)  
            if ids_here:  
                lines.append(f"Row ids touched: {', '.join(ids_here[:8])}")  
    elif category == "cs":  
        lines.append("Purpose: C# integration file.")  
    elif category == "lua":  
        lines.append("Purpose: Eluna Lua script.")  
    if opcodes:  
        lines.append("")  
        lines.append(f"Opcodes referenced: {', '.join(opcodes[:6])}")  
    return make_example(  
        f"Explain the role of the file {rel_path}.",  
        "\n".join(lines),  
    )  
  
  
def example_function(cls, method, sig, body, rel_path, line_num):  
    calls = find_called_functions(body)  
    tables = find_tables(body)  
    opcodes = find_opcodes(body)  
  
    body_lines = body.splitlines()  
    body_excerpt = "\n".join(body_lines[:40])  
    if len(body_lines) > 40:  
        body_excerpt += "\n    // ... (truncated)"  
  
    lines = [  
        f"File: {rel_path} (line {line_num})",  
        f"Function: {cls}::{method}",  
        f"Signature: {sig}",  
        "",  
        "Body:",  
        body_excerpt,  
    ]  
    if calls:  
        lines.append("")  
        lines.append("Calls into:")  
        for c in calls:  
            lines.append(f"  - {c}")  
    if tables:  
        lines.append("")  
        lines.append(f"Reads/writes tables: {', '.join(tables)}")  
    if opcodes:  
        lines.append("")  
        lines.append(f"Handles opcodes: {', '.join(opcodes)}")  
    return make_example(  
        f"Describe what {cls}::{method} does and what it interacts with.",  
        "\n".join(lines),  
    )  
  
  
def example_table_usage(table, snippets, source_file):  
    combined = "\n".join(snippets)  
    return make_example(  
        f"Show example SQL for the {table} table from {source_file}.",  
        f"File: {source_file}\nTable: {table}\n\n{combined}",  
    )  
  
  
# =============================================================================  
# Investigation examples — FILES TO CHECK / WHAT TO VERIFY / PROMPT FOR NEXT AI  
# =============================================================================  
  
def _other_files_for(table, exclude, limit=3):  
    others = sorted(f for f in TABLE_FILES.get(table, ()) if f != exclude)  
    return others[:limit]  
  
  
def example_sql_investigation(rel_path, content, table, variant=0):  
    ids = sorted(TABLE_IDS.get(table, ())) or find_related_ids(content)  
    sample_id = ids[0] if ids else "<id>"  
    sibs = sibling_tables(table)  
    others = _other_files_for(table, rel_path)  
  
    files_block = f"1. {rel_path}\n" \  
        f"   - Rows with id/entry = {sample_id} in the {table} statements\n"  
    n = 2  
    for f in others:  
        files_block += f"{n}. {f}\n   - Other rows touching {table}\n"  
        n += 1  
    if sibs:  
        files_block += f"{n}. Search the repo for `{sibs[0]}` (and: {', '.join(sibs[1:4])})\n" \  
            f"   - Companion rows this entry depends on\n"  
  
    verify = [  
        f"- SELECT * FROM {table} WHERE entry = {sample_id};",  
    ]  
    for s in sibs[:3]:  
        verify.append(f"- SELECT * FROM {s} WHERE entry = {sample_id};  -- companion data must exist and agree")  
    verify.append("- A later SQL update has not overwritten the row")  
  
    next_ai = (  
        f"Table {table}, entry {sample_id}.\n"  
        f"Run and inspect:\n"  
        + "\n".join(f"  {v[2:]}" for v in verify[:4])  
        + f"\nCheck the companion tables agree on entry {sample_id} "  
        f"({', '.join(sibs[:4]) or 'none discovered'}).\n"  
        f"If any required row is missing or mismatched, write the corrective "  
        f"INSERT/UPDATE and explain which engine table reads it at runtime.\n"  
        f"Success: the content tied to {sample_id} behaves correctly in-game."  
    )  
  
    # variant 0-2: broken-content phrasings; variant 3+: new-content phrasing  
    if variant < 3:  
        problem = PROBLEM_SQL_TEMPLATES[  
            (int(hashlib.md5(f"{rel_path}|{table}|{variant}".encode()).hexdigest(), 16))  
            % len(PROBLEM_SQL_TEMPLATES)  
        ].format(table=table, id=sample_id, rel=rel_path)  
    else:  
        problem = PROBLEM_SQL_CONTENT_TEMPLATES[  
            (int(hashlib.md5(f"{rel_path}|{table}|{variant}".encode()).hexdigest(), 16))  
            % len(PROBLEM_SQL_CONTENT_TEMPLATES)  
        ].format(table=table, id=sample_id, rel=rel_path)  
  
    response = (  
        f"FILES TO CHECK:\n{files_block}\n"  
        f"WHAT TO VERIFY:\n" + "\n".join(verify) + "\n\n"  
        f"PROMPT FOR NEXT AI:\n{next_ai}"  
    )  
    return make_example(problem, response)  
  
  
def example_cpp_investigation(rel_path, fns, opcodes, variant=0):  
    cls, method, sig, body, ln = fns[variant % len(fns)]  
    calls = find_called_functions(body)  
    header_guess = os.path.splitext(os.path.basename(rel_path))[0] + ".h"  
  
    response = (  
        f"FILES TO CHECK:\n"  
        f"1. {rel_path}\n"  
        f"   - Function {cls}::{method} at line {ln}\n"  
        f"   - Its return path and early exits\n"  
        f"2. {header_guess} (or the header declaring {cls})\n"  
        f"   - Declaration vs definition mismatch\n"  
        f"3. Search the repo for callers of {cls}::{method}\n"  
        f"   - What arguments callers pass and what they do with the result\n"  
        f"\nWHAT TO VERIFY:\n"  
        f"- Arguments are read in the order callers pass them\n"  
        f"- Expected helpers run before the return: "  
        f"{', '.join(calls[:4]) if calls else 'none detected'}\n"  
        + (f"- Opcode handling is correct: {', '.join(opcodes[:3])}\n" if opcodes else "")  
        + f"\nPROMPT FOR NEXT AI:\n"  
        f"In {rel_path}, function {cls}::{method} (line {ln}):\n"  
        f"- Trace the return path and list every early exit\n"  
        f"- Confirm these helpers are called and their results checked: "  
        f"{', '.join(calls[:4]) if calls else 'none'}\n"  
        f"- Compare the signature with its declaration in {header_guess}\n"  
        f"Success: the function produces the expected behaviour\n"  
        f"Test: trigger the in-game condition that invokes this function"  
    )  
    problem = PROBLEM_CPP_TEMPLATES[  
        (int(hashlib.md5(f"{rel_path}|{cls}|{method}|{variant}".encode()).hexdigest(), 16))  
        % len(PROBLEM_CPP_TEMPLATES)  
    ].format(cls=cls, method=method, rel=rel_path)  
    return make_example(problem, response)  
  
  
def example_problem_investigation(rel_path, content, category, fns=None,  
                                  tables=None, opcodes=None, variant=0):  
    if tables is None:  
        tables = find_tables(content)  
    if opcodes is None:  
        opcodes = find_opcodes(content)  
    if fns is None:  
        fns = extract_functions(content) if category == "cpp" else []  
  
    if category == "sql" and tables:  
        table = tables[variant % len(tables)]  
        return example_sql_investigation(rel_path, content, table, variant)  
    if category == "cpp" and fns:  
        return example_cpp_investigation(rel_path, fns, opcodes, variant)  
    return None  
  
  
# =============================================================================  
# Main build  
# =============================================================================  
  
def category_for(ext):  
    return {  
        ".cpp": "cpp", ".h": "header", ".hpp": "header",  
        ".sql": "sql", ".cs": "cs", ".inc": "header", ".lua": "lua",  
    }.get(ext, "other")  
  
  
def build():  
    repos = find_reference_repos()  
    if not repos:  
        print(f"No reference repo under {REFERENCE_DIR}", file=sys.stderr, flush=True)  
        sys.exit(1)  
  
    base_repo, model_key = installed_base_model()  
    print(f"Building dataset from {len(repos)} repo(s):", flush=True)  
    for r in repos:  
        print(f"  - {r}", flush=True)  
    print(f"Output: {OUTPUT_FILE}", flush=True)  
    print(f"Base model tag: {base_repo} (install key '{model_key}')", flush=True)  
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)  
  
    for repo in repos:  
        discover_schema(repo)  
    print(f"  discovered {len(KNOWN_TABLES)} tables, "  
          f"{sum(len(v) for v in TABLE_IDS.values())} known row ids",  
          flush=True)  
  
    count = 0  
    by_cat = {}  
    last_reported = 0  
    table_examples = 0  
    seen_table_fps = set()  
    inv_pool = []  # (rel_prefixed, content, cat, fns, tables, opcodes)  
  
    def emit(ex, cat):  
        nonlocal count  
        out.write(json.dumps(ex) + "\n")  
        count += 1  
        by_cat[cat] = by_cat.get(cat, 0) + 1  
  
    with open(OUTPUT_FILE, "w") as out:  
        meta = {"_meta": True, "base_model": base_repo, "install_key": model_key}  
        out.write(json.dumps(meta) + "\n")  
  
        print("Pass 2: generating examples ...", flush=True)  
        for repo in repos:  
            for full, rel in iter_repo_files(repo):  
                content = read_file(full)  
                if content is None:  
                    continue  
  
                rel_prefixed = repo_rel(repo, rel)  
                cat = category_for(os.path.splitext(full)[1].lower())  
                fns = extract_functions(content) if cat in ("cpp", "header") else []  
                tables = find_tables(content)  
                opcodes = find_opcodes(content)  
                size = len(content.encode("utf-8", "ignore"))  
  
                if size <= MAX_WHOLE_FILE_BYTES:  
                    emit(example_file_role(rel_prefixed, content, cat,  
                                           fns=fns, tables=tables,  
                                           opcodes=opcodes),  
                         "file_role")  
  
                if cat in ("cpp", "header"):  
                    for cls, method, sig, body, ln in fns[:6]:  
                        emit(example_function(cls, method, sig, body,  
                                              rel_prefixed, ln),  
                             "function")  
  
                if cat == "sql" and table_examples < MAX_TABLE_EXAMPLES:  
                    for table in tables[:4]:  
                        pat = re.compile(  
                            rf"^.*?\b{re.escape(table)}\b.*?;",  
                            re.MULTILINE | re.IGNORECASE,  
                        )  
                        matches = pat.findall(content)[:3]  
                        if not matches:  
                            continue  
                        fp = hashlib.md5(  
                            (table + "|" + matches[0]).encode()  
                        ).hexdigest()  
                        if fp in seen_table_fps:  
                            continue  
                        seen_table_fps.add(fp)  
                        emit(example_table_usage(table, matches, rel_prefixed),  
                             "table")  
                        table_examples += 1  
  
                # Investigation: up to INV_VARIANTS_PER_FILE phrasing variants  
                # per eligible file — the output-format examples matter most.  
                eligible = (cat == "sql" and tables) or (cat == "cpp" and fns)  
                if eligible:  
                    for v in range(INV_VARIANTS_PER_FILE):  
                        inv = example_problem_investigation(  
                            rel_prefixed, content, cat, fns=fns,  
                            tables=tables, opcodes=opcodes, variant=v)  
                        if inv:  
                            emit(inv, "investigation")  
                    if len(inv_pool) < INV_POOL_CAP:  
                        inv_pool.append(  
                            (rel_prefixed, content, cat, fns, tables, opcodes))  
  
                if count - last_reported >= 1000:  
                    last_reported = count  
                    print(f"  {count} examples so far ...", flush=True)  
  
        # --- Upsample investigation until it reaches the target fraction ---  
        inv_n = by_cat.get("investigation", 0)  
        target = int(count * INV_TARGET_FRACTION)  
        vround = INV_VARIANTS_PER_FILE  
        while inv_n < target and inv_pool and vround < 12:  
            print(f"Upsampling investigation (variant round {vround}, "  
                  f"{inv_n}/{target}) ...", flush=True)  
            for rel_p, content, cat, fns, tables, opcodes in inv_pool:  
                if inv_n >= target:  
                    break  
                inv = example_problem_investigation(  
                    rel_p, content, cat, fns=fns, tables=tables,  
                    opcodes=opcodes, variant=vround)  
                if inv:  
                    emit(inv, "investigation")  
                    inv_n += 1  
                    target = int(count * INV_TARGET_FRACTION)  
            vround += 1  
  
    print(flush=True)  
    print(f"Total examples: {count}", flush=True)  
    for cat, n in sorted(by_cat.items(), key=lambda x: -x[1]):  
        pct = 100.0 * n / count if count else 0  
        print(f"  {cat}: {n} ({pct:.1f}%)", flush=True)  
    print(f"Tables discovered: {len(KNOWN_TABLES)}", flush=True)  
    print(f"Base model: {base_repo}", flush=True)  
    print(f"Written to: {OUTPUT_FILE}", flush=True)  
  
  
if __name__ == "__main__":  
    build()
