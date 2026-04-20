# LeRobot 数据集格式与版本转换说明

本文档整理 LeRobot 数据集的三个主流格式版本 —— **v2.0 / v2.1 / v3.0** —— 的目录结构、文件作用、关键字段含义，以及它们之间互转时需要提取、保留或重算的参数。

> 参考资料
> - 官方 v3.0 说明：<https://huggingface.co/docs/lerobot/main/en/lerobot-dataset-v3>
> - 官方 v2.1 引入：<https://github.com/huggingface/lerobot/pull/711>
> - 官方 v2.1 → v3.0 迁移脚本：`src/lerobot/scripts/convert_dataset_v21_to_v30.py`
> - 社区 v3.0 → v2.1 / v2.1 → v2.0 实现：NVIDIA Isaac-GR00T `scripts/lerobot_conversion/convert_v3_to_v2.py`、Tavish9/any4lerobot `ds_version_convert`

---

## 一、版本速览

| 维度 | v2.0 | v2.1 | v3.0 |
|------|------|------|------|
| 存储粒度 | 一 episode 一文件 | 一 episode 一文件 | 多 episode 合并到共享大文件 |
| `info.json` 里的 `codebase_version` | `"v2.0"` | `"v2.1"` | `"v3.0"` |
| parquet 路径模板 | `data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet` | 同 v2.0 | `data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet` |
| 视频路径模板 | `videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4` | 同 v2.0 | `videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4` |
| episode 元数据 | `meta/episodes.jsonl` | `meta/episodes.jsonl` | `meta/episodes/chunk-*/file-*.parquet` |
| 任务元数据 | `meta/tasks.jsonl` | `meta/tasks.jsonl` | `meta/tasks.jsonl` 或 `meta/tasks.parquet`（官方主线以 jsonl 为准） |
| 全局统计 | `meta/stats.json`（全部特征的全局聚合） | 无此文件 | `meta/stats.json`（由 per-episode 重新聚合） |
| 逐 episode 统计 | 无 | `meta/episodes_stats.jsonl` | 合并进 `meta/episodes/*.parquet` 的 `stats/<feature>/<metric>` 列 |
| info 中路径计算 | 根据 `chunks_size` 推导 | 同 v2.0 | 依赖 `data_path` / `video_path` 模板 + episode 元数据的 `data/chunk_index`、`data/file_index`、`videos/{key}/chunk_index`、`videos/{key}/file_index` |

**结论**：

- v2.0 ↔ v2.1 只是元数据形态切换（全局 stats vs. per-episode stats），实际 parquet / mp4 布局不变，互转非常轻量。
- v2.1 ↔ v3.0 是“每 episode 一文件”与“多 episode 共享大文件”两种布局之间的互转，涉及 parquet 合并/切分、mp4 合并/切分。

---

## 二、目录结构详解

### 2.1 v2.0 / v2.1

```
<dataset_root>/
├── meta/
│   ├── info.json               # 整体元数据（必需）
│   ├── episodes.jsonl          # 每行一 episode（必需）
│   ├── tasks.jsonl             # 每行一个任务（必需）
│   ├── stats.json              # ⚠️ 仅 v2.0 才有（全局 stats）
│   └── episodes_stats.jsonl    # ⚠️ 仅 v2.1 才有（逐 episode stats）
├── data/
│   └── chunk-000/
│       ├── episode_000000.parquet
│       ├── episode_000001.parquet
│       └── ...
├── videos/                     # 可选（若无视频特征可缺省）
│   └── chunk-000/
│       └── observation.images.<camera>/
│           ├── episode_000000.mp4
│           └── episode_000001.mp4
└── images/                     # 可选
```

### 2.2 v3.0

