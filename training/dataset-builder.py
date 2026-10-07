#!/usr/bin/env python3  
# =============================================================================  
# DizerCore AI Assistant  
# -----------------------------------------------------------------------------  
# File:    training/dataset-builder.py  
# Purpose: Walk the reference repo and produce training examples that teach  
#          the Instruct model how the codebase is structured — file roles,  
#          function inventories, SQL table usage, and investigation prompts.  
#  
#          Tables are NOT hardcoded: a schema pass reads the repo's own SQL  
#          files (CREATE TABLE / INSERT INTO) to discover the real table set,  
#          which IDs exist in which tables, and which files reference each  
#          table. Investigation examples can therefore name real files and  
#          sibling tables and emit a ready-to-use prompt for a follow-up AI.  
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
  
SKIP_DIRS = {".git", "dep", "contrib", "doc", "docs", "tests", "cmake",  
             "build", "bin", "node_modules"}  
  
# Identifiers that follow a SQL keyword: FROM x, INSERT INTO x, UPDATE x,  
# JOIN x, DELETE FROM x, ALTER TABLE x, etc.  
TABLE_REF_RE = re.compile(  
    r"\b(?:from|into|update|join|table)\s+`?(\w+)`?",  
    re.IGNORECASE,  
)  
  
# Table declarations in schema/base files: CREATE TABLE `name` / IF NOT EXISTS  
CREATE_TABLE_RE = re.compile(  
    r"\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?`?(\w+)`?",  
    re.IGNORECASE,  
)  
  
# INSERT INTO `table` ... VALUES (12345, -> links real row IDs to tables  
INSERT_ID_RE = re.compile(  
    r"\binsert\s+into\s+`?(\w+)`?[^;]*?\bvalues\s*\(\s*(\d{1,10})\s*,",  
    re.IGNORECASE | re.DOTALL,  
)  
  
# Matches SQL keyword-embedded identifiers inside prose/code  
SQL_KEYWORD_BEFORE_RE = re.compile(  
    r"\b(?:from|into|update|join|table|delete\s+from)\s+`?(\w+)`?",  
    re.IGNORECASE,  
)  
  
# Common C++ types in TrinityCore so we can extract meaningful signatures  
TC_CLASSES = [  
    "Player", "Unit", "Creature", "Spell", "SpellInfo", "SpellMgr",  
    "WorldSession", "World", "ObjectMgr", "Map", "MapManager", "InstanceScript",  
    "CreatureAI", "ScriptedAI", "SmartAI", "SmartScript", "Quest", "Loot",  
    "Item", "GameObject", "TempSummon", "Pet", "Battleground",  
]  
  
# Names that look like tables after SQL keywords but are keywords/functions —  
# kept small; the real safety net is that only discovered schema names match.  
SQL_STOP_WORDS = {  
    "select", "set", "where", "values", "dual", "table", "tables",  
    "if", "not", "exists", "temporary", "like", "as", "on", "using",  
}  
  
# Maps the installed-model key (written to /data/.dizercore-model by the  
# installer when the user picks a model) to the HuggingFace repo the  
# notebook should train. Keeps training in the same family as whatever  
# llama-server is running.  
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
  
# Populated by discover_schema() before the example walk.  
KNOWN_TABLES = set()          # every real table name found in the repo  
TABLE_IDS = defaultdict(set)  # table -> set of real row ids seen in INSERTs  
TABLE_FILES = defaultdict(set)  # table -> set of rel paths that touch it  
  
  
def installed_base_model():  
    """Return the HF repo for the model the user installed, or the default."""  
    try:  
        with open(MODEL_KEY_FILE) as f:  
            key = f.read().strip()  
    except OSError:  
        key = ""  
    return BASE_MAP.get(key, DEFAULT_BASE), key or "(none)"  
  
  
def find_reference_repo():  
    if not os.path.isdir(REFERENCE_DIR):  
        return None  
    for name in sorted(os.listdir(REFERENCE_DIR)):  
        full = os.path.join(REFERENCE_DIR, name)  
        if os.path.isdir(full) and os.path.isdir(os.path.join(full, ".git")):  
            return full  
    return None  
  
  
# =============================================================================  
# Schema discovery — the repo tells us its own tables  
# =============================================================================  
  
