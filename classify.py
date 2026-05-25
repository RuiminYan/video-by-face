"""
视频按人脸分类工具 (OpenCV 原生版)

从视频中截取帧，用 OpenCV 内置的 YuNet 检测人脸、SFace 提取特征，
与参考照片对比，将视频移动到对应选手的文件夹中。

用法:
    python classify.py <视频目录> [选项]

示例:
    python classify.py "Z:\魔方比赛\260307"
    python classify.py "Z:\魔方比赛\260307" --copy --threshold 0.35
"""

import argparse
import os
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np
from tqdm import tqdm

# NOTE: OpenCV 的 imread/VideoCapture 在 Windows 上不支持非 ASCII 路径（如中文）
# 必须用 np.fromfile + imdecode 绕过

# 支持的视频扩展名
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv"}

# 脚本所在目录作为项目根目录
PROJECT_DIR = Path(__file__).resolve().parent
REFERENCES_DIR = PROJECT_DIR / "person"
OUTPUT_DIR = PROJECT_DIR
MODELS_DIR = PROJECT_DIR / "models"

# 模型下载地址 (OpenCV Zoo 官方)
YUNET_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
SFACE_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"

YUNET_PATH = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
SFACE_PATH = MODELS_DIR / "face_recognition_sface_2021dec.onnx"


def downloadModel(url: str, dest: Path) -> None:
    """下载模型文件（带进度显示）"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  📥 下载 {dest.name}...")

    # NOTE: 使用 urlretrieve + reporthook 显示下载进度
    def progressHook(blockNum, blockSize, totalSize):
        downloaded = blockNum * blockSize
        if totalSize > 0:
            pct = min(100, downloaded * 100 // totalSize)
            mb = downloaded / 1024 / 1024
            totalMb = totalSize / 1024 / 1024
            print(f"\r     {mb:.1f}/{totalMb:.1f} MB ({pct}%)", end="", flush=True)

    urllib.request.urlretrieve(url, str(dest), reporthook=progressHook)
    print()  # 换行


def ensureModels() -> None:
    """确保模型文件已下载"""
    if not YUNET_PATH.exists():
        downloadModel(YUNET_URL, YUNET_PATH)
    if not SFACE_PATH.exists():
        downloadModel(SFACE_URL, SFACE_PATH)
    print(f"  ✓ 模型已就绪: {MODELS_DIR}")


def imreadUnicode(path: Path) -> np.ndarray | None:
    """读取含中文路径的图片（绕过 cv2.imread 的 ASCII 限制）"""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


def createDetector(inputSize: tuple[int, int] = (320, 320)) -> cv2.FaceDetectorYN:
    """创建 YuNet 人脸检测器"""
    return cv2.FaceDetectorYN.create(
        model=str(YUNET_PATH),
        config="",
        input_size=inputSize,
        score_threshold=0.7,  # 检测置信度阈值
        nms_threshold=0.3,
        top_k=5,
    )


def createRecognizer() -> cv2.FaceRecognizerSF:
    """创建 SFace 人脸识别器"""
    return cv2.FaceRecognizerSF.create(
        model=str(SFACE_PATH),
        config="",
    )


# NOTE: OpenCV DNN (YuNet/SFace) 非线程安全——多线程共用一个实例会导致 C++ 层崩溃 (进程闪退)。
# 每个 worker 线程在 _getThreadModels() 里 lazily 持有自己的一份。
_threadLocal = threading.local()
_modelInitLock = threading.Lock()


def _getThreadModels() -> tuple[cv2.FaceDetectorYN, cv2.FaceRecognizerSF]:
    """Return this thread's (detector, recognizer), creating them on first call."""
    if not hasattr(_threadLocal, "detector"):
        # Serialize model loading to avoid concurrent ONNX parse in OpenCV
        with _modelInitLock:
            _threadLocal.detector = createDetector()
            _threadLocal.recognizer = createRecognizer()
    return _threadLocal.detector, _threadLocal.recognizer


def detectLargestFace(
    img: np.ndarray, detector: cv2.FaceDetectorYN
) -> np.ndarray | None:
    """
    检测图像中最大的人脸。

    返回: 人脸信息数组 (1x15) 或 None
    YuNet 输出格式: [x, y, w, h, ..., score] 共 15 个值
    """
    h, w = img.shape[:2]
    detector.setInputSize((w, h))
    _, faces = detector.detect(img)

    if faces is None or len(faces) == 0:
        return None

    # 取面积最大的脸（最可能是主体）
    areas = faces[:, 2] * faces[:, 3]  # w * h
    largest = faces[np.argmax(areas)]
    return largest


def extractEmbedding(
    img: np.ndarray,
    face: np.ndarray,
    recognizer: cv2.FaceRecognizerSF,
) -> np.ndarray:
    """从检测到的人脸中提取 128 维嵌入向量"""
    aligned = recognizer.alignCrop(img, face)
    return recognizer.feature(aligned)