```
<dataset_root>/
├── meta/
│   ├── info.json
│   ├── stats.json              # 必需（全局聚合统计）
│   ├── tasks.jsonl             # 任务定义（官方主线）
│   │   └── 部分工具链额外生成 meta/tasks.parquet，视为兼容格式
│   └── episodes/
│       └── chunk-000/
│           └── file-000.parquet
│           # 列：episode_index | length | tasks | task_index |
│           #     data/chunk_index | data/file_index |
│           #     dataset_from_index | dataset_to_index |
│           #     videos/<key>/chunk_index | videos/<key>/file_index |
│           #     videos/<key>/from_timestamp | videos/<key>/to_timestamp |
│           #     meta/episodes/chunk_index | meta/episodes/file_index |
│           #     stats/<feature>/min | stats/<feature>/max |
│           #     stats/<feature>/mean | stats/<feature>/std |
│           #     stats/<feature>/count
├── data/
│   └── chunk-000/
│       ├── file-000.parquet    # 多 episode 合并写入同一 parquet
│       └── file-001.parquet
└── videos/
    └── observation.images.<camera>/
        └── chunk-000/
            └── file-000.mp4    # 多 episode 视频 concat 到一起
```

---

## 三、核心文件字段说明

### 3.1 `meta/info.json`

所有版本共有（v2/v3）：

| 字段 | 类型 | 含义 | 版本差异 |
|------|------|------|----------|
| `codebase_version` | str | 格式版本标识 | `v2.0` / `v2.1` / `v3.0` |
| `robot_type` | str | 机器人类型（可空） | 三版通用 |
| `total_episodes` | int | episode 总数 | 三版通用 |
| `total_frames` | int | 帧总数 | 三版通用 |
| `total_tasks` | int | 任务总数 | 三版通用 |
| `total_chunks` | int | chunk 总数（v2）；v3.0 删除 | v2 独有 |
| `total_videos` | int | 视频文件总数（v2，= `total_episodes * camera 数`）；v3.0 删除 | v2 独有 |
| `chunks_size` | int | 每个 chunk 目录下最多多少个 episode 文件 | 三版通用（v3.0 中表示 chunk 目录里最多多少个 `file-*.parquet`） |
| `fps` | int/float | 采样频率 | 三版通用（v3.0 中非视频 feature 会再写一次 `fps` 字段） |
| `splits` | dict | train/val 划分 | 三版通用 |
| `data_path` | str | parquet 路径模板 | 参见上表 |
| `video_path` | str/null | 视频路径模板（无视频 = null） | 参见上表 |
| `features` | dict | 每个特征的 dtype / shape / names 等 | 三版通用 |
| `data_files_size_in_mb` | int | 单个合并 parquet 目标大小 | **仅 v3.0** |
| `video_files_size_in_mb` | int | 单个合并 mp4 目标大小 | **仅 v3.0** |

`features[*]` 结构示例：

```jsonc
{
  "observation.state": {
    "dtype": "float32",
    "shape": [26],
    "names": ["left_shoulder_pitch", "..."]
  },
  "observation.images.front": {
    "dtype": "video",
    "shape": [480, 640, 3],
    "names": ["height", "width", "channel"],
    "info": {                      // v3.0 也可写在这里
      "video.fps": 30.0,
      "video.codec": "h264",
      "video.pix_fmt": "yuv420p",
      "video.is_depth_map": false,
      "has_audio": false
    }
  }
}
```

### 3.2 `meta/tasks.jsonl`（v2.x / v3.0 官方主线）

```jsonl
{"task_index": 0, "task": "Pick up the red block"}
{"task_index": 1, "task": "Place it in the green bowl"}
```

> v3.0 某些实现额外生成 `meta/tasks.parquet`，列为 `task_index | task`；转换时两者等价。

### 3.3 `meta/episodes.jsonl`（v2.x）

每行一个 episode，典型字段：

```jsonl
{"episode_index": 0, "tasks": ["Pick up the red block"], "length": 420}
{"episode_index": 1, "tasks": ["Place it in the green bowl"], "length": 358}
```

> `tasks` 是任务字符串列表；长度由 `length` 给出；路径通过 `episode_index // chunks_size` 推导。

### 3.4 `meta/episodes_stats.jsonl`（v2.1 独有）

