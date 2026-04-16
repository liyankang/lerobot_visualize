# ROS2 Bag → LeRobot v2.1 转换工具 — 设计文档

## 总体架构

```
┌─────────────────────────────────────────────────────────┐
│                    浏览器 (前端)                          │
│  ros2_convert.html + ros2_app.js                        │
│  ┌─────┐ ┌───────┐ ┌──────┐ ┌──────┐ ┌──────┐         │
│  │Step1│→│Step 2 │→│Step 3│→│Step 4│→│Step 5│         │
│  │扫描 │ │Topic  │ │映射  │ │对齐  │ │转换  │         │
│  └──┬──┘ └───┬───┘ └──┬───┘ └──┬───┘ └──┬───┘         │
└─────┼────────┼────────┼────────┼────────┼───────────────┘
      │ REST   │        │        │        │
      ▼ API    ▼        ▼        ▼        ▼
┌─────────────────────────────────────────────────────────┐
│                    app.py (路由层)                        │
│  /api/ros2/scan  /topics  /save_config  /align  /convert│
└────────────────────────┬────────────────────────────────┘
                         │ 调用
                         ▼
┌─────────────────────────────────────────────────────────┐
│               ros2_converter.py (核心逻辑)               │
│                                                         │
│  scan_bags()  discover_topics()  align_one_episode()    │
│  convert_episode()  write_metadata()  ProjectState      │
└─────────────────────────────────────────────────────────┘
      │                          │
      ▼                          ▼
  ┌────────┐              ┌─────────────┐
  │rosbags │              │ffmpeg + cv2 │
  │(读 bag)│              │(编码视频)    │
  └────────┘              └─────────────┘
```

---

## 五步工作流详细设计

### Step 1: 扫描 Bag 目录

```
用户输入目录路径
        │
        ▼
  ┌─────────────────┐
  │ os.walk 递归扫描 │
  └────────┬────────┘
           │
           ▼
  ┌──────────────────────────────┐
  │ 识别 bag 目录的依据:          │
  │  ① metadata.yaml 存在        │
  │  ② 包含 *.mcap 文件          │
  │  ③ 包含 *.db3 文件           │
  │  满足任一条件即为 bag 目录     │
  └────────┬─────────────────────┘
           │
           ▼
  返回: [{path, name, storage_format, size_mb}, ...]
           │
           ▼
  持久化 → {project_dir}/step1_scan.json
```

---

### Step 2: 版本识别 + Topic 发现

```
取第一个 bag
    │
    ├──→ 读取 metadata.yaml
    │        │
    │        ▼
    │    version 字段判断 ROS 版本:
    │      version >= 8  → Jazzy
    │      version >= 4  → Humble
    │      version <  4  → Older
    │
    └──→ rosbags.Reader 打开 bag
             │
             ▼
      遍历 reader.connections
             │
             ▼
      ┌──────────────────────────────────────┐
      │ 对每个 topic 提取:                    │
      │   topic 名、msg_type、msg_count      │
      │   frequency = msg_count / duration   │
      └────────────┬─────────────────────────┘
                   │
                   ▼
         ┌─────────────────────────────┐
         │ 按消息类型自动分类:           │
         │                             │
         │ CompressedImage ─→ camera   │
         │ Image          ─→ camera   │
         │ JointState     ─→ joint    │
         │ 其他           ─→ other    │
         └────────────┬────────────────┘
                      │
                      ▼
         ┌──────────────────────────────────┐
         │ 智能推荐:                         │
         │                                  │
         │ camera topic:                    │
         │   名称推荐 = observation.images.  │
         │             + 简称提取            │
         │                                  │
         │ joint topic:                     │
         │   含 "state"/"feedback"          │
         │     → 默认勾选 State             │
         │   含 "command"/"target"/"action" │
         │     → 默认勾选 Action            │
         │                                  │
         │ base topic 推荐:                  │
         │   = 频率最低的 camera topic       │
         └──────────────────────────────────┘
              │
              ▼
  持久化 → {project_dir}/step2_topics.json
```

---

### Step 3: 用户配置 Topic 映射

