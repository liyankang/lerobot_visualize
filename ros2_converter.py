from __future__ import annotations
"""
ROS2 Bag 扫描、解析、对齐、转换核心模块。

使用 rosbags（纯 Python）读取 bag，不依赖 ROS2 环境。
"""

import json
import logging
import os
import pickle
import re
import struct
import subprocess
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import yaml

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# rosbags 惰性导入（安装检查推迟到实际使用时）
# ---------------------------------------------------------------------------
_rosbags_available = None
_cv2 = None


def _ensure_rosbags():
    global _rosbags_available
    if _rosbags_available is True:
        return
    try:
        from rosbags.rosbag2 import Reader          # noqa: F401
        from rosbags.typesys import Stores, get_typestore  # noqa: F401
        _rosbags_available = True
    except ImportError:
        _rosbags_available = False
        raise ImportError(
            "缺少 rosbags 库，请执行: pip install rosbags"
        )


def _ensure_cv2():
    global _cv2
    if _cv2 is not None:
        return _cv2
    try:
        import cv2
        _cv2 = cv2
        return _cv2
    except ImportError:
        raise ImportError(
            "缺少 opencv-python 库，请执行: pip install opencv-python"
        )


# ═══════════════════════════════════════════════════════════════════
#  常量 / 工具
# ═══════════════════════════════════════════════════════════════════

IMAGE_TYPES = {
    "sensor_msgs/msg/CompressedImage",
    "sensor_msgs/msg/Image",
}

COMPRESSED_IMAGE_TYPE = "sensor_msgs/msg/CompressedImage"
RAW_IMAGE_TYPE = "sensor_msgs/msg/Image"

JOINT_STATE_TYPES = {
    "sensor_msgs/msg/JointState",
}

# topic 名 → 简短 camera 名的转换规则
_CAMERA_STRIP = ["image_raw", "compressed", "color", "image", "camera"]
QUANTILES = (0.01, 0.10, 0.50, 0.90, 0.99)
DEFAULT_CHUNKS_SIZE = 1000


def _topic_to_short_name(topic: str) -> str:
    """将 /camera/left/image_raw/compressed → cam_left 之类的简称"""
    parts = [p for p in topic.strip("/").split("/")
             if p.lower() not in _CAMERA_STRIP]
    name = "_".join(parts) if parts else topic.strip("/").replace("/", "_")
    if not name.startswith("cam"):
        name = "cam_" + name
    return name


def _topic_to_state_name(topic: str) -> str:
    """将 /cr100/left_arm_state → left_arm_state"""
    parts = topic.strip("/").split("/")
    return parts[-1] if parts else topic


def _normalize_joint_name(name: str) -> str:
    """归一化关节名，便于做默认同名匹配。"""
    text = (name or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"(_joint|joint)$", "", text)
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


def _extract_joint_names(msg) -> list[str]:
    """从 JointState 消息中提取关节名列表。"""
    if msg is None:
        return []
    names = getattr(msg, "name", None)
    if names is None:
        return []
    result = []
    for idx, raw in enumerate(names):
        name = str(raw).strip()
        result.append(name or f"joint_{idx}")
    return result


# ═══════════════════════════════════════════════════════════════════
#  Step 1 — 扫描 Bag 目录
# ═══════════════════════════════════════════════════════════════════

def scan_bags(root_dir: str) -> list[dict]:
    """
    递归扫描目录，返回发现的 ROS2 bag 列表。
    每个 bag 目录通常包含 metadata.yaml + *.mcap 或 *.db3。
    """
    root = Path(root_dir)
    if not root.is_dir():
        raise ValueError(f"路径不存在或不是目录: {root_dir}")

    bags = []
    seen = set()

    for dirpath, _dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        if dp in seen:
            continue

        has_metadata = "metadata.yaml" in filenames
        mcap_files = [f for f in filenames if f.endswith(".mcap")]
        db3_files = [f for f in filenames if f.endswith(".db3")]

        if has_metadata or mcap_files or db3_files:
            seen.add(dp)
            # 计算总大小
            storage_files = mcap_files + db3_files
            total_bytes = sum((dp / f).stat().st_size for f in storage_files
                              if (dp / f).exists())
            fmt = "mcap" if mcap_files else "db3" if db3_files else "unknown"

            bags.append({
                "path": str(dp),
                "name": dp.name,
                "storage_format": fmt,
                "storage_files": storage_files,
                "has_metadata": has_metadata,
                "size_mb": round(total_bytes / (1024 * 1024), 1),
            })

    bags.sort(key=lambda b: b["path"])
    return bags


# ═══════════════════════════════════════════════════════════════════
#  Step 2 — 版本识别 + Topic 发现
# ═══════════════════════════════════════════════════════════════════

def detect_ros_version(bag_path: str) -> dict:
    """从 metadata.yaml 中检测 ROS 版本信息。"""
    meta_file = Path(bag_path) / "metadata.yaml"
    info = {"ros_distro": "unknown", "storage_id": "unknown", "version": 0}

    if not meta_file.exists():
        return info

    try:
        with open(meta_file, "r") as f:
            meta = yaml.safe_load(f)
        bag_info = meta.get("rosbag2_bagfile_information", {})
        info["version"] = bag_info.get("version", 0)
        info["storage_id"] = bag_info.get("storage_identifier", "unknown")

        # 版本推断: version >= 8 通常是 jazzy (Iron+), 5-7 通常是 humble
        v = info["version"]
        if v >= 8:
            info["ros_distro"] = "jazzy"
        elif v >= 4:
            info["ros_distro"] = "humble"
        else:
            info["ros_distro"] = "older"
    except Exception as e:
        log.warning(f"读取 metadata.yaml 失败: {e}")

    return info


