# =============================================================================  
# DizerCore AI Assistant  
# -----------------------------------------------------------------------------  
# File:    web-ui/training.py  
# Purpose: Flask blueprint for the Training tab. Handles dataset building,  
#          GGUF upload, model deployment, and revert to base model.  
#  
# Endpoints:  
#   GET  /api/training/status               -- overall state  
#   POST /api/training/build-dataset        -- kick off dataset build  
#   GET  /api/training/build-dataset/status -- poll build progress  
#   GET  /api/training/dataset/download     -- serve JSONL  
#   POST /api/training/upload-model         -- accept GGUF, deploy  
#   POST /api/training/revert               -- revert to base model  
# =============================================================================  
  
import json  
import os  
import subprocess  
import threading  
import time  
from flask import Blueprint, request, jsonify, send_file  
  
training_bp = Blueprint("training", __name__)  
  
# ---------- config ----------  
TRAINING_DIR = os.environ.get("TRAINING_DIR", "/data/training")  
DATASET_FILE = os.path.join(TRAINING_DIR, "dizercore-dataset.jsonl")  
MODELS_DIR = os.environ.get("MODELS_DIR", "/data/models")  
TRAINED_MODEL = os.path.join(MODELS_DIR, "dizercore-q4_k_m.gguf")  
LLAMA_SERVICE = "/etc/systemd/system/llama-server.service"  
DEPLOY_HELPER = "/data/web-ui/training-deploy.sh"  
WEB_UI_DIR = os.environ.get("WEB_UI_DIR", "/data/web-ui")  
DATASET_BUILDER = os.path.join(TRAINING_DIR, "dataset-builder.py")  
VENV_PYTHON = "/data/venvs/webui/bin/python"  
  
# ---------- state ----------  
_state = {  
    "building": False,  
    "log": "",  
    "exit_code": None,  
    "started_at": None,  
}  
_lock = threading.Lock()  
  
  
# =============================================================================  
# Status helpers  
# =============================================================================  
  
def _get_active_model():  
    """Parse the -m flag from llama-server.service (joins line continuations)."""  
    try:  
        with open(LLAMA_SERVICE) as f:  
            content = f.read()  
    except Exception:  
        return None  
  
    content = content.replace("\\\n", " ")  
  
    for line in content.splitlines():  
        if line.startswith("ExecStart="):  
            parts = line.split()  
            for i, tok in enumerate(parts):  
                if tok == "-m" and i + 1 < len(parts):  
                    return parts[i + 1].rstrip("\\").strip()  
    return None  
  
  
def _registry_filenames():  
    """Parse MODEL_REGISTRY from lib/common.sh -> {key: filename}."""  
    import glob  
    candidates = ["/data/dizercore-src/lib/common.sh", os.path.join(os.path.dirname(__file__), "..", "lib", "common.sh")]  
    for path in candidates:  
        if not os.path.isfile(path):  
            continue  
        try:  
            names = {}  
            for line in open(path, encoding="utf-8"):  
                if '"' in line and "|" in line:  
                    parts = line.strip().strip('"').split("|")  
                    if len(parts) >= 5 and parts[0] and parts[4].endswith(".gguf"):  
                        names[parts[0]] = parts[4]  
            if names:  
                return names  
        except Exception:  
            continue  
    return {}  
  
  
def _get_base_model():  
    """Read the user's installer choice; resolve filename from MODEL_REGISTRY."""  
    try:  
        with open("/data/.dizercore-model") as f:  
            key = f.read().strip()  
    except Exception:  
        key = "1.5b-base"  
    filenames = _registry_filenames()  
    if not filenames:  
        filenames = {  
            "0.5b":      "qwen2.5-coder-0.5b-instruct-q4_k_m.gguf",  
            "0.5b-base": "qwen2.5-0.5b-instruct-q4_k_m.gguf",  
            "1.5b":      "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf",  
            "1.5b-base": "qwen2.5-1.5b-instruct-q4_k_m.gguf",  
            "3b":        "qwen2.5-coder-3b-instruct-q4_k_m.gguf",  
            "3b-base":   "qwen2.5-3b-instruct-q4_k_m.gguf",  
        }  
    return os.path.join(MODELS_DIR, filenames.get(key, filenames["1.5b-base"]))  
  
  
