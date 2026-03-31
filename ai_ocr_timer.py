"""
AI Timer OCR — 提取视频末尾帧, 由 AI 在聊天中识别成绩

工作流:
  1. 脚本从每个视频中提取最后几秒的最佳帧 (大显亮着的), 保存到 jpg/need_ai/
  2. AI 看图识别成绩
  3. 用识别结果重命名

用法:
  # Step 1: 提取帧 (支持多目录, 空格分隔)
  python ai_ocr_timer.py <目录1> [目录2] [目录3] ...

  # Step 3: 用识别结果重命名 (--apply 配合 --dirs 指定哪些目录)
  python ai_ocr_timer.py <目录1> [目录2] ... --apply "4.716, 3.698, 43.243"

示例:
  python ai_ocr_timer.py "Z:\\魔方比赛\\260329"
  python ai_ocr_timer.py "Z:\\魔方比赛\\260329" "Z:\\魔方比赛\\260329\\沈懿"
"""

import argparse
import gc
import sys
import time
from pathlib import Path

import cv2
import numpy as np

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv"}

# 帧保存目录 (固定)
FRAME_OUT_DIR = Path(r"D:\cube\video-by-face\jpg\need_ai")


def hasRedLED(frame: np.ndarray, minPixels: int = 200) -> bool:
    """检测帧中是否有足够多的红色 LED 像素 (即大显是亮着的)."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for lo, hi in [
        ((0, 100, 150), (15, 255, 255)),
        ((168, 100, 150), (180, 255, 255)),
    ]:
        mask |= cv2.inRange(hsv, np.array(lo), np.array(hi))
    return int(cv2.countNonZero(mask)) >= minPixels


def extractLastFrames(videoPath: Path, seconds: float = 4.0, count: int = 8):
    """从视频最后 N 秒均匀取帧, 返回 [(time, frame), ...]"""
    cap = cv2.VideoCapture(str(videoPath))
    if not cap.isOpened():
        return []

    fps = cap.get(cv2.CAP_PROP_FPS)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total / fps if fps > 0 else 0

    startTime = max(0, duration - seconds)
    frames = []
    for i in range(count):
        t = startTime + (seconds * i / (count - 1)) if count > 1 else startTime
        framePos = min(int(t * fps), total - 1)
        cap.set(cv2.CAP_PROP_POS_FRAMES, framePos)
        ret, frame = cap.read()
        if ret:
            h, w = frame.shape[:2]
            if h > 1100:
                scale = 1080 / h
                frame = cv2.resize(frame, (int(w * scale), 1080), interpolation=cv2.INTER_AREA)
            frames.append((t, frame))

    cap.release()
    return frames


def pickBestFrame(frames: list) -> np.ndarray | None:
    """从后往前找第一个大显亮着的帧, 全找不到则返回最后一帧."""
    if not frames:
        return None
    for t, frame in reversed(frames):
        if hasRedLED(frame):
            return frame
    return frames[-1][1]


def collectVideos(targets: list[Path]) -> list[Path]:
    """
    从多个目录/文件路径中收集视频文件.
    - 如果是文件: 直接加入
    - 如果是目录: 只取该目录下的直接视频文件 (不递归)
    返回按路径排序的去重列表.
    """
    seen = set()
    videos = []
    for target in targets:
        if target.is_file() and target.suffix.lower() in VIDEO_EXTENSIONS:
            if target not in seen:
                seen.add(target)
                videos.append(target)
        elif target.is_dir():
            for f in sorted(target.iterdir()):
                if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS and f not in seen:
                    seen.add(f)
                    videos.append(f)
        else:
            print(f"⚠ 路径不存在或无效: {target}")
    return videos


def extractFrames(targets: list[Path], seconds: float = 4.0):
    """
    Step 1: 从多个目录/文件收集视频, 提取最佳帧保存到 FRAME_OUT_DIR.
    """
    FRAME_OUT_DIR.mkdir(parents=True, exist_ok=True)

    videos = collectVideos(targets)
    if not videos:
        print("没有找到任何视频文件")
        return []

    # 清空旧图片和列表
    for old in FRAME_OUT_DIR.glob("*.jpg"):
        old.unlink()
    listFile = FRAME_OUT_DIR / "_video_list.txt"
    if listFile.exists():
        listFile.unlink()

    videoList = []
    timings = []  # 每个视频的处理耗时
    totalStart = time.perf_counter()
    print(f"提取帧: {len(videos)} 个视频 → {FRAME_OUT_DIR}\n")

    for idx, vid in enumerate(videos, 1):
        t0 = time.perf_counter()
        frames = extractLastFrames(vid, seconds, count=8)
        if not frames:
            print(f"  [{idx}] {vid.name}: ⚠ 无法读取")
            continue

        best = pickBestFrame(frames)
        if best is None:
            print(f"  [{idx}] {vid.name}: ⚠ 无有效帧")
            continue

        fname = f"{idx}_{vid.stem}.jpg"
        cv2.imwrite(str(FRAME_OUT_DIR / fname), best, [cv2.IMWRITE_JPEG_QUALITY, 92])
        elapsed = time.perf_counter() - t0
        timings.append(elapsed)
        # 显示相对于父目录的路径，方便识别属于哪个选手/项目
        relPath = vid.relative_to(targets[0].parent) if len(targets) > 1 else Path(vid.name)
        print(f"  [{idx}] {relPath} → {fname}  ({elapsed:.1f}s)")
        videoList.append((idx, vid))
        gc.collect()

    # 保存视频列表 (绝对路径)
    with open(listFile, "w", encoding="utf-8") as f:
        for idx, vid in videoList:
            f.write(f"{idx}\t{vid}\n")

    totalElapsed = time.perf_counter() - totalStart
    avgElapsed = sum(timings) / len(timings) if timings else 0

    print(f"\n✅ 共提取 {len(videoList)} 个视频的帧")
    print(f"   保存位置: {FRAME_OUT_DIR}")
    print(f"   总耗时: {totalElapsed:.1f}s | 平均每视频: {avgElapsed:.1f}s")

    return videoList


def applyResults(results: str, dryRun: bool = False):
    """
    Step 3: 用 AI 识别结果重命名视频.
    results: 逗号分隔的成绩列表, 顺序与 _video_list.txt 一致.
    """
    listFile = FRAME_OUT_DIR / "_video_list.txt"
    if not listFile.exists():
        print(f"❌ 找不到 {listFile}, 请先运行 Step 1")
        sys.exit(1)

    # 读取视频列表
    videoMap: dict[int, Path] = {}
    with open(listFile, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            idx, path = line.split("\t", 1)
            videoMap[int(idx)] = Path(path)

    # 解析成绩
    times = [t.strip() for t in results.split(",") if t.strip()]

    if len(times) != len(videoMap):
        print(f"⚠ 成绩数量 ({len(times)}) 与视频数量 ({len(videoMap)}) 不匹配!")
        print(f"  视频数: {len(videoMap)}, 成绩数: {len(times)}")

    renamed = 0
    failed = 0

    for i, timeStr in enumerate(times, 1):
        if i not in videoMap:
            print(f"  [{i}] ⚠ 无对应视频, 跳过 {timeStr}")
            failed += 1
            continue

        video = videoMap[i]
        if not video.exists():
            print(f"  [{i}] ⚠ 文件不存在: {video.name}")
            failed += 1
            continue

        newName = timeStr + video.suffix
        if newName == video.name:
            print(f"  [{i}] [SKIP] {video.name}: 已正确命名")
            renamed += 1
            continue

        newPath = video.parent / newName
        if newPath.exists():
            idx2 = 2
            while True:
                newName = f"{timeStr} ({idx2}){video.suffix}"
                newPath = video.parent / newName
                if not newPath.exists():
                    break
                idx2 += 1

        isFail = timeStr.startswith("FAIL_")
        tag = "⚠ FAIL" if isFail else " OK "

        if dryRun:
            print(f"  [{i}] [DRY ] {video.name} → {newName}")
        else:
            for attempt in range(5):
                try:
                    video.rename(newPath)
                    print(f"  [{i}] [{tag}] {video.name} → {newName}")
                    break
                except PermissionError:
                    if attempt < 4:
                        time.sleep(1)
                    else:
                        print(f"  [{i}] [ERR ] {video.name}: 文件被锁定")
                        failed += 1
        renamed += 1

    print(f"\nDone: {renamed} ok, {failed} failed")


def main():
    parser = argparse.ArgumentParser(
        description="AI Timer OCR — 提取帧让 AI 识别成绩 (支持多目录)",
    )
    parser.add_argument("paths", nargs="*", help="视频文件或目录路径 (可多个, --apply 时可省略)")
    parser.add_argument("--seconds", type=float, default=4.0, help="取最后多少秒 (默认: 4.0)")
    parser.add_argument("--apply", type=str, default=None,
                        help='用 AI 识别的成绩重命名, 如: --apply "4.716, 3.698"')
    parser.add_argument("--dry-run", action="store_true", help="仅预览, 不实际重命名")
    args = parser.parse_args()

    if args.apply:
        applyResults(args.apply, dryRun=args.dry_run)
    else:
        if not args.paths:
            print("❌ 请提供至少一个视频目录路径")
            sys.exit(1)
        targets = [Path(p) for p in args.paths]
        extractFrames(targets, seconds=args.seconds)


if __name__ == "__main__":
    main()
