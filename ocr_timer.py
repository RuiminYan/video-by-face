"""
计时器成绩识别模块 (七段 LED 数码管 OCR)

从魔方比赛视频的最后几秒中，识别大显上的还原时间。
利用 LED 自发光的高饱和/高亮度红色特征进行定位，
再用七段数码管查表法逐位识别数字。

用法 (独立运行):
    python ocr_timer.py <视频路径或目录>

示例:
    python ocr_timer.py "D:\\cube\\video-by-face\\video\\4.716.MP4"
    python ocr_timer.py "D:\\cube\\video-by-face\\video"
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

# ---------- 常量 ----------

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv"}

# HSV 范围: 覆盖红色 + 橙色 + 黄色 LED
# 不同品牌大显 LED 颜色不同:
#   - QiYi 大显: 红色 (H ≈ 0~12, 168~180)
#   - MoYu 等: 橙色/黄色 (H ≈ 10~35)
# 窄范围: 仅红色 LED (高饱和, 抗干扰)
_HSV_TIGHT = [
    ((0, 130, 160), (12, 255, 255)),
    ((168, 130, 160), (180, 255, 255)),
]

# 宽范围: 红+橙+黄色 LED (更高亮度要求以排除肤色)
_HSV_WIDE = [
    ((0, 100, 200), (35, 255, 255)),
    ((168, 100, 200), (180, 255, 255)),
]

# 七段数码管编码查表
#   _A_
#  |   |
#  F   B
#  |_G_|
#  |   |
#  E   C
#  |_D_|
#
# 格式: (A, B, C, D, E, F, G) → 字符
_DIGIT_MAP = {
    (1, 1, 1, 1, 1, 1, 0): "0",
    (0, 1, 1, 0, 0, 0, 0): "1",
    (1, 1, 0, 1, 1, 0, 1): "2",
    (1, 1, 1, 1, 0, 0, 1): "3",
    (0, 1, 1, 0, 0, 1, 1): "4",
    (1, 0, 1, 1, 0, 1, 1): "5",
    (1, 0, 1, 1, 1, 1, 1): "6",
    (1, 1, 1, 0, 0, 0, 0): "7",
    (1, 1, 1, 1, 1, 1, 1): "8",
    (1, 1, 1, 1, 0, 1, 1): "9",
}


# ---------- 帧提取 ----------


def extractLastFrames(
    videoPath: Path, seconds: float = 2.0, count: int = 5
) -> list[np.ndarray]:
    """
    从视频最后 N 秒中截取帧，末尾更密集。

    采样策略: 后半段 (最后1秒) 的帧密度是前半段的 2 倍。
    这是因为成绩定型通常发生在视频最末尾。

    Args:
        videoPath: 视频文件路径
        seconds: 取最后多少秒 (默认 2.0)
        count: 取多少帧 (默认 5)

    Returns:
        帧列表 (BGR 格式 ndarray)
    """
    cap = cv2.VideoCapture(str(videoPath))
    if not cap.isOpened():
        return []

    fps = cap.get(cv2.CAP_PROP_FPS)
    totalFrames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    if fps <= 0 or totalFrames <= 0:
        cap.release()
        return []

    startFrame = max(0, int(totalFrames - fps * seconds))
    endFrame = totalFrames - 1
    midFrame = (startFrame + endFrame) // 2  # 后半起点

    # 前半均匀取 1/3 的帧, 后半均匀取 2/3 的帧
    nFront = max(1, count // 3)
    nBack = count - nFront

    posFront = np.linspace(startFrame, midFrame, nFront, endpoint=False, dtype=int)
    posBack = np.linspace(midFrame, endFrame, nBack, dtype=int)
    positions = np.concatenate([posFront, posBack])

    frames = []
    for pos in positions:
        cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        ret, frame = cap.read()
        if ret:
            frames.append(frame)

    cap.release()
    return frames


# ---------- 大显定位 ----------


def _createLedMask(
    frame: np.ndarray,
    hsvRanges: list[tuple[tuple, tuple]] | None = None,
) -> np.ndarray:
    """
    用 HSV 颜色过滤提取 LED 自发光像素。

    Args:
        frame: BGR 帧
        hsvRanges: HSV 范围列表, 默认用窄范围(仅红色)

    Returns:
        二值 mask (255=LED像素, 0=背景)
    """
    if hsvRanges is None:
        hsvRanges = _HSV_TIGHT
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for lo, hi in hsvRanges:
        mask |= cv2.inRange(hsv, lo, hi)

    # 闭操作：连接七段段间的间隙
    kernelClose = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernelClose)
    # 开操作：去除小噪点
    kernelOpen = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernelOpen)

    return mask


def _findDigitCandidates(
    mask: np.ndarray, imgH: int,
) -> list[tuple[int, int, int, int]]:
    """
    在 LED mask 中找到可能是数字的轮廓。

    筛选条件:
    - 高度在画面的 3%~30% 之间
    - 宽高比在 0.15~1.0 之间 (七段数字的典型范围)

    Returns:
        [(x, y, w, h), ...] 候选数字的包围盒列表
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    minH = imgH * 0.03
    maxH = imgH * 0.30
    candidates = []

    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if h < minH or h > maxH:
            continue
        aspect = w / h if h > 0 else 0
        if 0.15 <= aspect <= 1.0:
            candidates.append((x, y, w, h))

    return candidates