def _dataset_info():  
    if not os.path.isfile(DATASET_FILE):  
        return {"exists": False, "size": 0, "count": 0, "mtime": 0}  
    try:  
        size = os.path.getsize(DATASET_FILE)  
        mtime = os.path.getmtime(DATASET_FILE)  
        count = 0  
        with open(DATASET_FILE) as f:  
            for _ in f:  
                count += 1  
        return {"exists": True, "size": size, "count": count, "mtime": mtime}  
    except Exception:  
        return {"exists": False, "size": 0, "count": 0, "mtime": 0}  
  
  
def _trained_model_info():  
    if not os.path.isfile(TRAINED_MODEL):  
        return {"exists": False, "size": 0, "mtime": 0}  
    try:  
        return {  
            "exists": True,  
            "size": os.path.getsize(TRAINED_MODEL),  
            "mtime": os.path.getmtime(TRAINED_MODEL),  
        }  
    except Exception:  
        return {"exists": False, "size": 0, "mtime": 0}  
  
  
# =============================================================================  
# Status endpoint  
# =============================================================================  
  
@training_bp.route("/api/training/status")  
def status():  
    dataset = _dataset_info()  
    trained = _trained_model_info()  
    active = _get_active_model()  
    base = _get_base_model()  
  
    active_kind = "unknown"  
    if active == TRAINED_MODEL:  
        active_kind = "trained"  
    elif active == base:  
        active_kind = "base"  
    elif active:  
        active_kind = "custom"  
  
    with _lock:  
        build_state = {  
            "building": _state["building"],  
            "log": _state["log"],  
            "exit_code": _state["exit_code"],  
        }  
  
    return jsonify({  
        "dataset": dataset,  
        "trained_model": trained,  
        "active_model": active,  
        "base_model": base,  
        "active_kind": active_kind,  
        "dataset_file": DATASET_FILE,  
        "trained_model_file": TRAINED_MODEL,  
        "build": build_state,  
    })  
  
  
# =============================================================================  
# Dataset build  
# =============================================================================  
  
@training_bp.route("/api/training/build-dataset", methods=["POST"])  
def build_dataset():  
    with _lock:  
        if _state["building"]:  
            return jsonify({"error": "Build already running"}), 409  
  
    if not os.path.isfile(DATASET_BUILDER):  
        return jsonify({  
            "error": "dataset-builder.py not found at %s" % DATASET_BUILDER  
        }), 500  
  
    def run():  
        with _lock:  
            _state["building"] = True  
            _state["log"] = "starting...\n"  
            _state["exit_code"] = None  
            _state["started_at"] = time.time()  
  
        try:  
            python = VENV_PYTHON if os.path.isfile(VENV_PYTHON) else "python3"  
            env = {  
                **os.environ,  
                "REFERENCE_DIR": "/data/reference",  
                "OUTPUT_FILE": DATASET_FILE,  
                "PYTHONUNBUFFERED": "1",  
            }  
  
            proc = subprocess.Popen(  
                [python, "-u", DATASET_BUILDER],  
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,  
                text=True, bufsize=1, env=env,  
            )  
            for line in iter(proc.stdout.readline, ""):  
                with _lock:  
                    _state["log"] += line  
                    if _state["log"].count("\n") > 500:  
                        _state["log"] = "\n".join(_state["log"].split("\n")[-500:])  
            proc.wait()  
            with _lock:  
                _state["exit_code"] = proc.returncode  
        except Exception as e:  
            with _lock:  
                _state["log"] += "\nERROR: %s\n" % e  
                _state["exit_code"] = 1  
        finally:  
            with _lock:  
                _state["building"] = False  
  
    threading.Thread(target=run, daemon=True).start()  
    return jsonify({"status": "started"})  
  
  
@training_bp.route("/api/training/build-dataset/status")  
def build_status():  
    with _lock:  
        return jsonify({  
            "building": _state["building"],  
            "log": _state["log"],  
            "exit_code": _state["exit_code"],  
        })  
  
  