```
┌────────────────────────────────────────────────────────┐
│                    配置界面                              │
│                                                        │
│  ┌─ Camera Topics ───────────────────────────────────┐ │
│  │ [✓] /camera_high/...  → observation.images.cam_hi │ │
│  │ [✓] /camera_left/...  → observation.images.cam_le │ │
│  │ [ ] /camera_depth/... → (skip)                    │ │
│  └───────────────────────────────────────────────────┘ │
│                                                        │
│  ┌─ JointState Topics ──────────────────────────────┐  │
│  │ /cr100/left_arm_state     [✓ State] [  Action]   │  │
│  │ /cr100/right_arm_state    [✓ State] [  Action]   │  │
│  │ /cr100/dual_arm/command   [  State] [✓ Action]   │  │
│  │ /cr100/left_hand_state    [✓ State] [✓ Action]   │  │
│  │                                                   │  │
│  │  ※ 同一 topic 可同时勾选 State 和 Action          │  │
│  │  ※ 多个 topic 可同时映射到 State (拼接为长向量)    │  │
│  └───────────────────────────────────────────────────┘  │
│                                                        │
│  ┌─ 全局设置 ────────────────────────────────────────┐ │
│  │ Base topic:  [/camera_high/... ▼]  ← 仅 camera   │ │
│  │ FPS:         [30]                                 │ │
│  │ 容差 (sec):  [0.01]  ← 默认 10ms                 │ │
│  │ 输出目录:    [/path/to/output]                    │ │
│  └───────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────┘
                    │
                    ▼
        持久化 → {project_dir}/step3_config.json
```

---

### Step 4: 时间戳对齐

```
对每个 bag (= 一个 episode):

  ┌───────────────────────────────────┐
  │ rosbags.Reader 顺序读取全部消息    │
  │ 按 topic 分桶到 buffers           │
  └────────────────┬──────────────────┘
                   │
                   ▼
  ┌──────────────────────────────────────────────────────────┐
  │ 图像消息预处理 (存入 buffer 时):                          │
  │                                                          │
  │   CompressedImage                                        │
  │     │                                                    │
  │     ├─ 反序列化成功 → ("compressed", msg.data 字节)       │
  │     │                                                    │
  │     └─ 反序列化失败 → 搜索 CDR 中的 JPEG/PNG 头           │
  │                        ├─ 找到 → ("compressed", 图像字节) │
  │                        └─ 未找到 → 丢弃该消息             │
  │                                                          │
  │   Image (raw)                                            │
  │     │                                                    │
  │     └─ 反序列化成功 → ("raw", pixels, encoding, w, h)     │
  └──────────────────────────────────────────────────────────┘
                   │
                   ▼
  ┌──────────────────────────────────────────────────────────┐
  │ 以 base_topic 的每条消息时间戳为锚点                       │
  │                                                          │
  │  base_topic: ──●──────●──────●──────●──────●──           │
  │                |      |      |      |      |             │
  │  topic_A:   ─●───●──●──●──●──●───●──●──●──●──           │
  │               ↘   ↗    ↘  ↗   ↘   ↗                     │
  │  topic_B:   ────●───────●──────●───────●──────           │
  │                                                          │
  │  对每个锚点 t_base:                                       │
  │    对其余每个 topic, 二分查找 |t_topic - t_base| 最小的    │
  │                                                          │
  │    delta = |t_topic - t_base|                            │
  │    max_delta = max(delta) across all topics              │
  │                                                          │
  │    if max_delta > tolerance:                             │
  │      → 标记警告 (但不丢弃)                                │
  └──────────────────────────────────────────────────────────┘
                   │
                   ▼
  输出: aligned_frames = [
    {base_ts, data: {topic: value, ...}, max_delta_ns},
    ...
  ]
                   │
                   ▼
  持久化 → {project_dir}/step4_aligned/episode_000000.pkl
```

**对齐统计输出示例:**
```
Episode 0: 1200 帧, max_delta = 3.241 ms
Episode 1: 980 帧,  max_delta = 8.712 ms  ⚠ 超出容差
Episode 2: 1150 帧, max_delta = 2.103 ms
全局最大时间差: 8.712 ms
```

---

### Step 5: 转换为 LeRobot v2.1

