# 视频按人脸分类工具

将比赛视频按选手人脸自动分类到对应文件夹。

## 快速开始

### 1. 安装依赖

```powershell
pip install -r requirements.txt
```

### 2. 准备参考照片

在 `references/` 目录下，为每位选手创建一个子文件夹，放入 1~3 张**正脸照片**：

```
references/
├── 张三/
│   ├── photo1.jpg
│   └── photo2.jpg
├── 李四/
│   └── photo1.jpg
└── ...
```

> 照片要求：正脸、光线好、无遮挡。1 张就够，2~3 张可提高精度。

### 3. 运行

```powershell
# 先预览，不实际移动文件
python classify.py "Z:\魔方比赛\260307" --dry-run

# 确认无误后，正式移动
python classify.py "Z:\魔方比赛\260307"

# 如果想复制而非移动
python classify.py "Z:\魔方比赛\260307" --copy
```

### 4. 结果

分类后的目录结构：

```
D:\cube\video-by-face\
├── 张三/
│   ├── video001.MP4
│   └── video015.MP4
├── 李四/
│   └── video003.MOV
├── unknown/          ← 未成功匹配的视频
│   └── video042.MP4
└── ...
```

## 选项

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--dry-run` | 仅预览结果，不移动文件 | 否 |
| `--copy` | 复制而非移动 | 移动 |
| `--threshold` | 匹配阈值 (0~1, 越高越严格) | 0.60 |
| `--model` | 识别模型 | ArcFace |
| `--ref-dir` | 参考照片目录 | `./references/` |

## 故障排除

- **很多视频进了 unknown？** → 降低阈值: `--threshold 0.50`
- **张冠李戴？** → 提高阈值: `--threshold 0.70`，或增加参考照片数量
- **某些视频报错？** → 可能是视频损坏或全黑帧，手动处理即可
