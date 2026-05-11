# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

魔方比赛视频处理工具 — process Rubik's cube competition videos by:
1. Reading the seven-segment LED timer display via OCR and renaming the video to its solve time (e.g. `IMG_0001.MP4` → `4.716.MP4`).
2. Classifying videos into per-competitor folders via face recognition against reference photos in `person/<name>/`.

UI text and prompts are in Chinese; keep that convention when editing user-facing strings.

## Common commands

```powershell
pip install -r requirements.txt           # opencv-python, numpy, tqdm (psutil optional, used for worker sizing)

.\process-videos.ps1 "Z:\cube-comp\260307" -DryRun   # full pipeline preview
.\process-videos.ps1 "Z:\cube-comp\260307"           # OCR-rename then face-classify (move)
.\process-videos.ps1 "Z:\cube-comp\260307" -Copy     # classify by copy instead of move
.\process-videos.ps1 "Z:\cube-comp\260307" -SkipOCR  # only one stage
.\process-videos.ps1 "Z:\cube-comp\260307" -SkipClassify

python ocr_timer.py "<dir>" --rename --dry-run     # standalone seven-segment OCR
python ocr_timer.py "<file.mp4>" --debug           # single-file with per-frame trace
python classify.py  "<dir>" --threshold 0.35       # standalone face classify
python ai_ocr_timer.py "<dir1>" "<dir2>"           # AI-assisted OCR step 1: extract frames
python ai_ocr_timer.py --apply "4.716, 3.698, ..." # AI-assisted OCR step 3: rename from results
```

### Tests

```bash
python test_ocr.py
```

Regression suite over `video/good/` (must all pass — never regress) and `video/fail/` (known failures, expected to FAIL until fixed). Test names are the expected OCR output: `4.716.MP4` should read as `4.716`. Both directories are gitignored — they hold real competition footage.

## Architecture

### Two OCR pipelines coexist on purpose

- `ocr_timer.py` — fully automated seven-segment OCR. Used for batch processing. Fails on hard cases (occluded, blurry, small-in-frame 4K displays).
- `ai_ocr_timer.py` — extracts the best frame per video into `jpg/need_ai/` and stops. The AI then reads the frames in chat (see `.agent/workflows/ocr.md`, invoked as `/ocr`), and `--apply "t1, t2, ..."` renames in the original order. `_video_list.txt` is the index between the two halves; do not delete it between steps.

`ocr_timer.py` is kept as the fast-path even though `ai_ocr_timer.py` is more accurate.

### `ocr_timer.py` detection pipeline

```
extractLastFrames → _createLedMask (HSV per band: TIGHT→MED→WIDE)
                  → _findDigitCandidates (narrow segments merged via _mergeSegments — needed for 4K where each seven-segment bar is its own contour)
                  → _clusterIntoRows → _selectBestRow
                  → recognizeDigit (template match against pre-generated 24×40 masks; "1" detected by aspect ratio because templates are unreliable for it)
                  → assembleTime (decimal point is always before last 3 digits; 6-digit results render as `M_SS.mmm` for filename safety)
```

`detectTimerROI` runs Pass 1 at native resolution; if that finds nothing, Pass 2 downscales to 1080p and uses a larger morphological close kernel — designed for Speed Stacks displays that are tiny in 4K frames.

`readTimer` aggregates per-frame results with three strategies in order: (1) two consecutive identical frames in the tail third, cross-checked against global frequency to avoid being hijacked by a transient; (2) per-position majority vote across tail frames of equal length (corrects single-digit template errors like `4.101` vs `4.201`); (3) weighted vote across all frames (later = heavier). Frames reading `0.000` are filtered as timer-reset noise.

### `classify.py` face pipeline

OpenCV YuNet detector + SFace recognizer, both auto-downloaded from OpenCV Zoo into `models/` on first run. For each video, samples 3 frames from the middle 80% of the timeline, picks the largest face per frame, and matches against the reference DB by cosine similarity. Threshold 0.35 is the SFace sweet spot (same person typically >0.4, different <0.3). Below threshold → `unknown/`.

### Windows + Chinese paths

OpenCV's `cv2.imread` and (less reliably) `cv2.VideoCapture` do not handle non-ASCII paths on Windows. `classify.py` uses `np.fromfile + cv2.imdecode` (`imreadUnicode`). Keep this in mind when adding any new image-load code — directory and file names in this domain are routinely Chinese.

### Worker sizing

Both `classify.py` and `ocr_timer.py` size their thread pool from available RAM via `psutil` (50 MB/worker for classify, 200 MB/worker for OCR), falling back to fixed 4/2 if `psutil` is missing. Do not hardcode worker counts.

### Wall-clock timings in parallel mode are meaningless

`ocr_timer.py` deliberately suppresses per-video timing in the parallel path because the futures' wall-clock times overlap. Single-file/`--debug` mode runs sequentially and reports real per-video time. Preserve this distinction when changing the runner.

## Known broken cases

`video/fail/3.698.MP4` and `video/fail/4.098.MP4` are 4K videos where the LED display is tiny relative to the frame and surrounding red noise (jersey numbers, referee clothing) is mistaken for digits. See `handoff.md` for the full incident log of what's been tried and why each fix regressed `video/good/`. Read it before attempting another fix — do not repeat the same approach.

## Slash command

`/ocr <dir>` (defined in `.agent/workflows/ocr.md`) drives the AI-assisted OCR loop end-to-end: extract frames, view each `jpg/need_ai/*.jpg`, build a confirmation table, then `--apply` after the user confirms. Failed reads are renamed to `FAIL_<reason>.MP4` (e.g. `FAIL_灭屏`, `FAIL_遮挡`, `FAIL_模糊`, `FAIL_无大显`, `FAIL_残缺`) so they can be triaged manually later — preserve this convention.
