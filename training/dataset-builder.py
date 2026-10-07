#!/usr/bin/env python3  
# =============================================================================  
# DizerCore AI Assistant  
# -----------------------------------------------------------------------------  
# File:    training/dataset-builder.py  
# Purpose: Walk the reference repo and produce training examples that teach  
#          the Instruct model how TrinityCore is structured — file roles,  
#          function inventories, SQL table usage, and cross-references.  
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
  
REFERENCE_DIR = os.environ.get("REFERENCE_DIR", "/data/reference")  
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "/data/training/dizercore-dataset.jsonl")  
  
INCLUDE_EXTENSIONS = {".cpp", ".h", ".hpp", ".sql", ".cs", ".inc", ".lua"}  
MAX_WHOLE_FILE_BYTES = 4000  
MAX_CHUNK_BYTES = 2500  
MAX_FILE_BYTES = 500 * 1024  
MAX_TABLE_EXAMPLES = 500  
  
SKIP_DIRS = {".git", "dep", "contrib", "doc", "docs", "tests", "cmake",  
             "build", "bin", "node_modules"}  
  
# TrinityCore world DB tables commonly encountered  
TC_TABLES = [  
    "quest_template", "quest_template_addon", "quest_objectives",  
    "quest_poi", "quest_poi_points", "smart_scripts", "creature_template",  
    "creature_template_addon", "gameobject_template", "spell_area",  
    "spell_script_names", "spell_proc", "spell_group", "conditions",  
    "creature_loot_template", "reference_loot_template", "gameobject_loot_template",  
    "page_text", "npc_text", "gossip_menu", "gossip_menu_option",  
    "playercreateinfo", "playercreateinfo_spell", "skill_line_ability",  
    "creature", "gameobject", "waypoint_path", "waypoint_path_node",  
]  
TC_TABLE_SET = set(TC_TABLES)  
  
# Matches an identifier that follows a SQL keyword: FROM x, INSERT INTO x,  
# UPDATE x, JOIN x, DELETE FROM x, ALTER TABLE x, etc.  
TABLE_REF_RE = re.compile(  
    r"\b(?:from|into|update|join|table)\s+`?(\w+)`?",  
    re.IGNORECASE,  
)  
  
# Common C++ types in TrinityCore so we can extract meaningful signatures  
TC_CLASSES = [  
    "Player", "Unit", "Creature", "Spell", "SpellInfo", "SpellMgr",  
    "WorldSession", "World", "ObjectMgr", "Map", "MapManager", "InstanceScript",  
    "CreatureAI", "ScriptedAI", "SmartAI", "SmartScript", "Quest", "Loot",  
    "Item", "GameObject", "TempSummon", "Pet", "Battleground",  
]  
  
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
    """Return which TrinityCore world tables a file references.  
  
    Requires SQL-keyword context (FROM/INTO/UPDATE/JOIN/TABLE) so prose  
    mentions like 'conditions' or 'creature' don't false-positive.  
    """  
    found = set()  
    for m in TABLE_REF_RE.finditer(text):  
        name = m.group(1).lower()  
        if name in TC_TABLE_SET:  
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
  
def example_file_role(rel_path, content, category, fns=None, tables=None, opcodes=None):  
    """Whole-file role description."""  
    if tables is None:  
        tables = find_tables(content)  
    if fns is None:  
        fns = extract_functions(content) if category in ("cpp", "header") else []  
    if opcodes is None:  
        opcodes = find_opcodes(content)  
  
    lines = [f"File: {rel_path}", f"Category: {category}", ""]  
    if category == "cpp":  
        lines.append("Purpose: TrinityCore C++ source. Defines classes and functions that implement game logic.")  
        if fns:  
            lines.append("")  
            lines.append(f"Functions defined ({len(fns)}):")  
            for cls, method, sig, _, ln in fns[:15]:  
                lines.append(f"  - {cls}::{method}  (line {ln})")  
    elif category == "header":  
        lines.append("Purpose: TrinityCore C++ header. Declares classes and interfaces used by source files.")  
    elif category == "sql":  
        lines.append("Purpose: TrinityCore world database update. Contains INSERT/UPDATE/DELETE rows for game content.")  
        if tables:  
            lines.append("")  
            lines.append(f"Tables modified: {', '.join(tables)}")  
    elif category == "cs":  
        lines.append("Purpose: TrinityCore C# integration file.")  
    elif category == "lua":  
        lines.append("Purpose: TrinityCore Eluna Lua script.")  
    if opcodes:  
        lines.append("")  
        lines.append(f"Opcodes referenced: {', '.join(opcodes[:6])}")  
    return make_example(  
        f"Explain the role of the file {rel_path} in TrinityCore.",  
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
        f"Describe what TrinityCore's {cls}::{method} does and what it interacts with.",  
        "\n".join(lines),  
    )  
  
  
