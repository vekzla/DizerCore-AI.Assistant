#!/usr/bin/env python3  
# =============================================================================  
# DizerCore AI Assistant  
# -----------------------------------------------------------------------------  
# File:    web-ui/index-watcher.py  
# Purpose: Keep the FTS5 reference index up to date. Also rebuilds the  
#          training dataset when the reference repo changes, so the user's  
#          next training run has fresh data without any manual step.  
# =============================================================================  
  
import os  
import json  
import subprocess  
import sys  
import threading  
import time  
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  
  
# ---------- configuration ----------  
REFERENCE_DIR = os.environ.get("REFERENCE_DIR", "/data/reference")  
DB_FILE = os.environ.get("INDEX_DB", "/data/web-ui/dizercore-index.db")  
INDEXER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "indexer.py")  
DATASET_BUILDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "training", "dataset-builder.py")  
DATASET_FILE = os.environ.get("DATASET_FILE", "/data/training/dizercore-dataset.jsonl")  
VENV_PYTHON = "/data/venvs/webui/bin/python"  
ACTIVITY_FILE = os.environ.get("ACTIVITY_FILE", "/data/web-ui/.ai-activity")  
  
CHECK_INTERVAL = int(os.environ.get("WATCH_INTERVAL", "300"))  
WATCH_HOST = "127.0.0.1"  
WATCH_PORT = int(os.environ.get("WATCH_PORT", "8091"))  
INDEX_TIMEOUT = 3600  
DATASET_TIMEOUT = 1800  
  
REBUILD_DATASET = os.environ.get("REBUILD_DATASET", "1") == "1"  
  
_state = {  
    "running": False,  
    "last_check": 0,  
    "last_build": 0,  
    "last_build_status": None,  
    "last_reason": None,  
    "last_dataset_build": 0,  
    "last_dataset_status": None,  
    "log": [],  
}  
_lock = threading.Lock()  
  
  
def log(msg):  
    ts = time.strftime("%Y-%m-%d %H:%M:%S")  
    line = f"[{ts}] {msg}"  
    print(line, flush=True)  
    with _lock:  
        _state["log"].append(line)  
        if len(_state["log"]) > 200:  
            _state["log"] = _state["log"][-200:]  
  
  
# =============================================================================  
# Helpers  
# =============================================================================  
  
def find_reference_repo():  
    # REFERENCE_DIR itself may be the repo (cloned directly into it)  
    if os.path.isdir(os.path.join(REFERENCE_DIR, ".git")):  
        return REFERENCE_DIR  
    if not os.path.isdir(REFERENCE_DIR):  
        return None  
    for name in sorted(os.listdir(REFERENCE_DIR)):  
        full = os.path.join(REFERENCE_DIR, name)  
        if os.path.isdir(full) and os.path.isdir(os.path.join(full, ".git")):  
            return full  
    return None  
  
  
def newest_repo_mtime(repo):  
    latest = 0  
    for root, dirs, files in os.walk(repo):  
        dirs[:] = [d for d in dirs  
                   if d not in {".git", "dep", "contrib", "doc", "docs",  
                                "tests", "node_modules", "build", "bin",  
                                "cmake"}]  
        for fname in files:  
            ext = os.path.splitext(fname)[1].lower()  
            if ext not in {".cpp", ".c", ".h", ".hpp", ".cc", ".inl",  
                           ".sql", ".py", ".lua"}:  
                continue  
            try:  
                mtime = os.path.getmtime(os.path.join(root, fname))  
                if mtime > latest:  
                    latest = mtime  
            except OSError:  
                continue  
    return latest  
  
  
def _mtime(path):  
    try:  
        return os.path.getmtime(path)  
    except OSError:  
        return 0  
  
  
def ai_is_busy():  
    if not os.path.isfile(ACTIVITY_FILE):  
        return False  
    try:  
        with open(ACTIVITY_FILE) as f:  
            return bool(json.load(f).get("busy"))  
    except Exception:  
        return False  
  
  
def wait_until_ai_idle():  
    waited = 0  
    while ai_is_busy():  
        if waited == 0:  
            log("Web UI is busy with an AI task — deferring")  
        time.sleep(10)  
        waited += 10  
        if waited % 30 == 0:  
            log(f"Still waiting for Web UI (waited {waited}s)")  
    if waited > 0:  
        log(f"Web UI is idle — proceeding (waited {waited}s)")  
  
  
