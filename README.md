# LeRobot v2.1 数据集可视化编辑器

基于 Web 的 LeRobot v2.1 格式数据集浏览、编辑与导出工具。支持轨迹可视化、帧级别精细编辑、智能删除平滑性分析，以及元数据自动重算。

## 功能概览

- **数据集浏览** — 加载本地 LeRobot v2.1 数据集，按 episode 浏览关节轨迹曲线与同步视频
- **关节可视化** — 基于 Chart.js 的交互式图表，支持缩放、平移，按关节分组（左臂/左手/右臂/右手）显示
- **帧级编辑** — 框选或逐帧选择，支持删除指定帧段或整个 episode
- **智能平滑性分析** — 删除帧时自动检测拼接处的加速度异常，推荐保留桥接帧以维持轨迹连续性
  - Douglas-Peucker 关键帧提取：基于轨迹形状自动选取最能保持曲线形态的帧
  - Butterworth 滤波匹配：生成理想平滑轨迹，匹配最接近的真实帧
- **另存为新数据集** — 编辑后导出为完整的 LeRobot v2.1 数据集，自动重编号 episode/frame 索引、裁剪视频、重算统计信息（mean/std/min/max）

## 适用机器人

默认关节配置为 **CR100 双臂灵巧手**（26 自由度），包含：

| 分组 | 关节数 | 说明 |
|------|--------|------|
| left_arm | 7 | 左臂 (shoulder × 3 + elbow + wrist × 3) |
| left_hand | 6 | 左灵巧手 (thumb × 2 + index/middle/ring/pinky) |
| right_arm | 7 | 右臂 |
| right_hand | 6 | 右灵巧手 |

如需适配其他机器人，修改 `app.py` 中的 `JOINT_GROUPS` 即可。

## 安装

```bash
# Python >= 3.8
pip install -r requirements.txt
```

依赖：

| 包 | 用途 |
|----|------|
| flask | Web 服务 |
| pandas + pyarrow | Parquet 数据读写 |
| numpy | 数值计算 |
| scipy | Butterworth 滤波（可选，缺少时自动回退到 Hermite 插值） |

视频裁剪依赖系统安装的 `ffmpeg`：

```bash
# Ubuntu / Debian
sudo apt install ffmpeg
```

## 启动

```bash
python app.py [--port 7860] [--host 0.0.0.0]
```

浏览器访问 `http://localhost:7860`。

## 使用流程

```
加载数据集 → 选择 Episode → 查看轨迹/视频 → 选帧编辑 → 保存
```

1. **加载** — 在顶部输入框填入 LeRobot v2.1 数据集的本地路径，点击「加载」
2. **浏览** — 左侧列表选择 episode，右侧图表显示 `observation.state` 各关节轨迹，点击关节分组按钮筛选显示
3. **选帧** — 在图表上框选或点击选择要删除的帧段（高亮显示）
4. **删除** — 点击「删除选中帧」，系统自动分析拼接处平滑性：
   - 若平滑 → 直接删除
   - 若检测到不连续 → 弹窗展示问题关节及加速度异常倍数，推荐保留的桥接帧（绿色标注），用户可选择「应用推荐」或「强制删除」
5. **保存** — 输入输出路径，点击「另存为」导出编辑后的完整数据集

## 项目结构

```
lerobot_visualize/
├── app.py              # Flask 后端 + DatasetEditor 核心逻辑
├── templates/
│   └── index.html      # 前端页面 (HTML + CSS)
├── static/
│   └── app.js          # 前端交互逻辑 (Chart.js + API 调用)
├── verify_stats.py     # 统计信息验证脚本 (对比 lerobot 官方 API)
├── requirements.txt
└── README.md
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/load` | 加载数据集 |
| GET | `/api/episodes` | 获取 episode 列表 |
| GET | `/api/episode/<idx>` | 获取单个 episode 数据 |
| GET | `/api/video` | 获取视频文件 |
| POST | `/api/delete_episodes` | 删除整个 episode |
| POST | `/api/delete_frames` | 删除指定帧 |
| POST | `/api/analyze_deletion` | 分析删除后的平滑性 |
| POST | `/api/save` | 另存为新数据集 |

## 验证工具

`verify_stats.py` 用于验证编辑后数据集的统计信息是否正确：

```bash
# 需要在安装了 lerobot 的环境中运行
python verify_stats.py <dataset_path>
```