def example_table_usage(table, snippets, source_file):  
    """SQL table usage with example rows."""  
    combined = "\n".join(snippets)  
    return make_example(  
        f"Show example SQL for the TrinityCore {table} table from {source_file}.",  
        f"File: {source_file}\nTable: {table}\n\n{combined}",  
    )  
  
  
def example_problem_investigation(rel_path, content, category, fns=None, tables=None, opcodes=None):  
    """  
    Synthesize a problem + investigation checklist from a file's content.  
    This is the example type that most directly teaches the output format.  
    """  
    if tables is None:  
        tables = find_tables(content)  
    if opcodes is None:  
        opcodes = find_opcodes(content)  
    if fns is None:  
        fns = extract_functions(content) if category == "cpp" else []  
  
    if category == "sql" and tables:  
        table = tables[0]  
        ids = find_related_ids(content)  
        sample_id = ids[0] if ids else "<id>"  
        problem = f"The {table} data for entry {sample_id} is not behaving as expected."  
        response = (  
            f"FILES TO CHECK:\n"  
            f"1. {rel_path}\n"  
            f"   - Look for: rows with entry = {sample_id} in the {table} block\n"  
            f"   - Look for: any conditions or related rows the entry depends on\n"  
            f"\n"  
            f"WHAT TO VERIFY:\n"  
            f"- The {table} row for {sample_id} has all required columns set\n"  
            f"- Any foreign key references resolve in related tables: {', '.join(tables[1:4]) or 'none detected'}\n"  
            f"- The row has not been superseded by a later SQL update\n"  
            f"\n"  
            f"PROMPT FOR NEXT AI:\n"  
            f"In {table}:\n"  
            f"- Verify the row for entry {sample_id} has complete column values\n"  
            f"- Check that all referenced IDs exist in their target tables\n"  
            f"- Compare against the base schema if columns are missing\n"  
            f"Reference: {table}, {', '.join(tables[1:3]) or 'related tables'}\n"  
            f"Success: the {table} entry behaves as expected in-game\n"  
            f"Test: trigger the content that uses {sample_id} and confirm correct behaviour"  
        )  
        return make_example(problem, response)  
  
    if category == "cpp" and fns:  
        cls, method, sig, body, ln = fns[0]  
        calls = find_called_functions(body)  
        problem = f"The TrinityCore function {cls}::{method} is not behaving correctly."  
        response = (  
            f"FILES TO CHECK:\n"  
            f"1. {rel_path}\n"  
            f"   - Look for: function {cls}::{method} at line {ln}\n"  
            f"   - Look for: the logic inside it and its return path\n"  
            + (f"2. (search for callers of {cls}::{method})\n   - Look for: how this function is invoked and with what arguments\n" if not calls else "")  
            + f"\nWHAT TO VERIFY:\n"  
            f"- The function reads its arguments correctly from the caller\n"  
            f"- It calls the expected helpers: {', '.join(calls[:4]) if calls else 'none detected'}\n"  
            f"- The return value matches what callers expect\n"  
            + (f"- Opcode handling is correct: {', '.join(opcodes[:3])}\n" if opcodes else "")  
            + f"\nPROMPT FOR NEXT AI:\n"  
            f"In {rel_path}, function {cls}::{method}:\n"  
            f"- Verify the function reads the correct arguments from its caller\n"  
            f"- Confirm it calls the expected helpers before returning\n"  
            f"- Check that the return value matches the expected type\n"  
            f"Reference: {rel_path}, {cls}.h\n"  
            f"Success: the function returns without error and produces the expected behaviour\n"  
            f"Test: trigger the condition that calls this function and observe the result"  
        )  
        return make_example(problem, response)  
  
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
  
        for root, dirs, files in os.walk(repo):  
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]  
  
            for fname in files:  
                ext = os.path.splitext(fname)[1].lower()  
                if ext not in INCLUDE_EXTENSIONS:  
                    continue  
                full = os.path.join(root, fname)  
                rel = os.path.relpath(full, repo)  
  
                try:  
                    size = os.path.getsize(full)  
                    if size > MAX_FILE_BYTES:  
                        continue  
                    with open(full, "r", errors="ignore") as f:  
                        content = f.read()  
                except Exception:  
                    continue  
                if len(content.strip()) < 50:  
                    continue  
  
                cat = category_for(ext)  
  
                fns = extract_functions(content) if cat in ("cpp", "header") else []  
                tables = find_tables(content)  
                opcodes = find_opcodes(content)  
  
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
  
                # --- Example 4: synthesized investigation (1 per file) ---  
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
    print(f"Base model: {base_repo}", flush=True)  
    print(f"Written to: {OUTPUT_FILE}", flush=True)  
  
  
if __name__ == "__main__":  
    build()
