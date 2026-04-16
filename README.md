# LeRobot 工具箱

基于 Web 的 LeRobot v2.1 数据集工具集合，包含可视化编辑器、VLA 数据分析页和 ROS2 Bag 转换工具。

启动后访问 `http://localhost:7860` 进入工具箱门户页，选择所需工具。

## 工具列表

| 工具 | 路径 | 说明 |
|------|------|------|
| 可视化编辑器 | `/visualize` | 浏览、编辑 LeRobot v2.1 数据集 |
| LeRobot 数据分析 | `/data-analysis` | 按 joint group 分析每个对应关节的 state/action 统计、Sigma 区间和分布 |
| ROS2 Bag 转换 | `/ros2-convert` | 将 ROS2 bag 转换为 LeRobot v2.1 格式 |

---

## 工具一: 可视化编辑器

LeRobot v2.1 格式数据集浏览、编辑与导出工具。支持轨迹可视化、URDF 机器人 3D 回放、帧级别精细编辑、智能删除平滑性分析，以及元数据自动重算。

### 功能概览

- **数据集浏览** — 加载本地 LeRobot v2.1 数据集，按 episode 浏览关节轨迹曲线与同步视频
- **关节可视化** — 基于 Chart.js 的交互式图表，支持缩放、平移，按关节分组（左臂/左手/右臂/右手）显示 State 与 Action 曲线
- **URDF 3D 预览与动作回放** — 上传 URDF 及 mesh 资源后，页面内嵌 Three.js 3D 预览，跟随播放时间轴实时驱动机器人关节姿态
  - 自动过滤 URDF 中的 fixed 关节，仅映射可动关节
  - 模糊匹配数据集关节名与 URDF 关节名，按变体长度加权评分避免短名误配
  - 弹窗式 **关节映射复核面板**：自动匹配后逐项展示结果，用户可直接在下拉菜单中调整错误映射，确认后生效
  - 自动检测角度/弧度单位（旋转关节最大值 > 2π 时自动 deg→rad 转换），也可手动切换
  - 支持 State / Action 数据源切换
- **帧级编辑** — 框选或逐帧选择，支持删除指定帧段或整个 episode
- **智能平滑性分析** — 删除帧时自动检测拼接处的加速度异常，推荐保留桥接帧以维持轨迹连续性
  - Douglas-Peucker 关键帧提取：基于轨迹形状自动选取最能保持曲线形态的帧
  - Butterworth 滤波匹配：生成理想平滑轨迹，匹配最接近的真实帧
- **另存为新数据集** — 编辑后导出为完整的 LeRobot v2.1 数据集，自动重编号 episode/frame 索引、裁剪视频、重算统计信息（mean/std/min/max/quantiles）

### 关节配置

系统按以下优先级自动识别关节名称与分组，**无需手动修改代码**：

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1 | CLI `--joint-config` | 命令行指定的 JSON 配置文件 |
| 2 | `<数据集>/joint_config.json` | 数据集根目录下的配置文件 |
| 3 | `<数据集>/meta/joint_config.json` | 数据集 meta 目录下的配置文件 |
| 4 | `info.json` features.names | LeRobot v2.1 元数据中的关节名称字段 |
| 5 | 维度匹配默认值 | 状态维度恰好 = 26 时使用内置 CR100 配置 |
| 6 | 通用编号 | `joint_0`, `joint_1`, ... |

#### 自动分组规则

当有关节名称但没有分组信息时，系统按命名中的关键词自动归类：

- **side**: `left` / `right`
- **arm**: shoulder、elbow、wrist
- **hand**: thumb、index、middle、ring、pinky、finger、grip、prox、meta、distal
- **head**: head、neck、jaw
- **torso**: torso、spine、waist、hip、pelvis
- **leg**: knee、ankle、foot、toe、thigh

组合为 `left_arm`、`right_hand` 等分组键，无法识别的归入 `other`。

#### 配置文件格式

`joint_config.json` 支持三种写法：

**完整写法**（推荐）— 同时指定名称列表和分组：

```json
{
  "joint_names": [
    "left_shoulder_pitch", "left_shoulder_roll", "left_shoulder_yaw",
    "left_elbow", "left_wrist_roll", "left_wrist_pitch", "left_wrist_yaw",
    "left_thumb_prox", "left_thumb_meta", "left_index_prox",
    "left_middle_prox", "left_ring_prox", "left_pinky_prox",
    "right_shoulder_pitch", "right_shoulder_roll", "right_shoulder_yaw",
    "right_elbow", "right_wrist_roll", "right_wrist_pitch", "right_wrist_yaw",
    "right_thumb_prox", "right_thumb_meta", "right_index_prox",
    "right_middle_prox", "right_ring_prox", "right_pinky_prox"
  ],
  "joint_groups": {
    "left_arm": ["left_shoulder_pitch", "left_shoulder_roll", "left_shoulder_yaw",
                  "left_elbow", "left_wrist_roll", "left_wrist_pitch", "left_wrist_yaw"],
    "left_hand": ["left_thumb_prox", "left_thumb_meta", "left_index_prox",
                   "left_middle_prox", "left_ring_prox", "left_pinky_prox"],
    "right_arm": ["right_shoulder_pitch", "right_shoulder_roll", "right_shoulder_yaw",
                   "right_elbow", "right_wrist_roll", "right_wrist_pitch", "right_wrist_yaw"],
    "right_hand": ["right_thumb_prox", "right_thumb_meta", "right_index_prox",
                    "right_middle_prox", "right_ring_prox", "right_pinky_prox"]
  }
}
```

