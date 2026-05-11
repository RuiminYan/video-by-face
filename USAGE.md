# 抓比赛成绩

## 跑

```powershell
cubing-fetch <slug>
```

`<slug>` = `https://cubing.com/live/<slug>` 那段，如 `Chengdu-Welcoming-Summer-2026`。

输出到 `Z:\魔方比赛\<YYMMDD> <比赛英文名>\<选手>\<项目 轮次 成绩 avg>\attempts.txt`。

## 加 / 删选手

`D:\cube\video-by-face\person\<拼音首字母><中文名>\`，里面放参考人脸图 `1.png` `2.png` …

可选 `event.txt`，一行一个项目码（`333` `222` `444` `555` `333oh` `pyram` `skewb` `minx` `sq1` `clock` `333bf` …）。没这文件 = 全项目都抓。

## 常用参数

```
--dry-run               预览不写盘
--events 333,222        只抓某些项目
--all-people            person/ 之外的选手也建文件夹
--ignore-event-filter   忽略所有 event.txt
--out "X:\xxx"          换输出根目录
```

## 重跑

attempts.txt 会覆盖；**旧的 round 文件夹不会自动删**。要彻底重建：先手动删 `Z:\魔方比赛\<YYMMDD> ...\` 再跑。