def extractFrames(videoPath: Path, count: int = 3) -> list[np.ndarray]:
    """
    从视频中均匀截取多帧。

    策略: 跳过首尾 10%，在中间 80% 区域均匀取帧，
    避免开头/结尾的黑屏、转场等干扰。
    """
    # NOTE: VideoCapture 也不支持中文路径，但可以接受 Windows 短路径
    # 这里用 os.open + FileIO 无法绕过，只能用短路径或 raw bytes
    # 实测 VideoCapture 对中文路径兼容性比 imread 好，但仍可能失败
    cap = cv2.VideoCapture(str(videoPath))
    if not cap.isOpened():
        return []

    totalFrames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if totalFrames <= 0:
        cap.release()
        return []

    # 跳过首尾 10%
    start = int(totalFrames * 0.1)
    end = int(totalFrames * 0.9)
    if end <= start:
        start, end = 0, totalFrames - 1

    positions = np.linspace(start, end, count, dtype=int)
    frames = []

    for pos in positions:
        cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        ret, frame = cap.read()
        if ret:
            frames.append(frame)

    cap.release()
    return frames


def buildReferenceDb(
    refDir: Path,
    detector: cv2.FaceDetectorYN,
    recognizer: cv2.FaceRecognizerSF,
) -> dict[str, list[np.ndarray]]:
    """
    为每位选手的参考照片计算人脸嵌入向量。

    返回: {人名: [embedding1, embedding2, ...], ...}
    """
    db = {}
    personDirs = [d for d in refDir.iterdir() if d.is_dir()]

    if not personDirs:
        print(f"❌ 参考照片目录为空: {refDir}")
        sys.exit(1)

    print(f"📷 加载参考照片 ({len(personDirs)} 人)...")

    for personDir in sorted(personDirs):
        name = personDir.name
        embeddings = []
        photos = [
            f
            for f in personDir.iterdir()
            if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
        ]

        if not photos:
            print(f"  ⚠ {name}: 没有找到照片，跳过")
            continue

        for photo in photos:
            img = imreadUnicode(photo)
            if img is None:
                print(f"  ⚠ {name}/{photo.name}: 无法读取图片")
                continue

            face = detectLargestFace(img, detector)
            if face is None:
                print(f"  ⚠ {name}/{photo.name}: 未检测到人脸")
                continue

            emb = extractEmbedding(img, face, recognizer)
            embeddings.append(emb)

        if embeddings:
            db[name] = embeddings
            print(f"  ✓ {name}: {len(embeddings)} 张照片已加载")
        else:
            print(f"  ✗ {name}: 所有照片都无法提取人脸")

    if not db:
        print("❌ 没有成功加载任何参考照片")
        sys.exit(1)

    return db


def matchFace(
    embedding: np.ndarray,
    refDb: dict[str, list[np.ndarray]],
    recognizer: cv2.FaceRecognizerSF,
    threshold: float,
) -> tuple[str | None, float]:
    """
    将一个人脸嵌入向量与参考数据库对比。

    返回: (匹配的人名或None, 最高相似度)
    """
    bestName = None
    bestScore = 0.0

    for name, refEmbeddings in refDb.items():
        for refEmb in refEmbeddings:
            # NOTE: FR_COSINE 返回余弦相似度，范围 [0, 1]
            score = recognizer.match(
                embedding, refEmb, cv2.FaceRecognizerSF_FR_COSINE
            )
            if score > bestScore:
                bestScore = score
                bestName = name

    if bestScore >= threshold:
        return bestName, bestScore
    return None, bestScore


def classifyVideo(
    videoPath: Path,
    refDb: dict[str, list[np.ndarray]],
    detector: cv2.FaceDetectorYN,
    recognizer: cv2.FaceRecognizerSF,
    threshold: float,
) -> tuple[str | None, float]:
    """
    对单个视频进行人脸分类。

    策略: 截取 3 帧，每帧都尝试匹配，取最高置信度的结果。
    多帧投票提高鲁棒性（应对眨眼、低头等瞬间）。
    """
    frames = extractFrames(videoPath, count=3)
    if not frames:
        return None, 0.0

    bestName = None
    bestScore = 0.0

    for frame in frames:
        face = detectLargestFace(frame, detector)
        if face is None:
            continue

        emb = extractEmbedding(frame, face, recognizer)
        name, score = matchFace(emb, refDb, recognizer, threshold)
        if score > bestScore:
            bestScore = score
            bestName = name

    # 最终判断是否达到阈值
    if bestScore >= threshold:
        return bestName, bestScore
    return None, bestScore