**仅分组** — 关节名称从分组中推导：

```json
{
  "joint_groups": {
    "arm": ["shoulder", "elbow", "wrist"],
    "gripper": ["finger_left", "finger_right"]
  }
}
```

**仅名称列表** — 分组按命名规则自动生成：

```json
{
  "joint_names": ["shoulder", "elbow", "wrist", "finger_left", "finger_right"]
}
```

#### 默认内置配置

当状态维度恰好为 26 且未找到其他配置时，使用内置 **CR100 双臂灵巧手** 配置：

| 分组 | 关节数 | 说明 |
|------|--------|------|
| left_arm | 7 | 左臂 (shoulder × 3 + elbow + wrist × 3) |
| left_hand | 6 | 左灵巧手 (thumb × 2 + index/middle/ring/pinky) |
| right_arm | 7 | 右臂 |
| right_hand | 6 | 右灵巧手 |

## 安装

```bash
# Python >= 3.10
pip install -r requirements.txt
```

依赖：

| 包 | 用途 |
|----|------|
| flask | Web 服务 |
| pandas + pyarrow | Parquet 数据读写 |
| numpy | 数值计算 |
| scipy | Butterworth 滤波（可选，缺少时自动回退到 Hermite 插值） |
| rosbags | ROS2 bag 读取（纯 Python，不依赖 ROS2 环境） |
| opencv-python | 图像解码（ROS2 转换工具使用） |
| pyyaml | YAML 解析（ROS2 metadata.yaml） |

视频处理依赖系统安装的 `ffmpeg`：

```bash
# Ubuntu / Debian
sudo apt install ffmpeg
```

## 启动

```bash
python app.py [--port 7860] [--host 0.0.0.0] [--joint-config path/to/joint_config.json]
```

浏览器访问 `http://localhost:7860`。

### 使用流程

```
加载数据集 → 选择 Episode → 上传 URDF → 复核关节映射 → 查看轨迹/视频/3D 回放 → 选帧编辑 → 保存
```

1. **加载** — 在顶部输入框填入 LeRobot v2.1 数据集的本地路径，点击「加载」
2. **浏览** — 左侧列表选择 episode，右侧图表显示 `observation.state` / `action` 各关节轨迹，点击关节分组按钮筛选显示
3. **加载 URDF** — 点击顶部「上传 URDF」或「上传目录」，建议优先上传包含 `.urdf + meshes/贴图` 的完整目录；支持 `stl / dae / obj(+mtl) / gltf / glb` 格式
4. **复核映射** — URDF 加载后自动弹出关节映射配置面板，展示数据集关节与 URDF 关节的自动匹配结果及置信度（精确/近似/低置信）。有误的行直接在下拉菜单中修改，确认后 3D 预览开始联动
5. **调整参数** — URDF 面板提供：数据源切换（State / Action）、角度单位（自动检测 / 角度制 / 弧度制）、随时可点击「编辑映射」重新调整
6. **选帧** — 在图表上点击两次选择帧段（高亮显示），或手动输入帧范围
7. **删除** — 点击「删除选中帧」，系统自动分析拼接处平滑性：
   - 若平滑 → 直接删除
   - 若检测到不连续 → 弹窗展示问题关节及加速度异常倍数，推荐保留的桥接帧（绿色标注），用户可选择「应用推荐」或「强制删除」
8. **保存** — 输入输出路径，点击「另存为」导出编辑后的完整数据集（含视频裁剪和统计重算，进度实时显示）

### URDF 3D 回放说明

#### 关节映射

系统采用模糊匹配算法自动建立数据集关节与 URDF 关节之间的映射：

- **精确匹配** — 关节名去除 `_joint` 等后缀后完全一致（如 `left_elbow` ↔ `left_elbow_joint`）
- **近似匹配** — 变体名存在交集，按最长匹配变体长度加权评分，避免 `pitch` 这类短通用词导致误配
- **按序回退** — 无名称匹配时按索引顺序对齐

所有自动匹配结果在弹窗中展示，用户确认或手动修正后才会生效。后续可通过 URDF 面板的「编辑映射」按钮随时重新调整。

#### 角度单位自动检测

- 自动模式：检测旋转关节（revolute/continuous）数据值最大绝对值是否超过 2π（≈6.28），超过则判定为角度制并自动执行 `degree → radian` 转换
- 仅对旋转关节转换，prismatic（直线）关节保持原值
- 可通过 URDF 面板手动切换为「角度制」或「弧度制」覆盖自动检测

