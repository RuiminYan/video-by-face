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
import gc
import os
import sys
import time
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

# 中范围: 红+橙+黄 LED (中等阈值, 平衡覆盖与抗干扰)
_HSV_MED = [
    ((0, 120, 180), (30, 255, 255)),
    ((168, 120, 180), (180, 255, 255)),
]

# 宽范围: 红+橙+黄色 LED (最宽松, 覆盖低饱和度的黄色 LED)
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

    性能优化: 顺序读取代替随机 seek。
    H.264/H.265 随机 seek 需从最近关键帧解码，25帧 ≈ 8.3s；
    顺序从 startFrame 读取并筛选目标帧，仅需 ~0.85s。

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
    # 用 int() 转换，避免 np.int64 在 set 中比较慢
    positions: set[int] = {int(p) for p in np.concatenate([posFront, posBack])}

    # 顺序读取: 只 seek 一次到起始位置，逐帧读取并筛选
    cap.set(cv2.CAP_PROP_POS_FRAMES, startFrame)
    frames = []
    for frameIdx in range(startFrame, endFrame + 1):
        ret, frame = cap.read()
        if not ret:
            break
        if frameIdx in positions:
            frames.append(frame)
            if len(frames) >= count:
                break  # 采到足够帧就停，不读后面多余的帧

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


