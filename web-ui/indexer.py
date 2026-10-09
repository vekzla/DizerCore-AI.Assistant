#!/usr/bin/env python3  
# =============================================================================  
# DizerCore AI Assistant  
# -----------------------------------------------------------------------------  
# File:    web-ui/indexer.py  
# Purpose: Build a SQLite FTS5 index of ALL reference repos found under  
#          /data/reference (the TrinityCore mirror plus any schema-dump  
#          repos). Indexed paths are stored as "<RepoName>/<relpath>".  
#          Only indexes the current expansion's SQL (12.x) plus base schema  
#          plus source code. Set INDEX_ALL_SQL=1 to index every SQL file.  
# =============================================================================  
  
import fcntl  
import os  
import sqlite3  
import sys  
import time  
  
REPO_DIR = os.environ.get("REFERENCE_DIR", "/data/reference")  
DB_FILE = os.environ.get("INDEX_DB", "/data/web-ui/dizercore-index.db")  
  
SQL_INCLUDE_DIRS = {"12.x"}  
INCLUDE_BASE_SQL = True  
INDEX_ALL_SQL = os.environ.get("INDEX_ALL_SQL", "0") == "1"  
  
INDEX_EXTENSIONS = {  
    ".cpp", ".c", ".h", ".hpp", ".cc", ".inl",  
    ".sql", ".py", ".lua",  
    ".cs", ".inc", ".ipp", ".md", ".txt",  
}  
  
MAX_FILE_SIZE = 5 * 1024 * 1024  
  
  
def find_reference_repos():  
    """Return EVERY git repo under REPO_DIR, sorted by name.  
  
    The schema-dump repos the user pushes to Gitea land here as siblings of  
    the main TrinityCore mirror. Returning only the first one would silently  
    ignore them.  
    """  
    if os.path.isdir(os.path.join(REPO_DIR, ".git")):  
        return [REPO_DIR]  
    repos = []  
    if not os.path.isdir(REPO_DIR):  
        return repos  
    for name in sorted(os.listdir(REPO_DIR)):  
        full = os.path.join(REPO_DIR, name)  
        if os.path.isdir(full) and os.path.isdir(os.path.join(full, ".git")):  
            repos.append(full)  
    return repos  
  
  
def should_include_path(rel_path):  
    parts = rel_path.split(os.sep)  
    if not parts:  
        return True  
    top = parts[0]  
    if top in {".git", "dep", "contrib", "doc", "docs", "tests",  
               "node_modules", "build", "bin", "cmake"}:  
        return False  
    if top == "src":  
        return True  
    if top == "sql":  
        if INDEX_ALL_SQL:  
            return True  
        if len(parts) < 2:  
            return False  
        section = parts[1]  
        if section == "base":  
            return INCLUDE_BASE_SQL  
        if section == "old" and len(parts) >= 3:  
            return parts[2] in SQL_INCLUDE_DIRS  
        if section == "updates" and len(parts) >= 3:  
            return parts[2] in SQL_INCLUDE_DIRS  
        return False  
    # Secondary repos (schema dumps etc.) have no src/sql layout — index  
    # their top-level .sql/.txt/.md files so CREATE TABLE dumps are found.  
    if top not in {"src", "sql"}:  
        return True  
    return False  
  
  