def main():
    parser = argparse.ArgumentParser(
        description="视频按人脸分类工具 (OpenCV)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python classify.py "Z:\\魔方比赛\\260307"
  python classify.py "Z:\\魔方比赛\\260307" --copy
  python classify.py "Z:\\魔方比赛\\260307" --threshold 0.35 --dry-run
        """,
    )
    parser.add_argument("video_dir", help="包含视频文件的源目录")
    parser.add_argument(
        "--ref-dir",
        default=str(REFERENCES_DIR),
        help=f"参考照片目录 (默认: {REFERENCES_DIR})",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        # NOTE: 默认输出到视频源目录，分类后的子文件夹建在视频旁边
        help="输出目录 (默认: 与视频源目录相同)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.35,
        # NOTE: SFace 余弦相似度范围 0~1，同一人通常 >0.4，不同人 <0.3
        help="匹配阈值 (默认: 0.35, 越高越严格, 建议范围 0.25~0.50)",
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="复制视频而非移动 (默认: 移动)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览分类结果，不实际移动/复制文件",
    )

    args = parser.parse_args()

    videoDir = Path(args.video_dir)
    refDir = Path(args.ref_dir)
    outputDir = Path(args.output_dir) if args.output_dir else videoDir
    unknownDir = outputDir / "unknown"

    if not videoDir.exists():
        print(f"❌ 视频目录不存在: {videoDir}")
        sys.exit(1)
    if not refDir.exists():
        print(f"❌ 参考照片目录不存在: {refDir}")
        print(f"   请创建 {refDir} 并放入选手照片")
        print(f"   结构: person/<人名>/<照片.jpg>")
        sys.exit(1)

    # 收集所有视频文件
    videos = sorted(
        f
        for f in videoDir.iterdir()
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
    )

    if not videos:
        print(f"❌ 目录中没有视频文件: {videoDir}")
        sys.exit(1)

    print(f"🎬 找到 {len(videos)} 个视频文件")
    print(f"📁 参考照片目录: {refDir}")
    print(f"📁 输出目录: {outputDir}")
    print(f"🔧 阈值: {args.threshold}")
    print(f"📋 模式: {'仅预览' if args.dry_run else '复制' if args.copy else '移动'}")
    print()

    # 确保模型文件已下载
    print("🧠 初始化模型...")
    ensureModels()

    detector = createDetector()
    recognizer = createRecognizer()
    print("  ✓ 模型加载完成\n")

    # 构建参考人脸数据库
    refDb = buildReferenceDb(refDir, detector, recognizer)

    print(f"\n🚀 开始分类 {len(videos)} 个视频...\n")

    # 统计结果
    stats = {"success": 0, "unknown": 0, "error": 0}
    results = []  # (视频名, 匹配人名, 相似度)
    startTime = time.time()

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _classifyOne(video):
        try:
            # 每个 worker 用自己的 detector/recognizer (OpenCV DNN 非线程安全)
            threadDetector, threadRecognizer = _getThreadModels()
            name, score = classifyVideo(
                video, refDb, threadDetector, threadRecognizer, args.threshold
            )
            return video, name, score, None
        except Exception as e:
            return video, None, 0.0, e

    def _handleResult(video, name, score, error):
        if error is not None:
            results.append((video.name, f"❌ 错误: {error}", 0.0))
            stats["error"] += 1
            return

        if name:
            destDir = outputDir / name
            results.append((video.name, name, score))
            stats["success"] += 1

            if not args.dry_run:
                destDir.mkdir(parents=True, exist_ok=True)
                dest = destDir / video.name
                if args.copy:
                    shutil.copy2(video, dest)
                else:
                    shutil.move(str(video), str(dest))
        else:
            results.append((video.name, "❓ unknown", score))
            stats["unknown"] += 1

            if not args.dry_run:
                unknownDir.mkdir(parents=True, exist_ok=True)
                dest = unknownDir / video.name
                if args.copy:
                    shutil.copy2(video, dest)
                else:
                    shutil.move(str(video), str(dest))

    # Calculate workers based on available memory
    # Each classify worker: own SFace (~40MB) + YuNet + 3 frames + inference ≈ 100MB
    try:
        import psutil
        availMB = psutil.virtual_memory().available // (1024 * 1024)
        reserveMB = 2048
        perWorkerMB = 100
        memWorkers = max(1, (availMB - reserveMB) // perWorkerMB)
    except ImportError:
        memWorkers = 4
    workers = min(memWorkers, os.cpu_count() or 4, len(videos))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_classifyOne, v): v for v in videos}
        for future in tqdm(as_completed(futures), total=len(videos), desc="分类进度", unit="个"):
            video, name, score, error = future.result()
            _handleResult(video, name, score, error)

    elapsed = time.time() - startTime

    # 打印分类报告
    print("\n" + "=" * 70)
    print("📊 分类报告")
    print("=" * 70)

    # 按人名分组显示
    grouped = {}
    for videoName, personName, score in results:
        grouped.setdefault(personName, []).append((videoName, score))

    for personName in sorted(grouped.keys()):
        items = grouped[personName]
        print(f"\n👤 {personName} ({len(items)} 个视频):")
        for videoName, score in sorted(items):
            print(f"   {videoName}  (相似度: {score:.3f})")

    print(f"\n{'=' * 70}")
    print(f"✅ 成功匹配: {stats['success']}")
    print(f"❓ 未识别:   {stats['unknown']}")
    print(f"❌ 出错:     {stats['error']}")
    print(f"⏱  总耗时:   {elapsed:.1f} 秒")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