def iter_repo_files(repo, exts=None):  
    """Yield (full_path, rel_path) for every includable file in the repo."""  
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
    """First pass: learn the repo's real tables, row ids, and file map.  
  
    Sources of truth, in order of reliability:  
      1. CREATE TABLE statements in schema/base .sql files (authoritative)  
      2. INSERT INTO `table` ... VALUES (id, ... in any .sql (also gives ids)  
      3. SQL-keyword references in any source file (fills TABLE_FILES)  
    """  
    print("Pass 1: discovering schema ...", flush=True)  
  
    for full, rel in iter_repo_files(repo, exts={".sql"}):  
        content = read_file(full)  
        if content is None:  
            continue  
        for m in CREATE_TABLE_RE.finditer(content):  
            KNOWN_TABLES.add(m.group(1).lower())  
        for m in INSERT_ID_RE.finditer(content):  
            table = m.group(1).lower()  
            KNOWN_TABLES.add(table)  
            if len(TABLE_IDS[table]) < MAX_IDS_PER_TABLE:  
                TABLE_IDS[table].add(m.group(2))  
            if len(TABLE_FILES[table]) < MAX_FILES_PER_TABLE:  
                TABLE_FILES[table].add(rel)  
  
    # Second sweep: which source files reference which (discovered) tables.  
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
                TABLE_FILES[name].add(rel)  
  
    # Fallback: if the repo has no schema files at all, accept any name that  
    # appears after a SQL keyword in .sql files (weaker, but better than none).  
    if not KNOWN_TABLES:  
        for full, rel in iter_repo_files(repo, exts={".sql"}):  
            content = read_file(full)  
            if content is None:  
                continue  
            for m in TABLE_REF_RE.finditer(content):  
                name = m.group(1).lower()  
                if name not in SQL_STOP_WORDS:  
                    KNOWN_TABLES.add(name)  
  
    print(f"  discovered {len(KNOWN_TABLES)} tables, "  
          f"{sum(len(v) for v in TABLE_IDS.values())} known row ids",  
          flush=True)  
  
  
def sibling_tables(table, limit=6):  
    """Tables sharing the first '_' segment — quest_template -> quest_* etc.  
  
    This is what lets an investigation example point at the addon/objective/  
    conditions tables without any of them being hardcoded.  
    """  
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
    """Return a list of (class_name, method_name, signature, body, line_start)."""  
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
    """Rough heuristic — extract identifiers followed by '(' inside a body."""  
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
    """Extract CMSG_*, SMSG_*, MSG_* identifiers."""  
    return sorted(set(re.findall(r"\b(?:CMSG|SMSG|MSG)_[A-Z0-9_]+\b", text)))  
  
  
def find_tables(text):  
    """Return which repo tables a file references.  
  
    Matches only names discovered in the schema pass, and only when a SQL  
    keyword precedes them — prose mentions can't false-positive.  
    """  
    found = set()  
    for m in TABLE_REF_RE.finditer(text):  
        name = m.group(1).lower()  
        if name in KNOWN_TABLES:  
            found.add(name)  
    return sorted(found)  
  
  
def find_related_ids(text):  
    """Look for spawn/quest/spell IDs that appear in the file."""  
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
# Example generators — one per category  
# =============================================================================  
  
def example_file_role(rel_path, content, category, fns=None, tables=None,  
                      opcodes=None):  
    """Whole-file role description."""  
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
    """Per-function inventory with call sites and the actual body."""  
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
    """SQL table usage with example rows."""  
    combined = "\n".join(snippets)  
    return make_example(  
        f"Show example SQL for the {table} table from {source_file}.",  
        f"File: {source_file}\nTable: {table}\n\n{combined}",  
    )  
  
  
# =============================================================================  
# Investigation examples — teach the model to hunt a bug end-to-end  
#  
# Given "quest X is not completing", the answer names the real files that  
# touch the data, the sibling tables that must agree, the exact SQL to run,  
# and finishes with a PROMPT FOR NEXT AI block the user can paste into any  
# other model to produce the actual fix.  
# =============================================================================  
  
def _other_files_for(table, exclude, limit=3):  
    others = sorted(f for f in TABLE_FILES.get(table, ()) if f != exclude)  
    return others[:limit]  
  
  
def example_sql_investigation(rel_path, content, table):  
    """Problem framed around a real table + real row id from the file."""  
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
  
    problem = (f"{table} entry {sample_id} is not behaving as expected "  
               f"(e.g. quest/item/spell not working).")  
    response = (  
        f"FILES TO CHECK:\n{files_block}\n"  
        f"WHAT TO VERIFY:\n" + "\n".join(verify) + "\n\n"  
        f"PROMPT FOR NEXT AI:\n{next_ai}"  
    )  
    return make_example(problem, response)  
  
  