# =============================================================================  
# Index rebuild  
# =============================================================================  
  
def run_indexer(reason):  
    with _lock:  
        if _state["running"]:  
            log("Rebuild already in progress — skipping")  
            return False  
        _state["running"] = True  
        _state["last_build_status"] = "running"  
        _state["last_reason"] = reason  
  
    proc = None  
    try:  
        python = VENV_PYTHON if os.path.isfile(VENV_PYTHON) else sys.executable  
        log(f"Starting index rebuild ({reason})")  
        started = time.time()  
  
        proc = subprocess.Popen(  
            [python, INDEXER],  
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,  
            text=True, bufsize=1,  
        )  
        # Forward every indexer line so optimize/errors are visible  
        for line in iter(proc.stdout.readline, ""):  
            line = line.rstrip()  
            if line:  
                log(f"  indexer: {line}")  
  
        proc.wait(timeout=INDEX_TIMEOUT)  
        elapsed = time.time() - started  
  
        ok = proc.returncode == 0  
        if ok:  
            log(f"Index rebuild complete in {elapsed:.1f}s")  
            with _lock:  
                _state["last_build_status"] = "ok"  
        else:  
            log(f"Index rebuild failed (exit {proc.returncode})")  
            with _lock:  
                _state["last_build_status"] = f"error:{proc.returncode}"  
  
        with _lock:  
            _state["last_build"] = time.time()  
        return ok  
  
    except subprocess.TimeoutExpired:  
        log(f"Indexer timed out after {INDEX_TIMEOUT}s — killing")  
        try:  
            proc.kill()  
        except Exception:  
            pass  
        with _lock:  
            _state["last_build_status"] = "timeout"  
            _state["last_build"] = time.time()  
        return False  
    except Exception as e:  
        log(f"Indexer error: {e}")  
        with _lock:  
            _state["last_build_status"] = f"error:{e}"  
            _state["last_build"] = time.time()  
        return False  
    finally:  
        with _lock:  
            _state["running"] = False  
  
  
# =============================================================================  
# Dataset rebuild  
# =============================================================================  
  
def run_dataset_builder():  
    if not os.path.isfile(DATASET_BUILDER):  
        log(f"dataset-builder.py not found at {DATASET_BUILDER} — skipping")  
        return False  
  
    # Don't overlap with index rebuild or another dataset build  
    with _lock:  
        if _state["running"]:  
            log("Another rebuild running — skipping dataset build")  
            return False  
        _state["running"] = True  
        _state["last_dataset_status"] = "running"  
  
    proc = None  
    try:  
        python = VENV_PYTHON if os.path.isfile(VENV_PYTHON) else sys.executable  
        log("Starting dataset rebuild")  
        started = time.time()  
  
        env = {**os.environ, "OUTPUT_FILE": DATASET_FILE}  
  
        proc = subprocess.Popen(  
            [python, DATASET_BUILDER],  
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,  
            text=True, bufsize=1, env=env,  
        )  
  
        for line in iter(proc.stdout.readline, ""):  
            line = line.rstrip()  
            if line:  
                log(f"  dataset: {line}")  
  
        proc.wait(timeout=DATASET_TIMEOUT)  
        elapsed = time.time() - started  
  
        if proc.returncode == 0:  
            log(f"Dataset rebuild complete in {elapsed:.1f}s")  
            with _lock:  
                _state["last_dataset_status"] = "ok"  
                _state["last_dataset_build"] = time.time()  
            return True  
        else:  
            log(f"Dataset rebuild failed (exit {proc.returncode})")  
            with _lock:  
                _state["last_dataset_status"] = f"error:{proc.returncode}"  
                _state["last_dataset_build"] = time.time()  
            return False  
    except subprocess.TimeoutExpired:  
        log(f"Dataset builder timed out after {DATASET_TIMEOUT}s — killing")  
        try:  
            proc.kill()  
        except Exception:  
            pass  
        with _lock:  
            _state["last_dataset_status"] = "timeout"  
            _state["last_dataset_build"] = time.time()  
        return False  
    except Exception as e:  
        log(f"Dataset builder error: {e}")  
        with _lock:  
            _state["last_dataset_status"] = f"error:{e}"  
            _state["last_dataset_build"] = time.time()  
        return False  
    finally:  
        with _lock:  
            _state["running"] = False  
  
  
# =============================================================================  
# Watcher loop  
# =============================================================================  
  