```jsonl
{"episode_index": 0,
 "stats": {
   "observation.state": {
     "min":   [[...26 个...]],
     "max":   [[...]],
     "mean":  [[...]],
     "std":   [[...]],
     "count": [420]
   },
   "observation.images.front": {
     "min":   [[[0.02]], [[0.01]], [[0.03]]],   // shape (3,1,1) 的 per-channel 统计
     "max":   [[[0.99]], [[1.00]], [[0.98]]],
     "mean":  [[[0.42]], [[0.51]], [[0.37]]],
     "std":   [[[0.22]], [[0.19]], [[0.24]]],
     "count": [72]                              // 视频采样帧数
   }
 }
}
```

### 3.5 `meta/stats.json`（v2.0 与 v3.0 都有，但**来源不同**）

结构与 `episodes_stats` 中单条 `stats` 子字典一致，只是把所有 episode 聚合成一份全局统计：

```json
{
  "observation.state": {
    "min":  [...],
    "max":  [...],
    "mean": [...],
    "std":  [...],
    "count": [12345]
  },
  "observation.images.front": { ... }
}
```

聚合公式（`aggregate_feature_stats`，官方算法）：

```
min'    = min over episodes
max'    = max over episodes
count'  = Σ count
mean'   = Σ (mean * count) / count'
var'    = Σ (var + (mean - mean')²) * count / count'
std'    = sqrt(var')
```

### 3.6 v3.0 的 `meta/episodes/*.parquet`

每行一个 episode，列命名采用 `flatten_dict` 风格（用 `/` 分隔嵌套键）：

| 列 | 类型 | 说明 |
|----|------|------|
| `episode_index` | int64 | episode 序号 |
| `length` | int64 | 帧数（从 v2.1 的 `episodes.jsonl` 带入） |
| `tasks` | list[str] | 任务字符串列表 |
| `task_index` | int64（可选） | 主任务索引 |
| `dataset_from_index` | int64 | 该 episode 在合并 parquet 中的起始全局行 |
| `dataset_to_index` | int64 | 结束全局行（不含） |
| `data/chunk_index` | int64 | 合并 parquet 所在 chunk 目录号 |
| `data/file_index` | int64 | 合并 parquet 的 file 号 |
| `videos/<key>/chunk_index` | int64 | 视频 chunk 号（每个 camera 一组） |
| `videos/<key>/file_index` | int64 | 视频 file 号 |
| `videos/<key>/from_timestamp` | float64 | 该 episode 在合并 mp4 中的起始秒 |
| `videos/<key>/to_timestamp` | float64 | 结束秒（不含） |
| `meta/episodes/chunk_index` | int64 | 本记录所在 episodes/*.parquet 的 chunk 号 |
| `meta/episodes/file_index` | int64 | 本记录所在 episodes/*.parquet 的 file 号 |
| `stats/<feature>/min` | list/ndarray | flatten 后的逐 episode 统计 |
| `stats/<feature>/max` | ... | ... |
| `stats/<feature>/mean` / `std` / `count` | ... | ... |

---

## 四、转换过程中的关键参数

### 4.1 v2.1 → v3.0（合并）

**需要保留/计算：**

| 参数 | 来源 | 用途 |
|------|------|------|
| `features` / `fps` / `robot_type` / `splits` / `chunks_size` | 来源 `info.json` | 原样写入目标 |
| `data_files_size_in_mb` | 用户配置（默认 100） | 控制合并 parquet 的切分阈值 |
| `video_files_size_in_mb` | 用户配置（默认 500） | 控制合并 mp4 的切分阈值 |
| 每 episode 的 `episode_index`, `length`, `tasks`, `task_index` | `episodes.jsonl` | 写入 `meta/episodes/*.parquet` |
| 每 episode 的 per-episode stats | `episodes_stats.jsonl` | 作为 `stats/<feature>/<metric>` 列写入；并用 `aggregate_feature_stats` 聚合成 `meta/stats.json` |
| 每 episode 的 parquet 大小、帧数 | 扫描 `data/chunk-*/episode_*.parquet` | 贪心累积到当前合并 parquet；写入 `dataset_from_index`、`dataset_to_index`、`data/chunk_index`、`data/file_index` |
| 每 episode 每个 camera 的视频时长、文件大小 | 扫描 `videos/...` + ffprobe | 写入 `videos/<key>/from_timestamp`、`to_timestamp`、`chunk_index`、`file_index` |
| 每个 camera 的视频 codec / fps / pix_fmt | ffprobe | 合并时保持一致，避免 concat 失败 |

**切分规则**（贪心）：

```
if size_in_mb + ep_size_in_mb >= file_size_limit and 已累积 >= 1:
    flush 当前合并文件
    chunk_idx, file_idx = update_chunk_file_indices(...)
    重置累积