```
对每个 episode 的 aligned data:

  ┌─────────────────────────────────────────────┐
  │              5a. 视频编码                     │
  │                                             │
  │  aligned frame                              │
  │       │                                     │
  │       ▼                                     │
  │  _decode_image_entry()                      │
  │       │                                     │
  │       ├─ ("compressed", bytes)              │
  │       │     → cv2.imdecode → RGB            │
  │       │                                     │
  │       └─ ("raw", pixels, enc, w, h)         │
  │             → reshape + cvtColor → RGB      │
  │       │                                     │
  │       ├─ 解码成功 → 加入 rgb_frames          │
  │       └─ 解码失败 → 用前一帧替代 + bad_count │
  │                                             │
  │  rgb_frames ─→ ffmpeg stdin pipe            │
  │                  -c:v libx264               │
  │                  -pix_fmt yuv420p           │
  │                  -crf 22                    │
  │                ─→ .mp4 文件                  │
  └─────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────┐
  │              5b. Parquet 生成                 │
  │                                             │
  │  JointState topics                          │
  │       │                                     │
  │       ▼                                     │
  │  提取 msg.position → float 数组              │
  │       │                                     │
  │       ▼                                     │
  │  ┌──────────────────────────────────────┐   │
  │  │ 拼接策略:                             │   │
  │  │                                      │   │
  │  │ State topics (多个):                  │   │
  │  │   topic_A.position ++ topic_B.pos... │   │
  │  │   → observation.state_0..N           │   │
  │  │                                      │   │
  │  │ Action topics (多个):                 │   │
  │  │   topic_C.position ++ topic_D.pos... │   │
  │  │   → action_0..M                     │   │
  │  └──────────────────────────────────────┘   │
  │       │                                     │
  │       ▼                                     │
  │  DataFrame → .parquet (per episode)         │
  └─────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────┐
  │           5c. 元数据 + 统计                   │
  │                                             │
  │  info.json                                  │
  │    codebase_version, fps, total_episodes,   │
  │    total_frames, features schema            │
  │                                             │
  │  episodes.jsonl                             │
  │    每行: {episode_index, length, task}       │
  │                                             │
  │  episodes_stats.jsonl                       │
  │    每行: {episode_index, stats: {           │
  │      observation.state: {min,max,mean,std}, │
  │      action: {min,max,mean,std}             │
  │    }}                                       │
  └─────────────────────────────────────────────┘
```

**输出目录结构:**
```
output_dir/
├── meta/
│   ├── info.json
│   ├── episodes.jsonl
│   └── episodes_stats.jsonl
├── data/
│   └── chunk-000/
│       ├── episode_000000.parquet
│       ├── episode_000001.parquet
│       └── ...
└── videos/
    └── chunk-000/
        ├── observation.images.cam_high_episode_000000.mp4
        ├── observation.images.cam_left_episode_000000.mp4
        └── ...
```

---

## 断点续做机制

```
{project_dir}/
├── step1_scan.json        ← 扫描结果
├── step2_topics.json      ← topic 发现结果
├── step3_config.json      ← 用户配置
├── step4_align/
│   ├── episode_000000.pkl ← 逐 episode 持久化
│   ├── episode_000001.pkl
│   └── ...
└── step5_convert.json     ← 转换完成标记

每次打开页面 → /api/ros2/resume 检查已完成的步骤
  → 自动跳到未完成的步骤继续
```

---

## 图像处理流水线

```
                    ROS2 Bag 中的原始数据
                           │
            ┌──────────────┴──────────────┐
            ▼                             ▼
    CompressedImage                    Image (raw)
    (JPEG/PNG 字节流)              (bgr8/rgb8/mono8 像素)
            │                             │
            ▼                             ▼
     反序列化 msg.data              反序列化 msg.data
            │                       + encoding/w/h
            │                             │
     ┌──────┴──────┐                      │
     ▼             ▼                      │
  成功:         失败:                      │
  直接取       搜索 CDR 中                  │
  msg.data    JPEG(FFD8FF)                │
              PNG(89504E47)               │
     │             │                      │
     │        ┌────┴────┐                 │
     │        ▼         ▼                 │
     │      找到      未找到               │
     │     取字节    丢弃消息              │
     │        │                           │
     ▼        ▼                           ▼
  ("compressed", bytes)    ("raw", bytes, enc, w, h)
            │                             │
            └──────────┬──────────────────┘
                       │
                       ▼  转换阶段
              _decode_image_entry()
                       │
            ┌──────────┴──────────┐
            ▼                     ▼
     cv2.imdecode            np.reshape
     (JPEG/PNG)           + cvtColor
            │              (按 encoding)
            │                     │
            └──────────┬──────────┘
                       │
                 ┌─────┴─────┐
                 ▼           ▼
              成功:        失败:
             RGB帧      前一帧替代
                         + 计数警告
                 │           │
                 └─────┬─────┘
                       ▼
                 ffmpeg pipe
                   → .mp4
```

---

## 依赖关系

```
ros2_converter.py  (核心, 无 ROS2 依赖)
    ├── rosbags        读取 mcap / db3
    ├── numpy          数值计算
    ├── opencv-python  图像解码
    ├── pandas         DataFrame → Parquet
    ├── pyarrow        Parquet 引擎
    ├── pyyaml         metadata.yaml 解析
    └── ffmpeg (系统)  视频编码

app.py  (路由层)
    └── flask          Web 服务

前端 (无额外依赖)
    ├── ros2_convert.html  页面结构 + 样式
    └── ros2_app.js        交互逻辑
```