def watcher_loop():  
    log(f"Watcher started — checking every {CHECK_INTERVAL}s")  
    log(f"  dataset rebuild: {'enabled' if REBUILD_DATASET else 'disabled'}")  
    time.sleep(60)  
  
    while True:  
        try:  
            repo = find_reference_repo()  
            if not repo:  
                log("No reference repo found — sleeping")  
                time.sleep(CHECK_INTERVAL)  
                continue  
  
            repo_mtime = newest_repo_mtime(repo)  
            idx_mtime = _mtime(DB_FILE)  
  
            with _lock:  
                _state["last_check"] = time.time()  
  
            if repo_mtime > idx_mtime:  
                delta = repo_mtime - idx_mtime  
                log(f"Repo newer than index by {delta:.0f}s — rebuild")  
                wait_until_ai_idle()  
                index_ok = run_indexer("repo changed")  
  
                if index_ok and REBUILD_DATASET:  
                    ds_mtime = _mtime(DATASET_FILE)  
                    if repo_mtime > ds_mtime:  
                        log("Repo newer than dataset — rebuilding dataset")  
                        wait_until_ai_idle()  
                        run_dataset_builder()  
            else:  
                age_repo = int(time.time() - repo_mtime) if repo_mtime else -1  
                age_idx = int(time.time() - idx_mtime) if idx_mtime else -1  
                log(f"Index is current (repo {age_repo}s, index {age_idx}s)")  
  
        except Exception as e:  
            log(f"Watcher loop error: {e}")  
  
        time.sleep(CHECK_INTERVAL)  
  
  
# =============================================================================  
# HTTP control API  
# =============================================================================  
  
class Handler(BaseHTTPRequestHandler):  
    def log_message(self, *args, **kwargs):  
        pass  
  
    def _send_json(self, code, data):  
        body = json.dumps(data).encode()  
        self.send_response(code)  
        self.send_header("Content-Type", "application/json")  
        self.send_header("Content-Length", str(len(body)))  
        self.end_headers()  
        self.wfile.write(body)  
  
    def do_GET(self):  
        if self.path == "/status":  
            with _lock:  
                data = {  
                    "running": _state["running"],  
                    "last_check": _state["last_check"],  
                    "last_build": _state["last_build"],  
                    "last_build_status": _state["last_build_status"],  
                    "last_reason": _state["last_reason"],  
                    "last_dataset_build": _state["last_dataset_build"],  
                    "last_dataset_status": _state["last_dataset_status"],  
                    "ai_busy": ai_is_busy(),  
                    "log": _state["log"][-50:],  
                }  
            self._send_json(200, data)  
        elif self.path == "/health":  
            self._send_json(200, {"ok": True})  
        else:  
            self._send_json(404, {"error": "not found"})  
  
    def do_POST(self):  
        if self.path == "/trigger":  
            def runner():  
                wait_until_ai_idle()  
                run_indexer("manual trigger")  
            threading.Thread(target=runner, daemon=True).start()  
            self._send_json(202, {"status": "triggered"})  
        elif self.path == "/trigger-dataset":  
            def runner():  
                wait_until_ai_idle()  
                run_dataset_builder()  
            threading.Thread(target=runner, daemon=True).start()  
            self._send_json(202, {"status": "triggered"})  
        else:  
            self._send_json(404, {"error": "not found"})  
  
  
def start_http_server():  
    # ThreadingHTTPServer so /status answers even while a build saturates  
    # the CPU — a single-threaded server was why the UI showed "down".  
    server = ThreadingHTTPServer((WATCH_HOST, WATCH_PORT), Handler)  
    server.daemon_threads = True  
    log(f"Control API listening on http://{WATCH_HOST}:{WATCH_PORT}")  
    server.serve_forever()  
  
  
def main():  
    log("DizerCore index watcher starting")  
    log(f"  Reference dir:   {REFERENCE_DIR}")  
    log(f"  Index DB:        {DB_FILE}")  
    log(f"  Dataset file:    {DATASET_FILE}")  
    log(f"  Check interval:  {CHECK_INTERVAL}s")  
  
    if not os.path.isfile(INDEXER):  
        log(f"ERROR: indexer.py not found at {INDEXER} — exiting")  
        sys.exit(1)  
  
    t = threading.Thread(target=start_http_server, daemon=True)  
    t.start()  
    watcher_loop()  
  
  
if __name__ == "__main__":  
    main()