#### 支持的 Mesh 格式

| 格式 | 说明 |
|------|------|
| `.stl` | 常见 URDF mesh 格式，自动计算法线 |
| `.dae` (COLLADA) | 含材质和纹理 |
| `.obj` (+`.mtl`) | 自动尝试加载同名 .mtl 材质 |
| `.gltf` / `.glb` | glTF 2.0 格式 |

#### 调试技巧

浏览器 DevTools Console（F12）中可查看 `[URDF]` 前缀的诊断日志：

- 关节映射诊断表（`console.table`）
- fixed 关节过滤数量
- 角度单位自动检测结果
- 未映射的数据集/URDF 关节列表

## 项目结构

```
lerobot_visualize/
├── app.py                  # Flask 后端 + 可视化编辑器 + URDF 解析 + ROS2 API 路由
├── ros2_converter.py       # ROS2 Bag 转换核心: 扫描/解析/对齐/转换
├── templates/
│   ├── portal.html         # 工具箱门户首页
│   ├── index.html          # 可视化编辑器页面
│   └── ros2_convert.html   # ROS2 Bag 转换页面
├── static/
│   ├── app.js              # 可视化编辑器前端 (Three.js + urdf-loader + Chart.js)
│   └── ros2_app.js         # ROS2 转换前端交互
├── verify_stats.py         # 统计信息验证脚本
├── requirements.txt
└── README.md
```

## API 接口

### 可视化编辑器

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/load` | 加载数据集 |
| GET | `/api/episodes` | 获取 episode 列表 |
| GET | `/api/episode/<idx>` | 获取单个 episode 数据 |
| GET | `/api/video` | 获取视频文件 |
| POST | `/api/urdf/upload` | 上传 URDF 与关联资源，返回关节信息和映射所需的 joint_info |
| GET | `/api/urdf_asset/<package>/<path>` | 获取 URDF 引用的 mesh/贴图资源 |
| POST | `/api/delete_episodes` | 删除整个 episode |
| POST | `/api/delete_frames` | 删除指定帧 |
| POST | `/api/analyze_deletion` | 分析删除后的平滑性 |
| GET | `/api/save_progress` | 查询保存进度 |
| POST | `/api/save` | 另存为新数据集 |

### ROS2 Bag 转换

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/ros2/scan` | 扫描目录中的 bag 文件 |
| POST | `/api/ros2/topics` | 从 bag 中发现 topic |
| POST | `/api/ros2/save_config` | 保存用户映射配置 |
| POST | `/api/ros2/align` | 执行时间戳对齐（异步） |
| POST | `/api/ros2/convert` | 转换为 LeRobot v2.1（异步） |
| GET | `/api/ros2/progress` | 轮询长任务进度 |
| POST | `/api/ros2/resume` | 检查已完成步骤（断点续做） |

### 通用

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/browse` | 浏览服务端目录结构（跨平台） |

---

## 工具二: ROS2 Bag 转换

将 ROS2 bag 数据转换为 LeRobot v2.1 格式数据集。纯 Python 实现（`rosbags` 库），无需安装 ROS2 环境。

### 特性

- **自动 Bag 发现** — 递归扫描目录，支持 MCAP 和 DB3 格式
- **ROS 版本识别** — 从 metadata.yaml 自动判断 Humble / Jazzy
- **Topic 自动分类** — 根据消息类型智能分类（相机 / 关节状态 / 其他），自动推荐 LeRobot 特征名
- **灵活 Topic 映射** — JointState topic 可任意勾选映射到 State 和/或 Action（支持同时勾选）
- **时间戳对齐** — 以用户选择的 base topic 为锚点，二分查找最近时间戳，报告每帧 max delta
- **自包含转换** — 不依赖 lerobot 包，自行生成 parquet、视频（ffmpeg H.264）、元数据
- **坏帧防护** — 图像解码失败时自动用前一帧替代并记录警告
- **断点续做** — 每步结果持久化，中断后可自动恢复进度

### 使用流程

访问 `/ros2-convert`，按 5 步向导操作：

1. **扫描** — 选择 bag 目录，发现所有 bag 文件
2. **Topic 发现** — 查看所有 topic 的类型、频率、自动分类
3. **映射配置** — 设置 camera 命名、JointState 的 State/Action 角色、base topic、FPS、容差
4. **时间戳对齐** — 执行对齐，查看每个 episode 的 max delta 统计
5. **转换** — 生成 LeRobot v2.1 数据集，完成后可直接在可视化编辑器中打开验证

---

## 快捷键（可视化编辑器）

| 快捷键 | 功能 |
|--------|------|
| `Space` | 播放 / 暂停 |
| `←` / `→` | 逐帧前进 / 后退 |
| `Shift+←` / `Shift+→` | 跳 10 帧 |
| `Home` / `End` | 跳到首帧 / 末帧 |

## 验证工具

`verify_stats.py` 用于验证编辑后数据集的统计信息是否正确：

```bash
# 需要在安装了 lerobot 的环境中运行
python verify_stats.py <dataset_path>
```