# =============================================================================  
# Dataset download  
# =============================================================================  
  
@training_bp.route("/api/training/dataset/download")  
def download_dataset():  
    if not os.path.isfile(DATASET_FILE):  
        return jsonify({"error": "Dataset not built yet"}), 404  
    return send_file(DATASET_FILE, as_attachment=True,  
                     download_name="dizercore-dataset.jsonl",  
                     mimetype="application/x-ndjson")  
  
  
# =============================================================================  
# Model upload  
# =============================================================================  
  
def _validate_gguf(path):  
    """Check the file starts with GGUF magic bytes."""  
    try:  
        with open(path, "rb") as f:  
            magic = f.read(4)  
        return magic == b"GGUF"  
    except Exception:  
        return False  
  
  
@training_bp.route("/api/training/upload-model", methods=["POST"])  
def upload_model():  
    if "model" not in request.files:  
        return jsonify({"error": "No file in request"}), 400  
    f = request.files["model"]  
    if not f.filename:  
        return jsonify({"error": "No filename"}), 400  
    if not f.filename.lower().endswith(".gguf"):  
        return jsonify({"error": "File must be a .gguf"}), 400  
  
    tmp_path = TRAINED_MODEL + ".uploading"  
    try:  
        f.save(tmp_path)  
    except Exception as e:  
        return jsonify({"error": "Save failed: %s" % e}), 500  
  
    size = os.path.getsize(tmp_path)  
    size_mb = size / (1024 * 1024)  
  
    if size < 400 * 1024 * 1024:  
        os.remove(tmp_path)  
        return jsonify({"error": "File too small (%.0f MB). Expected >=400 MB." % size_mb}), 400  
    if size > 3 * 1024 * 1024 * 1024:  
        os.remove(tmp_path)  
        return jsonify({"error": "File too large (%.0f MB). Expected <=3 GB." % size_mb}), 400  
  
    if not _validate_gguf(tmp_path):  
        os.remove(tmp_path)  
        return jsonify({"error": "Not a valid GGUF file (magic bytes mismatch)"}), 400  
  
    try:  
        if os.path.exists(TRAINED_MODEL):  
            os.remove(TRAINED_MODEL)  
        os.rename(tmp_path, TRAINED_MODEL)  
        os.chmod(TRAINED_MODEL, 0o644)  
    except Exception as e:  
        return jsonify({"error": "Move failed: %s" % e}), 500  
  
    try:  
        r = subprocess.run(  
            ["sudo", "-n", DEPLOY_HELPER, "deploy", TRAINED_MODEL],  
            capture_output=True, text=True, timeout=60,  
        )  
        if r.returncode != 0:  
            return jsonify({  
                "error": "Deploy failed",  
                "stderr": r.stderr.strip(),  
                "stdout": r.stdout.strip(),  
            }), 500  
    except subprocess.TimeoutExpired:  
        return jsonify({"error": "Deploy helper timed out"}), 500  
    except Exception as e:  
        return jsonify({"error": "Deploy error: %s" % e}), 500  
  
    return jsonify({  
        "status": "deployed",  
        "file": TRAINED_MODEL,  
        "size_mb": round(size_mb, 1),  
    })  
  
  
# =============================================================================  
# Revert  
# =============================================================================  
  
@training_bp.route("/api/training/revert", methods=["POST"])  
def revert():  
    try:  
        r = subprocess.run(  
            ["sudo", "-n", DEPLOY_HELPER, "revert"],  
            capture_output=True, text=True, timeout=60,  
        )  
        if r.returncode != 0:  
            return jsonify({  
                "error": "Revert failed",  
                "stderr": r.stderr.strip(),  
                "stdout": r.stdout.strip(),  
            }), 500  
    except subprocess.TimeoutExpired:  
        return jsonify({"error": "Revert helper timed out"}), 500  
    except Exception as e:  
        return jsonify({"error": "Revert error: %s" % e}), 500  
    return jsonify({"status": "reverted"})
