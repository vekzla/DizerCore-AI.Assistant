# DizerCore LoRA Training

Fine-tune Qwen2.5-Coder-1.5B on your TrinityCore repo. Runs once, on Google Colab's free T4 GPU, in about 2 hours.

---

## Prerequisites

- DizerCore installed with a reference repo cloned at `/data/reference/`
- A Google account (for Colab + Drive)
- ~4 GB free in Google Drive

---

## Step 1 — Build the dataset (on the Pi)

Open `http://<pi-ip>:5000` → click **Training** → click **Build Dataset**.

Wait 2–5 minutes. The log streams live. Result:

```
Total examples: 39501
  explain_sql: 22299
  explain_file: 13779
  explain_function: 3423
Written to: /data/training/dizercore-dataset.jsonl
```

Expect **4,000–15,000+ examples**. Under 500 means the reference repo is too small.

---

## Step 2 — Upload dataset to Google Drive

1. Open [drive.google.com](https://drive.google.com)
2. Create a folder called `dizercore` in **My Drive**
3. Upload `dizercore-dataset.jsonl` into it

Download the dataset first by clicking **Download Dataset** in the Training tab.

Final path must be:

```
MyDrive/dizercore/dizercore-dataset.jsonl
```

---

## Step 3 — Open the Colab notebook

```
https://colab.research.google.com/github/vekzla/DizerCore-AI.Assistant/blob/main/training/dizercore-colab.ipynb
```

**Before running:**
- **Runtime → Change runtime type → T4 GPU → Save**

**Then:**
- **Runtime → Run all**
- Approve Google Drive access when prompted

The notebook is **one cell**. It handles everything from install to GGUF conversion.

---

## Step 4 — Wait ~2 hours

| Phase | Time |
|---|---|
| Install + load | ~3 min |
| Train 1 epoch (15k examples, LoRA r=16) | ~1 h 30 min |
| Merge adapter onto fp16 base | ~3 min |
| Convert HF → fp16 GGUF | ~5 min |
| Quantize to Q4_K_M | ~8 min |

**Watch for:**
```
=== DONE ===
GGUF: 940 MB
```

A download link appears right after. Click it — you get `dizercore-q4_k_m.gguf`.

The file is also copied to your Drive at `MyDrive/dizercore/dizercore-q4_k_m.gguf` as backup.

---

## Step 5 — Upload to the Pi

Open `http://<pi-ip>:5000` → **Training** tab:

1. Click **Choose a .gguf file**
2. Select `dizercore-q4_k_m.gguf`
3. Click **Upload & Deploy**

Upload takes 60–120 seconds. Deploy takes ~15 seconds.

The Training button in the header turns **purple** when active.

---

## Step 6 — Test

In the main prompt box, type:

```
feathering the nest quest credit not firing
```

Compare to the base model output. The trained version references real TrinityCore paths and tables.

---

## Reverting

Training tab → **Revert to Base Model**. Switches back in ~15 seconds. Trained file stays on disk.

---

## When to Retrain

Only when:
- The base model version changes (Qwen releases a major update)
- Your TrinityCore fork diverges significantly
- You've added substantial new SQL content

For personal WoW emulation use, one training run is enough for months.

---

## Cost

Free. Colab's T4 tier requires no payment.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Colab stuck on "Connecting" | Peak-hour GPU saturation. Try evening AEDT (US midnight). Or: switch to CPU → connect → switch to T4 → connect. |
| Training crashes on `functools.partial` | You're running an outdated notebook. Re-open from the GitHub link above. |
| `save_pretrained` fails | Same — outdated notebook. Current version merges on fresh fp16 base. |
| Upload fails on the Pi | Check file size (~940 MB). Retry. |
| Trained output is worse | Training tab → Revert to Base Model. |
| GGUF file is under 400 MB | Conversion was interrupted. Redownload from Drive. |

---

## Files Produced

| File | Size | Purpose |
|---|---|---|
| `/content/dizercore-f16.gguf` | ~2.9 GB | Intermediate, deleted with session |
| `/content/dizercore-q4_k_m.gguf` | ~940 MB | **This is what goes on the Pi** |
| `MyDrive/dizercore/dizercore-q4_k_m.gguf` | ~940 MB | Backup copy on Drive |