def example_cpp_investigation(rel_path, fns, opcodes):  
    """Problem framed around a real function in the file."""  
    cls, method, sig, body, ln = fns[0]  
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
    problem = f"{cls}::{method} in {rel_path} is not behaving correctly."  
    return make_example(problem, response)  
  
  
def example_problem_investigation(rel_path, content, category, fns=None,  
                                  tables=None, opcodes=None):  
    """Dispatch to the SQL or C++ investigation generator."""  
    if tables is None:  
        tables = find_tables(content)  
    if opcodes is None:  
        opcodes = find_opcodes(content)  
    if fns is None:  
        fns = extract_functions(content) if category == "cpp" else []  
  
    if category == "sql" and tables:  
        return example_sql_investigation(rel_path, content, tables[0])  
    if category == "cpp" and fns:  
        return example_cpp_investigation(rel_path, fns, opcodes)  
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
    repo = find_reference_repo()  
    if not repo:  
        print(f"No reference repo under {REFERENCE_DIR}", file=sys.stderr, flush=True)  
        sys.exit(1)  
  
    base_repo, model_key = installed_base_model()  
    print(f"Building dataset from: {repo}", flush=True)  
    print(f"Output: {OUTPUT_FILE}", flush=True)  
    print(f"Base model tag: {base_repo} (install key '{model_key}')", flush=True)  
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)  
  
    discover_schema(repo)  
  
    count = 0  
    by_cat = {}  
    last_reported = 0  
    table_examples = 0  
    seen_table_fps = set()  
  
    with open(OUTPUT_FILE, "w") as out:  
        # Line 1: metadata record. The notebook reads base_model from it and  
        # filters it out of training examples.  
        meta = {"_meta": True, "base_model": base_repo, "install_key": model_key}  
        out.write(json.dumps(meta) + "\n")  
  
        print("Pass 2: generating examples ...", flush=True)  
        for full, rel in iter_repo_files(repo):  
            content = read_file(full)  
            if content is None:  
                continue  
  
            cat = category_for(os.path.splitext(full)[1].lower())  
            fns = extract_functions(content) if cat in ("cpp", "header") else []  
            tables = find_tables(content)  
            opcodes = find_opcodes(content)  
            size = len(content.encode("utf-8", "ignore"))  
  
            # --- Example 1: file role (small files) ---  
            if size <= MAX_WHOLE_FILE_BYTES:  
                ex = example_file_role(rel, content, cat,  
                                       fns=fns, tables=tables, opcodes=opcodes)  
                out.write(json.dumps(ex) + "\n")  
                count += 1  
                by_cat["file_role"] = by_cat.get("file_role", 0) + 1  
  
            # --- Example 2: per-function inventory (C++ files only) ---  
            if cat in ("cpp", "header"):  
                for cls, method, sig, body, ln in fns[:6]:  
                    ex = example_function(cls, method, sig, body, rel, ln)  
                    out.write(json.dumps(ex) + "\n")  
                    count += 1  
                    by_cat["function"] = by_cat.get("function", 0) + 1  
  
            # --- Example 3: table usage (SQL files, deduped + capped) ---  
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
                    ex = example_table_usage(table, matches, rel)  
                    out.write(json.dumps(ex) + "\n")  
                    count += 1  
                    table_examples += 1  
                    by_cat["table"] = by_cat.get("table", 0) + 1  
  
            # --- Example 4: investigation (1 per file) ---  
            inv = example_problem_investigation(rel, content, cat,  
                                              fns=fns, tables=tables,  
                                              opcodes=opcodes)  
            if inv:  
                out.write(json.dumps(inv) + "\n")  
                count += 1  
                by_cat["investigation"] = by_cat.get("investigation", 0) + 1  
  
            if count - last_reported >= 1000:  
                last_reported = count  
                print(f"  {count} examples so far ...", flush=True)  
  
    print(flush=True)  
    print(f"Total examples: {count}", flush=True)  
    for cat, n in sorted(by_cat.items(), key=lambda x: -x[1]):  
        print(f"  {cat}: {n}", flush=True)  
    print(f"Tables discovered: {len(KNOWN_TABLES)}", flush=True)  
    print(f"Base model: {base_repo}", flush=True)  
    print(f"Written to: {OUTPUT_FILE}", flush=True)  
  
  
if __name__ == "__main__":  
    build()