def discover_topics(bag_path: str) -> dict:
    """
    打开 bag，发现所有 topic 及其元信息。
    返回 {topics: [...], duration_sec, message_count, ros_version: {...}}
    """
    _ensure_rosbags()
    from rosbags.rosbag2 import Reader
    from rosbags.typesys import Stores, get_typestore

    bag_dir = Path(bag_path)
    ros_version = detect_ros_version(bag_path)
    typestore = get_typestore(Stores.ROS2_HUMBLE)

    topics = []
    duration_sec = 0.0
    total_messages = 0

    with Reader(bag_dir) as reader:
        duration_ns = reader.duration  # 纳秒
        duration_sec = duration_ns / 1e9 if duration_ns > 0 else 0.0
        total_messages = reader.message_count
        connections = list(reader.connections)

        joint_meta = {}
        joint_connections = [c for c in connections if c.msgtype in JOINT_STATE_TYPES]
        pending_joint_topics = {c.topic for c in joint_connections}
        if joint_connections:
            for conn, _timestamp, rawdata in reader.messages(connections=joint_connections):
                topic = conn.topic
                if topic not in pending_joint_topics:
                    continue
                msg = _safe_deserialize(typestore, rawdata, conn.msgtype)
                if msg is None:
                    continue

                joint_names = _extract_joint_names(msg)
                joint_positions = _extract_joint_positions(msg)
                joint_meta[topic] = {
                    "joint_names": joint_names,
                    "joint_count": len(joint_names) if joint_names else len(joint_positions),
                }
                pending_joint_topics.remove(topic)
                if not pending_joint_topics:
                    break

        for conn in connections:
            msg_type = conn.msgtype
            topic_name = conn.topic
            msg_count = conn.msgcount

            freq = round(msg_count / duration_sec, 1) if duration_sec > 0 else 0.0

            # 自动分类
            if msg_type in IMAGE_TYPES:
                category = "camera"
                suggested_name = f"observation.images.{_topic_to_short_name(topic_name)}"
                default_role = "camera"
            elif msg_type in JOINT_STATE_TYPES:
                category = "joint_state"
                tn_lower = topic_name.lower()
                # 智能推荐 state/action
                is_state = any(k in tn_lower for k in ["state", "status", "feedback"])
                is_action = any(k in tn_lower for k in ["command", "target", "action", "goal"])
                default_role = "state+action" if (is_state and is_action) else \
                               "action" if is_action else "state"
                suggested_name = _topic_to_state_name(topic_name)
            else:
                category = "other"
                suggested_name = topic_name.strip("/").replace("/", ".")
                default_role = "skip"

            topic_info = {
                "topic": topic_name,
                "msg_type": msg_type,
                "msg_count": msg_count,
                "frequency_hz": freq,
                "category": category,
                "suggested_name": suggested_name,
                "default_role": default_role,
            }
            if category == "joint_state":
                meta = joint_meta.get(topic_name, {})
                topic_info["joint_names"] = meta.get("joint_names", [])
                if meta.get("joint_count") is not None:
                    topic_info["joint_count"] = int(meta.get("joint_count", 0))

            topics.append(topic_info)

    # 按分类排序: camera > joint_state > other，同类内按频率
    order = {"camera": 0, "joint_state": 1, "other": 2}
    topics.sort(key=lambda t: (order.get(t["category"], 9), -t["frequency_hz"]))

    # 推荐 base topic: 频率最低的 camera
    camera_topics = [t for t in topics if t["category"] == "camera"]
    recommended_base = ""
    if camera_topics:
        lowest = min(camera_topics, key=lambda t: t["frequency_hz"])
        recommended_base = lowest["topic"]

    return {
        "topics": topics,
        "duration_sec": round(duration_sec, 2),
        "message_count": total_messages,
        "ros_version": ros_version,
        "recommended_base": recommended_base,
    }


# ═══════════════════════════════════════════════════════════════════
#  Step 4 — 时间戳对齐
# ═══════════════════════════════════════════════════════════════════

def _safe_deserialize(typestore, rawdata, msgtype):
    """安全反序列化 CDR 数据，兼容 rosbags 0.11 尾部字节断言问题。"""
    try:
        return typestore.deserialize_cdr(rawdata, msgtype)
    except Exception:
        # 截断尾部 padding 后重试
        if len(rawdata) > 4:
            try:
                return typestore.deserialize_cdr(rawdata[:-1], msgtype)
            except Exception:
                pass
        return None


def _extract_image_bytes_from_cdr(rawdata: bytes) -> bytes | None:
    """
    当 rosbags 反序列化失败时，尝试从 CDR rawdata 中手动提取
    CompressedImage.data 字段的原始字节。

    CompressedImage CDR 布局 (ROS2 Humble):
      [4B CDR header] [Header msg] [format string] [4B data_len] [data bytes]

    这里用启发式方法: 在 rawdata 中搜索 JPEG (FFD8FF) 或 PNG (89504E47)
    标识头来定位图像数据起始位置。
    """
    # JPEG 标识
    jpeg_start = rawdata.find(b'\xff\xd8\xff')
    if jpeg_start >= 0:
        return bytes(rawdata[jpeg_start:])

    # PNG 标识
    png_start = rawdata.find(b'\x89PNG')
    if png_start >= 0:
        return bytes(rawdata[png_start:])

    return None


def _pick_nearest_message(msgs: list, ts_arr: np.ndarray, anchor_ts: int):
    """返回最接近锚点时间戳的消息及其时间差。"""
    idx = int(np.searchsorted(ts_arr, anchor_ts))
    best_idx = min(idx, len(ts_arr) - 1)
    best_delta = abs(int(ts_arr[best_idx]) - int(anchor_ts))

    if idx > 0:
        prev_idx = idx - 1
        prev_delta = abs(int(ts_arr[prev_idx]) - int(anchor_ts))
        if prev_delta < best_delta:
            best_idx = prev_idx
            best_delta = prev_delta

    return msgs[best_idx][1], int(best_delta)


def _video_rel_path(video_key: str, episode_idx: int, chunk_size: int = DEFAULT_CHUNKS_SIZE) -> str:
    """生成 chunk-first 的标准视频相对路径。"""
    chunk = f"chunk-{episode_idx // chunk_size:03d}"
    ep_tag = f"episode_{episode_idx:06d}"
    return f"videos/{chunk}/{video_key}/{ep_tag}.mp4"


def _compute_feature_stats(arr: np.ndarray) -> dict:
    """计算数值特征统计。"""
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    stats = {
        "min": arr.min(axis=0).tolist(),
        "max": arr.max(axis=0).tolist(),
        "mean": arr.mean(axis=0).tolist(),
        "std": arr.std(axis=0).tolist(),
        "count": [int(arr.shape[0])],
    }
    for q in QUANTILES:
        stats[f"q{int(q * 100):02d}"] = np.quantile(arr, q, axis=0).tolist()
    return stats


def _estimate_num_samples(dataset_len: int, min_num_samples: int = 100,
                          max_num_samples: int = 10_000, power: float = 0.75) -> int:
    """按 LeRobot 工具的启发式估算图像统计采样数。"""
    if dataset_len <= 0:
        return 0
    if dataset_len < min_num_samples:
        min_num_samples = dataset_len
    return max(min_num_samples, min(int(dataset_len ** power), max_num_samples))


def _sample_frame_indices(frame_count: int) -> list[int]:
    """在整段视频上均匀采样统计帧。"""
    if frame_count <= 0:
        return []
    if frame_count == 1:
        return [0]

    raw = np.round(
        np.linspace(0, frame_count - 1, _estimate_num_samples(frame_count))
    ).astype(int).tolist()

    sampled = []
    seen = set()
    for idx in raw:
        idx = max(0, min(frame_count - 1, int(idx)))
        if idx in seen:
            continue
        seen.add(idx)
        sampled.append(idx)
    return sampled


def _compress_int_ranges(sorted_indices: list[int]) -> list[tuple[int, int]]:
    """将有序索引压缩成连续区间。"""
    if not sorted_indices:
        return []
    ranges = []
    start = prev = sorted_indices[0]
    for idx in sorted_indices[1:]:
        if idx == prev + 1:
            prev = idx
            continue
        ranges.append((start, prev))
        start = prev = idx
    ranges.append((start, prev))
    return ranges


