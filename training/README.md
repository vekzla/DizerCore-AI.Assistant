# DizerCore LoRA Training  
  
Fine-tune your installed Qwen2.5 model on the TrinityCore reference repo using Kaggle's free T4 GPU. The dataset carries a meta record that tells the notebook which base model to train — no manual config.  
  
## Prerequisites  
  
- DizerCore installed, reference repo cloned at `/data/reference/`  
- A Kaggle account  
  
## Workflow  
  
**1. Build the dataset (on the Pi)**  
  
`http://<pi-ip>:5000` → **Training** → **Build Dataset**. Wait 2–5 min. Expect 4,000–15,000+ examples written to `/data/training/dizercore-dataset.jsonl`. Line 1 is a meta record (`{"_meta": true, "base_model": ..., "install_key": ...}`) recording the installed model — **if you switch models, rebuild the dataset before retraining.**  
  
**2. Upload to Kaggle**  
  
Training tab → **Download Dataset**, then [kaggle.com/datasets](https://www.kaggle.com/datasets) → **New Dataset** → upload the JSONL → note the slug (e.g. `vekzla/dizercore-wow`). To update later: dataset page → **New Version**.  
  
**3. Run the notebook**  
  
[kaggle.com/code](https://www.kaggle.com/code) → **New Notebook** → **File → Import** → `training/dizercore-colab.ipynb` (name is historical — it's a Kaggle notebook). **Add Data** → attach your dataset. **Settings → Accelerator → GPU T4**. In cell 1 set `DATASET` to the path shown in the Data panel, e.g. `/kaggle/input/<slug>/dizercore-dataset.jsonl`. Then **Run All**.  
  
**4. Wait** — install ~3 min, training is the long phase (1.5B ≈ 1–2 h on T4, 3B slower), then merge → GGUF convert → Q4_K_M quantize. Done when you see:
=== DONE ===
Base trained: Qwen/Qwen2.5-3B-Instruct
GGUF: /kaggle/working/dizercore/dizercore-q4_k_m.gguf
Verify `Base trained:` matches the Pi's installed model — if it says 1.5B, the dataset's meta row is stale (rebuild → re-upload → rerun).  
  
**5. Download** — Output panel (right sidebar) → `dizercore/dizercore-q4_k_m.gguf`. FileLink doesn't render on Kaggle; always use the Output panel.  
  
**6. Deploy** — Training tab → **Choose a .gguf file** → **Upload & Deploy**. The Pi validates it and restarts `llama-server`. Header Training button turns purple when a trained model is active.  
  
**7. Test** — prompt: `feathering the nest quest credit not firing`. Trained output should reference real TrinityCore paths/tables.  
  
## Reverting  
  
Training tab → **Revert to Base Model**. Trained file stays on disk for re-deploy.  
  
## Retraining  
  
Only when you switch base models, the fork diverges significantly, or substantial new SQL is added. Free — Kaggle quota ~30 GPU h/week.  
  
## Troubleshooting  
  
| Problem | Fix |  
|---|---|  
| `Dataset not found at /kaggle/input/...` | Fix `DATASET` slug in cell 1 from the Data panel |  
| `Base model:` doesn't match Pi | Stale meta row — rebuild dataset, upload new version, rerun |  
| CUDA OOM in Train cell | `per_device_train_batch_size=2`, `gradient_accumulation_steps=8` |  
| Convert/quantize import errors | llama.cpp `requirements.txt` install must succeed — rerun last cell |  
| GGUF under ~400 MB | Quantize interrupted — rerun last cell or notebook |  
| Trained output worse | Training tab → Revert to Base Model |  
  
## Files produced  
  
| File | Purpose |  
|---|---|  
| `/kaggle/working/dizercore/dizercore-f16.gguf` | Intermediate, discarded with session |  
| `/kaggle/working/dizercore/dizercore-q4_k_m.gguf` | **This goes on the Pi** |  
| `/kaggle/working/dizercore/adapter/` | LoRA adapter, kept for debugging |