追加当前 episode
```

`update_chunk_file_indices`：`file_idx += 1`，当 `file_idx >= chunks_size` 时进位到 `chunk_idx += 1`。

### 4.2 v3.0 → v2.1（拆分）

**需要保留/计算：**

| 参数 | 来源 | 用途 |
|------|------|------|
| `meta/episodes/*.parquet` 的全量行 | 加载并按 `episode_index` 排序 | 驱动所有拆分 |
| 每 episode 的 `dataset_from_index`、`dataset_to_index` | episodes parquet | 从合并 parquet 切片出每个 episode |
| 每 episode 的 `videos/<key>/from_timestamp`、`to_timestamp` | episodes parquet | 用 `ffmpeg -ss -t -c copy` 切出每段 mp4 |
| 每 episode 的 `stats/<feature>/*` | episodes parquet | 反 flatten 成嵌套字典写入 `episodes_stats.jsonl` |
| `meta/tasks.parquet` 或 `tasks.jsonl` | v3.0 任务元数据 | 转成 v2.1 的 `tasks.jsonl` |
| `chunks_size` | info.json | 计算 `episode_index // chunks_size` 作为 `episode_chunk` |
| `features`、`fps`、`robot_type` | info.json | 写入 v2.1 info.json |
| `total_chunks`、`total_videos` | 根据 `total_episodes` 与 camera 数量重算 | v2.1 独有字段 |

### 4.3 v2.1 ↔ v2.0（只改元数据）

- **v2.1 → v2.0**：
  - 读 `meta/episodes_stats.jsonl` → `aggregate_feature_stats` → 写 `meta/stats.json`
  - 删除 `meta/episodes_stats.jsonl`
  - `info.json.codebase_version = "v2.0"`
  - 其他 parquet / mp4 / episodes.jsonl / tasks.jsonl 不变

- **v2.0 → v2.1**（本工具**未实现**，仅作参考）：需要读取每一 episode 的 parquet + 采样视频帧，重新计算 per-episode stats，写 `meta/episodes_stats.jsonl`，删除 `meta/stats.json`。

---

## 五、可能的数据一致性检查

- `total_episodes`、`total_frames`、`total_tasks` 与实际元数据一致
- 每个 `episode_*.parquet` 的 `episode_index` 列与文件名一致
- v3.0 中 `dataset_to_index - dataset_from_index == episode.length`
- v3.0 中所有 camera 的 episode 数相同
- v2.1 episodes_stats / v3.0 stats 列与 `features` 对齐（非 string 类型的特征都应有 stats）

本工具在转换结束时会自动在“对比视图”中暴露目录树、关键文件（info.json / episodes*.jsonl / episodes/*.parquet schema 等）的左右对照，便于人工复核。

---

## 六、在本工具中的对应实现

| 功能 | 入口 |
|------|------|
| 门户入口 | `/` 选择 “LeRobot 版本转换” 卡片 |
| 转换主页 | `/converter` |
| 扫描/识别版本 | `POST /api/convert/inspect` |
| 启动转换 | `POST /api/convert/start`（异步任务 + 轮询进度） |
| 查询进度 | `GET /api/convert/progress` |
| 取消任务 | `POST /api/convert/cancel` |
| 左右对比目录树 | `POST /api/convert/tree` |
| 读取元数据预览 | `GET /api/convert/file_preview` |

实现代码位于 `lerobot_converter.py`。