def _read_exact(stream, size: int) -> bytes:
    """从二进制流读取固定字节数。"""
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _probe_video_params(video_path: str) -> dict:
    """用 ffprobe 获取视频宽高。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        str(video_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams", [])
        if not streams:
            return {}
        st = streams[0]
        return {
            "width": int(st.get("width") or 0),
            "height": int(st.get("height") or 0),
        }
    except Exception:
        return {}


def _compute_image_feature_stats_from_video(video_path: str, frame_count: int) -> dict | None:
    """从编码后的视频中采样 RGB 帧并计算 camera 统计。"""
    sampled_indices = _sample_frame_indices(frame_count)
    if not sampled_indices:
        return None

    params = _probe_video_params(video_path)
    width = int(params.get("width") or 0)
    height = int(params.get("height") or 0)
    if width <= 0 or height <= 0:
        return None

    out_w, out_h = width, height
    if max(width, height) >= 300:
        downsample = int(width / 150) if width > height else int(height / 150)
        downsample = max(downsample, 1)
        out_w = max(1, width // downsample)
        out_h = max(1, height // downsample)

    ranges = _compress_int_ranges(sampled_indices)
    select_parts = []
    for start, end in ranges:
        if start == end:
            select_parts.append(f"eq(n\\,{start})")
        else:
            select_parts.append(f"between(n\\,{start}\\,{end})")
    filters = [f"select='{'+'.join(select_parts)}'"]
    if out_w != width or out_h != height:
        filters.append(f"scale={out_w}:{out_h}")

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path),
        "-vf", ",".join(filters),
        "-vsync", "0",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "pipe:1",
    ]

    proc = None
    frame_size = out_w * out_h * 3
    decoded_frames = 0
    channel_batches = []
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        while True:
            buf = _read_exact(proc.stdout, frame_size)
            if not buf:
                break
            if len(buf) != frame_size:
                return None
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 3).astype(np.float64) / 255.0
            channel_batches.append(frame)
            decoded_frames += 1
        return_code = proc.wait(timeout=600)
        if return_code != 0:
            return None
    except Exception:
        if proc is not None:
            proc.kill()
        return None
    finally:
        if proc is not None:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    if decoded_frames == 0 or not channel_batches:
        return None

    pixels = np.concatenate(channel_batches, axis=0)

    def _as_image_stat(values):
        return np.asarray(values, dtype=np.float64).reshape(3, 1, 1).tolist()

    stats = {
        "min": _as_image_stat(pixels.min(axis=0)),
        "max": _as_image_stat(pixels.max(axis=0)),
        "mean": _as_image_stat(pixels.mean(axis=0)),
        "std": _as_image_stat(pixels.std(axis=0)),
        "count": [int(decoded_frames)],
    }
    for q in QUANTILES:
        stats[f"q{int(q * 100):02d}"] = _as_image_stat(np.quantile(pixels, q, axis=0))
    return stats


def _compute_image_feature_stats_from_pixels(channel_batches: list[np.ndarray]) -> dict | None:
    """从采样后的 RGB 像素批次直接计算图像统计。"""
    if not channel_batches:
        return None

    pixels = np.concatenate(channel_batches, axis=0)

    def _as_image_stat(values):
        return np.asarray(values, dtype=np.float64).reshape(3, 1, 1).tolist()

    stats = {
        "min": _as_image_stat(pixels.min(axis=0)),
        "max": _as_image_stat(pixels.max(axis=0)),
        "mean": _as_image_stat(pixels.mean(axis=0)),
        "std": _as_image_stat(pixels.std(axis=0)),
        "count": [len(channel_batches)],
    }
    for q in QUANTILES:
        stats[f"q{int(q * 100):02d}"] = _as_image_stat(np.quantile(pixels, q, axis=0))
    return stats


def _compute_image_feature_stats_from_frames(frames_rgb: list[np.ndarray]) -> dict | None:
    """直接从 RGB 帧列表计算 camera 统计。"""
    frame_count = len(frames_rgb)
    sampled_indices = _sample_frame_indices(frame_count)
    if not sampled_indices:
        return None

    channel_batches = []
    for idx in sampled_indices:
        frame = frames_rgb[idx]
        if frame is None:
            continue
        arr = np.asarray(frame, dtype=np.uint8).reshape(-1, 3).astype(np.float64) / 255.0
        channel_batches.append(arr)
    return _compute_image_feature_stats_from_pixels(channel_batches)


def _aggregate_episode_stats(ep_stats_list: list[dict]) -> dict:
    """将 episode stats 聚合为全局 stats.json。"""
    all_keys = {}
    for es in ep_stats_list:
        for key, value in es.items():
            all_keys.setdefault(key, []).append(value)

    global_stats = {}
    for key, stats_list in all_keys.items():
        mins = np.array([s["min"] for s in stats_list], dtype=np.float64)
        maxs = np.array([s["max"] for s in stats_list], dtype=np.float64)
        means = np.array([s["mean"] for s in stats_list], dtype=np.float64)
        stds = np.array([s["std"] for s in stats_list], dtype=np.float64)
        counts = np.array([s["count"][0] for s in stats_list], dtype=np.float64)
        total_count = counts.sum()
        count_weights = counts.reshape((len(counts),) + (1,) * (means.ndim - 1))
        total_mean = (means * count_weights).sum(axis=0) / total_count
        total_var = ((stds ** 2 + (means - total_mean) ** 2) * count_weights).sum(axis=0) / total_count

        merged = {
            "min": mins.min(axis=0).tolist(),
            "max": maxs.max(axis=0).tolist(),
            "mean": total_mean.tolist(),
            "std": np.sqrt(np.maximum(0, total_var)).tolist(),
            "count": [int(total_count)],
        }
        for metric in stats_list[0]:
            if not metric.startswith("q"):
                continue
            if not all(metric in s for s in stats_list):
                continue
            values = np.array([s[metric] for s in stats_list], dtype=np.float64)
            merged[metric] = ((values * count_weights).sum(axis=0) / total_count).tolist()
        global_stats[key] = merged

    return global_stats


def _build_anchor_timestamps(base_msgs: list, target_fps: int, rebuild_timestamps: bool) -> np.ndarray:
    """生成对齐锚点时间轴。"""
    base_ts = np.array([m[0] for m in base_msgs], dtype=np.int64)
    if len(base_ts) == 0:
        return base_ts

    if not rebuild_timestamps:
        return base_ts

    if target_fps <= 0:
        raise ValueError("目标输出频率 FPS 必须大于 0")

    step_ns = max(1, int(round(1e9 / float(target_fps))))
    start_ts = int(base_ts[0])
    end_ts = int(base_ts[-1])
    anchors = np.arange(start_ts, end_ts + max(1, step_ns // 2), step_ns, dtype=np.int64)
    if len(anchors) == 0:
        anchors = np.array([start_ts], dtype=np.int64)
    return anchors


def align_one_episode(bag_path: str, config: dict, episode_idx: int,
                      project_dir: str) -> dict:
    """
    对齐单个 bag 的时间戳。

    config 字段:
      - base_topic: str  基准 topic (camera)
      - selected_topics: [{topic, role, name}]  用户选择的 topic 列表
      - tolerance_sec: float  容差 (秒)
      - fps: int  目标输出帧率
      - rebuild_timestamps: bool  是否按目标帧率重建统一时间轴

    返回 {episode_idx, frame_count, max_delta_ms, warnings, output_path}
    """
    _ensure_rosbags()
    from rosbags.rosbag2 import Reader
    from rosbags.typesys import Stores, get_typestore

    typestore = get_typestore(Stores.ROS2_HUMBLE)
    bag_dir = Path(bag_path)
    base_topic = config["base_topic"]
    target_fps = int(config.get("fps", 30) or 30)
    rebuild_timestamps = bool(config.get("rebuild_timestamps", False))
    tolerance_ns = int(config.get("tolerance_sec", 0.01) * 1e9)
    selected = {t["topic"]: t for t in config["selected_topics"]}

    # 收集所有消息，按 topic 分桶
    buffers: dict[str, list] = defaultdict(list)

    with Reader(bag_dir) as reader:
        topic_set = set(selected.keys())
        connections = [c for c in reader.connections if c.topic in topic_set]
        conn_type = {c.topic: c.msgtype for c in connections}

        for conn, timestamp, rawdata in reader.messages(connections=connections):
            topic = conn.topic
            msgtype = conn_type[topic]
            is_image = msgtype in IMAGE_TYPES

            if is_image:
                msg = _safe_deserialize(typestore, rawdata, conn.msgtype)

                if msgtype == COMPRESSED_IMAGE_TYPE:
                    # CompressedImage: msg.data 是 JPEG/PNG 字节流
                    if msg is not None and hasattr(msg, "data"):
                        img_bytes = bytes(msg.data)
                    else:
                        # 反序列化失败 → 从 CDR 中搜索 JPEG/PNG 头
                        img_bytes = _extract_image_bytes_from_cdr(rawdata)
                    if img_bytes:
                        buffers[topic].append((timestamp, ("compressed", img_bytes)))

                elif msgtype == RAW_IMAGE_TYPE:
                    # Image: msg.data 是原始像素，需要 encoding/width/height 才能解码
                    if msg is not None and hasattr(msg, "data"):
                        encoding = getattr(msg, "encoding", "bgr8")
                        w = getattr(msg, "width", 0)
                        h = getattr(msg, "height", 0)
                        step = getattr(msg, "step", 0)
                        is_bigendian = bool(getattr(msg, "is_bigendian", False))
                        buffers[topic].append((timestamp, (
                            "raw", bytes(msg.data), encoding, w, h, step, is_bigendian)))
            else:
                msg = _safe_deserialize(typestore, rawdata, conn.msgtype)
                if msg is not None:
                    buffers[topic].append((timestamp, msg))

    # 按 base_topic 的时间戳对齐
    base_msgs = buffers.get(base_topic, [])
    if not base_msgs:
        return {
            "episode_idx": episode_idx,
            "frame_count": 0,
            "max_delta_ms": 0,
            "warnings": [f"base topic {base_topic} 无消息"],
            "output_path": "",
        }

    # 为其余 topic 构建时间戳数组（用于二分查找）
    anchor_timestamps = _build_anchor_timestamps(base_msgs, target_fps, rebuild_timestamps)
    base_ts_arr = np.array([m[0] for m in base_msgs], dtype=np.int64)
    other_topics = {t: buffers[t] for t in selected if t != base_topic and buffers[t]}
    ts_arrays = {t: np.array([m[0] for m in msgs], dtype=np.int64) for t, msgs in other_topics.items()}

    aligned_frames = []
    warnings = []
    global_max_delta_ns = 0

    for anchor_ts in anchor_timestamps:
        if rebuild_timestamps:
            base_data, base_delta_ns = _pick_nearest_message(base_msgs, base_ts_arr, int(anchor_ts))
        else:
            base_data = base_msgs[len(aligned_frames)][1]
            base_delta_ns = 0

        frame = {
            "base_ts": int(anchor_ts),
            "data": {base_topic: base_data},
            "deltas": {base_topic: base_delta_ns} if rebuild_timestamps else {},
        }
        if rebuild_timestamps:
            frame["anchor_delta_ns"] = base_delta_ns
        frame_max_delta = 0

        for topic, msgs in other_topics.items():
            ts_arr = ts_arrays[topic]
            msg, delta_ns = _pick_nearest_message(msgs, ts_arr, int(anchor_ts))
            frame["data"][topic] = msg
            frame["deltas"][topic] = delta_ns

            if delta_ns > frame_max_delta:
                frame_max_delta = delta_ns

        frame["max_delta_ns"] = frame_max_delta

        if frame_max_delta > tolerance_ns:
            warnings.append(
                f"帧 {len(aligned_frames)}: max_delta={frame_max_delta/1e6:.2f}ms "
                f"超出容差 {tolerance_ns/1e6:.1f}ms"
            )

        if frame_max_delta > global_max_delta_ns:
            global_max_delta_ns = frame_max_delta

        aligned_frames.append(frame)

    # 持久化
    out_dir = Path(project_dir) / "step4_aligned"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"episode_{episode_idx:06d}.pkl"
    with open(out_path, "wb") as f:
        pickle.dump({
            "episode_idx": episode_idx,
            "bag_path": str(bag_path),
            "config": config,
            "frames": aligned_frames,
            "timeline": {
                "mode": "uniform_fps" if rebuild_timestamps else "base_topic",
                "target_fps": target_fps,
                "frame_count": len(aligned_frames),
            },
        }, f)

    return {
        "episode_idx": episode_idx,
        "frame_count": len(aligned_frames),
        "max_delta_ms": round(global_max_delta_ns / 1e6, 3),
        "warnings": warnings[:20],  # 只返回前20条
        "output_path": str(out_path),
        "timeline_mode": "uniform_fps" if rebuild_timestamps else "base_topic",
        "target_fps": target_fps,
    }


# ═══════════════════════════════════════════════════════════════════
#  Step 5 — 转换为 LeRobot v2.1（自包含，不依赖 lerobot 包）
# ═══════════════════════════════════════════════════════════════════

def _decode_compressed_image(data: bytes, prev_frame=None) -> tuple[np.ndarray | None, bool]:
    """
    解码压缩图像字节 (JPEG/PNG) → RGB ndarray。
    失败时返回 (prev_frame, False)（前一帧替代）。
    """
    if not data or len(data) < 8:
        return prev_frame, False
    try:
        cv2 = _ensure_cv2()
        np_arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_UNCHANGED)
        if img is None:
            return prev_frame, False
        if img.ndim == 2:
            gray = _to_display_uint8(img)
            return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB), True
        if img.ndim == 3 and img.shape[2] == 4:
            img8 = _to_display_uint8(img)
            return cv2.cvtColor(img8, cv2.COLOR_BGRA2RGB), True
        if img.ndim == 3 and img.shape[2] == 3:
            img8 = _to_display_uint8(img)
            return cv2.cvtColor(img8, cv2.COLOR_BGR2RGB), True
        return prev_frame, False
    except Exception:
        return prev_frame, False


def _to_display_uint8(img: np.ndarray) -> np.ndarray:
    """将数值图像归一化到 uint8，便于写入普通 RGB 视频。"""
    if img.dtype == np.uint8:
        return img

    arr = img.astype(np.float32, copy=False)
    finite = np.isfinite(arr)
    if not finite.any():
        return np.zeros(arr.shape, dtype=np.uint8)

    vals = arr[finite]
    lo = float(vals.min())
    hi = float(vals.max())
    if hi <= lo:
        return np.zeros(arr.shape, dtype=np.uint8)

    scaled = np.clip((arr - lo) * (255.0 / (hi - lo)), 0, 255)
    return scaled.astype(np.uint8)


def _decode_interleaved_image(data: bytes, width: int, height: int, channels: int,
                              dtype, step: int | None) -> np.ndarray:
    """解码带可选行步长的交错排列图像。"""
    itemsize = np.dtype(dtype).itemsize
    min_row_bytes = width * channels * itemsize
    row_bytes = step or min_row_bytes
    if row_bytes < min_row_bytes:
        raise ValueError(f"非法 step={row_bytes}, 小于最小需求 {min_row_bytes}")

    total_bytes = height * row_bytes
    if len(data) < total_bytes:
        raise ValueError(f"buffer 太短: 期望 {total_bytes}B, 实际 {len(data)}B")

    raw = np.frombuffer(data[:total_bytes], dtype=dtype).reshape(height, row_bytes // itemsize)
    return raw[:, :width * channels].reshape(height, width, channels)


def _decode_single_channel_image(data: bytes, width: int, height: int, dtype,
                                 step: int | None) -> np.ndarray:
    """解码带可选行步长的单通道图像。"""
    itemsize = np.dtype(dtype).itemsize
    min_row_bytes = width * itemsize
    row_bytes = step or min_row_bytes
    if row_bytes < min_row_bytes:
        raise ValueError(f"非法 step={row_bytes}, 小于最小需求 {min_row_bytes}")

    total_bytes = height * row_bytes
    if len(data) < total_bytes:
        raise ValueError(f"buffer 太短: 期望 {total_bytes}B, 实际 {len(data)}B")

    raw = np.frombuffer(data[:total_bytes], dtype=dtype).reshape(height, row_bytes // itemsize)
    return raw[:, :width]


def _decode_raw_image(data: bytes, encoding: str, width: int, height: int,
                      step: int | None = None, is_bigendian: bool = False,
                      prev_frame=None) -> tuple[np.ndarray | None, bool]:
    """
    解码原始像素字节 (sensor_msgs/Image) → RGB ndarray。
    支持常见 encoding: bgr8, rgb8, mono8, bgra8, rgba8, yuyv, nv12,
    bayer, 16UC1, 32FC1 等。
    """
    if not data or width <= 0 or height <= 0:
        return prev_frame, False
    try:
        cv2 = _ensure_cv2()
        enc = (encoding or "").strip().lower()
        if enc in ("bgr8", "8uc3"):
            img = _decode_interleaved_image(data, width, height, 3, np.uint8, step)
            return cv2.cvtColor(img, cv2.COLOR_BGR2RGB), True
        elif enc in ("rgb8",):
            return _decode_interleaved_image(data, width, height, 3, np.uint8, step).copy(), True
        elif enc in ("bgra8",):
            img = _decode_interleaved_image(data, width, height, 4, np.uint8, step)
            return cv2.cvtColor(img, cv2.COLOR_BGRA2RGB), True
        elif enc in ("rgba8",):
            img = _decode_interleaved_image(data, width, height, 4, np.uint8, step)
            return cv2.cvtColor(img, cv2.COLOR_RGBA2RGB), True
        elif enc in ("mono8", "8uc1"):
            gray = _decode_single_channel_image(data, width, height, np.uint8, step)
            return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB), True
        elif enc in ("16uc1", "mono16"):
            dtype = np.dtype(">u2" if is_bigendian else "<u2")
            gray16 = _decode_single_channel_image(data, width, height, dtype, step)
            gray8 = _to_display_uint8(gray16)
            return cv2.cvtColor(gray8, cv2.COLOR_GRAY2RGB), True
        elif enc in ("32fc1",):
            dtype = np.dtype(">f4" if is_bigendian else "<f4")
            gray32 = _decode_single_channel_image(data, width, height, dtype, step)
            gray8 = _to_display_uint8(gray32)
            return cv2.cvtColor(gray8, cv2.COLOR_GRAY2RGB), True
        elif enc in ("yuyv", "yuyv422", "yuv422"):
            row_bytes = step or (width * 2)
            total_bytes = height * row_bytes
            if len(data) < total_bytes:
                return prev_frame, False
            packed = np.frombuffer(data[:total_bytes], dtype=np.uint8).reshape(height, row_bytes)
            yuv = packed[:, :width * 2].reshape(height, width, 2)
            return cv2.cvtColor(yuv, cv2.COLOR_YUV2RGB_YUY2), True
        elif enc in ("uyvy", "uyvy422"):
            row_bytes = step or (width * 2)
            total_bytes = height * row_bytes
            if len(data) < total_bytes:
                return prev_frame, False
            packed = np.frombuffer(data[:total_bytes], dtype=np.uint8).reshape(height, row_bytes)
            yuv = packed[:, :width * 2].reshape(height, width, 2)
            return cv2.cvtColor(yuv, cv2.COLOR_YUV2RGB_UYVY), True
        elif enc in ("nv12",):
            expected = int(height * width * 3 / 2)
            if len(data) < expected:
                return prev_frame, False
            yuv = np.frombuffer(data[:expected], dtype=np.uint8).reshape(height * 3 // 2, width)
            return cv2.cvtColor(yuv, cv2.COLOR_YUV2RGB_NV12), True
        elif enc in ("nv21",):
            expected = int(height * width * 3 / 2)
            if len(data) < expected:
                return prev_frame, False
            yuv = np.frombuffer(data[:expected], dtype=np.uint8).reshape(height * 3 // 2, width)
            return cv2.cvtColor(yuv, cv2.COLOR_YUV2RGB_NV21), True
        elif enc.startswith("bayer_"):
            gray = _decode_single_channel_image(data, width, height, np.uint8, step)
            bayer_map = {
                "bayer_rggb8": cv2.COLOR_BAYER_RG2RGB,
                "bayer_bggr8": cv2.COLOR_BAYER_BG2RGB,
                "bayer_gbrg8": cv2.COLOR_BAYER_GB2RGB,
                "bayer_grbg8": cv2.COLOR_BAYER_GR2RGB,
            }
            code = bayer_map.get(enc)
            if code is not None:
                return cv2.cvtColor(gray, code), True
            return prev_frame, False
        else:
            # 兜底: 尝试 3 通道 BGR
            expected = height * width * 3
            if len(data) >= expected:
                img = np.frombuffer(data[:expected], dtype=np.uint8).reshape(height, width, 3)
                return cv2.cvtColor(img, cv2.COLOR_BGR2RGB), True
            return prev_frame, False
    except Exception:
        return prev_frame, False


def _decode_image_entry(entry, prev_frame=None) -> tuple[np.ndarray | None, bool]:
    """
    统一解码入口: 根据对齐阶段存入的 tuple 格式自动选择解码方式。

    entry 格式:
      ("compressed", jpeg_bytes)
      ("raw", pixel_bytes, encoding, width, height)
      ("raw", pixel_bytes, encoding, width, height, step, is_bigendian)

    返回 (rgb, ok)；ok=False 表示已用 prev_frame 或未解码成功。
    """
    if not isinstance(entry, tuple) or len(entry) < 2:
        return prev_frame, False

    fmt = entry[0]
    if fmt == "compressed":
        return _decode_compressed_image(entry[1], prev_frame)
    elif fmt == "raw":
        if len(entry) >= 7:
            return _decode_raw_image(
                entry[1], entry[2], entry[3], entry[4], entry[5], entry[6], prev_frame
            )
        if len(entry) == 5:
            return _decode_raw_image(entry[1], entry[2], entry[3], entry[4], None, False, prev_frame)
    return prev_frame, False


def _describe_image_entry(entry) -> str:
    """给日志/报错用的简短图像条目描述。"""
    if not isinstance(entry, tuple) or not entry:
        return "empty"
    fmt = entry[0]
    if fmt == "compressed":
        size = len(entry[1]) if len(entry) > 1 and entry[1] is not None else 0
        return f"compressed bytes={size}"
    if fmt == "raw":
        encoding = entry[2] if len(entry) > 2 else "?"
        width = entry[3] if len(entry) > 3 else "?"
        height = entry[4] if len(entry) > 4 else "?"
        step = entry[5] if len(entry) > 5 else "?"
        return f"raw encoding={encoding} size={width}x{height} step={step}"
    return str(fmt)


def _extract_joint_positions(msg) -> list[float]:
    """从 JointState 消息中提取 position 数组。"""
    if msg is None:
        return []
    pos = getattr(msg, "position", None)
    if pos is None:
        return []
    return [float(v) for v in pos]


def _sanitize_joint_name_list(raw_names, fallback_count: int = 0) -> list[str]:
    """清洗配置中的关节名列表。"""
    names = []
    if isinstance(raw_names, list):
        for idx, raw in enumerate(raw_names):
            text = str(raw).strip()
            names.append(text or f"joint_{idx}")
    if names:
        return names
    return [f"joint_{i}" for i in range(max(0, int(fallback_count or 0)))]


def _normalize_joint_mapping(topic_cfg: dict) -> list[dict]:
    """规范化单个 JointState topic 的关节映射配置。"""
    source_names = _sanitize_joint_name_list(
        topic_cfg.get("joint_names"),
        topic_cfg.get("joint_count", 0),
    )
    raw_mapping = topic_cfg.get("joint_mapping")
    normalized = []

    if isinstance(raw_mapping, list) and raw_mapping:
        for order, item in enumerate(raw_mapping):
            if not isinstance(item, dict):
                continue

            try:
                source_index = int(item.get("source_index"))
            except (TypeError, ValueError):
                continue
            if source_index < 0:
                continue

            source_name = ""
            if source_index < len(source_names):
                source_name = source_names[source_index]
            source_name = str(item.get("source_name") or source_name).strip() or f"joint_{source_index}"

            target_name = str(item.get("target_name") or source_name).strip()
            if not target_name:
                continue

            target_index = item.get("target_index")
            try:
                target_index = int(target_index) if target_index is not None else None
            except (TypeError, ValueError):
                target_index = None

            normalized.append({
                "source_index": source_index,
                "source_name": source_name,
                "target_name": target_name,
                "target_index": target_index,
                "mapping_order": order,
            })
        if normalized:
            return normalized

    return [
        {
            "source_index": idx,
            "source_name": name,
            "target_name": name,
            "target_index": None,
            "mapping_order": idx,
        }
        for idx, name in enumerate(source_names)
    ]


def _build_role_joint_layout(config: dict, role: str) -> list[dict]:
    """根据配置构建某个角色(state/action)的输出关节布局。"""
    layout = []
    for topic_order, topic_cfg in enumerate(config.get("selected_topics", [])):
        if topic_cfg.get("category") != "joint_state":
            continue
        if role not in str(topic_cfg.get("role", "")):
            continue

        for mapping in _normalize_joint_mapping(topic_cfg):
            layout.append({
                "topic": topic_cfg.get("topic", ""),
                "topic_order": topic_order,
                **mapping,
            })

    layout.sort(key=lambda item: (
        0 if item.get("target_index") is not None else 1,
        item.get("target_index") if item.get("target_index") is not None else item.get("topic_order", 0),
        item.get("topic_order", 0),
        item.get("mapping_order", item.get("source_index", 0)),
        item.get("source_index", 0),
    ))

    for output_index, item in enumerate(layout):
        item["output_index"] = output_index
    return layout


def _encode_video_ffmpeg(frames_rgb: list[np.ndarray], output_path: str,
                         fps: int, hw_accel: bool = False) -> bool:
    """用 ffmpeg 子进程 + pipe 编码视频，避免写中间图片文件。"""
    if not frames_rgb:
        return False

    cv2 = _ensure_cv2()

    h, w = frames_rgb[0].shape[:2]
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    keyint = max(1, min(int(fps) if fps else 30, 30))

    cmd = ["ffmpeg", "-y",
           "-f", "rawvideo",
           "-vcodec", "rawvideo",
           "-pix_fmt", "rgb24",
           "-s", f"{w}x{h}",
           "-r", str(fps),
           "-i", "-",
           "-c:v", "libx264",
           "-pix_fmt", "yuv420p",
           "-preset", "fast",
           "-crf", "22",
           "-g", str(keyint),
           "-keyint_min", str(keyint),
           "-sc_threshold", "0",
           "-movflags", "+faststart",
           str(out)]

    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for frame in frames_rgb:
            if frame.shape[:2] != (h, w):
                frame = cv2.resize(frame, (w, h))
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        proc.wait(timeout=300)
        if proc.returncode != 0:
            stderr = proc.stderr.read().decode("utf-8", errors="ignore")
            log.error(f"ffmpeg 编码失败 ({output_path}): {stderr.strip()}")
            return False
        return True
    except Exception as e:
        log.error(f"ffmpeg 编码失败: {e}")
        return False


def _open_video_writer_ffmpeg(output_path: str, width: int, height: int, fps: int):
    """打开 ffmpeg 原始 RGB 管道写入器。"""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    keyint = max(1, min(int(fps) if fps else 30, 30))
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "-i", "-",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "fast",
        "-crf", "22",
        "-g", str(keyint),
        "-keyint_min", str(keyint),
        "-sc_threshold", "0",
        "-movflags", "+faststart",
        str(out),
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _close_video_writer_ffmpeg(proc, output_path: str) -> bool:
    """关闭 ffmpeg 写入器并检查状态。"""
    try:
        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=300)
        if proc.returncode != 0:
            stderr = proc.stderr.read().decode("utf-8", errors="ignore")
            log.error(f"ffmpeg 编码失败 ({output_path}): {stderr.strip()}")
            return False
        return True
    except Exception as e:
        log.error(f"ffmpeg 编码失败: {e}")
        return False
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


def convert_episode(aligned_path: str, output_dir: str, episode_idx: int,
                    config: dict, global_frame_offset: int = 0) -> dict:
    """
    将对齐后的单个 episode 转换为 LeRobot v2.1 格式。

    config 字段:
      - fps: int
      - selected_topics: [{topic, role, name, category}]
      - task: str (任务描述)

    Parquet 中 timestamp 为 episode 内秒数 i/fps，与按 fps 编码的 MP4 时间轴一致。
    """
    import pandas as pd

    with open(aligned_path, "rb") as f:
        ep_data = pickle.load(f)

    frames = ep_data["frames"]
    if not frames:
        return {"episode_idx": episode_idx, "frame_count": 0}

    fps = config["fps"]
    task = config.get("task", "")
    topic_map = {t["topic"]: t for t in config["selected_topics"]}
    out = Path(output_dir)
    chunk_size = int(config.get("chunks_size", DEFAULT_CHUNKS_SIZE) or DEFAULT_CHUNKS_SIZE)
    state_layout = _build_role_joint_layout(config, "state")
    action_layout = _build_role_joint_layout(config, "action")

    # 分离 camera / state / action topics
    camera_topics = []
    state_topics = []
    action_topics = []

    for t in config["selected_topics"]:
        if t["category"] == "camera" and t["role"] != "skip":
            camera_topics.append(t)
        elif t["category"] == "joint_state":
            if "state" in t["role"]:
                state_topics.append(t)
            if "action" in t["role"]:
                action_topics.append(t)

    # --- 5a. 视频编码 ---
    video_paths = {}
    video_shapes = {}
    camera_stats = {}
    for cam_info in camera_topics:
        cam_name = cam_info["name"]
        cam_topic = cam_info["topic"]
        vid_rel = _video_rel_path(cam_name, episode_idx, chunk_size)
        vid_path = out / vid_rel

        prev_frame = None
        bad_count = 0
        first_ok_mean = None
        first_entry_desc = None
        writer_proc = None
        writer_shape = None
        sampled_frame_indices = set(_sample_frame_indices(len(frames)))
        sampled_pixels = []
        cv2 = _ensure_cv2()

        for frame_idx, frame in enumerate(frames):
            img_entry = frame["data"].get(cam_topic)
            ok = False
            if img_entry is None:
                decoded = prev_frame if prev_frame is not None \
                    else np.zeros((480, 640, 3), dtype=np.uint8)
                bad_count += 1
            else:
                if first_entry_desc is None:
                    first_entry_desc = _describe_image_entry(img_entry)
                decoded, ok = _decode_image_entry(img_entry, prev_frame)
                if decoded is None:
                    decoded = prev_frame if prev_frame is not None \
                        else np.zeros((480, 640, 3), dtype=np.uint8)
                    bad_count += 1
                elif not ok:
                    bad_count += 1
            prev_frame = decoded
            if ok and first_ok_mean is None:
                first_ok_mean = float(decoded.mean())

            if writer_proc is None:
                h, w = decoded.shape[:2]
                writer_shape = (h, w)
                writer_proc = _open_video_writer_ffmpeg(str(vid_path), w, h, fps)
                video_shapes[cam_name] = [h, w, int(decoded.shape[2]) if decoded.ndim == 3 else 1]

            frame_to_write = decoded
            if frame_to_write.shape[:2] != writer_shape:
                frame_to_write = cv2.resize(frame_to_write, (writer_shape[1], writer_shape[0]))

            writer_proc.stdin.write(frame_to_write.tobytes())
            if frame_idx in sampled_frame_indices:
                sampled_pixels.append(
                    np.asarray(frame_to_write, dtype=np.uint8).reshape(-1, 3).astype(np.float64) / 255.0
                )

        if bad_count > 0:
            log.warning(f"Episode {episode_idx} camera {cam_name}: "
                        f"{bad_count}/{len(frames)} 帧解码失败或缺失，已用前一帧/黑图替代")
        if bad_count == len(frames):
            if writer_proc is not None:
                try:
                    writer_proc.kill()
                except Exception:
                    pass
            raise ValueError(
                f"camera {cam_name} ({cam_topic}) 的 {len(frames)} 帧全部解码失败，"
                f"首帧条目: {first_entry_desc or 'missing'}。"
                "请检查原始图像 encoding、topic 选择或对齐产物"
            )
        if first_ok_mean is not None and first_ok_mean < 1.0:
            log.warning(
                f"Episode {episode_idx} camera {cam_name}: 首个成功解码帧均值仅 {first_ok_mean:.2f}，"
                "视频内容可能接近全黑，请检查是否误选了深度/红外 topic"
            )

        if writer_proc is None:
            raise RuntimeError(f"未能初始化视频写入器: {vid_path}")

        image_stats = _compute_image_feature_stats_from_pixels(sampled_pixels)
        if image_stats:
            camera_stats[cam_name] = image_stats
        if not _close_video_writer_ffmpeg(writer_proc, str(vid_path)):
            raise RuntimeError(f"ffmpeg 编码失败: {vid_path}")
        video_paths[cam_name] = vid_rel

    # --- 5b. Parquet 生成 ---
    # timestamp: episode 内秒数，与 ffmpeg 按 fps 顺序编码的 MP4 时间轴一致（非 ROS 绝对时间）
    fps_f = float(fps)
    rows = []
    for i, frame in enumerate(frames):
        row = {
            "frame_index": global_frame_offset + i,
            "episode_index": episode_idx,
            "index": global_frame_offset + i,
            "task_index": 0,
            "timestamp": i / fps_f,
        }
        joint_cache = {}

        def get_joint_positions(topic_name: str) -> list[float]:
            if topic_name not in joint_cache:
                joint_cache[topic_name] = _extract_joint_positions(frame["data"].get(topic_name))
            return joint_cache[topic_name]

        # State: 按用户映射后的顺序提取 position
        if state_layout:
            for mapping in state_layout:
                positions = get_joint_positions(mapping["topic"])
                source_index = mapping["source_index"]
                if source_index >= len(positions):
                    raise ValueError(
                        f"topic {mapping['topic']} 的关节索引 {source_index} 越界: "
                        f"当前帧只有 {len(positions)} 个 position"
                    )
                row[f"observation.state_{mapping['output_index']}"] = positions[source_index]
        elif state_topics:
            state_vec = []
            for st in state_topics:
                state_vec.extend(get_joint_positions(st["topic"]))
            for j, v in enumerate(state_vec):
                row[f"observation.state_{j}"] = v

        # Action: 按用户映射后的顺序提取 position
        if action_layout:
            for mapping in action_layout:
                positions = get_joint_positions(mapping["topic"])
                source_index = mapping["source_index"]
                if source_index >= len(positions):
                    raise ValueError(
                        f"topic {mapping['topic']} 的关节索引 {source_index} 越界: "
                        f"当前帧只有 {len(positions)} 个 position"
                    )
                row[f"action_{mapping['output_index']}"] = positions[source_index]
        elif action_topics:
            action_vec = []
            for at in action_topics:
                action_vec.extend(get_joint_positions(at["topic"]))
            for j, v in enumerate(action_vec):
                row[f"action_{j}"] = v

        rows.append(row)

    df = pd.DataFrame(rows)
    chunk = f"chunk-{episode_idx // 1000:03d}"
    ep_tag = f"episode_{episode_idx:06d}"
    pq_dir = out / "data" / chunk
    pq_dir.mkdir(parents=True, exist_ok=True)
    pq_path = pq_dir / f"{ep_tag}.parquet"
    df.to_parquet(pq_path, engine="pyarrow", index=False)

    # 返回统计信息
    state_cols = [c for c in df.columns if c.startswith("observation.state_")]
    action_cols = [c for c in df.columns if c.startswith("action_")]

    stats = {}
    for cols_group, prefix in [(state_cols, "observation.state"),
                                (action_cols, "action")]:
        if not cols_group:
            continue
        arr = df[cols_group].to_numpy(dtype=np.float64)
        stats[prefix] = _compute_feature_stats(arr)

    for scalar_col in ("timestamp", "frame_index", "episode_index", "index", "task_index"):
        if scalar_col in df.columns:
            stats[scalar_col] = _compute_feature_stats(df[scalar_col].to_numpy(dtype=np.float64))

    stats.update(camera_stats)

    return {
        "episode_idx": episode_idx,
        "frame_count": len(frames),
        "video_paths": video_paths,
        "video_shapes": video_shapes,
        "parquet_path": str(pq_path),
        "stats": stats,
    }


def write_metadata(output_dir: str, episodes: list[dict], config: dict):
    """写入 LeRobot 标准元数据文件。"""
    out = Path(output_dir)
    meta_dir = out / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)

    fps = config["fps"]
    task = config.get("task", "")
    chunk_size = int(config.get("chunks_size", DEFAULT_CHUNKS_SIZE) or DEFAULT_CHUNKS_SIZE)

    total_frames = sum(ep["frame_count"] for ep in episodes)
    tasks = [{"task_index": 0, "task": task}]
    state_layout = _build_role_joint_layout(config, "state")
    action_layout = _build_role_joint_layout(config, "action")
    state_names = [item["target_name"] for item in state_layout if item.get("target_name")]
    action_names = [item["target_name"] for item in action_layout if item.get("target_name")]

    # 构建 features
    features = {}

    # State features
    state_topics = [t for t in config["selected_topics"]
                    if t["category"] == "joint_state" and "state" in t.get("role", "")]
    if state_topics and episodes:
        state_dim = 0
        for st in state_topics:
            # 从第一个 episode 推断维度
            for ep in episodes:
                s = ep.get("stats", {}).get("observation.state", {})
                if "min" in s:
                    state_dim = len(s["min"])
                    break
            if state_dim:
                break
        if state_dim:
            features["observation.state"] = {
                "dtype": "float32",
                "shape": [state_dim],
                "names": state_names if len(state_names) == state_dim else None,
            }

    # Action features
    action_topics = [t for t in config["selected_topics"]
                     if t["category"] == "joint_state" and "action" in t.get("role", "")]
    if action_topics and episodes:
        action_dim = 0
        for at in action_topics:
            for ep in episodes:
                s = ep.get("stats", {}).get("action", {})
                if "min" in s:
                    action_dim = len(s["min"])
                    break
            if action_dim:
                break
        if action_dim:
            features["action"] = {
                "dtype": "float32",
                "shape": [action_dim],
                "names": action_names if len(action_names) == action_dim else None,
            }

    # Camera features
    camera_topics = [t for t in config["selected_topics"]
                     if t["category"] == "camera" and t.get("role") != "skip"]
    for cam in camera_topics:
        shape = None
        for ep in episodes:
            shape = ep.get("video_shapes", {}).get(cam["name"])
            if shape:
                break
        if not shape:
            shape = [480, 640, 3]
        features[cam["name"]] = {
            "dtype": "video",
            "shape": shape,
            "names": ["height", "width", "channels"],
            "video_info": {
                "video.fps": fps,
                "video.codec": "h264",
                "video.pix_fmt": "yuv420p",
            }
        }

    for scalar_key, dtype in [
        ("timestamp", "float32"),
        ("frame_index", "int64"),
        ("episode_index", "int64"),
        ("index", "int64"),
        ("task_index", "int64"),
    ]:
        features[scalar_key] = {
            "dtype": dtype,
            "shape": [1],
            "names": None,
        }

    episodes_stats = []
    for ep in episodes:
        episodes_stats.append({
            "episode_index": ep["episode_idx"],
            "stats": ep.get("stats", {}),
        })

    global_stats = _aggregate_episode_stats([ep["stats"] for ep in episodes if ep.get("stats")])

    info = {
        "codebase_version": "v2.1",
        "robot_type": config.get("robot_type", "unknown"),
        "total_episodes": len(episodes),
        "total_frames": total_frames,
        "total_tasks": len(tasks),
        "chunks_size": chunk_size,
        "fps": fps,
        "splits": {"train": f"0:{len(episodes)}"},
        "data_path": "data/chunk-{chunk_index:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{chunk_index:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "keys": list(features.keys()),
        "features": features,
        "task": task,
    }

    with open(meta_dir / "info.json", "w") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)

    # episodes.jsonl
    with open(meta_dir / "episodes.jsonl", "w") as f:
        for ep in episodes:
            line = {
                "episode_index": ep["episode_idx"],
                "length": ep["frame_count"],
                "task_index": 0,
            }
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    with open(meta_dir / "tasks.jsonl", "w") as f:
        for line in tasks:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    with open(meta_dir / "stats.json", "w") as f:
        json.dump(global_stats, f, indent=2, ensure_ascii=False)

    if state_names:
        with open(meta_dir / "joint_config.json", "w") as f:
            json.dump({"joint_names": state_names}, f, indent=2, ensure_ascii=False)

    with open(meta_dir / "episodes_stats.jsonl", "w") as f:
        for line in episodes_stats:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    return info


# ═══════════════════════════════════════════════════════════════════
#  进度管理 / 断点续做
# ═══════════════════════════════════════════════════════════════════

class ProjectState:
    """管理单个转换项目的进度状态。"""

    def __init__(self, project_dir: str):
        self.dir = Path(project_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def save_step(self, step: str, data: dict):
        with open(self.dir / f"{step}.json", "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def load_step(self, step: str) -> dict | None:
        p = self.dir / f"{step}.json"
        if p.exists():
            with open(p) as f:
                return json.load(f)
        return None

    def has_step(self, step: str) -> bool:
        return (self.dir / f"{step}.json").exists()

    def get_progress(self) -> dict:
        """返回当前进度概览。"""
        steps = ["step1_scan", "step2_topics", "step3_config",
                 "step4_align", "step5_convert"]
        progress = {}
        for s in steps:
            progress[s] = self.has_step(s)
        return progress