def build_index():  
    repos = find_reference_repos()  
    if not repos:  
        print(f"No reference repo found under {REPO_DIR}", file=sys.stderr)  
        sys.exit(1)  
  
    print(f"Indexing {len(repos)} repo(s):")  
    for r in repos:  
        print(f"  - {r}")  
    print(f"Database: {DB_FILE}")  
    if INDEX_ALL_SQL:  
        print("Mode: ALL SQL versions")  
    else:  
        print(f"Mode: SQL versions {sorted(SQL_INCLUDE_DIRS)}"  
              f"{' + base schema' if INCLUDE_BASE_SQL else ''}")  
    print()  
  
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)  
  
    # Exclusive lock so a manual run and the watcher can never race  
    lock_path = DB_FILE + ".lock"  
    lock_fd = open(lock_path, "w")  
    try:  
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  
    except OSError:  
        print("Another index build is already in progress — exiting",  
              file=sys.stderr)  
        sys.exit(2)  
  
    # Per-process temp file, then atomic rename. A second process can no  
    # longer delete or steal this file mid-build.  
    tmp_db = f"{DB_FILE}.building.{os.getpid()}"  
    if os.path.exists(tmp_db):  
        os.remove(tmp_db)  
  
    conn = sqlite3.connect(tmp_db)  
    conn.executescript("""  
        PRAGMA journal_mode = MEMORY;  
        PRAGMA synchronous = OFF;  
        PRAGMA temp_store = MEMORY;  
        PRAGMA cache_size = -64000;  
  
        CREATE VIRTUAL TABLE files USING fts5(  
            path,  
            content,  
            tokenize = 'unicode61 remove_diacritics 2'  
        );  
    """)  
  
    count = 0  
    total_bytes = 0  
    skipped_by_size = 0  
    skipped_by_path = 0  
    started = time.time()  
  
    conn.execute("BEGIN")  
  
    for repo in repos:  
        repo_name = os.path.basename(repo)  
        print(f"Walking {repo_name} ...", flush=True)  
  
        for root, dirs, files in os.walk(repo):  
            dirs[:] = [d for d in dirs  
                       if d not in {".git", "node_modules", "build", "bin"}]  
  
            for fname in files:  
                ext = os.path.splitext(fname)[1].lower()  
                if ext not in INDEX_EXTENSIONS:  
                    continue  
  
                full = os.path.join(root, fname)  
                rel = os.path.relpath(full, repo)  
  
                if not should_include_path(rel):  
                    skipped_by_path += 1  
                    continue  
  
                try:  
                    size = os.path.getsize(full)  
                except OSError:  
                    continue  
  
                if size > MAX_FILE_SIZE:  
                    skipped_by_size += 1  
                    continue  
  
                try:  
                    with open(full, "r", errors="ignore") as f:  
                        content = f.read()  
                except Exception:  
                    continue  
  
                # Prefix with the repo name so matches show which repo they  
                # came from and two repos can never produce the same path.  
                conn.execute(  
                    "INSERT INTO files (path, content) VALUES (?, ?)",  
                    (f"{repo_name}/{rel}", content),  
                )  
                count += 1  
                total_bytes += len(content)  
  
                if count % 500 == 0:  
                    elapsed = time.time() - started  
                    rate = count / elapsed if elapsed > 0 else 0  
                    print(f"  {count} files ({total_bytes // (1024*1024)} MB) "  
                          f"in {elapsed:.1f}s ({rate:.0f} files/s)",  
                          flush=True)  
  
    print()  
    print("Optimizing FTS5 index ...", flush=True)  
    conn.commit()  
    conn.execute("INSERT INTO files(files) VALUES('optimize')")  
    conn.commit()  
    conn.close()  
  
    # Atomic swap — os.replace is atomic even when DB_FILE exists  
    os.replace(tmp_db, DB_FILE)  
    try:  
        os.chmod(DB_FILE, 0o644)  
    except OSError:  
        pass  
  
elapsed = time.time() - started  
    size = os.path.getsize(DB_FILE) / (1024 * 1024)  
    print()  
    print(f"Indexed {count} files, {total_bytes // (1024*1024)} MB of text")  
    print(f"Skipped {skipped_by_path} by path filter")  
    print(f"Skipped {skipped_by_size} by size "  
          f"(>{MAX_FILE_SIZE // (1024*1024)} MB or unreadable)")  
    print(f"Index written to {DB_FILE} ({size:.1f} MB) in {elapsed:.0f}s")  
  
  
if __name__ == "__main__":  
    build_index()
