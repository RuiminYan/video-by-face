# OCR Pipeline Handoff Notes

## 项目简介

`D:\cube\video-by-face\` — 魔方比赛视频处理工具，核心功能是通过 OCR 读取七段 LED 大显成绩，自动重命名视频文件。

**关键文件：**
- `ocr_timer.py` — OCR 核心逻辑
- `classify.py` — 人脸识别分类
- `run.ps1` — 主入口（PowerShell）
- `test_ocr.py` — 回归测试脚本（直接运行即可）

**运行测试：**
```
cd D:\cube\video-by-face
python test_ocr.py
```

测试视频按结果分两组：
- `video/good/` — 当前已通过的视频（11个）
- `video/fail/` — 已知失败的视频（2个，待修复）

---

## 当前状态（2026-03-30）

**通过率：11/13（85%）**，其中 `video/fail/` 的 2 个是已知 bug。

### 已修复（本轮完成）
- **Speed Stacks 显示器支持**：`new/3.455`, `new/4.574`（Speed Stacks 大显段间距大，需特殊处理）
- **43.243 投票被劫持**：`43.243`（连续稳定性检测被假阳性连续2帧推翻，已加全局频率交叉验证）

### 已知 Bug（`video/fail/`）

#### 1. `3.698.MP4` — 4K 视频，大显占屏幕比例小

**现象：** 识别为 `6.121`（实际 `3.698`）

**根因：** 4K 分辨率下，大显 LED 段高度约 70px（相对画面高 2160px 只有 3%），每段在 HSV mask 里是独立 contour，宽高比 ar ≈ 0.25（<0.4），被分到 narrowSegs 组做 segment merge。但段间 x 间距约 80px，而 merge 阈值是 `avgH * 1.2 ≈ 84px`（刚好可以 merge 或有时不够）。合并后形成的数字 bbox 宽高比可能超出 `[0.15, 1.0]` 而被过滤，导致识别为噪声（号码牌红色 "3" → `6.121`）。

**已尝试但失败的方法：**

1. **等比缩放 morphological close kernel**（4K 用 (15,7)）→ 破坏 43.243（把相邻数字的段合并到一起），回退
2. **无条件 downscale 到 1080p**（`_recognizeFrame` 里先缩再检测）→ 43.243 识别率暴跌（10倍体积的大显被缩小后数字识别出错），改为仅在 Pass 1 失败时 fallback
3. **Pass 2 大 kernel (25,15)**：4K downscale 后用更大 kernel 合并段。对 Speed Stacks 有效，但 3.698 的 Pass 1 在4K下找到了 4 位噪声（号码牌），不会 fallback 到 Pass 2
4. **`pass1HasCandidates` 保护**：若 Pass 1 找到候选但 <4位，不走 Pass 2 → 3.455 被 None（其 Pass 1 也找到了噪声候选）

**当前架构（`detectTimerROI`）：**
```python
# Pass 1: 原始分辨率，标准 close kernel (7,3)
for hsvRanges in [TIGHT, MED, WIDE]:
    mask = _createLedMask(frame, hsvRanges)  # close (7,3)
    candidates = _findDigitCandidates(mask, imgH)  # narrow merge: ar<0.4 → merge
    rows = _clusterIntoRows(candidates)
    bestRow = _selectBestRow(rows)
    if bestRow >= 4: break
else:
    # Pass 2: 只在 Pass 1 完全无候选时触发（4K→1080p downscale + 大 kernel）
    ...
```

**建议方向：**
- 3.698 的 Pass 1 会把画面中的号码牌（红色 "3"）检测成 4 个一行，认为找到了大显。需要在 `_selectBestRow` 里加一个**高宽比一致性检测**（真正的大显一行内所有数字高度应该接近，号码牌的数字高度差异大）
- 或者：加一个**ROI 过滤**，大显必须在画面右半边+垂直中部（基于比赛拍摄规律）

#### 2. `4.098.MP4` — 4K 视频，大显右边紧贴裁判白色衣物

**现象：** 识别为 `6.222`

**根因：** 和 3.698 类似，4K 下段太小。Pass 2 downscale 到 1080p 后，大 close kernel 把大显数字段和旁边裁判白色衣物连在一起，变成巨大 contour，宽高比超出范围被过滤，只剩下噪声被检测到。

**建议方向：**
- 限制检测区域（大显通常在画面右侧 40%~90% 的 x 区间），过滤掉左边的 contour
- 在 Pass 2 中加 contour 不得覆盖超过 frame 宽度 20% 的过滤

---

## 关键代码结构

```
ocr_timer.py
├── _HSV_TIGHT / _HSV_MED / _HSV_WIDE  — HSV 颜色范围（纯红 LED）
├── _createLedMask(frame, hsvRanges)   — 颜色过滤 + morphological close (7,3)
├── _mergeSegments(narrowSegs)         — 合并 ar<0.4 的 LED 段（4K 支持）
├── _findDigitCandidates(mask, imgH)   — 找数字候选，调用 merge
├── _clusterIntoRows(candidates)       — 按 y 坐标聚类成行
├── _selectBestRow(rows)               — 选最佳行（最多数字 + 最右 + 合理间距）
├── detectTimerROI(frame)              — Pass 1 + Pass 2 双通道检测
├── recognizeDigit(digitMask, ...)     — 七段模板匹配识别单个数字
├── assembleTime(digits)               — 拼装 "X.XXX" or "X_XX.XXX"
├── _recognizeFrame(frame)             — 完整单帧识别（含 downscale fallback）
└── readTimer(videoPath)               — 多帧投票，返回最终成绩
    ├── Strategy 1: tail 连续≥2帧 + 全局频率交叉验证
    ├── Strategy 2: tail 窗口多数投票
    └── Strategy 3: 全局加权投票（越后面的帧权重越高）
```

## 提示词（给下一个 AI）

你好！接手一个魔方比赛视频 OCR 项目，目标是修复 `D:\cube\video-by-face\video\fail\` 中的 2 个视频识别失败问题。

**立即运行测试：**
```powershell
cd D:\cube\video-by-face
python test_ocr.py
```

**项目目标：** 从比赛录像中自动 OCR 读取七段 LED 大显上的成绩数字（如 `4.098`、`3.698`），用于自动重命名视频文件。

**当前问题：**
1. `video/fail/3.698.MP4` → 识别为 `6.121`（应为 `3.698`）
2. `video/fail/4.098.MP4` → 识别为 `6.222`（应为 `4.098`）

两者都是 4K 视频，大显在画面中占比小，LED 段在4K下是独立像素块，段间距大，难以被 morphological close 合并。Pass 1 在4K下会把画面中的号码牌/红色噪声误识别为数字行，Pass 2 （downscale+大kernel）因噪声干扰也失败。

**已尝试的失败方案详见 `handoff.md`**，不要重复相同思路。

**建议的调试工具：**
```python
# 查看某帧检测到了什么
import sys; sys.path.insert(0, r"D:\cube\video-by-face")
from ocr_timer import extractLastFrames, detectTimerROI
from pathlib import Path
frames = extractLastFrames(Path(r"D:\cube\video-by-face\video\fail\3.698.MP4"), 4.0, 25)
detection = detectTimerROI(frames[0])
# detection = (roi, roiMask, digitBboxes) or None
```

**注意：** `video/good/` 里的视频必须全部通过，不能引入回归！