def _clusterIntoRows(
    candidates: list[tuple[int, int, int, int]],
) -> list[list[tuple[int, int, int, int]]]:
    """
    将数字候选按 y 坐标聚类，找出同一水平线上的数字组。

    Returns:
        [[digit1, digit2, ...], ...] 按行分组的数字列表
    """
    if not candidates:
        return []

    # 按 y 排序
    sortedCands = sorted(candidates, key=lambda d: d[1])

    rows: list[list[tuple[int, int, int, int]]] = []
    currentRow = [sortedCands[0]]

    for d in sortedCands[1:]:
        refH = currentRow[0][3]  # 参考行首个数字的高度
        if abs(d[1] - currentRow[0][1]) < refH * 0.5:
            currentRow.append(d)
        else:
            rows.append(currentRow)
            currentRow = [d]
    rows.append(currentRow)

    return rows


def _selectBestRow(
    rows: list[list[tuple[int, int, int, int]]],
) -> list[tuple[int, int, int, int]] | None:
    """
    选择最可能是选手大显的数字行。

    策略:
    1. 过滤行内高度不一致的候选 (真正大显数字高度差异 < 30%)
    2. 取含数字最多且平均高度最大的行
    3. 至少要有 3 个一致数字才认为有效

    Returns:
        最佳行的数字列表 (按x排序), 或 None
    """
    cleanedRows = []
    for row in rows:
        if len(row) < 3:
            continue
        # 取中位数高度和宽度, 过滤掉高度偏差>35%的异常候选
        # 但如果候选很窄 (宽度 < 中位宽度的 50%), 可能是 "1", 保留
        heights = sorted(d[3] for d in row)
        widths = sorted(d[2] for d in row)
        medH = heights[len(heights) // 2]
        medW = widths[len(widths) // 2]
        consistent = [
            d for d in row
            if abs(d[3] - medH) < medH * 0.35 or d[2] < medW * 0.5
        ]
        if len(consistent) >= 3:
            cleanedRows.append(consistent)

    if not cleanedRows:
        return None

    # 评分: 数字数量 × 平均高度
    best = max(cleanedRows, key=lambda r: len(r) * sum(d[3] for d in r) / len(r))
    best.sort(key=lambda d: d[0])
    return best


def detectTimerROI(
    frame: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, list[tuple[int, int, int, int]]] | None:
    """
    定位帧中计时器大显的 ROI 区域。

    双通道策略: 先用窄范围 (仅红色LED) 检测，如果失败再用
    宽范围 (红+橙+黄) 重试，以兼容不同品牌大显。

    Returns:
        (roi_bgr, roi_mask, digit_bboxes_in_roi) 或 None
        digit_bboxes_in_roi 中的坐标已转换为相对于 ROI 的坐标
    """
    imgH, imgW = frame.shape[:2]

    # 双通道: 先窄后宽
    for hsvRanges in [_HSV_TIGHT, _HSV_WIDE]:
        mask = _createLedMask(frame, hsvRanges)
        candidates = _findDigitCandidates(mask, imgH)
        if not candidates:
            continue
        rows = _clusterIntoRows(candidates)
        bestRow = _selectBestRow(rows)
        # 魔方比赛时间至少 X.XXX = 4位数字
        if bestRow is not None and len(bestRow) >= 4:
            break
    else:
        return None

    # 计算 ROI 包围盒 (稍微扩展以包含小数点)
    minX = min(d[0] for d in bestRow)
    minY = min(d[1] for d in bestRow)
    maxX = max(d[0] + d[2] for d in bestRow)
    maxY = max(d[1] + d[3] for d in bestRow)

    padX = int((maxX - minX) * 0.08)
    padY = int((maxY - minY) * 0.15)
    x1 = max(0, minX - padX)
    y1 = max(0, minY - padY)
    x2 = min(imgW, maxX + padX)
    y2 = min(imgH, maxY + padY)

    roi = frame[y1:y2, x1:x2]
    roiMask = mask[y1:y2, x1:x2]

    # 坐标转换为相对 ROI
    roiBboxes = [(d[0] - x1, d[1] - y1, d[2], d[3]) for d in bestRow]

    return roi, roiMask, roiBboxes


# ---------- 数字识别 (模板匹配) ----------

# 程序化生成标准七段数字模板
_TEMPLATE_W, _TEMPLATE_H = 24, 40


def _generateDigitTemplate(
    digitStr: str, width: int = 24, height: int = 40, thickness: int = 4
) -> np.ndarray:
    """程序化生成一个标准七段数字的 mask 模板"""
    img = np.zeros((height, width), dtype=np.uint8)
    segmentsOn = {
        "0": "ABCDEF",  "1": "BC",    "2": "ABDEG",  "3": "ABCDG",
        "4": "BCFG",    "5": "ACDFG", "6": "ACDEFG", "7": "ABC",
        "8": "ABCDEFG", "9": "ABCDFG",
    }
    active = segmentsOn.get(digitStr, "")
    t, m = thickness, 2
    mid = height // 2

    # 水平段
    if "A" in active:
        cv2.rectangle(img, (m + t, m), (width - m - t, m + t), 255, -1)
    if "G" in active:
        cv2.rectangle(img, (m + t, mid - t // 2), (width - m - t, mid + t // 2), 255, -1)
    if "D" in active:
        cv2.rectangle(img, (m + t, height - m - t), (width - m - t, height - m), 255, -1)

    # 垂直段
    if "F" in active:
        cv2.rectangle(img, (m, m + t), (m + t, mid - t // 2), 255, -1)
    if "B" in active:
        cv2.rectangle(img, (width - m - t, m + t), (width - m, mid - t // 2), 255, -1)
    if "E" in active:
        cv2.rectangle(img, (m, mid + t // 2), (m + t, height - m - t), 255, -1)
    if "C" in active:
        cv2.rectangle(img, (width - m - t, mid + t // 2), (width - m, height - m - t), 255, -1)

    return img


# 模块加载时预生成所有模板
_DIGIT_TEMPLATES = {d: _generateDigitTemplate(d) for d in "0123456789"}


def recognizeDigit(
    digitMask: np.ndarray,
    bboxW: int = 0,
    bboxH: int = 0,
    avgW: float = 0,
) -> str | None:
    """
    用模板匹配识别单个数字。

    策略:
    1. 宽高比预判: 如果数字非常窄 (aspect < 0.35 且相对宽度 < 0.55)，
       直接返回 "1" (七段 LED 上 "1" 极窄, 模板匹配不可靠)
    2. 将数字 mask resize 到标准大小后与预生成模板做归一化互相关

    模板匹配相比七段查表的优势:
    - 对透视变形/笔画倾斜鲁棒
    - 不依赖严格的水平/垂直笔画假设

    Args:
        digitMask: 单个数字的二值 mask
        bboxW: 包围盒宽度 (用于宽高比判断)
        bboxH: 包围盒高度
        avgW: 同行所有数字的平均宽度

    Returns:
        识别到的数字字符 ("0"~"9") 或 None
    """
    h, w = digitMask.shape[:2]
    if h < 5 or w < 3:
        return None

    # 如果提供了包围盒尺寸，用宽高比预判 "1"
    if bboxW > 0 and bboxH > 0:
        aspect = bboxW / bboxH
        widthRatio = bboxW / avgW if avgW > 0 else 1.0
        if aspect < 0.35 and widthRatio < 0.55:
            return "1"

    # 模板匹配
    resized = cv2.resize(digitMask, (_TEMPLATE_W, _TEMPLATE_H), interpolation=cv2.INTER_AREA)
    _, resized = cv2.threshold(resized, 127, 255, cv2.THRESH_BINARY)

    bestDigit = None
    bestScore = -1.0

    for digitStr, tmpl in _DIGIT_TEMPLATES.items():
        result = cv2.matchTemplate(resized, tmpl, cv2.TM_CCOEFF_NORMED)
        score = result[0, 0]
        if score > bestScore:
            bestScore = score
            bestDigit = digitStr

    return bestDigit


# ---------- 组装时间字符串 ----------


def assembleTime(digits: list[str | None]) -> str | None:
    """
    将识别到的数字组装为时间字符串。

    规则: 小数点永远在最后 3 位数字前 (千分之一秒精度)。
    - 4位 → X.XXX  (如 4915 → 4.915)
    - 5位 → XX.XXX (如 43243 → 43.243)
    - 6位 → X:XX.XXX 但文件名用 X_XX.XXX (如 146315 → 1_46.315)

    Args:
        digits: 识别到的数字列表 (可能含 None)

    Returns:
        时间字符串 或 None (识别失败)
    """
    # 如果超过一半的数字没识别出来，放弃
    noneCount = sum(1 for d in digits if d is None)
    if noneCount > len(digits) / 2:
        return None

    raw = "".join(d if d is not None else "?" for d in digits)

    if "?" in raw:
        return None

    n = len(raw)
    if n < 4 or n > 7:
        return None

    # 最后 3 位是毫秒
    millis = raw[-3:]
    rest = raw[:-3]

    if len(rest) <= 2:
        # X.XXX 或 XX.XXX
        return f"{rest}.{millis}"
    else:
        # X:XX.XXX → 文件名用 X_XX.XXX
        minutes = rest[:-2]
        seconds = rest[-2:]
        return f"{minutes}_{seconds}.{millis}"


# ---------- 主入口 ----------


def _recognizeFrame(frame: np.ndarray) -> str | None:
    """
    对单帧进行完整的计时器识别流程。

    Returns:
        时间字符串 (如 "4.716") 或 None
    """
    detection = detectTimerROI(frame)
    if detection is None:
        return None

    roi, roiMask, digitBboxes = detection

    # 计算平均数字宽度 (用于 "1" 的宽高比预判)
    avgW = sum(d[2] for d in digitBboxes) / len(digitBboxes)

    # 逐个数字识别
    digits: list[str | None] = []
    for x, y, w, h in digitBboxes:
        digitMask = roiMask[y : y + h, x : x + w]
        digit = recognizeDigit(digitMask, bboxW=w, bboxH=h, avgW=avgW)
        digits.append(digit)

    # 用固定规则组装 (小数点在最后3位前)
    return assembleTime(digits)


def readTimer(
    videoPath: Path,
    seconds: float = 4.0,
    frameCount: int = 15,
    debug: bool = False,
) -> str | None:
    """
    从视频中识别计时器成绩。

    策略 (稳定性检测):
    1. 取视频最后 N 秒的帧 (密集采样)
    2. 每帧尝试定位大显并识别数字
    3. 寻找连续多帧读取到相同数字的片段 → 成绩已定型
    4. 如果没有连续稳定的读数，回退到投票策略

    成绩定型的标志: 计时器停止后，连续帧显示相同数字。
    计时器还在跑的时候，每帧数字都在变化。

    Args:
        videoPath: 视频文件路径
        seconds: 取最后多少秒 (默认 4.0，覆盖更大范围)
        frameCount: 取帧数量 (默认 10，密集采样)
        debug: 是否输出调试信息

    Returns:
        时间字符串 (如 "4.716") 或 None
    """
    frames = extractLastFrames(videoPath, seconds, frameCount)
    if not frames:
        if debug:
            print(f"  ⚠ 无法从视频中提取帧")
        return None

    # 每帧识别，保留顺序
    results: list[str | None] = []

    for i, frame in enumerate(frames):
        timeStr = _recognizeFrame(frame)
        results.append(timeStr)
        if debug:
            print(f"  帧{i}: {timeStr or '未检测到'}")

    # 策略 1: 稳定性检测 — 找连续 ≥2 帧相同的读数
    # 从后往前找（最后稳定的读数最可能是最终成绩）
    for i in range(len(results) - 1, 0, -1):
        if results[i] is not None and results[i] == results[i - 1]:
            if debug:
                print(f"  ✓ 稳定性检测: 帧{i-1}~{i} 连续读到 '{results[i]}'")
            return results[i]

    # 策略 2: 加权投票 — 后面帧权重更高 (更可能是最终成绩)
    validResults = [(i, r) for i, r in enumerate(results) if r is not None]
    if not validResults:
        if debug:
            print(f"  ❌ 所有帧都未成功识别")
        return None

    # 帧越靠后权重越高: weight = 1 + i/total
    total = len(results)
    scores: dict[str, float] = {}
    for i, r in validResults:
        w = 1.0 + i / total
        scores[r] = scores.get(r, 0) + w

    bestResult = max(scores, key=scores.get)  # type: ignore
    counter = Counter(r for _, r in validResults)

    if debug:
        print(f"  投票回退: {counter} → '{bestResult}' ({counter[bestResult]}票, 加权={scores[bestResult]:.1f})")

    return bestResult


# ---------- CLI ----------


def main():
    parser = argparse.ArgumentParser(
        description="计时器成绩识别工具 (七段 LED OCR)",
    )
    parser.add_argument("path", help="视频文件或目录路径")
    parser.add_argument("--seconds", type=float, default=4.0, help="取最后多少秒 (默认: 4.0)")
    parser.add_argument("--frames", type=int, default=15, help="取帧数量 (默认: 15)")
    parser.add_argument("--debug", action="store_true", help="输出调试信息")
    args = parser.parse_args()

    target = Path(args.path)

    if target.is_file():
        videos = [target]
    elif target.is_dir():
        videos = sorted(
            f for f in target.iterdir()
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
        )
    else:
        print(f"❌ 路径不存在: {target}")
        sys.exit(1)

    if not videos:
        print(f"❌ 未找到视频文件")
        sys.exit(1)

    print(f"🔢 识别 {len(videos)} 个视频的计时器成绩...\n")

    for video in videos:
        result = readTimer(
            video,
            seconds=args.seconds,
            frameCount=args.frames,
            debug=args.debug,
        )

        expected = video.stem  # 文件名就是预期成绩
        status = "✅" if result == expected else "❓"
        print(f"  {status} {video.name}: {result or '未识别'}" +
              (f" (预期: {expected})" if result != expected else ""))


if __name__ == "__main__":
    main()