def _mergeSegments(
    segments: list[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    """
    合并属于同一数字的 LED 段 (segments) 为完整数字 bbox。

    只处理宽高比 < 0.4 的窄段 (明确是七段显示器的单段)。
    如果两个段在 x 方向间距 < 段高 且 y 方向重叠，则合并。
    """
    if not segments:
        return []

    rects = sorted(segments, key=lambda d: d[0])
    merged: list[list[int]] = []  # [x, y, x2, y2]

    for x, y, w, h in rects:
        x2, y2 = x + w, y + h
        if merged:
            last = merged[-1]
            lx, ly, lx2, ly2 = last
            avgH = ((ly2 - ly) + h) / 2
            xGap = x - lx2
            yOverlap = min(y2, ly2) - max(y, ly)
            if xGap < avgH * 1.2 and yOverlap > 0:
                last[0] = min(lx, x)
                last[1] = min(ly, y)
                last[2] = max(lx2, x2)
                last[3] = max(ly2, y2)
                continue
        merged.append([x, y, x2, y2])

    return [(m[0], m[1], m[2] - m[0], m[3] - m[1]) for m in merged]


def _findDigitCandidates(
    mask: np.ndarray, imgH: int, minHRatio: float = 0.03,
) -> list[tuple[int, int, int, int]]:
    """
    在 LED mask 中找到可能是数字的轮廓。

    筛选条件:
    - 高度在画面的 minHRatio~30% 之间
    - 宽高比在 0.15~1.0 之间 (七段数字的典型范围)
    - 窄段 (ar < 0.4) 会被合并为完整数字

    Returns:
        [(x, y, w, h), ...] 候选数字的包围盒列表
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    minH = imgH * minHRatio
    maxH = imgH * 0.30
    normalDigits = []   # ar >= 0.4: likely complete digits
    narrowSegs = []     # ar <  0.4: likely individual segments

    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if h < minH or h > maxH:
            continue
        aspect = w / h if h > 0 else 0
        if aspect < 0.15 or aspect > 1.0:
            continue
        if aspect < 0.4:
            narrowSegs.append((x, y, w, h))
        else:
            normalDigits.append((x, y, w, h))

    # Merge narrow segments into digit-level bboxes
    mergedSegs = _mergeSegments(narrowSegs)
    # Filter merged: only keep those with reasonable digit aspect ratio
    for m in mergedSegs:
        ar = m[2] / m[3] if m[3] > 0 else 0
        if 0.15 <= ar <= 1.0:
            normalDigits.append(m)

    return normalDigits


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

    # 过滤掉间距异常的离群数字 (来自不同显示器的误纳入)
    # 大显上相邻数字间距通常 < 数字宽度的 2 倍
    if len(best) >= 4:
        gaps = [best[i + 1][0] - (best[i][0] + best[i][2]) for i in range(len(best) - 1)]
        medGap = sorted(gaps)[len(gaps) // 2]
        threshold = max(medGap * 3, best[0][2] * 3)  # 3x median gap or 3x digit width
        # Remove digits that are too far from their neighbor
        keep = list(range(len(best)))
        for i in range(len(gaps)):
            if gaps[i] > threshold:
                # Large gap between i and i+1: one side is the outlier
                # Keep the side with more digits
                leftCount = i + 1
                rightCount = len(best) - i - 1
                if leftCount <= rightCount:
                    keep = [j for j in keep if j > i]
                else:
                    keep = [j for j in keep if j <= i]
        best = [best[j] for j in keep]
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

    # --- Pass 1: standard detection ---
    for hsvRanges in [_HSV_TIGHT, _HSV_MED, _HSV_WIDE]:
        mask = _createLedMask(frame, hsvRanges)
        candidates = _findDigitCandidates(mask, imgH)
        if not candidates:
            continue
        rows = _clusterIntoRows(candidates)
        bestRow = _selectBestRow(rows)
        if bestRow is not None and len(bestRow) >= 4:
            break
    else:
        # --- Pass 2: aggressive close for small displays (Speed Stacks) ---
        bigKernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 15))
        for hsvRanges in [_HSV_TIGHT, _HSV_MED]:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            rawMask = np.zeros(hsv.shape[:2], dtype=np.uint8)
            for lo, hi in hsvRanges:
                rawMask |= cv2.inRange(hsv, lo, hi)
            mask = cv2.morphologyEx(rawMask, cv2.MORPH_CLOSE, bigKernel)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                                    cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
            candidates = _findDigitCandidates(mask, imgH, minHRatio=0.015)
            if not candidates:
                continue
            rows = _clusterIntoRows(candidates)
            bestRow = _selectBestRow(rows)
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
    # "1" 在七段LED上只有BC两个竖段, 宽度远小于其他数字
    # 模板匹配对 "1" 极不可靠 (总是误判为 "2"), 必须用宽高比拦截
    if bboxW > 0 and bboxH > 0:
        aspect = bboxW / bboxH
        widthRatio = bboxW / avgW if avgW > 0 else 1.0
        if aspect < 0.35 and widthRatio < 0.65:
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
        # X.XXX or XX.XXX
        rest = rest.lstrip('0') or '0'  # strip leading zeros, keep at least '0'
        return f"{rest}.{millis}"
    else:
        # X:XX.XXX -> filename uses X_XX.XXX
        minutes = rest[:-2].lstrip('0') or '0'
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

    # Fallback: if detection failed on high-res frame, try downscaled
    # (Speed Stacks displays are small; segments too tiny relative to 4K frame)
    if detection is None:
        h, w = frame.shape[:2]
        if h > 1100:
            scale = 1080 / h
            frame = cv2.resize(frame, (int(w * scale), 1080),
                               interpolation=cv2.INTER_AREA)
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
    frameCount: int = 25,
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
        # Filter out timer-reset readings (0.000 is never a valid solve time)
        if timeStr is not None and timeStr.replace('_', '').replace('.', '').lstrip('0') == '':
            timeStr = None
        results.append(timeStr)
        if debug:
            print(f"  帧{i}: {timeStr or '未检测到'}")

    # All strategies operate on the tail window (last 1/3 frames = timer likely stopped)
    tailStart = len(results) * 2 // 3

    # Strategy 1: consecutive >=2 frames in tail window
    # Cross-check: if another value appears more often globally, prefer it
    allValid = [r for r in results if r is not None]
    globalCounter = Counter(allValid)

    for i in range(len(results) - 1, tailStart, -1):
        if results[i] is not None and results[i] == results[i - 1]:
            stableVal = results[i]
            stableCount = globalCounter[stableVal]
            # Check if any other value dominates globally
            if globalCounter:
                globalBest, globalBestCount = globalCounter.most_common(1)[0]
                if globalBest != stableVal and globalBestCount > stableCount + 1:
                    if debug:
                        print(f"  Override stability '{stableVal}'({stableCount}x)"
                              f" → global '{globalBest}'({globalBestCount}x)")
                    return globalBest
            if debug:
                print(f"  OK stability: f{i-1}~{i} = '{stableVal}'")
            return stableVal

    # Strategy 2: per-digit majority in tail window
    tailResults = [r for r in results[tailStart:] if r is not None]
    if len(tailResults) >= 2:
        # If all tail results have same digit count, do per-digit majority vote
        # This corrects single-digit template matching errors (e.g. 4.101 vs 4.201)
        lengths = set(len(r) for r in tailResults)
        if len(lengths) == 1:
            L = lengths.pop()
            merged = ""
            for pos in range(L):
                chars = [r[pos] for r in tailResults]
                merged += Counter(chars).most_common(1)[0][0]
            # Re-apply decimal formatting (assembleTime uses raw digits)
            if debug:
                print(f"  OK tail-merge: '{merged}' (from {len(tailResults)} frames)")
            return merged

        # Otherwise, simple majority
        tailCounter = Counter(tailResults)
        tailBest, tailCount = tailCounter.most_common(1)[0]
        if tailCount >= 2:
            if debug:
                print(f"  OK tail-vote: '{tailBest}' ({tailCount}x in last {len(results)-tailStart} frames)")
            return tailBest

    # Strategy 3: weighted vote across all frames (later = heavier)
    validResults = [(i, r) for i, r in enumerate(results) if r is not None]
    if not validResults:
        if debug:
            print(f"  FAIL: no frames recognized")
        return None

    total = len(results)
    scores: dict[str, float] = {}
    for i, r in validResults:
        w = 1.0 + i / total
        scores[r] = scores.get(r, 0) + w

    bestResult = max(scores, key=scores.get)  # type: ignore
    counter = Counter(r for _, r in validResults)

    if debug:
        print(f"  Fallback vote: '{bestResult}' ({counter[bestResult]}x, w={scores[bestResult]:.1f})")

    return bestResult


# ---------- CLI ----------


def main():
    parser = argparse.ArgumentParser(
        description="计时器成绩识别工具 (七段 LED OCR)",
    )
    parser.add_argument("path", help="视频文件或目录路径")
    parser.add_argument("--seconds", type=float, default=4.0, help="取最后多少秒 (默认: 4.0)")
    parser.add_argument("--frames", type=int, default=25, help="取帧数量 (默认: 25)")
    parser.add_argument("--rename", action="store_true", help="识别后重命名视频文件")
    parser.add_argument("--dry-run", action="store_true", help="仅预览, 不实际重命名")
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
        print(f"Path not found: {target}")
        sys.exit(1)

    if not videos:
        print(f"No video files found")
        sys.exit(1)

    print(f"OCR: {len(videos)} videos\n")

    def _ocrOne(v: Path) -> tuple[Path, str | None, float]:
        t0 = time.perf_counter()
        r = readTimer(v, seconds=args.seconds, frameCount=args.frames, debug=args.debug)
        gc.collect()
        return v, r, time.perf_counter() - t0

    renamed = 0
    failed = 0
    timings: list[float] = []
    totalStart = time.perf_counter()

    def _handleResult(video: Path, result: str | None, elapsed: float, showTime: bool = True) -> tuple[int, int]:
        """Handle one OCR result: print + rename. Returns (ok, fail) counts."""
        timings.append(elapsed)
        t = f"  ({elapsed:.1f}s)" if showTime else ""
        if result is None:
            print(f"  [FAIL] {video.name}: not recognized{t}")
            return 0, 1

        if args.rename or args.dry_run:
            newName = result + video.suffix
            if newName == video.name:
                print(f"  [SKIP] {video.name}: already named correctly{t}")
                return 1, 0

            newPath = video.parent / newName
            if newPath.exists():
                idx = 2
                while True:
                    newName = f"{result} ({idx}){video.suffix}"
                    newPath = video.parent / newName
                    if not newPath.exists():
                        break
                    idx += 1

            if args.dry_run:
                print(f"  [DRY ] {video.name} -> {newName}{t}")
            else:
                for attempt in range(5):
                    try:
                        video.rename(newPath)
                        print(f"  [ OK ] {video.name} -> {newName}{t}")
                        break
                    except PermissionError:
                        if attempt < 4:
                            time.sleep(1)
                        else:
                            print(f"  [ERR ] {video.name}: file locked, skipped")
                            return 0, 1
            return 1, 0
        else:
            print(f"  [ OK ] {video.name}: {result}{t}")
            return 1, 0

    if args.debug or len(videos) == 1:
        # 单线程: 显示每个视频耗时 (真实值)
        for v in videos:
            _, result, elapsed = _ocrOne(v)
            ok, fail = _handleResult(v, result, elapsed, showTime=True)
            renamed += ok
            failed += fail
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        # Calculate workers based on available memory
        # Each OCR worker: 25 frames × ~6MB + processing ≈ 200MB
        try:
            import psutil
            availMB = psutil.virtual_memory().available // (1024 * 1024)
            reserveMB = 2048  # keep 2GB for system
            perWorkerMB = 200
            memWorkers = max(1, (availMB - reserveMB) // perWorkerMB)
        except ImportError:
            memWorkers = 2  # safe fallback
        workers = min(memWorkers, os.cpu_count() or 4, len(videos))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_ocrOne, v): v for v in videos}
            for future in as_completed(futures):
                video, result, elapsed = future.result()
                # 并行模式: 不显示单视频耗时 (线程墙钟时间无意义)
                ok, fail = _handleResult(video, result, elapsed, showTime=False)
                renamed += ok
                failed += fail

    totalElapsed = time.perf_counter() - totalStart
    avgElapsed = totalElapsed / len(videos) if videos else 0
    print(f"\nDone: {renamed} ok, {failed} failed | 总耗时: {totalElapsed:.1f}s | 平均每视频: {avgElapsed:.1f}s")


if __name__ == "__main__":
    main()
