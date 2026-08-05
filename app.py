#!/usr/bin/env python3
from __future__ import annotations
"""
LeRobot v2.1 数据集可视化编辑器

简单 Web 工具，支持浏览、编辑、删除 episodes/frames，并另存为新数据集。
自动重新计算元数据统计信息（mean, std, min, max）。

启动:
    pip install -r requirements.txt
    python app.py [--port 7860]
    # 浏览器访问 http://localhost:7860
"""

import json
import os
import shutil
import logging
import argparse
import copy
import shlex
import subprocess
import threading
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, render_template, request, jsonify, send_file, abort
import pandas as pd
import numpy as np

try:
    from scipy.signal import butter, filtfilt
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

import image_analyzer as img_analyzer
import dataset_batch_tools as batch_tools
import dataset_field_editor as field_editor
import video_transcoder
from training_check_service import TrainingCheckService
from stats_verify_service import StatsVerifyService
from health_check_service import HealthCheckService

# ═══════════════════════ 配置 ═══════════════════════

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# 全局编辑器实例
_editor = None
_analysis_editor = None
_save_progress_lock = threading.Lock()
_save_progress = {
    "active": False,
    "stage": "idle",
    "title": "空闲",
    "detail": "",
    "current": 0,
    "total": 0,
}
_urdf_assets = {}

# 图像分析全局状态
_img_analyzer = None
_img_analysis_lock = threading.Lock()
_img_analysis_progress = {
    "running": False,
    "stage": "",
    "title": "",
    "detail": "",
    "current": 0,
    "total": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "result": None,
}


def _set_img_analysis_progress(**kwargs):
    with _img_analysis_lock:
        _img_analysis_progress.update(kwargs)


def _img_analysis_progress_cb(stage, title, detail, current, total):
    with _img_analysis_lock:
        _img_analysis_progress["stage"] = stage
        _img_analysis_progress["title"] = title
        _img_analysis_progress["detail"] = detail or ""
        _img_analysis_progress["current"] = int(current)
        _img_analysis_progress["total"] = int(total)


def _get_img_analysis_progress():
    with _img_analysis_lock:
        data = dict(_img_analysis_progress)
    started_at = data.get("started_at")
    finished_at = data.get("finished_at")
    now = time.time()
    total = max(0, int(data.get("total", 0) or 0))
    current = max(0, int(data.get("current", 0) or 0))
    data["percent"] = max(0, min(100, round(current * 100 / total))) if total > 0 else 0
    if started_at:
        end = finished_at or now
        data["elapsed_sec"] = max(0.0, end - started_at)
    return data


def set_save_progress(stage, title, detail="", current=0, total=0, active=True):
    with _save_progress_lock:
        _save_progress.update({
            "active": active,
            "stage": stage,
            "title": title,
            "detail": detail,
            "current": int(current),
            "total": int(total),
        })


def get_save_progress():
    with _save_progress_lock:
        data = dict(_save_progress)
    total = data.get("total", 0)
    current = data.get("current", 0)
    if total > 0:
        data["percent"] = max(0, min(100, round(current * 100 / total)))
    else:
        data["percent"] = None
    return data


def _safe_upload_rel_path(raw_path: str, fallback_name: str) -> str:
    """规范化上传资源的相对路径，阻止路径穿越。"""
    candidate = (raw_path or fallback_name or "").replace("\\", "/").strip()
    candidate = candidate.lstrip("/")
    if not candidate:
        candidate = Path(fallback_name or "upload.bin").name

    path = PurePosixPath(candidate)
    if any(part in ("", ".", "..") for part in path.parts):
        return Path(fallback_name or "upload.bin").name
    return str(path)


def _inspect_urdf(urdf_path: Path):
    """解析 URDF 基本信息，用于前端做关节匹配和状态提示。

    返回每个关节的类型、轴向和限位信息，供前端做单位检测和映射诊断。
    """
    root = ET.parse(urdf_path).getroot()
    if root.tag != "robot":
        raise ValueError("URDF 根节点必须是 <robot>")

    joints = []
    movable_joints = []
    joint_info = {}
    for joint in root.findall("joint"):
        name = (joint.get("name") or "").strip()
        joint_type = (joint.get("type") or "").strip().lower()
        if not name:
            continue
        joints.append(name)

        info = {"type": joint_type}
        axis_el = joint.find("axis")
        if axis_el is not None:
            try:
                info["axis"] = [float(x) for x in
                                (axis_el.get("xyz") or "0 0 1").split()]
            except ValueError:
                pass
        limit_el = joint.find("limit")
        if limit_el is not None:
            try:
                info["lower"] = float(limit_el.get("lower", 0))
                info["upper"] = float(limit_el.get("upper", 0))
            except ValueError:
                pass
        joint_info[name] = info

        if joint_type != "fixed":
            movable_joints.append(name)

    return {
        "robot_name": (root.get("name") or urdf_path.stem).strip() or urdf_path.stem,
        "joint_names": joints,
        "movable_joint_names": movable_joints,
        "joint_info": joint_info,
    }

# 默认关节配置 (CR100 双臂灵巧手)，仅当数据集维度恰好匹配时使用
_DEFAULT_JOINT_GROUPS = {
    "left_arm": [
        "left_shoulder_pitch", "left_shoulder_roll", "left_shoulder_yaw",
        "left_elbow", "left_wrist_roll", "left_wrist_pitch", "left_wrist_yaw",
    ],
    "left_hand": [
        "left_thumb_prox", "left_thumb_meta", "left_index_prox",
        "left_middle_prox", "left_ring_prox", "left_pinky_prox",
    ],
    "right_arm": [
        "right_shoulder_pitch", "right_shoulder_roll", "right_shoulder_yaw",
        "right_elbow", "right_wrist_roll", "right_wrist_pitch", "right_wrist_yaw",
    ],
    "right_hand": [
        "right_thumb_prox", "right_thumb_meta", "right_index_prox",
        "right_middle_prox", "right_ring_prox", "right_pinky_prox",
    ],
}
_DEFAULT_JOINT_NAMES = []
for _g in ["left_arm", "left_hand", "right_arm", "right_hand"]:
    _DEFAULT_JOINT_NAMES.extend(_DEFAULT_JOINT_GROUPS[_g])

# 关节自动分组关键词
_ARM_TOKENS = frozenset({"shoulder", "elbow", "wrist"})
_HAND_TOKENS = frozenset({
    "thumb", "index", "middle", "ring", "pinky",
    "finger", "grip", "grasp", "prox", "meta", "distal",
})
_HEAD_TOKENS = frozenset({"head", "neck", "jaw"})
_TORSO_TOKENS = frozenset({"torso", "spine", "waist", "hip", "pelvis", "chest"})
_LEG_TOKENS = frozenset({"knee", "ankle", "foot", "toe", "thigh", "shin"})

_joint_config_override = None


# ═══════════════════════ DatasetEditor ═══════════════════════

class DatasetEditor:
    """LeRobot v2.1 数据集的加载、编辑和保存"""
    QUANTILES = (0.01, 0.10, 0.50, 0.90, 0.99)
    FULL_RESOLUTION_POSITION_VELOCITY_PREVIEW = True

    def __init__(self, dataset_path: str, joint_config: str = None):
        self.root = Path(dataset_path).resolve()
        self.original_root = self.root
        self._joint_config_path = joint_config
        self.joint_constraints = {}
        self.joint_constraint_source = ""
        self.info = {}
        self.episodes_meta = []
        self.tasks = []
        self.episode_data = {}          # ep_idx -> pd.DataFrame
        self._orig_indices = {}         # current_ep_idx -> original_ep_idx
        self._orig_video_files = {}     # original_ep_idx -> {cam_name: abs_path}
        self._orig_ep_lengths = {}      # original_ep_idx -> 原始帧数
        self.modified = False
        self._load()
        self.joint_names, self.joint_groups = self._detect_joint_info()

    # ─── 加载 ───

    def _load(self):
        meta = self.root / "meta"

        # info.json
        with open(meta / "info.json") as f:
            self.info = json.load(f)

        # episodes.jsonl
        self.episodes_meta = self._read_jsonl(meta / "episodes.jsonl")

        # tasks.jsonl
        self.tasks = self._read_jsonl(meta / "tasks.jsonl")

        # Parquet 数据
        self._load_parquet_data()

        # 修正 episodes_meta 与实际数据一致
        self._reconcile_metadata()

        # 视频文件映射
        self._scan_video_files()

        log.info(
            f"已加载: {len(self.episodes_meta)} episodes, "
            f"{sum(len(d) for d in self.episode_data.values())} 帧, "
            f"{sum(len(v) for v in self._orig_video_files.values())} 个视频文件"
        )

    @staticmethod
    def _read_jsonl(path):
        items = []
        if path.exists():
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        items.append(json.loads(line))
        return items

    def _load_parquet_data(self):
        """扫描 data/ 目录加载所有 episode 的 parquet 文件"""
        data_dir = self.root / "data"
        if not data_dir.exists():
            log.warning(f"data/ 目录不存在: {data_dir}")
            return

        for pq_file in sorted(data_dir.rglob("*.parquet")):
            ep_idx = self._parse_episode_index(pq_file.stem)
            if ep_idx is not None:
                try:
                    df = pd.read_parquet(pq_file)
                    df = self._normalize_parquet_df(df, pq_file)
                    if "frame_index" in df.columns:
                        df["_orig_frame_idx"] = df["frame_index"].copy()
                    else:
                        df["_orig_frame_idx"] = range(len(df))
                    self.episode_data[ep_idx] = df
                    self._orig_indices[ep_idx] = ep_idx
                    self._orig_ep_lengths[ep_idx] = len(df)
                except Exception as e:
                    log.warning(f"读取 {pq_file} 失败: {e}")

    @staticmethod
    def _split_vector_component_columns(df, vector_key):
        """查找形如 observation.state_0 / action_3 的拆列向量字段。"""
        prefix = f"{vector_key}_"
        pairs = []
        for col in df.columns:
            if not col.startswith(prefix):
                continue
            suffix = col[len(prefix):]
            if suffix.isdigit():
                pairs.append((int(suffix), col))
        pairs.sort(key=lambda item: item[0])
        return [col for _, col in pairs]

    @staticmethod
    def _materialize_vector_column(df, vector_key, source_path=None):
        """将拆列的向量字段重新拼成 LeRobot 标准 list/ndarray 列。"""
        if vector_key in df.columns:
            return df

        component_cols = DatasetEditor._split_vector_component_columns(df, vector_key)
        if not component_cols:
            return df

        arr = df[component_cols].to_numpy(dtype=np.float64, copy=True)
        df[vector_key] = [row.copy() for row in arr]
        df = df.drop(columns=component_cols)

        if source_path is not None:
            log.info(
                f"从拆列字段恢复 {vector_key}: {source_path} "
                f"({len(component_cols)} 维)"
            )
        return df

    @staticmethod
    def _normalize_parquet_df(df, source_path=None):
        """兼容不同写法的 LeRobot 向量列。"""
        df = DatasetEditor._materialize_vector_column(
            df, "observation.state", source_path
        )
        df = DatasetEditor._materialize_vector_column(df, "action", source_path)
        return df

    def _reconcile_metadata(self):
        """确保 episodes_meta 与实际 parquet 数据一致"""
        known = {em["episode_index"] for em in self.episodes_meta}

        # 为没有元数据的 parquet 文件创建默认条目
        for ep_idx in sorted(self.episode_data.keys()):
            if ep_idx not in known:
                self.episodes_meta.append({
                    "episode_index": ep_idx,
                    "length": len(self.episode_data[ep_idx]),
                    "task_index": 0,
                })

        # 删除没有数据的元数据条目, 并更新 length
        valid_meta = []
        for em in self.episodes_meta:
            idx = em["episode_index"]
            if idx in self.episode_data:
                em["length"] = len(self.episode_data[idx])
                valid_meta.append(em)
        self.episodes_meta = sorted(valid_meta, key=lambda x: x["episode_index"])

    def _scan_video_files(self):
        """扫描 videos/ 目录建立视频文件映射"""
        video_dir = self.root / "videos"
        if not video_dir.exists():
            return

        for vf in sorted(video_dir.rglob("*.mp4")):
            ep_idx = self._parse_episode_index(vf.stem)
            if ep_idx is None:
                continue
            rel_parts = vf.relative_to(video_dir).parts
            cam_key = None
            if len(rel_parts) >= 3 and rel_parts[0].startswith("chunk-"):
                # v2.1 chunk-first: videos/chunk-000/observation.images.cam/episode_xxxxxx.mp4
                cam_key = rel_parts[1]
            elif len(rel_parts) >= 3 and rel_parts[1].startswith("chunk-"):
                # 兼容 key-first: videos/observation.images.cam/chunk-000/file-000.mp4
                cam_key = rel_parts[0]
            else:
                cam_key = vf.parent.name
            if cam_key.startswith("chunk-"):
                continue
            cam_name = cam_key.split(".")[-1] if "." in cam_key else cam_key
            self._orig_video_files.setdefault(ep_idx, {})[cam_name] = str(vf)

    # ─── 关节自动检测 ───

    def _detect_joint_info(self):
        """从配置文件、数据集 features 或命名规则自动识别关节名与分组。

        优先级: CLI 配置 > 数据集目录配置 > info.json names > 维度匹配默认 > 通用编号
        """
        config = self._try_load_joint_config()
        if config is not None:
            return config

        names = self._extract_feature_names("observation.state")
        if names:
            groups = self._auto_group_joints(names)
            log.info(f"从 info.json features 提取 {len(names)} 个关节名, "
                     f"自动分为 {len(groups)} 组: "
                     + ", ".join(f"{k}({len(v)})" for k, v in groups.items()))
            return names, groups

        dim = self._infer_state_dim()
        if dim and dim == len(_DEFAULT_JOINT_NAMES):
            log.info(f"状态维度 {dim} 匹配默认 CR100 配置")
            return (list(_DEFAULT_JOINT_NAMES),
                    {k: list(v) for k, v in _DEFAULT_JOINT_GROUPS.items()})
        if dim and dim > 0:
            names = [f"joint_{i}" for i in range(dim)]
            groups = {"all": list(names)}
            log.info(f"状态维度 {dim}, 生成 joint_0..joint_{dim-1} "
                     "(可在数据集目录放置 joint_config.json 自定义)")
            return names, groups

        log.info("无法推断关节信息，使用默认 CR100 配置")
        return (list(_DEFAULT_JOINT_NAMES),
                {k: list(v) for k, v in _DEFAULT_JOINT_GROUPS.items()})

    def _try_load_joint_config(self):
        """尝试从配置文件加载关节信息，返回 (names, groups) 或 None。"""
        candidates = []
        if self._joint_config_path:
            candidates.append(Path(self._joint_config_path))
        candidates.append(self.root / "joint_config.json")
        candidates.append(self.root / "meta" / "joint_config.json")

        for path in candidates:
            if not path.exists():
                continue
            try:
                with open(path) as f:
                    cfg = json.load(f)
                names = cfg.get("joint_names", [])
                groups = cfg.get("joint_groups", {})
                if groups and not names:
                    names = [j for joints in groups.values() for j in joints]
                if names and not groups:
                    groups = self._auto_group_joints(names)
                constraints = self._parse_joint_constraints_config(cfg, names)
                if constraints:
                    self.joint_constraints = constraints
                    self.joint_constraint_source = str(path)
                if names and groups:
                    log.info(f"从 {path} 加载关节配置: "
                             f"{len(names)} 关节, {len(groups)} 组")
                    return names, groups
            except Exception as e:
                log.warning(f"读取关节配置 {path} 失败: {e}")
        return None

    def _extract_feature_names(self, feature_key):
        """从 info.json features 中提取指定特征的名称列表。"""
        feat = self.info.get("features", {}).get(feature_key, {})
        raw = feat.get("names")
        if raw is None:
            return None

        names = None
        if isinstance(raw, list):
            if raw and isinstance(raw[0], list):
                names = raw[0]
            elif raw and isinstance(raw[0], str):
                names = raw
        elif isinstance(raw, dict):
            for v in raw.values():
                if isinstance(v, list) and v and isinstance(v[0], str):
                    names = v
                    break

        if names and all(isinstance(n, str) and n.strip() for n in names):
            return [n.strip() for n in names]
        return None

    def _infer_state_dim(self):
        """从 info.json shape 或实际数据推断 observation.state 维度。"""
        return self._infer_feature_dim("observation.state")

    def _infer_feature_dim(self, feature_key):
        """从 info.json shape 或实际数据推断向量特征维度。"""
        shape = (self.info.get("features", {})
                 .get(feature_key, {})
                 .get("shape"))
        if isinstance(shape, list) and shape:
            try:
                return int(shape[-1])
            except (TypeError, ValueError):
                return None

        for df in self.episode_data.values():
            if feature_key not in df.columns:
                continue
            for sample in df[feature_key].tolist():
                sample_list = self._to_list(sample)
                if sample_list:
                    return len(sample_list)
        return None

    @staticmethod
    def _auto_group_joints(names):
        """根据关节命名中的关键词自动分组。

        识别 side (left/right) 和 part (arm/hand/head/torso/leg)，
        组合为 'left_arm'、'right_hand' 等分组键。
        """
        groups = {}
        for name in names:
            tokens = set(name.lower().replace("-", "_").split("_"))

            side = ""
            if "left" in tokens:
                side = "left"
            elif "right" in tokens:
                side = "right"

            part = ""
            if tokens & _ARM_TOKENS:
                part = "arm"
            elif tokens & _HAND_TOKENS:
                part = "hand"
            elif tokens & _HEAD_TOKENS:
                part = "head"
            elif tokens & _TORSO_TOKENS:
                part = "torso"
            elif tokens & _LEG_TOKENS:
                part = "leg"

            if side and part:
                key = f"{side}_{part}"
            elif part:
                key = part
            elif side:
                key = side
            else:
                key = "other"

            groups.setdefault(key, []).append(name)

        return groups if groups else {"all": list(names)}

    @staticmethod
    def _parse_episode_index(stem: str):
        """从文件名 stem 中提取 episode 索引, 如 'episode_000003' -> 3"""
        if not stem.startswith("episode_"):
            return None
        try:
            return int(stem.split("_", 1)[1])
        except (ValueError, IndexError):
            return None

    # ─── 查询 ───

    def get_summary(self):
        features = self.info.get("features", {})
        cameras = [
            k.split(".")[-1]
            for k, meta in features.items()
            if meta.get("dtype") in ("image", "video")
        ]
        return {
            "path": str(self.root),
            "fps": self.info.get("fps", 30),
            "robot_type": self.info.get("robot_type", "unknown"),
            "total_episodes": len(self.episodes_meta),
            "total_frames": sum(len(d) for d in self.episode_data.values()),
            "cameras": cameras,
            "modified": self.modified,
        }

    def get_episodes(self):
        result = []
        for em in self.episodes_meta:
            idx = em["episode_index"]
            length = em.get("length",
                            len(self.episode_data[idx]) if idx in self.episode_data else 0)
            result.append({
                "episode_index": idx,
                "length": length,
                "task_index": em.get("task_index", 0),
            })
        return result

    def get_episode_data(self, ep_idx):
        """获取单个 episode 的完整帧数据"""
        if ep_idx not in self.episode_data:
            return None

        df = self.episode_data[ep_idx]
        n = len(df)
        frames = []

        frame_indices = df["frame_index"].tolist() if "frame_index" in df.columns else list(range(n))
        timestamps = df["timestamp"].tolist() if "timestamp" in df.columns else [0.0] * n

        has_state = "observation.state" in df.columns
        has_action = "action" in df.columns
        states = df["observation.state"].tolist() if has_state else [[] for _ in range(n)]
        actions = df["action"].tolist() if has_action else [[] for _ in range(n)]

        for i in range(n):
            frames.append({
                "frame_index": int(frame_indices[i]),
                "timestamp": float(timestamps[i]) if timestamps[i] is not None else 0.0,
                "state": self._to_list(states[i]),
                "action": self._to_list(actions[i]),
            })

        videos = self._get_video_urls(ep_idx)
        return {"episode_index": ep_idx, "frames": frames, "videos": videos}

    def _get_video_urls(self, ep_idx):
        orig_idx = self._orig_indices.get(ep_idx, ep_idx)
        vfiles = self._orig_video_files.get(orig_idx, {})
        return {cam: f"/api/video?path={path}" for cam, path in vfiles.items()}

    @staticmethod
    def _to_list(val):
        if val is None:
            return []
        if isinstance(val, np.ndarray):
            return val.astype(float).tolist()
        if isinstance(val, (list, tuple)):
            try:
                return [float(x) for x in val]
            except (TypeError, ValueError):
                return []
        return []

    @staticmethod
    def _format_group_label(group_key: str):
        parts = [part.capitalize() for part in str(group_key).split("_") if part]
        return " ".join(parts) if parts else "Other"

    @staticmethod
    def _normalize_joint_key(name):
        return str(name or "").strip().lower().replace("-", "_").replace(" ", "_")

    @staticmethod
    def _to_float_or_none(value):
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        if np.isfinite(result):
            return result
        return None

    @classmethod
    def _map_axis_scalar_values(cls, raw, axis_names):
        if raw is None:
            return {}

        axis_names = list(axis_names or [])
        result = {}
        if isinstance(raw, list):
            for idx, value in enumerate(raw[:len(axis_names)]):
                scalar = cls._to_float_or_none(value)
                if scalar is None:
                    continue
                result[axis_names[idx]] = scalar
            return result

        if not isinstance(raw, dict):
            return result

        norm_lookup = {cls._normalize_joint_key(name): name for name in axis_names}
        for key, value in raw.items():
            scalar = cls._to_float_or_none(value)
            if scalar is None:
                continue
            target = None
            if key in axis_names:
                target = key
            else:
                target = norm_lookup.get(cls._normalize_joint_key(key))
                if target is None:
                    try:
                        idx = int(key)
                    except (TypeError, ValueError):
                        idx = None
                    if idx is not None and 0 <= idx < len(axis_names):
                        target = axis_names[idx]
            if target is not None:
                result[target] = scalar
        return result

    @classmethod
    def _parse_joint_constraints_config(cls, cfg, axis_names):
        containers = []
        for key in ("joint_constraints", "joint_limits", "constraints"):
            raw = cfg.get(key)
            if isinstance(raw, dict):
                containers.append(raw)

        if not containers:
            return {}

        constraint_keys = {
            "lower": ("lower", "min_position"),
            "upper": ("upper", "max_position"),
            "velocity_limit": ("velocity", "velocity_limit", "max_velocity"),
            "torque_limit": ("torque", "torque_limit", "effort", "max_torque"),
        }

        axis_names = list(axis_names or [])
        norm_lookup = {cls._normalize_joint_key(name): name for name in axis_names}
        result = {}
        for container in containers:
            for raw_name, payload in container.items():
                if not isinstance(payload, dict):
                    continue
                target_name = raw_name if raw_name in axis_names else norm_lookup.get(
                    cls._normalize_joint_key(raw_name)
                )
                if not target_name:
                    continue
                target = result.setdefault(target_name, {})
                for dst_key, aliases in constraint_keys.items():
                    for alias in aliases:
                        if alias not in payload:
                            continue
                        scalar = cls._to_float_or_none(payload.get(alias))
                        if scalar is not None:
                            target[dst_key] = scalar
                        break
        return {name: data for name, data in result.items() if data}

    def _resolve_feature_axis_names(self, feature_key, dim, fallback_prefix):
        if dim <= 0:
            return []

        names = self._extract_feature_names(feature_key)
        if names and len(names) == dim:
            return list(names)

        if feature_key == "action" and dim == len(self.joint_names):
            return list(self.joint_names)
        if feature_key == "observation.state" and dim == len(self.joint_names):
            return list(self.joint_names)

        return [f"{fallback_prefix}_{i}" for i in range(dim)]

    def _collect_feature_series(self, feature_key, fallback_prefix):
        """汇总指定向量特征，返回全量矩阵、轴名和按 episode 拆分的时序。"""
        dim = self._infer_feature_dim(feature_key)
        if not dim or dim <= 0:
            return np.empty((0, 0), dtype=np.float64), [], []

        axis_names = self._resolve_feature_axis_names(feature_key, dim, fallback_prefix)
        rows = []
        episodes = []
        for ep_idx, df in sorted(self.episode_data.items()):
            if feature_key not in df.columns:
                continue
            ep_rows = []
            ep_timestamps = []
            timestamps = df["timestamp"].tolist() if "timestamp" in df.columns else [None] * len(df)
            for row_idx, value in enumerate(df[feature_key].tolist()):
                row = self._to_list(value)
                if len(row) != dim:
                    continue
                rows.append(row)
                ep_rows.append(row)
                ts_val = timestamps[row_idx] if row_idx < len(timestamps) else None
                ep_timestamps.append(self._to_float_or_none(ts_val))
            if ep_rows:
                episodes.append({
                    "episode_index": int(ep_idx),
                    "values": np.asarray(ep_rows, dtype=np.float64),
                    "timestamps": np.asarray([
                        np.nan if ts is None else ts for ts in ep_timestamps
                    ], dtype=np.float64),
                })

        if not rows:
            return np.empty((0, dim), dtype=np.float64), axis_names, episodes

        matrix = np.asarray(rows, dtype=np.float64)
        return matrix, axis_names, episodes

    def _collect_feature_matrix(self, feature_key, fallback_prefix):
        matrix, axis_names, _episodes = self._collect_feature_series(
            feature_key, fallback_prefix
        )
        return matrix, axis_names

    @staticmethod
    def _build_histogram(values, bins=20):
        values = np.asarray(values, dtype=np.float64).reshape(-1)
        values = values[np.isfinite(values)]
        if values.size == 0:
            return {"bins": [], "max_count": 0}

        vmin = float(values.min())
        vmax = float(values.max())
        if np.isclose(vmin, vmax):
            return {
                "bins": [{
                    "start": round(vmin - 0.5, 6),
                    "end": round(vmax + 0.5, 6),
                    "count": int(values.size),
                }],
                "max_count": int(values.size),
            }

        hist_bins = min(int(bins), max(8, int(np.sqrt(values.size))))
        counts, edges = np.histogram(values, bins=hist_bins)
        result_bins = []
        for i, count in enumerate(counts):
            result_bins.append({
                "start": round(float(edges[i]), 6),
                "end": round(float(edges[i + 1]), 6),
                "count": int(count),
            })
        return {
            "bins": result_bins,
            "max_count": int(counts.max()) if len(counts) else 0,
        }

    @classmethod
    def _compute_joint_metric_bundle(cls, values):
        arr = np.asarray(values, dtype=np.float64).reshape(-1)
        arr = arr[np.isfinite(arr)]
        if arr.size == 0:
            return None

        mean = float(arr.mean())
        std = float(arr.std())
        variance = float(arr.var())
        sigma1_low = mean - std
        sigma1_high = mean + std
        sigma2_low = mean - 2 * std
        sigma2_high = mean + 2 * std

        within_sigma1 = float(np.mean((arr >= sigma1_low) & (arr <= sigma1_high)))
        within_sigma2 = float(np.mean((arr >= sigma2_low) & (arr <= sigma2_high)))

        return {
            "count": int(arr.size),
            "mean": mean,
            "variance": variance,
            "std": std,
            "min": float(arr.min()),
            "max": float(arr.max()),
            "range": float(arr.max() - arr.min()),
            "q01": float(np.quantile(arr, 0.01)),
            "q10": float(np.quantile(arr, 0.10)),
            "q25": float(np.quantile(arr, 0.25)),
            "q50": float(np.quantile(arr, 0.50)),
            "q75": float(np.quantile(arr, 0.75)),
            "q90": float(np.quantile(arr, 0.90)),
            "q99": float(np.quantile(arr, 0.99)),
            "sigma_1": {
                "lower": sigma1_low,
                "upper": sigma1_high,
                "coverage_ratio": within_sigma1,
            },
            "sigma_2": {
                "lower": sigma2_low,
                "upper": sigma2_high,
                "coverage_ratio": within_sigma2,
            },
            "histogram": cls._build_histogram(arr),
        }

    def _extract_feature_constraints(self, feature_key, axis_names):
        feature_meta = self.info.get("features", {}).get(feature_key, {})
        if not isinstance(feature_meta, dict):
            return {}

        keys = {
            "lower": ("lower",),
            "upper": ("upper",),
            "velocity_limit": ("velocity", "velocity_limit", "max_velocity"),
            "torque_limit": ("torque", "torque_limit", "max_torque", "effort"),
        }
        result = {}
        containers = [feature_meta]
        if isinstance(feature_meta.get("limits"), dict):
            containers.append(feature_meta.get("limits"))

        for container in containers:
            for dst_key, aliases in keys.items():
                raw = None
                for alias in aliases:
                    if alias in container:
                        raw = container.get(alias)
                        break
                mapped = self._map_axis_scalar_values(raw, axis_names)
                for axis_name, value in mapped.items():
                    result.setdefault(axis_name, {})[dst_key] = value
        return {name: data for name, data in result.items() if data}

    def _resolve_joint_constraint(self, joint_name, source_name, feature_constraints):
        candidates = [joint_name, source_name]
        norm_candidates = {self._normalize_joint_key(name) for name in candidates if name}
        merged = {}
        merged_sources = []

        def merge_from(mapping, source_label):
            for key, payload in mapping.items():
                if self._normalize_joint_key(key) not in norm_candidates or not isinstance(payload, dict):
                    continue
                for field in ("lower", "upper", "velocity_limit", "torque_limit"):
                    if field not in merged and field in payload:
                        merged[field] = payload[field]
                merged_sources.append(source_label)
                break

        if self.joint_constraints:
            merge_from(self.joint_constraints, "config")
        if feature_constraints:
            merge_from(feature_constraints, "feature")

        if merged and merged_sources:
            merged["source"] = "+".join(sorted(set(merged_sources)))
        return merged or None

    def _sanitize_episode_timestamps(self, timestamps, count):
        nominal_dt = 1.0 / max(float(self.info.get("fps", 30) or 30), 1e-6)
        if count <= 0:
            return np.empty((0,), dtype=np.float64)
        ts = np.asarray(timestamps, dtype=np.float64).reshape(-1)
        if ts.size != count or not np.all(np.isfinite(ts)):
            return np.arange(count, dtype=np.float64) * nominal_dt
        diffs = np.diff(ts)
        if diffs.size and np.any(diffs <= 0):
            return np.arange(count, dtype=np.float64) * nominal_dt
        return ts

    def _compute_temporal_metric_bundle(self, episodes, joint_idx):
        nominal_dt = 1.0 / max(float(self.info.get("fps", 30) or 30), 1e-6)
        delta_abs_parts = []
        delta2_abs_parts = []
        vel_abs_parts = []
        acc_abs_parts = []
        jerk_abs_parts = []
        for episode in episodes:
            values = episode["values"][:, joint_idx]
            if values.size < 2:
                continue
            timestamps = self._sanitize_episode_timestamps(
                episode.get("timestamps", []), len(values)
            )
            dt = np.diff(timestamps)
            dt = np.where(dt > 1e-9, dt, nominal_dt)
            delta = np.diff(values)
            delta_abs_parts.append(np.abs(delta))
            velocity = delta / dt
            vel_abs_parts.append(np.abs(velocity))
            if values.size >= 3:
                delta2 = np.diff(values, n=2)
                delta2_abs = np.abs(delta2)
                delta2_abs_parts.append(delta2_abs)
                acc_dt = np.diff(timestamps)[1:]
                acc_dt = np.where(acc_dt > 1e-9, acc_dt, nominal_dt)
                acceleration = np.diff(velocity) / acc_dt
                acc_abs_parts.append(np.abs(acceleration))
                if acceleration.size >= 2:
                    jerk_dt = acc_dt[1:] if acc_dt.size >= 2 else np.full(
                        acceleration.size - 1, nominal_dt, dtype=np.float64
                    )
                    jerk_dt = np.where(jerk_dt > 1e-9, jerk_dt, nominal_dt)
                    jerk = np.diff(acceleration) / jerk_dt
                    jerk_abs_parts.append(np.abs(jerk))

        if not delta_abs_parts:
            return None

        delta_abs = np.concatenate(delta_abs_parts) if delta_abs_parts else np.empty((0,))
        delta2_abs = np.concatenate(delta2_abs_parts) if delta2_abs_parts else np.empty((0,))
        vel_abs = np.concatenate(vel_abs_parts) if vel_abs_parts else np.empty((0,))
        acc_abs = np.concatenate(acc_abs_parts) if acc_abs_parts else np.empty((0,))
        jerk_abs = np.concatenate(jerk_abs_parts) if jerk_abs_parts else np.empty((0,))

        spike_ratio = None
        if delta2_abs.size:
            threshold = float(delta2_abs.mean() + 3.0 * delta2_abs.std())
            spike_ratio = float(np.mean(delta2_abs > threshold)) if threshold > 0 else 0.0

        return {
            "samples": int(delta_abs.size),
            "smoothness": float(delta_abs.mean()) if delta_abs.size else None,
            "max_delta": float(delta_abs.max()) if delta_abs.size else None,
            "jerk": float(delta2_abs.mean()) if delta2_abs.size else None,
            "max_jerk": float(delta2_abs.max()) if delta2_abs.size else None,
            "velocity_abs_mean": float(vel_abs.mean()) if vel_abs.size else None,
            "velocity_abs_max": float(vel_abs.max()) if vel_abs.size else None,
            "acceleration_abs_mean": float(acc_abs.mean()) if acc_abs.size else None,
            "acceleration_abs_max": float(acc_abs.max()) if acc_abs.size else None,
            "jerk_abs_mean": float(jerk_abs.mean()) if jerk_abs.size else None,
            "jerk_abs_max": float(jerk_abs.max()) if jerk_abs.size else None,
            "spike_ratio": spike_ratio,
        }

    @staticmethod
    def _compute_anomaly_threshold(values, mad_scale=6.0, std_scale=3.0):
        arr = np.asarray(values, dtype=np.float64).reshape(-1)
        arr = arr[np.isfinite(arr)]
        if arr.size == 0:
            return None

        abs_arr = np.abs(arr)
        median_abs = float(np.median(abs_arr))
        mad_abs = float(np.median(np.abs(abs_arr - median_abs)))
        robust_threshold = median_abs + mad_scale * mad_abs

        mean_abs = float(abs_arr.mean())
        std_abs = float(abs_arr.std())
        fallback_threshold = mean_abs + std_scale * std_abs

        candidates = [value for value in (robust_threshold, fallback_threshold) if np.isfinite(value) and value > 0]
        if not candidates:
            return float(abs_arr.max()) if abs_arr.size else None
        return float(max(candidates))

    @staticmethod
    def _downsample_xy(x, y, max_points=96, anomaly_mask=None, threshold=None):
        x = np.asarray(x, dtype=np.float64).reshape(-1)
        y = np.asarray(y, dtype=np.float64).reshape(-1)
        if x.size == 0 or y.size == 0 or x.size != y.size:
            return {
                "x": [],
                "y": [],
                "anomaly": [],
                "anomaly_count": 0,
                "threshold": None,
            }

        mask = np.isfinite(x) & np.isfinite(y)
        x = x[mask]
        y = y[mask]
        if anomaly_mask is None:
            anomaly_mask = np.zeros(x.shape, dtype=bool)
        else:
            anomaly_mask = np.asarray(anomaly_mask, dtype=bool).reshape(-1)
            anomaly_mask = anomaly_mask[mask] if anomaly_mask.size == mask.size else np.zeros(x.shape, dtype=bool)
        if x.size == 0:
            return {
                "x": [],
                "y": [],
                "anomaly": [],
                "anomaly_count": 0,
                "threshold": None,
            }

        selected_idx = np.arange(x.size)
        if x.size > max_points:
            anomaly_idx = np.flatnonzero(anomaly_mask)
            if anomaly_idx.size >= max_points:
                selected_idx = np.unique(
                    np.linspace(0, anomaly_idx.size - 1, max_points).round().astype(int)
                )
                selected_idx = anomaly_idx[selected_idx]
            else:
                regular_count = max_points - anomaly_idx.size
                regular_idx = np.linspace(0, x.size - 1, regular_count).round().astype(int)
                selected_idx = np.unique(np.concatenate([regular_idx, anomaly_idx]))
            x = x[selected_idx]
            y = y[selected_idx]
            anomaly_mask = anomaly_mask[selected_idx]

        return {
            "x": [round(float(v), 6) for v in x.tolist()],
            "y": [round(float(v), 6) for v in y.tolist()],
            "anomaly": anomaly_mask.astype(bool).tolist(),
            "anomaly_count": int(np.sum(anomaly_mask)),
            "threshold": round(float(threshold), 6) if threshold is not None and np.isfinite(threshold) else None,
        }

    @staticmethod
    def _select_preview_indices(length, max_points=96, priority_mask=None):
        if length <= 0:
            return np.empty((0,), dtype=np.int64)

        selected_idx = np.arange(length, dtype=np.int64)
        if length <= max_points:
            return selected_idx

        if priority_mask is None:
            priority_idx = np.empty((0,), dtype=np.int64)
        else:
            priority_mask = np.asarray(priority_mask, dtype=bool).reshape(-1)
            priority_idx = (
                np.flatnonzero(priority_mask)
                if priority_mask.size == length
                else np.empty((0,), dtype=np.int64)
            )

        if priority_idx.size >= max_points:
            sampled = np.linspace(0, priority_idx.size - 1, max_points).round().astype(int)
            return priority_idx[sampled]

        regular_count = max_points - priority_idx.size
        regular_idx = np.linspace(0, length - 1, regular_count).round().astype(int)
        return np.unique(np.concatenate([regular_idx, priority_idx]))

    @staticmethod
    def _slice_preview_xy(x, y, selected_idx, anomaly_mask=None, threshold=None):
        x = np.asarray(x, dtype=np.float64).reshape(-1)
        y = np.asarray(y, dtype=np.float64).reshape(-1)
        if x.size == 0 or y.size == 0 or x.size != y.size:
            return {
                "x": [],
                "y": [],
                "anomaly": [],
                "anomaly_count": 0,
                "threshold": None,
            }

        mask = np.isfinite(x) & np.isfinite(y)
        x = x[mask]
        y = y[mask]
        if anomaly_mask is None:
            anomaly_mask = np.zeros(x.shape, dtype=bool)
        else:
            anomaly_mask = np.asarray(anomaly_mask, dtype=bool).reshape(-1)
            anomaly_mask = anomaly_mask[mask] if anomaly_mask.size == mask.size else np.zeros(x.shape, dtype=bool)

        if x.size == 0:
            return {
                "x": [],
                "y": [],
                "anomaly": [],
                "anomaly_count": 0,
                "threshold": None,
            }

        selected_idx = np.asarray(selected_idx, dtype=np.int64).reshape(-1)
        selected_idx = selected_idx[(selected_idx >= 0) & (selected_idx < x.size)]
        if selected_idx.size == 0:
            selected_idx = np.arange(x.size, dtype=np.int64)

        x = x[selected_idx]
        y = y[selected_idx]
        anomaly_mask = anomaly_mask[selected_idx]

        return {
            "x": [round(float(v), 6) for v in x.tolist()],
            "y": [round(float(v), 6) for v in y.tolist()],
            "anomaly": anomaly_mask.astype(bool).tolist(),
            "anomaly_count": int(np.sum(anomaly_mask)),
            "threshold": round(float(threshold), 6) if threshold is not None and np.isfinite(threshold) else None,
        }

    @staticmethod
    def _smooth_values_at_velocity_anomalies(values, velocity_anomaly, window=5):
        values = np.asarray(values, dtype=np.float64).reshape(-1)
        velocity_anomaly = np.asarray(velocity_anomaly, dtype=bool).reshape(-1)
        if values.size < 3 or velocity_anomaly.size != values.size - 1:
            return None

        anomaly_idx = np.flatnonzero(velocity_anomaly)
        if anomaly_idx.size == 0:
            return None

        window = max(3, int(window or 5))
        if window % 2 == 0:
            window += 1
        radius = window // 2

        affected = np.zeros(values.shape, dtype=bool)
        for idx in anomaly_idx:
            center = idx + 1
            start = max(1, center - radius)
            end = min(values.size - 1, center + radius + 1)
            affected[start:end] = True

        smoothed = values.copy()
        affected_idx = np.flatnonzero(affected)
        if affected_idx.size == 0:
            return None

        segments = []
        seg_start = int(affected_idx[0])
        prev = int(affected_idx[0])
        for raw_idx in affected_idx[1:]:
            idx = int(raw_idx)
            if idx == prev + 1:
                prev = idx
                continue
            segments.append((seg_start, prev))
            seg_start = idx
            prev = idx
        segments.append((seg_start, prev))

        for start, end in segments:
            left_anchor = max(0, start - 1)
            right_anchor = min(values.size - 1, end + 1)
            span = right_anchor - left_anchor
            if span <= 0:
                continue
            for frame_idx in range(start, end + 1):
                alpha = (frame_idx - left_anchor) / span
                smoothed[frame_idx] = (
                    values[left_anchor] * (1.0 - alpha)
                    + values[right_anchor] * alpha
                )
        return smoothed, affected, anomaly_idx

    @staticmethod
    def _build_action_smoothing_preview(values, timestamps, selected_idx, velocity_anomaly, window=5):
        values = np.asarray(values, dtype=np.float64).reshape(-1)
        timestamps = np.asarray(timestamps, dtype=np.float64).reshape(-1)
        if timestamps.size != values.size:
            return None

        result = DatasetEditor._smooth_values_at_velocity_anomalies(
            values, velocity_anomaly, window=window
        )
        if result is None:
            return None

        smoothed, affected, anomaly_idx = result
        delta = smoothed - values

        position_mask = affected[1:]
        preview_position = DatasetEditor._slice_preview_xy(
            timestamps[1:],
            smoothed[1:],
            selected_idx,
            anomaly_mask=position_mask,
        )
        preview_delta = DatasetEditor._slice_preview_xy(
            timestamps[1:],
            delta[1:],
            selected_idx,
            anomaly_mask=position_mask,
        )

        affected_delta = np.abs(delta[affected])
        return {
            "method": "boundary_linear_interpolation",
            "window": int(window),
            "source": "velocity_anomaly",
            "anomaly_count": int(anomaly_idx.size),
            "affected_frame_count": int(np.sum(affected)),
            "position": preview_position,
            "delta": preview_delta,
            "max_abs_delta": (
                round(float(affected_delta.max()), 6) if affected_delta.size else 0.0
            ),
            "mean_abs_delta": (
                round(float(affected_delta.mean()), 6) if affected_delta.size else 0.0
            ),
        }

    def smooth_action_jumps(self, window=5):
        """Apply the same velocity-anomaly based local smoothing to all action rows."""
        dim = self._infer_feature_dim("action")
        if not dim or dim <= 0:
            raise ValueError("当前数据集没有可平滑的 action 特征")

        window = max(3, int(window or 5))
        if window % 2 == 0:
            window += 1

        episode_reports = []
        total_anomalies = 0
        total_affected = 0
        max_abs_delta = 0.0

        for ep_idx, df in sorted(self.episode_data.items()):
            if "action" not in df.columns or len(df) < 2:
                continue

            action_rows = [self._to_list(value) for value in df["action"].tolist()]
            valid_rows = [row for row in action_rows if len(row) == dim]
            if len(valid_rows) != len(action_rows):
                continue

            values = np.asarray(action_rows, dtype=np.float64)
            timestamps = self._sanitize_episode_timestamps(
                [
                    np.nan if self._to_float_or_none(value) is None else self._to_float_or_none(value)
                    for value in (df["timestamp"].tolist() if "timestamp" in df.columns else [])
                ],
                len(values),
            )
            nominal_dt = 1.0 / max(float(self.info.get("fps", 30) or 30), 1e-6)
            dt = np.diff(timestamps)
            dt = np.where(dt > 1e-9, dt, nominal_dt)

            smoothed_values = values.copy()
            affected_any = np.zeros(len(values), dtype=bool)
            ep_anomalies = 0
            ep_max_delta = 0.0
            joint_reports = []

            for joint_idx in range(dim):
                velocity = np.diff(values[:, joint_idx]) / dt
                threshold = self._compute_anomaly_threshold(velocity)
                velocity_anomaly = (
                    np.abs(velocity) > threshold
                    if threshold is not None and velocity.size
                    else np.zeros(velocity.shape, dtype=bool)
                )
                result = self._smooth_values_at_velocity_anomalies(
                    values[:, joint_idx], velocity_anomaly, window=window
                )
                if result is None:
                    continue

                joint_smoothed, affected, anomaly_idx = result
                delta = joint_smoothed - values[:, joint_idx]
                smoothed_values[:, joint_idx] = joint_smoothed
                affected_any |= affected
                joint_max_delta = float(np.max(np.abs(delta[affected]))) if np.any(affected) else 0.0
                ep_anomalies += int(anomaly_idx.size)
                ep_max_delta = max(ep_max_delta, joint_max_delta)
                joint_reports.append({
                    "joint_index": int(joint_idx),
                    "anomaly_count": int(anomaly_idx.size),
                    "affected_frame_count": int(np.sum(affected)),
                    "max_abs_delta": round(joint_max_delta, 6),
                    "threshold": round(float(threshold), 6) if threshold is not None and np.isfinite(threshold) else None,
                })

            if ep_anomalies <= 0:
                continue

            df = df.copy()
            df["action"] = [row.copy() for row in smoothed_values]
            self.episode_data[ep_idx] = df

            affected_count = int(np.sum(affected_any))
            total_anomalies += ep_anomalies
            total_affected += affected_count
            max_abs_delta = max(max_abs_delta, ep_max_delta)
            episode_reports.append({
                "episode_index": int(ep_idx),
                "anomaly_count": int(ep_anomalies),
                "affected_frame_count": affected_count,
                "max_abs_delta": round(ep_max_delta, 6),
                "joints": joint_reports,
            })

        self.modified = bool(episode_reports)
        return {
            "window": int(window),
            "episodes_changed": len(episode_reports),
            "anomaly_count": int(total_anomalies),
            "affected_frame_count": int(total_affected),
            "max_abs_delta": round(float(max_abs_delta), 6),
            "episodes": episode_reports,
        }

    def _build_temporal_preview(self, episodes, joint_idx, include_smoothing=False):
        representative = None
        for episode in sorted(episodes, key=lambda item: item["values"].shape[0], reverse=True):
            if joint_idx < episode["values"].shape[1] and episode["values"].shape[0] >= 2:
                representative = episode
                break

        if representative is None:
            return None

        values = representative["values"][:, joint_idx]
        timestamps = self._sanitize_episode_timestamps(
            representative.get("timestamps", []), len(values)
        )
        nominal_dt = 1.0 / max(float(self.info.get("fps", 30) or 30), 1e-6)
        dt = np.diff(timestamps)
        dt = np.where(dt > 1e-9, dt, nominal_dt)
        velocity = np.diff(values) / dt if values.size >= 2 else np.empty((0,))

        if values.size >= 3:
            acc_dt = np.diff(timestamps)[1:]
            acc_dt = np.where(acc_dt > 1e-9, acc_dt, nominal_dt)
            acceleration = np.diff(velocity) / acc_dt
        else:
            acceleration = np.empty((0,))

        if acceleration.size >= 2:
            jerk_dt = np.diff(timestamps)[2:]
            jerk_dt = np.where(jerk_dt > 1e-9, jerk_dt, nominal_dt)
            jerk_dt = jerk_dt[: acceleration.size - 1]
            jerk = np.diff(acceleration) / jerk_dt
        else:
            jerk = np.empty((0,))

        velocity_threshold = self._compute_anomaly_threshold(velocity)
        acceleration_threshold = self._compute_anomaly_threshold(acceleration)
        jerk_threshold = self._compute_anomaly_threshold(jerk)
        velocity_anomaly = np.abs(velocity) > velocity_threshold if velocity_threshold is not None and velocity.size else np.zeros(velocity.shape, dtype=bool)
        acceleration_anomaly = (
            np.abs(acceleration) > acceleration_threshold
            if acceleration_threshold is not None and acceleration.size
            else np.zeros(acceleration.shape, dtype=bool)
        )
        jerk_anomaly = (
            np.abs(jerk) > jerk_threshold
            if jerk_threshold is not None and jerk.size
            else np.zeros(jerk.shape, dtype=bool)
        )

        # Keep the sampling path in place, but allow temporarily switching the
        # position/velocity preview to full resolution for debugging.
        if self.FULL_RESOLUTION_POSITION_VELOCITY_PREVIEW:
            shared_preview_idx = np.arange(velocity.size, dtype=np.int64)
        else:
            # Position and velocity should share the same sampled timestamps so their
            # visual comparison reflects the same moments in time.
            shared_preview_idx = self._select_preview_indices(
                velocity.size, max_points=96, priority_mask=velocity_anomaly
            )

        preview = {
            "episode_index": int(representative["episode_index"]),
            "frame_count": int(values.size),
            "position": self._slice_preview_xy(
                timestamps[1:], values[1:], shared_preview_idx
            ),
            "velocity": self._slice_preview_xy(
                timestamps[1:],
                velocity,
                shared_preview_idx,
                anomaly_mask=velocity_anomaly,
                threshold=velocity_threshold,
            ),
            "acceleration": self._downsample_xy(
                timestamps[2:], acceleration, anomaly_mask=acceleration_anomaly, threshold=acceleration_threshold
            ),
            "jerk": self._downsample_xy(
                timestamps[3:], jerk, anomaly_mask=jerk_anomaly, threshold=jerk_threshold
            ),
        }
        if include_smoothing:
            smoothing = self._build_action_smoothing_preview(
                values, timestamps, shared_preview_idx, velocity_anomaly, window=5
            )
            if smoothing:
                preview["smoothing"] = smoothing

        return preview

    def _compute_constraint_metric_bundle(self, episodes, joint_idx, constraint):
        if not constraint:
            return None

        lower = constraint.get("lower")
        upper = constraint.get("upper")
        velocity_limit = constraint.get("velocity_limit")
        torque_limit = constraint.get("torque_limit")
        nominal_dt = 1.0 / max(float(self.info.get("fps", 30) or 30), 1e-6)

        angle_total = 0
        angle_violations = 0
        velocity_total = 0
        velocity_violations = 0
        max_observed_velocity = None

        for episode in episodes:
            values = episode["values"][:, joint_idx]
            if values.size:
                if lower is not None or upper is not None:
                    invalid = np.zeros(values.shape, dtype=bool)
                    if lower is not None:
                        invalid |= values < lower
                    if upper is not None:
                        invalid |= values > upper
                    angle_violations += int(np.sum(invalid))
                    angle_total += int(values.size)

            if velocity_limit is not None and values.size >= 2:
                timestamps = self._sanitize_episode_timestamps(
                    episode.get("timestamps", []), len(values)
                )
                dt = np.diff(timestamps)
                dt = np.where(dt > 1e-9, dt, nominal_dt)
                velocity_abs = np.abs(np.diff(values) / dt)
                if velocity_abs.size:
                    local_max = float(np.max(velocity_abs))
                    max_observed_velocity = local_max if max_observed_velocity is None else max(
                        max_observed_velocity, local_max
                    )
                    velocity_violations += int(np.sum(velocity_abs > velocity_limit))
                    velocity_total += int(velocity_abs.size)

        available = any(value is not None for value in (lower, upper, velocity_limit, torque_limit))
        if not available:
            return None

        return {
            "available": True,
            "source": constraint.get("source"),
            "lower": lower,
            "upper": upper,
            "velocity_limit": velocity_limit,
            "torque_limit": torque_limit,
            "angle_out_of_range_ratio": (
                float(angle_violations / angle_total) if angle_total else None
            ),
            "angle_out_of_range_count": int(angle_violations),
            "angle_sample_count": int(angle_total),
            "velocity_out_of_range_ratio": (
                float(velocity_violations / velocity_total) if velocity_total else None
            ),
            "velocity_out_of_range_count": int(velocity_violations),
            "velocity_sample_count": int(velocity_total),
            "max_observed_velocity": max_observed_velocity,
        }

    def _summarize_temporal_module(self, joint_groups):
        per_source = {"state": [], "action": []}
        for group in joint_groups:
            for joint in group["joints"]:
                state_source = joint.get("state") or {}
                action_source = joint.get("action") or {}
                if state_source.get("temporal"):
                    per_source["state"].append({
                        "joint_name": joint["joint_name"],
                        **state_source["temporal"],
                    })
                if action_source.get("temporal"):
                    per_source["action"].append({
                        "joint_name": joint["joint_name"],
                        **action_source["temporal"],
                    })

        sources = {}
        for source_key, items in per_source.items():
            if not items:
                sources[source_key] = None
                continue
            sorted_by_jerk = sorted(
                [item for item in items if item.get("jerk") is not None],
                key=lambda item: item.get("jerk", -1),
                reverse=True,
            )
            sorted_by_velocity = sorted(
                [item for item in items if item.get("velocity_abs_max") is not None],
                key=lambda item: item.get("velocity_abs_max", -1),
                reverse=True,
            )
            sorted_by_acceleration = sorted(
                [item for item in items if item.get("acceleration_abs_max") is not None],
                key=lambda item: item.get("acceleration_abs_max", -1),
                reverse=True,
            )
            sorted_by_true_jerk = sorted(
                [item for item in items if item.get("jerk_abs_max") is not None],
                key=lambda item: item.get("jerk_abs_max", -1),
                reverse=True,
            )
            sources[source_key] = {
                "joint_count": len(items),
                "mean_smoothness": float(np.mean([
                    item["smoothness"] for item in items if item.get("smoothness") is not None
                ])),
                "mean_jerk": float(np.mean([
                    item["jerk"] for item in items if item.get("jerk") is not None
                ])) if any(item.get("jerk") is not None for item in items) else None,
                "top_jerk_joints": [
                    {
                        "joint_name": item["joint_name"],
                        "jerk": item.get("jerk"),
                        "spike_ratio": item.get("spike_ratio"),
                    }
                    for item in sorted_by_jerk[:5]
                ],
                "top_velocity_joints": [
                    {
                        "joint_name": item["joint_name"],
                        "velocity_abs_max": item.get("velocity_abs_max"),
                        "velocity_abs_mean": item.get("velocity_abs_mean"),
                    }
                    for item in sorted_by_velocity[:5]
                ],
                "top_acceleration_joints": [
                    {
                        "joint_name": item["joint_name"],
                        "acceleration_abs_max": item.get("acceleration_abs_max"),
                        "acceleration_abs_mean": item.get("acceleration_abs_mean"),
                    }
                    for item in sorted_by_acceleration[:5]
                ],
                "top_true_jerk_joints": [
                    {
                        "joint_name": item["joint_name"],
                        "jerk_abs_max": item.get("jerk_abs_max"),
                        "jerk_abs_mean": item.get("jerk_abs_mean"),
                    }
                    for item in sorted_by_true_jerk[:5]
                ],
            }

        return {
            "available": any(value is not None for value in sources.values()),
            "sources": sources,
        }

    def _summarize_constraint_module(self, joint_groups):
        items = []
        for group in joint_groups:
            for joint in group["joints"]:
                for source_key in ("state", "action"):
                    source = joint.get(source_key)
                    if source and source.get("constraints"):
                        items.append({
                            "joint_name": joint["joint_name"],
                            "source": source_key,
                            **source["constraints"],
                        })

        if not items:
            return {
                "available": False,
                "message": (
                    "当前未从 joint_config.json 或 info.json 中解析到明确的 "
                    "joint / velocity / torque limit。"
                ),
            }

        angle_items = [item for item in items if item.get("angle_out_of_range_ratio") is not None]
        velocity_items = [item for item in items if item.get("velocity_out_of_range_ratio") is not None]
        top_angle = sorted(
            angle_items,
            key=lambda item: item.get("angle_out_of_range_ratio", -1),
            reverse=True,
        )[:5]
        top_velocity = sorted(
            velocity_items,
            key=lambda item: item.get("velocity_out_of_range_ratio", -1),
            reverse=True,
        )[:5]

        return {
            "available": True,
            "constraint_count": len(items),
            "joint_limit_count": sum(
                1 for item in items if item.get("lower") is not None or item.get("upper") is not None
            ),
            "velocity_limit_count": sum(
                1 for item in items if item.get("velocity_limit") is not None
            ),
            "top_angle_violations": [
                {
                    "joint_name": item["joint_name"],
                    "source": item["source"],
                    "ratio": item.get("angle_out_of_range_ratio"),
                }
                for item in top_angle
            ],
            "top_velocity_violations": [
                {
                    "joint_name": item["joint_name"],
                    "source": item["source"],
                    "ratio": item.get("velocity_out_of_range_ratio"),
                }
                for item in top_velocity
            ],
        }

    def _compute_timestamp_summary(self):
        nominal_dt = 1.0 / max(float(self.info.get("fps", 30) or 30), 1e-6)
        all_dt = []
        for df in self.episode_data.values():
            if "timestamp" not in df.columns or len(df) < 2:
                continue
            ts = np.asarray([
                np.nan if self._to_float_or_none(v) is None else float(v)
                for v in df["timestamp"].tolist()
            ], dtype=np.float64)
            ts = self._sanitize_episode_timestamps(ts, len(ts))
            dt = np.diff(ts)
            if dt.size:
                all_dt.append(dt)

        if not all_dt:
            return {
                "available": False,
                "message": "数据集中没有可用的逐帧 timestamp 序列。",
            }

        dt = np.concatenate(all_dt)
        jitter = np.abs(dt - nominal_dt)
        return {
            "available": True,
            "nominal_dt": nominal_dt,
            "mean_dt": float(dt.mean()),
            "std_dt": float(dt.std()),
            "min_dt": float(dt.min()),
            "max_dt": float(dt.max()),
            "jitter_mean": float(jitter.mean()),
            "jitter_max": float(jitter.max()),
            "sample_count": int(dt.size),
        }

    @staticmethod
    def _lagged_correlation(x, y, lag):
        if lag > 0:
            xs = x[:-lag]
            ys = y[lag:]
        elif lag < 0:
            xs = x[-lag:]
            ys = y[:lag]
        else:
            xs = x
            ys = y
        if xs.size < 4 or ys.size < 4:
            return None, 0
        if np.std(xs) < 1e-9 or np.std(ys) < 1e-9:
            return None, 0
        corr = np.corrcoef(xs, ys)[0, 1]
        if not np.isfinite(corr):
            return None, 0
        return float(corr), int(xs.size)

    def _compute_state_action_alignment(self, state_episodes, action_episodes, base_names):
        fps = max(float(self.info.get("fps", 30) or 30), 1e-6)
        max_lag_frames = max(3, min(15, int(round(fps * 0.5))))
        state_lookup = {item["episode_index"]: item for item in state_episodes}
        action_lookup = {item["episode_index"]: item for item in action_episodes}
        shared_episodes = sorted(set(state_lookup) & set(action_lookup))
        if not shared_episodes:
            return {
                "available": False,
                "message": "缺少同时包含 state 和 action 的 episode，无法计算 lag correlation。",
            }

        per_joint = []
        for joint_idx, joint_name in enumerate(base_names):
            best = None
            for lag in range(-max_lag_frames, max_lag_frames + 1):
                weighted_abs = 0.0
                weighted_signed = 0.0
                total_weight = 0
                for ep_idx in shared_episodes:
                    state_values = state_lookup[ep_idx]["values"]
                    action_values = action_lookup[ep_idx]["values"]
                    if joint_idx >= state_values.shape[1] or joint_idx >= action_values.shape[1]:
                        continue
                    corr, weight = self._lagged_correlation(
                        state_values[:, joint_idx], action_values[:, joint_idx], lag
                    )
                    if corr is None or weight <= 0:
                        continue
                    total_weight += weight
                    weighted_abs += abs(corr) * weight
                    weighted_signed += corr * weight
                if total_weight <= 0:
                    continue
                candidate = {
                    "lag_frames": int(lag),
                    "lag_seconds": float(lag / fps),
                    "abs_correlation": float(weighted_abs / total_weight),
                    "correlation": float(weighted_signed / total_weight),
                    "sample_weight": int(total_weight),
                }
                if (
                    best is None
                    or candidate["abs_correlation"] > best["abs_correlation"] + 1e-9
                    or (
                        abs(candidate["abs_correlation"] - best["abs_correlation"]) <= 1e-9
                        and abs(candidate["lag_frames"]) < abs(best["lag_frames"])
                    )
                ):
                    best = candidate
            if best:
                per_joint.append({
                    "joint_name": joint_name,
                    **best,
                })

        if not per_joint:
            return {
                "available": False,
                "message": "state / action 维度存在，但可用相关性不足，无法稳定估计 lag。",
            }

        lags = np.asarray([item["lag_frames"] for item in per_joint], dtype=np.float64)
        return {
            "available": True,
            "max_lag_frames": max_lag_frames,
            "sign_note": "正值表示 action 相对 state 滞后，负值表示 action 超前。",
            "joint_count": len(per_joint),
            "median_lag_frames": float(np.median(lags)),
            "mean_abs_lag_frames": float(np.mean(np.abs(lags))),
            "max_abs_lag_frames": float(np.max(np.abs(lags))),
            "mean_abs_correlation": float(np.mean([
                item["abs_correlation"] for item in per_joint
            ])),
            "top_lag_joints": sorted(
                per_joint,
                key=lambda item: (abs(item["lag_frames"]), item["abs_correlation"]),
                reverse=True,
            )[:5],
            "per_joint": per_joint,
        }

    def build_joint_analysis_report(self):
        """构建面向分析页的关节统计报告。"""
        state_matrix, state_names, state_episodes = self._collect_feature_series(
            "observation.state", "state_joint")
        action_matrix, action_names, action_episodes = self._collect_feature_series(
            "action", "action_joint")

        state_dim = len(state_names)
        action_dim = len(action_names)
        pair_count = max(state_dim, action_dim)

        base_names = []
        for idx in range(pair_count):
            if idx < state_dim:
                base_names.append(state_names[idx])
            elif idx < action_dim:
                base_names.append(action_names[idx])
            else:
                base_names.append(f"joint_{idx}")

        group_lookup = {}
        for key, joints in self.joint_groups.items():
            for joint_name in joints:
                group_lookup[joint_name] = key

        auto_groups = self._auto_group_joints(base_names)
        auto_group_lookup = {}
        for key, joints in auto_groups.items():
            for joint_name in joints:
                auto_group_lookup[joint_name] = key

        state_constraints = self._extract_feature_constraints("observation.state", state_names)
        action_constraints = self._extract_feature_constraints("action", action_names)
        lag_report = self._compute_state_action_alignment(state_episodes, action_episodes, base_names)
        lag_by_joint = {
            item["joint_name"]: item for item in lag_report.get("per_joint", [])
        } if lag_report.get("available") else {}

        grouped = {}
        for idx, joint_name in enumerate(base_names):
            group_key = group_lookup.get(joint_name) or auto_group_lookup.get(joint_name) or "other"
            joint_entry = {
                "joint_index": idx,
                "joint_name": joint_name,
                "state_name": state_names[idx] if idx < state_dim else None,
                "action_name": action_names[idx] if idx < action_dim else None,
                "alignment": lag_by_joint.get(joint_name),
                "state": None,
                "action": None,
            }
            if idx < state_dim and state_matrix.size:
                state_bundle = self._compute_joint_metric_bundle(state_matrix[:, idx])
                if state_bundle:
                    state_bundle["temporal"] = self._compute_temporal_metric_bundle(state_episodes, idx)
                    state_bundle["temporal_preview"] = self._build_temporal_preview(state_episodes, idx)
                    state_bundle["constraints"] = self._compute_constraint_metric_bundle(
                        state_episodes,
                        idx,
                        self._resolve_joint_constraint(joint_name, joint_entry["state_name"], state_constraints),
                    )
                joint_entry["state"] = state_bundle
            if idx < action_dim and action_matrix.size:
                action_bundle = self._compute_joint_metric_bundle(action_matrix[:, idx])
                if action_bundle:
                    action_bundle["temporal"] = self._compute_temporal_metric_bundle(action_episodes, idx)
                    action_bundle["temporal_preview"] = self._build_temporal_preview(
                        action_episodes, idx, include_smoothing=True
                    )
                    action_bundle["constraints"] = self._compute_constraint_metric_bundle(
                        action_episodes,
                        idx,
                        self._resolve_joint_constraint(joint_name, joint_entry["action_name"], action_constraints),
                    )
                joint_entry["action"] = action_bundle
            grouped.setdefault(group_key, []).append(joint_entry)

        joint_groups = []
        for group_key, joints in grouped.items():
            joint_groups.append({
                "key": group_key,
                "label": self._format_group_label(group_key),
                "joint_count": len(joints),
                "joints": joints,
            })
        joint_groups.sort(key=lambda item: item["label"].lower())

        return {
            "summary": {
                **self.get_summary(),
                "state_joint_count": state_dim,
                "action_joint_count": action_dim,
                "joint_pair_count": pair_count,
            },
            "metric_groups": [
                {
                    "key": "basic",
                    "label": "基础统计",
                    "description": "均值、方差、标准差、最小值、最大值与极差。",
                    "metrics": ["mean", "variance", "std", "min", "max", "range"],
                },
                {
                    "key": "percentile",
                    "label": "分位统计",
                    "description": "1%、10%、25%、50%、75%、90%、99% 分位点。",
                    "metrics": ["q01", "q10", "q25", "q50", "q75", "q90", "q99"],
                },
                {
                    "key": "sigma",
                    "label": "Sigma 波动带",
                    "description": "均值 ±1σ 与 ±2σ 区间，同时给出各自覆盖比例。",
                    "metrics": ["sigma_1", "sigma_2"],
                },
                {
                    "key": "distribution",
                    "label": "分布概览",
                    "description": "每个关节的直方图分布，用于观察异常峰、多峰与长尾。",
                    "metrics": ["histogram"],
                },
                {
                    "key": "temporal",
                    "label": "时序平滑性",
                    "description": "包括 smoothness、jerk、速度/加速度绝对值和 spike ratio。",
                    "metrics": [
                        "temporal.smoothness",
                        "temporal.velocity_abs_mean",
                        "temporal.velocity_abs_max",
                        "temporal.acceleration_abs_mean",
                        "temporal.acceleration_abs_max",
                        "temporal.jerk",
                        "temporal.jerk_abs_mean",
                        "temporal.jerk_abs_max",
                        "temporal.spike_ratio",
                    ],
                },
                {
                    "key": "constraint",
                    "label": "物理约束",
                    "description": "包括 joint limit、velocity limit 和超限比例；未配置时会明确标注。",
                    "metrics": [
                        "constraints.lower",
                        "constraints.upper",
                        "constraints.velocity_limit",
                        "constraints.angle_out_of_range_ratio",
                        "constraints.velocity_out_of_range_ratio",
                    ],
                },
                {
                    "key": "alignment",
                    "label": "时间对齐",
                    "description": "包括 timestamp cadence 统计和 state/action lag correlation。",
                    "metrics": [
                        "alignment.lag_frames",
                        "alignment.lag_seconds",
                        "alignment.abs_correlation",
                    ],
                },
            ],
            "smoothness": self._summarize_temporal_module(joint_groups),
            "constraints": self._summarize_constraint_module(joint_groups),
            "alignment": {
                "timestamp": self._compute_timestamp_summary(),
                "state_action_lag": lag_report,
                "image_alignment": {
                    "available": False,
                    "message": (
                        "当前 LeRobot 数据集只暴露统一 frame timestamp，缺少独立的 "
                        "image/state/action 子时间戳，暂时无法直接做 image vs state/action 明确对齐差。"
                    ),
                },
            },
            "joint_groups": joint_groups,
        }

    # ─── 视频处理 ───

    @staticmethod
    def _compress_int_ranges(sorted_indices):
        """将有序整数列表压缩为连续区间: [0,1,2,5,6] -> [(0,2),(5,6)]"""
        if not sorted_indices:
            return []
        ranges = []
        start = end = sorted_indices[0]
        for i in range(1, len(sorted_indices)):
            if sorted_indices[i] == end + 1:
                end = sorted_indices[i]
            else:
                ranges.append((start, end))
                start = end = sorted_indices[i]
        ranges.append((start, end))
        return ranges

    @staticmethod
    def _probe_video_params(src_path):
        """用 ffprobe 获取源视频的编码参数, 用于输出格式匹配。"""
        params = {}
        try:
            r = subprocess.run([
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", "-select_streams", "v:0",
                str(src_path),
            ], capture_output=True, timeout=30)
            if r.returncode == 0:
                streams = json.loads(r.stdout).get("streams", [])
                if streams:
                    s = streams[0]
                    params.update({
                        "codec": s.get("codec_name", "h264"),
                        "pix_fmt": s.get("pix_fmt", "yuv420p"),
                        "bit_rate": s.get("bit_rate"),
                        "profile": s.get("profile"),
                        "width": int(s.get("width") or 0),
                        "height": int(s.get("height") or 0),
                    })
        except Exception:
            pass

        # 探测关键帧间隔: 读取前 60 帧, 计算相邻 I 帧之间的最大距离
        try:
            r2 = subprocess.run([
                "ffprobe", "-v", "quiet", "-select_streams", "v:0",
                "-show_entries", "frame=key_frame", "-of", "csv=p=0",
                "-read_intervals", "%+#60",
                str(src_path),
            ], capture_output=True, timeout=30)
            if r2.returncode == 0:
                flags = r2.stdout.decode().strip().split("\n")
                key_positions = [i for i, f in enumerate(flags) if f.strip() == "1"]
                if len(key_positions) >= 2:
                    max_gap = max(
                        key_positions[i+1] - key_positions[i]
                        for i in range(len(key_positions) - 1))
                    params["keyint"] = max_gap
        except Exception:
            pass

        return params

    @staticmethod
    def _probe_video_stream(src_path):
        """轻量探测视频真实 fps / 帧数 / 时长, 用于一致性检查。

        失败时返回 None; 缺失字段为 None。尽量只用 format+stream 头, 不做
        -count_frames 防止慢; 若 nb_frames 缺失则用 duration * fps 兜底。
        """
        try:
            r = subprocess.run([
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", "-select_streams", "v:0",
                "-show_format",
                str(src_path),
            ], capture_output=True, timeout=30)
            if r.returncode != 0:
                return None
            payload = json.loads(r.stdout)
        except FileNotFoundError:
            return None
        except Exception:
            return None

        streams = payload.get("streams") or []
        if not streams:
            return None
        s = streams[0]
        fmt = payload.get("format") or {}

        def _parse_fraction(v):
            if not v:
                return None
            try:
                if "/" in str(v):
                    num, den = str(v).split("/", 1)
                    num_f, den_f = float(num), float(den)
                    if den_f <= 0:
                        return None
                    return num_f / den_f
                return float(v)
            except (TypeError, ValueError):
                return None

        fps = _parse_fraction(s.get("avg_frame_rate")) or _parse_fraction(s.get("r_frame_rate"))
        duration = None
        for candidate in (s.get("duration"), fmt.get("duration")):
            try:
                if candidate is not None:
                    duration = float(candidate)
                    break
            except (TypeError, ValueError):
                continue

        nb_frames = None
        for candidate in (s.get("nb_frames"), s.get("nb_read_frames")):
            try:
                if candidate is not None:
                    nb_frames = int(candidate)
                    break
            except (TypeError, ValueError):
                continue
        if nb_frames is None and fps and duration:
            nb_frames = int(round(fps * duration))

        return {
            "fps": fps,
            "nb_frames": nb_frames,
            "duration": duration,
            "codec": s.get("codec_name"),
        }

    def check_integrity(self, max_workers=8, fps_rel_tol=0.01, frame_count_tol=0):
        """比对每集每路视频与 parquet 的一致性, 返回结构化报告。

        检查项:
          - video_fps vs info.json fps (相对误差 > fps_rel_tol 判 error)
          - video_nb_frames vs parquet 行数 (超过 frame_count_tol 判 error)
          - video_duration vs parquet_rows/info_fps (差 > 2 帧 判 error)
        """
        info_fps = float(self.info.get("fps", 30) or 30)
        if info_fps <= 0:
            info_fps = 30.0
        frame_tol_sec = 2.0 / info_fps

        jobs = []
        for cur_idx, df in self.episode_data.items():
            orig_idx = self._orig_indices.get(cur_idx, cur_idx)
            vfiles = self._orig_video_files.get(orig_idx, {})
            parquet_rows = int(len(df))
            for cam_name, vpath in vfiles.items():
                jobs.append((cur_idx, cam_name, vpath, parquet_rows))

        probed_map = {}
        if jobs:
            unique_paths = {job[2] for job in jobs}
            worker_count = max(1, min(max_workers, len(unique_paths)))
            with ThreadPoolExecutor(max_workers=worker_count) as ex:
                future_to_path = {
                    ex.submit(self._probe_video_stream, p): p
                    for p in unique_paths
                }
                for fut in as_completed(future_to_path):
                    path = future_to_path[fut]
                    try:
                        probed_map[path] = fut.result()
                    except Exception:
                        probed_map[path] = None

        ffprobe_missing = bool(jobs) and all(probed_map.get(p) is None for p in {j[2] for j in jobs})

        episodes_report = {}
        error_count = 0
        warning_count = 0
        affected_episodes = set()

        for cur_idx, cam_name, vpath, parquet_rows in jobs:
            probed = probed_map.get(vpath)
            entry = {
                "camera": cam_name,
                "video_path": os.path.basename(vpath),
                "parquet_rows": parquet_rows,
                "video_fps": None,
                "video_nb_frames": None,
                "video_duration": None,
                "issues": [],
            }
            level = None

            if probed is None:
                entry["issues"].append({
                    "level": "warning",
                    "code": "probe_failed",
                    "message": "ffprobe 探测失败 (文件缺失或 ffprobe 未安装)",
                })
                level = "warning"
            else:
                vfps = probed.get("fps")
                vframes = probed.get("nb_frames")
                vdur = probed.get("duration")
                entry["video_fps"] = vfps
                entry["video_nb_frames"] = vframes
                entry["video_duration"] = vdur

                if vfps is not None and info_fps > 0:
                    rel_err = abs(vfps - info_fps) / info_fps
                    if rel_err > fps_rel_tol:
                        entry["issues"].append({
                            "level": "error",
                            "code": "fps_mismatch",
                            "message": (
                                f"视频真实 fps {vfps:.3f} 与 info.json {info_fps:g} "
                                f"不符 (相对误差 {rel_err*100:.2f}%)"
                            ),
                        })
                        level = "error"

                if vframes is not None:
                    diff = vframes - parquet_rows
                    if abs(diff) > frame_count_tol:
                        entry["issues"].append({
                            "level": "error",
                            "code": "frame_count_mismatch",
                            "message": (
                                f"视频帧数 {vframes} 与 parquet 行数 {parquet_rows} "
                                f"不符 (差 {diff:+d})"
                            ),
                        })
                        level = "error"

                if vdur is not None and parquet_rows > 0:
                    expected = parquet_rows / info_fps
                    if abs(vdur - expected) > frame_tol_sec:
                        entry["issues"].append({
                            "level": "error",
                            "code": "duration_mismatch",
                            "message": (
                                f"视频时长 {vdur:.3f}s 与 parquet 推算 "
                                f"{expected:.3f}s 不符 (差 {vdur-expected:+.3f}s)"
                            ),
                        })
                        level = "error"

            if level == "error":
                error_count += 1
                affected_episodes.add(cur_idx)
            elif level == "warning":
                warning_count += 1

            ep_bucket = episodes_report.setdefault(cur_idx, {
                "episode_index": int(cur_idx),
                "cameras": [],
                "max_level": None,
            })
            ep_bucket["cameras"].append(entry)
            if level == "error":
                ep_bucket["max_level"] = "error"
            elif level == "warning" and ep_bucket["max_level"] != "error":
                ep_bucket["max_level"] = "warning"

        episodes_list = sorted(episodes_report.values(), key=lambda x: x["episode_index"])
        only_bad = [ep for ep in episodes_list if ep["max_level"] in ("error", "warning")]

        return {
            "info_fps": info_fps,
            "total_episodes_checked": len(episodes_list),
            "total_videos_checked": len(jobs),
            "affected_episodes": sorted(int(i) for i in affected_episodes),
            "error_count": error_count,
            "warning_count": warning_count,
            "ffprobe_missing": ffprobe_missing,
            "episodes": only_bad,
        }

    def _reencode_video(self, src_path, keep_frame_indices, dst_path,
                        cached_params=None):
        """用 ffmpeg select 滤镜重编码视频, 仅保留指定帧, 匹配源编码格式。
        cached_params: 预先探测的编码参数, 避免重复 ffprobe。
        """
        if not Path(src_path).exists():
            return False

        params = cached_params or self._probe_video_params(src_path)
        codec_name = params.get("codec", "h264")
        pix_fmt = params.get("pix_fmt", "yuv420p")
        bit_rate = params.get("bit_rate")
        keyint = params.get("keyint", 2)

        encoder_map = {
            "h264": "libx264", "hevc": "libx265", "h265": "libx265",
            "vp8": "libvpx", "vp9": "libvpx-vp9",
            "av1": "libsvtav1",
        }
        encoder = encoder_map.get(codec_name, "libx264")

        ranges = self._compress_int_ranges(sorted(keep_frame_indices))
        parts = [f"between(n\\,{s}\\,{e})" for s, e in ranges]
        select_expr = "+".join(parts)

        cmd = [
            "ffmpeg", "-hide_banner", "-y", "-i", str(src_path),
            "-vf", f"select='{select_expr}',setpts=N/FRAME_RATE/TB",
            "-c:v", encoder,
            "-pix_fmt", pix_fmt,
            "-g", str(keyint),
        ]
        if bit_rate:
            cmd += ["-b:v", str(bit_rate)]
        elif encoder == "libsvtav1":
            cmd += ["-crf", "30"]
        else:
            cmd += ["-crf", "18"]
        if encoder == "libx264":
            cmd += ["-preset", "fast"]
        elif encoder == "libsvtav1":
            cmd += ["-preset", "12"]
        elif encoder == "libx265":
            cmd += ["-preset", "fast"]
        cmd += ["-movflags", "+faststart", "-an", dst_path]

        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=600)
            if result.returncode == 0:
                return True
            else:
                stderr = result.stderr.decode(errors="replace")[:500]
                log.warning(f"ffmpeg 重编码失败 {Path(dst_path).name}: {stderr}")
                return False
        except FileNotFoundError:
            log.warning("ffmpeg 未安装, 无法裁剪视频; 请安装 ffmpeg")
            return False
        except subprocess.TimeoutExpired:
            log.warning(f"ffmpeg 重编码超时: {dst_path}")
            return False
        except Exception as e:
            log.warning(f"视频重编码异常: {e}")
            return False

    # ─── 编辑 ───

    def delete_episodes(self, indices_to_delete):
        """删除选中的 episodes 并重新索引"""
        idx_set = set(indices_to_delete)
        remaining = [em for em in self.episodes_meta if em["episode_index"] not in idx_set]

        new_data = {}
        new_orig = {}
        new_meta = []

        for new_idx, em in enumerate(remaining):
            old_idx = em["episode_index"]
            orig_idx = self._orig_indices.get(old_idx, old_idx)

            meta_copy = dict(em)
            meta_copy["episode_index"] = new_idx
            new_meta.append(meta_copy)

            if old_idx in self.episode_data:
                df = self.episode_data[old_idx].copy()
                df["episode_index"] = new_idx
                new_data[new_idx] = df

            new_orig[new_idx] = orig_idx

        self.episodes_meta = new_meta
        self.episode_data = new_data
        self._orig_indices = new_orig
        self.modified = True
        self._refresh_info()
        return len(new_meta)

    def delete_frames(self, ep_idx, frame_indices):
        """删除 episode 中的指定帧 (state, action, 视频帧在保存时统一裁剪)"""
        if ep_idx not in self.episode_data:
            return 0

        df = self.episode_data[ep_idx]
        df = df[~df["frame_index"].isin(set(frame_indices))].copy()
        df.reset_index(drop=True, inplace=True)
        df["frame_index"] = range(len(df))

        if len(df) == 0:
            self.delete_episodes([ep_idx])
            return 0

        self.episode_data[ep_idx] = df
        for em in self.episodes_meta:
            if em["episode_index"] == ep_idx:
                em["length"] = len(df)
                break

        self.modified = True
        self._refresh_info()
        return len(df)

    def _refresh_info(self):
        self.info["total_episodes"] = len(self.episodes_meta)
        self.info["total_frames"] = sum(len(d) for d in self.episode_data.values())
        self.info["total_tasks"] = len(self.tasks)
        self.info["chunks_size"] = int(self.info.get("chunks_size", 1000) or 1000)
        self.info["data_path"] = "data/chunk-{chunk_index:03d}/episode_{episode_index:06d}.parquet"
        self.info["video_path"] = "videos/chunk-{chunk_index:03d}/{video_key}/episode_{episode_index:06d}.mp4"

    def _feature_signature(self):
        features = self.info.get("features", {}) or {}
        signature = {}
        for key, meta in sorted(features.items()):
            signature[key] = {
                "dtype": meta.get("dtype"),
                "shape": copy.deepcopy(meta.get("shape")),
                "names": copy.deepcopy(meta.get("names")),
            }
        return signature

    def assert_merge_compatible(self, other_editor):
        if not isinstance(other_editor, DatasetEditor):
            raise ValueError("待拼接对象不是有效的数据集编辑器")

        if self._feature_signature() != other_editor._feature_signature():
            raise ValueError("待拼接数据集与当前数据集的 features 定义不一致，无法安全拼接")

        base_fps = float(self.info.get("fps", 0) or 0)
        other_fps = float(other_editor.info.get("fps", 0) or 0)
        if abs(base_fps - other_fps) > 1e-9:
            raise ValueError(f"待拼接数据集 FPS 不一致: {base_fps} vs {other_fps}")

        base_robot = str(self.info.get("robot_type", "") or "").strip()
        other_robot = str(other_editor.info.get("robot_type", "") or "").strip()
        if base_robot and other_robot and base_robot != other_robot:
            raise ValueError(f"待拼接数据集 robot_type 不一致: {base_robot} vs {other_robot}")

    @staticmethod
    def _task_signature(task_payload):
        if isinstance(task_payload, dict):
            normalized = {k: v for k, v in task_payload.items() if k != "task_index"}
        else:
            normalized = {"task": str(task_payload)}
        return json.dumps(normalized, sort_keys=True, ensure_ascii=False)

    @staticmethod
    def _copy_task_payload(task_payload):
        if isinstance(task_payload, dict):
            return copy.deepcopy(task_payload)
        return {"task": str(task_payload)}

    def _build_task_name_lookup(self):
        lookup = {}
        for idx, task in enumerate(self.tasks):
            if not isinstance(task, dict):
                continue
            task_index = int(task.get("task_index", idx))
            task_name = str(task.get("task", "") or "").strip()
            if task_name:
                lookup[task_name] = task_index
        return lookup

    def _resolve_episode_task_index(self, ep_idx, episode_meta):
        raw_task_index = episode_meta.get("task_index")
        if raw_task_index is not None:
            try:
                return int(raw_task_index)
            except (TypeError, ValueError):
                pass

        df = self.episode_data.get(ep_idx)
        if df is not None and "task_index" in df.columns and len(df):
            series = df["task_index"].dropna()
            if not series.empty:
                try:
                    return int(series.iloc[0])
                except (TypeError, ValueError):
                    pass

        task_lookup = self._build_task_name_lookup()
        tasks = episode_meta.get("tasks")
        if isinstance(tasks, list):
            for task_name in tasks:
                task_index = task_lookup.get(str(task_name))
                if task_index is not None:
                    return int(task_index)

        return 0

    def _build_task_merge_plan(self, append_editors):
        merged_tasks = []
        merged_task_key_to_index = {}
        per_editor_task_maps = []
        all_editors = [self, *append_editors]

        for editor in all_editors:
            task_map = {}
            for fallback_idx, task in enumerate(editor.tasks):
                payload = self._copy_task_payload(task)
                old_task_index = int(payload.get("task_index", fallback_idx))
                key = self._task_signature(payload)
                if key not in merged_task_key_to_index:
                    merged_idx = len(merged_tasks)
                    payload["task_index"] = merged_idx
                    merged_tasks.append(payload)
                    merged_task_key_to_index[key] = merged_idx
                task_map[old_task_index] = merged_task_key_to_index[key]
            if not task_map and merged_tasks:
                task_map[0] = 0
            per_editor_task_maps.append(task_map)

        return merged_tasks, per_editor_task_maps

    def _resolve_video_feature_key(self, cam_name, video_keys):
        for key in video_keys:
            if key.endswith(cam_name):
                return key
        return f"observation.images.{cam_name}"

    def _build_save_sources(self, append_paths, report):
        append_editors = []
        normalized_paths = []

        for raw_path in append_paths or []:
            candidate = str(raw_path or "").strip()
            if not candidate:
                continue
            path = str(Path(candidate).resolve())
            if path == str(self.root):
                continue
            if path in normalized_paths:
                continue

            report("prepare_merge", "正在检查拼接数据集", f"正在读取待拼接数据集: {path}", 0, 1)
            editor = DatasetEditor(path, joint_config=_joint_config_override)
            self.assert_merge_compatible(editor)
            append_editors.append(editor)
            normalized_paths.append(path)

        merged_tasks, task_maps = self._build_task_merge_plan(append_editors)
        save_sources = []
        for editor_idx, editor in enumerate([self, *append_editors]):
            save_sources.append({
                "editor": editor,
                "task_map": task_maps[editor_idx] if editor_idx < len(task_maps) else {},
                "path": str(editor.root),
            })
        return save_sources, merged_tasks

    # ─── 平滑性分析 ───

    def _get_state_array(self, ep_idx):
        """提取 observation.state 为 2D numpy 数组 (N, D)"""
        if ep_idx not in self.episode_data:
            return None
        df = self.episode_data[ep_idx]
        if "observation.state" not in df.columns:
            return None
        raw = [self._to_list(v) for v in df["observation.state"].tolist()]
        if not raw or not all(len(v) > 0 for v in raw):
            return None
        return np.array(raw, dtype=np.float64)

    def _find_junctions(self, n_frames, del_set):
        """找出删除后产生的所有拼接点: (左锚帧, 右锚帧, 被删帧列表)"""
        remaining = sorted(set(range(n_frames)) - del_set)
        junctions = []
        for i in range(len(remaining) - 1):
            left, right = remaining[i], remaining[i + 1]
            if right > left + 1:
                between = sorted(
                    f for f in range(left + 1, right) if f in del_set)
                if between:
                    junctions.append((left, right, between))
        return junctions

    def _compute_accel_threshold(self, states, k_sigma=3.0):
        """从整条轨迹计算每个关节维度的加速度阈值 (鲁棒统计: median + k*MAD)

        使用全量数据 + 鲁棒统计, 使阈值不随删除选择而波动.
        MAD (Median Absolute Deviation) 对异常值不敏感.
        """
        n = len(states)
        if n < 3:
            return None
        all_accel = np.abs(np.diff(states, n=2, axis=0))
        if len(all_accel) == 0:
            return None
        median_acc = np.median(all_accel, axis=0)
        mad = np.median(np.abs(all_accel - median_acc), axis=0)
        return np.maximum(median_acc + k_sigma * 1.4826 * mad, 1e-6)

    def _is_splice_smooth(self, states, left, right, threshold,
                          val_floor=0.0):
        """双条件检测: 加速度异常 且 值变化显著 才判定为不平滑
        val_floor: 每关节位置变化绝对值低于此值时视为无意义, 跳过该关节

        注意: 删除帧后 left 和 right 在编辑时间线中直接相邻,
        因此拼接速度 = states[right] - states[left] (不做 gap 归一化).
        """
        n = len(states)
        v_splice = states[right] - states[left]
        significant = np.abs(v_splice) > val_floor
        if left >= 1:
            v_before = states[left] - states[left - 1]
            accel_bad = np.abs(v_splice - v_before) > threshold
            if np.any(accel_bad & significant):
                return False
        if right < n - 1:
            v_after = states[right + 1] - states[right]
            accel_bad = np.abs(v_after - v_splice) > threshold
            if np.any(accel_bad & significant):
                return False
        return True

    def _splice_accel_ratio(self, states, left, right, threshold,
                            val_floor=0.0):
        """计算拼接处各关节加速度比值, 值变化不显著的关节比值归零
        比值上限 999 以免 UI 显示极端数字"""
        n = len(states)
        D = states.shape[1]
        ratios = np.zeros(D)
        safe_thr = np.maximum(threshold, 1e-10)
        v_splice = states[right] - states[left]
        significant = np.abs(v_splice) > val_floor
        if left >= 1:
            v_before = states[left] - states[left - 1]
            r = np.abs(v_splice - v_before) / safe_thr
            ratios = np.maximum(ratios, np.where(significant, r, 0.0))
        if right < n - 1:
            v_after = states[right + 1] - states[right]
            r = np.abs(v_after - v_splice) / safe_thr
            ratios = np.maximum(ratios, np.where(significant, r, 0.0))
        return np.minimum(ratios, 999.0)

    def _find_bridge_dp(self, states, left, right, candidates,
                        dp_threshold, min_count=0):
        """Douglas-Peucker 关键帧提取, 保证至少返回 min_count 个帧.

        先用 dp_threshold 做标准 DP; 若结果不足 min_count,
        则自动降低阈值 (取所有候选偏差的中位数) 重跑, 直至满足要求.
        """
        result = self._dp_recurse(states, left, right, candidates,
                                  dp_threshold)
        if len(result) >= min_count or not candidates:
            return result

        devs = []
        span = max(right - left, 1)
        for c in candidates:
            t = (c - left) / span
            interp = states[left] * (1 - t) + states[right] * t
            devs.append(float(np.max(np.abs(states[c] - interp))))
        devs.sort(reverse=True)

        for attempt in range(3):
            needed = min(min_count, len(candidates))
            if needed <= len(result):
                break
            cut_idx = min(needed - 1, len(devs) - 1)
            new_thr = devs[cut_idx] * 0.99
            result = self._dp_recurse(states, left, right, candidates,
                                      new_thr)
            if len(result) >= needed:
                break
        return result

    def _dp_recurse(self, states, left, right, candidates, dp_threshold):
        """标准 Douglas-Peucker 递归"""
        if not candidates:
            return []
        span = right - left
        if span <= 1:
            return []

        max_dev = -1.0
        pivot_idx = 0
        for idx, c in enumerate(candidates):
            t = (c - left) / span
            interp = states[left] * (1 - t) + states[right] * t
            dev = float(np.max(np.abs(states[c] - interp)))
            if dev > max_dev:
                max_dev = dev
                pivot_idx = idx

        if max_dev <= dp_threshold:
            return []

        pivot = candidates[pivot_idx]
        left_cands = candidates[:pivot_idx]
        right_cands = candidates[pivot_idx + 1:]

        left_res = self._dp_recurse(
            states, left, pivot, left_cands, dp_threshold)
        right_res = self._dp_recurse(
            states, pivot, right, right_cands, dp_threshold)
        return left_res + [pivot] + right_res

    def _find_bridge_by_filter(self, states, left, right, candidates,
                               fps=30):
        """通过平滑参考轨迹 + 相似度匹配寻找桥接帧
        优先 Butterworth 滤波, 回退到 Hermite 三次插值"""
        if not candidates:
            return []

        n = len(states)
        D = states.shape[1]
        span = right - left
        if span <= 1:
            return []

        ideal_values = None

        # 方案 A: Butterworth 低通滤波
        if _HAS_SCIPY and n >= 12:
            try:
                nyq = fps / 2.0
                cutoff = min(fps / 8.0, nyq * 0.9)
                Wn = cutoff / nyq
                b, a = butter(3, Wn, btype='low')
                filtered = np.zeros_like(states)
                for d in range(D):
                    filtered[:, d] = filtfilt(b, a, states[:, d])
                ideal_values = filtered[candidates]
            except Exception:
                ideal_values = None

        # 方案 B: Hermite 三次插值
        if ideal_values is None:
            p0, p1 = states[left], states[right]
            m0 = ((states[left] - states[max(0, left - 1)]) * span
                   if left > 0 else np.zeros(D))
            m1 = ((states[min(n - 1, right + 1)] - states[right]) * span
                   if right < n - 1 else np.zeros(D))
            ideal_values = np.zeros((len(candidates), D))
            for i, c in enumerate(candidates):
                t = (c - left) / span
                t2, t3 = t * t, t * t * t
                h00 = 2 * t3 - 3 * t2 + 1
                h10 = t3 - 2 * t2 + t
                h01 = -2 * t3 + 3 * t2
                h11 = t3 - t2
                ideal_values[i] = (h00 * p0 + h10 * m0
                                   + h01 * p1 + h11 * m1)

        # 按时间段分组, 每段选最接近理想值的真实帧
        real_values = states[candidates]
        distances = np.linalg.norm(real_values - ideal_values, axis=1)

        K = max(2, 1 + len(candidates) // 3)
        K = min(K, len(candidates))
        if len(candidates) <= K:
            return list(candidates)

        selected = []
        seg_len = len(candidates) / K
        for seg_i in range(K):
            lo = int(seg_i * seg_len)
            hi = min(int((seg_i + 1) * seg_len), len(candidates))
            if lo >= hi:
                continue
            best_idx = lo + int(np.argmin(distances[lo:hi]))
            selected.append(candidates[best_idx])
        return sorted(set(selected))

    def analyze_deletion(self, ep_idx, frame_indices, k_sigma=3.0,
                         joint_indices=None):
        """分析删除帧后的平滑性, 返回桥接帧建议
        joint_indices: 仅分析这些关节列索引 (来自前端关节选择面板)
        """
        states = self._get_state_array(ep_idx)
        if states is None or len(states) < 4:
            return {"smooth": True, "message": "数据不足，跳过分析"}

        # 按前端选择过滤关节, 保留原始索引映射
        if joint_indices is not None and len(joint_indices) > 0:
            ji = [int(j) for j in joint_indices
                  if 0 <= int(j) < states.shape[1]]
            if not ji:
                return {"smooth": True, "message": "选中关节无有效数据"}
            joint_map = ji
            states = states[:, ji]
        else:
            joint_map = list(range(states.shape[1]))

        n = len(states)
        del_set = set(frame_indices)
        fps = self.info.get("fps", 30)

        junctions = self._find_junctions(n, del_set)
        if not junctions:
            return {"smooth": True}

        # 加速度阈值 (从整条轨迹的鲁棒统计量计算, 不随删除选择波动)
        threshold = self._compute_accel_threshold(states, k_sigma)
        if threshold is None:
            return {"smooth": True, "message": "无法计算阈值"}

        # Douglas-Peucker 阈值: 基于每帧平均绝对变化量 (用较低倍数以保留更多形状细节)
        non_del = sorted(set(range(n)) - del_set)
        if len(non_del) >= 2:
            nd_states = states[non_del]
            avg_abs_vel = np.mean(np.abs(np.diff(nd_states, axis=0)), axis=0)
            dp_threshold = float(np.max(avg_abs_vel)) * 0.5
        else:
            dp_threshold = float(np.max(threshold))

        # 值变化下限: 整体数据尺度的 0.1%
        overall_scale = float(np.max(np.ptp(states, axis=0)))
        val_floor = overall_scale * 0.001

        problem_junctions = []
        all_bridge, all_filter = [], []

        for left, right, deleted in junctions:
            if self._is_splice_smooth(states, left, right, threshold,
                                      val_floor):
                continue

            ratios = self._splice_accel_ratio(states, left, right, threshold,
                                              val_floor)
            bad_joints = [joint_map[int(j)] for j in np.where(ratios > 1.0)[0]]
            problem_junctions.append({
                "left_frame": int(left),
                "right_frame": int(right),
                "deleted_count": len(deleted),
                "max_accel_ratio": round(float(np.max(ratios)), 2),
                "problematic_joints": bad_joints,
            })

            min_bridges = max(2, len(deleted) // 3)
            bridges = self._find_bridge_dp(
                states, left, right, deleted, dp_threshold, min_bridges)
            all_bridge.extend(bridges)

            filter_frames = self._find_bridge_by_filter(
                states, left, right, deleted, fps)
            all_filter.extend(filter_frames)

        if not problem_junctions:
            return {"smooth": True}

        result = {"smooth": False, "junctions": problem_junctions}

        if all_bridge:
            result["recommendation"] = {
                "method": "bridge",
                "frames": sorted(set(all_bridge)),
            }
            if all_filter:
                result["alternative"] = {
                    "method": "filter",
                    "frames": sorted(set(all_filter)),
                }
        elif all_filter:
            result["recommendation"] = {
                "method": "filter",
                "frames": sorted(set(all_filter)),
            }
        else:
            result["recommendation"] = {
                "method": "none",
                "frames": [],
            }
        return result

    # ─── 统计 ───

    @staticmethod
    def _compute_feature_stats(arr):
        """对 2D array (N, D) 或 1D array (N,) 计算统计量，返回 list 格式。"""
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        stats = {
            "min":  arr.min(0).tolist(),
            "max":  arr.max(0).tolist(),
            "mean": arr.mean(0).tolist(),
            "std":  arr.std(0).tolist(),
            "count": int(arr.shape[0]),
        }
        for q in DatasetEditor.QUANTILES:
            stats[f"q{int(q * 100):02d}"] = np.quantile(arr, q, axis=0).tolist()
        return stats

    @staticmethod
    def _estimate_num_samples(dataset_len, min_num_samples=100,
                              max_num_samples=10_000, power=0.75):
        """按 lerobot 的启发式估计需要采样多少张图像。"""
        if dataset_len <= 0:
            return 0
        if dataset_len < min_num_samples:
            min_num_samples = dataset_len
        return max(min_num_samples, min(int(dataset_len ** power), max_num_samples))

    @classmethod
    def _sample_frame_indices(cls, frame_count):
        """在整段视频上均匀采样帧索引。"""
        if frame_count <= 0:
            return []
        if frame_count == 1:
            return [0]

        num_samples = cls._estimate_num_samples(frame_count)
        raw = np.round(np.linspace(0, frame_count - 1, num_samples)).astype(int).tolist()

        sampled = []
        seen = set()
        for idx in raw:
            idx = max(0, min(frame_count - 1, int(idx)))
            if idx in seen:
                continue
            seen.add(idx)
            sampled.append(idx)
        return sampled

    @staticmethod
    def _read_exact(stream, size):
        """从二进制流中读取固定字节数。"""
        chunks = []
        remaining = size
        while remaining > 0:
            chunk = stream.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _compute_image_feature_stats_from_video(self, video_path, frame_count):
        """从视频中采样图像帧并计算每通道统计，结果对齐 lerobot 的 [3,1,1] 形状。"""
        sampled_indices = self._sample_frame_indices(frame_count)
        if not sampled_indices:
            return None

        params = self._probe_video_params(video_path)
        width = int(params.get("width") or 0)
        height = int(params.get("height") or 0)
        if width <= 0 or height <= 0:
            log.warning(f"无法获取视频尺寸，跳过图像统计: {video_path}")
            return None

        out_w, out_h = width, height
        if max(width, height) >= 300:
            downsample_factor = int(width / 150) if width > height else int(height / 150)
            downsample_factor = max(downsample_factor, 1)
            out_w = max(1, width // downsample_factor)
            out_h = max(1, height // downsample_factor)

        ranges = self._compress_int_ranges(sampled_indices)
        parts = []
        for start, end in ranges:
            if start == end:
                parts.append(f"eq(n\\,{start})")
            else:
                parts.append(f"between(n\\,{start}\\,{end})")
        select_expr = "+".join(parts)
        filters = [f"select='{select_expr}'"]
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
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            while True:
                buf = self._read_exact(proc.stdout, frame_size)
                if not buf:
                    break
                if len(buf) != frame_size:
                    log.warning(f"读取视频帧不完整，跳过图像统计: {video_path}")
                    return None

                frame = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 3).astype(np.float64)
                frame /= 255.0
                channel_batches.append(frame)
                decoded_frames += 1

            return_code = proc.wait(timeout=600)
            if return_code != 0:
                stderr = proc.stderr.read().decode(errors="replace")[:500]
                log.warning(f"ffmpeg 读取图像统计失败 {Path(video_path).name}: {stderr}")
                return None
        except FileNotFoundError:
            log.warning("ffmpeg 未安装，无法计算图像统计")
            return None
        except subprocess.TimeoutExpired:
            if proc is not None:
                proc.kill()
            log.warning(f"图像统计超时: {video_path}")
            return None
        except Exception as e:
            log.warning(f"图像统计异常 {video_path}: {e}")
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
        min_channels = pixels.min(axis=0)
        max_channels = pixels.max(axis=0)
        mean_channels = pixels.mean(axis=0)
        std_channels = pixels.std(axis=0)

        def to_image_stat_list(values):
            return np.asarray(values, dtype=np.float64).reshape(3, 1, 1).tolist()

        stats = {
            "min": to_image_stat_list(min_channels),
            "max": to_image_stat_list(max_channels),
            "mean": to_image_stat_list(mean_channels),
            "std": to_image_stat_list(std_channels),
            "count": int(decoded_frames),
        }
        for q in self.QUANTILES:
            stats[f"q{int(q * 100):02d}"] = to_image_stat_list(
                np.quantile(pixels, q, axis=0)
            )
        return stats

    def compute_episode_stats(self, video_paths_by_episode=None, progress_cb=None,
                              skip_video_stats=False):
        """计算每个 episode 的统计数据 (lerobot v2.1 格式)"""
        features = self.info.get("features", {})
        vector_cols = [c for c in ("observation.state", "action")
                       if any(c in df.columns for df in self.episode_data.values())]
        scalar_cols = [c for c in ("timestamp", "frame_index", "episode_index",
                                    "index", "task_index")
                       if any(c in df.columns for df in self.episode_data.values())]
        image_cols = []
        if not skip_video_stats:
            image_cols = [
                key for key, meta in features.items()
                if meta.get("dtype") in ("image", "video")
            ]

        results = []
        valid_episodes = [
            em["episode_index"] for em in self.episodes_meta
            if em["episode_index"] in self.episode_data
        ]
        total_episodes = len(valid_episodes)
        if progress_cb:
            detail = "正在汇总每个 episode 的数值统计..."
            if not skip_video_stats:
                detail = "正在汇总每个 episode 的数值与图像统计..."
            progress_cb(
                "compute_stats",
                "正在计算统计信息",
                detail,
                0,
                total_episodes,
            )
        for em in self.episodes_meta:
            idx = em["episode_index"]
            if idx not in self.episode_data:
                continue
            df = self.episode_data[idx]
            stats = {}

            for col in vector_cols:
                if col not in df.columns:
                    continue
                vals = [self._to_list(v) for v in df[col].tolist() if v is not None]
                valid = [v for v in vals if len(v) > 0]
                if valid:
                    stats[col] = self._compute_feature_stats(
                        np.array(valid, dtype=np.float64))

            for col in scalar_cols:
                if col not in df.columns:
                    continue
                arr = df[col].dropna().values.astype(np.float64)
                if len(arr) > 0:
                    stats[col] = self._compute_feature_stats(arr)

            if video_paths_by_episode and not skip_video_stats:
                ep_video_paths = video_paths_by_episode.get(idx, {})
                for key in image_cols:
                    video_path = ep_video_paths.get(key)
                    if not video_path:
                        continue
                    image_stats = self._compute_image_feature_stats_from_video(
                        video_path, len(df))
                    if image_stats:
                        stats[key] = image_stats

            results.append({"episode_index": idx, "stats": stats})
            if progress_cb:
                progress_cb(
                    "compute_stats",
                    "正在计算统计信息",
                    f"已完成 episode {idx} 的统计 ({len(results)}/{total_episodes})",
                    len(results),
                    total_episodes,
                )
        return results

    def compute_stats(self, video_paths_by_episode=None, progress_cb=None,
                      skip_video_stats=False):
        """基于 episode stats 聚合全局统计 (lerobot aggregate_stats 公式)"""
        ep_stats_list = self.compute_episode_stats(
            video_paths_by_episode, progress_cb, skip_video_stats=skip_video_stats)
        all_keys = {}
        for es in ep_stats_list:
            for k in es["stats"]:
                all_keys.setdefault(k, []).append(es["stats"][k])

        global_stats = {}
        for key, stats_list in all_keys.items():
            mins = np.array([s["min"] for s in stats_list], dtype=np.float64)
            maxs = np.array([s["max"] for s in stats_list], dtype=np.float64)
            means = np.array([s["mean"] for s in stats_list], dtype=np.float64)
            stds = np.array([s["std"] for s in stats_list], dtype=np.float64)
            counts = np.array([
                s["count"][0] if isinstance(s.get("count"), list) else s["count"]
                for s in stats_list
            ], dtype=np.float64)
            total_count = counts.sum()
            count_weights = counts.reshape((len(counts),) + (1,) * (means.ndim - 1))
            total_mean = (means * count_weights).sum(0) / total_count
            total_var = (
                (stds ** 2 + (means - total_mean) ** 2) * count_weights
            ).sum(0) / total_count
            total_std = np.sqrt(np.maximum(0, total_var))

            merged = {
                "min":  mins.min(0).tolist(),
                "max":  maxs.max(0).tolist(),
                "mean": total_mean.tolist(),
                "std":  total_std.tolist(),
                "count": int(total_count),
            }

            for metric in stats_list[0]:
                if not metric.startswith("q"):
                    continue
                if not all(metric in s for s in stats_list):
                    continue
                values = np.array([s[metric] for s in stats_list], dtype=np.float64)
                merged[metric] = ((values * count_weights).sum(0) / total_count).tolist()

            global_stats[key] = merged
        return global_stats, ep_stats_list

    # ─── 保存 ───

    def save_as(self, output_path: str, progress_cb=None, append_paths=None,
                skip_video_stats=False):
        """另存为新数据集 (含重算的统计元数据，可在尾部拼接额外数据集)"""
        out = Path(output_path).resolve()

        def report(stage, title, detail="", current=0, total=0):
            if progress_cb:
                progress_cb(stage, title, detail, current, total)

        report("prepare", "正在准备保存", "正在创建输出目录...", 0, 1)
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True, exist_ok=True)

        meta_dir = out / "meta"
        meta_dir.mkdir()

        self._refresh_info()
        save_sources, merged_tasks = self._build_save_sources(append_paths or [], report)

        # ── 修正数据列 (timestamp, index) 以保证统计和 Parquet 一致 ──
        data_tpl = self.info.get(
            "data_path",
            "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet")
        video_tpl = self.info.get("video_path", "")
        chunks_size = self.info.get("chunks_size", 1000)
        features = self.info.get("features", {})
        video_keys = [
            k for k, meta in features.items()
            if meta.get("dtype") in ("image", "video")
        ]
        fps = self.info.get("fps", 30)

        merged_episode_meta = []
        save_jobs = []
        next_episode_index = 0
        global_idx = 0
        total_frames = 0

        for source in save_sources:
            editor = source["editor"]
            task_map = source["task_map"]
            for em in editor.episodes_meta:
                old_idx = em["episode_index"]
                if old_idx not in editor.episode_data:
                    continue

                df = editor.episode_data[old_idx]
                orig_idx = editor._orig_indices.get(old_idx, old_idx)
                orig_len = editor._orig_ep_lengths.get(orig_idx, len(df))
                resolved_task_index = editor._resolve_episode_task_index(old_idx, em)
                merged_task_index = task_map.get(resolved_task_index, 0 if merged_tasks else None)

                meta_copy = copy.deepcopy(em)
                meta_copy["episode_index"] = next_episode_index
                meta_copy["length"] = int(len(df))
                if merged_task_index is not None:
                    meta_copy["task_index"] = int(merged_task_index)
                    merged_task = merged_tasks[merged_task_index] if merged_task_index < len(merged_tasks) else {}
                    task_name = merged_task.get("task")
                    if task_name:
                        meta_copy["tasks"] = [task_name]
                merged_episode_meta.append(meta_copy)

                save_jobs.append({
                    "editor": editor,
                    "source_episode_index": old_idx,
                    "output_episode_index": next_episode_index,
                    "merged_task_index": merged_task_index,
                    "start_global_index": global_idx,
                    "length": len(df),
                    "orig_idx": orig_idx,
                    "orig_len": orig_len,
                })

                global_idx += len(df)
                total_frames += len(df)
                next_episode_index += 1

        merged_info = copy.deepcopy(self.info)
        merged_info["total_episodes"] = len(merged_episode_meta)
        merged_info["total_frames"] = int(total_frames)
        merged_info["total_tasks"] = len(merged_tasks)
        merged_info["chunks_size"] = int(merged_info.get("chunks_size", 1000) or 1000)
        merged_info["data_path"] = "data/chunk-{chunk_index:03d}/episode_{episode_index:06d}.parquet"
        merged_info["video_path"] = "videos/chunk-{chunk_index:03d}/{video_key}/episode_{episode_index:06d}.mp4"

        # ── info.json / episodes.jsonl / tasks.jsonl ──
        report("write_meta", "正在写入元数据", "正在写入 info / episodes / tasks ...", 0, 3)
        with open(meta_dir / "info.json", "w") as f:
            json.dump(merged_info, f, indent=2, ensure_ascii=False)

        report("write_meta", "正在写入元数据", "正在写入 episodes.jsonl ...", 1, 3)
        with open(meta_dir / "episodes.jsonl", "w") as f:
            for em in merged_episode_meta:
                f.write(json.dumps(em, ensure_ascii=False) + "\n")

        report("write_meta", "正在写入元数据", "正在写入 tasks.jsonl ...", 2, 3)
        with open(meta_dir / "tasks.jsonl", "w") as f:
            for t in merged_tasks:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        report("write_meta", "正在写入元数据", "元数据文件已完成", 3, 3)

        # ── Parquet 数据 + 收集视频任务 ──
        encode_tasks = []   # (src_path, keep_indices, dst_path)
        copy_tasks = []     # (src_path, dst_path)
        saved_video_paths = {}
        prepared_episode_data = {}
        total_episodes = len(save_jobs)
        report("write_parquet", "正在导出 Parquet", "正在写出每个 episode 的 parquet 文件...", 0, total_episodes)

        written_episodes = 0
        for job in save_jobs:
            editor = job["editor"]
            src_idx = job["source_episode_index"]
            idx = job["output_episode_index"]
            df = editor.episode_data[src_idx].copy()
            chunk = idx // chunks_size
            orig_idx = job["orig_idx"]
            orig_videos = editor._orig_video_files.get(orig_idx, {})
            frames_edited = (len(df) != job["orig_len"])

            if len(df) != job["orig_len"]:
                df["timestamp"] = [i / fps for i in range(len(df))]
            if "episode_index" in df.columns:
                df["episode_index"] = idx
            if "frame_index" in df.columns:
                df["frame_index"] = range(len(df))
            if "index" in df.columns:
                df["index"] = range(job["start_global_index"], job["start_global_index"] + len(df))
            if job["merged_task_index"] is not None and "task_index" in df.columns:
                df["task_index"] = int(job["merged_task_index"])
            prepared_episode_data[idx] = df.copy()

            # ── 保存 Parquet ──
            save_df = df.drop(columns=["_orig_frame_idx"], errors="ignore")
            try:
                rel = data_tpl.format(
                    episode_chunk=chunk, chunk_index=chunk, episode_index=idx)
            except KeyError:
                rel = f"data/chunk-{chunk:03d}/episode_{idx:06d}.parquet"
            pq_path = out / rel
            pq_path.parent.mkdir(parents=True, exist_ok=True)
            save_df.to_parquet(pq_path, index=False)
            written_episodes += 1
            report(
                "write_parquet",
                "正在导出 Parquet",
                f"已写出 episode {idx} ({written_episodes}/{total_episodes})",
                written_episodes,
                total_episodes,
            )

            # ── 收集视频任务 ──
            for cam_name, src_path_str in orig_videos.items():
                src = Path(src_path_str)
                if not src.exists():
                    continue

                vkey = self._resolve_video_feature_key(cam_name, video_keys)

                if video_tpl:
                    try:
                        dst_rel = video_tpl.format(
                            episode_chunk=chunk, chunk_index=chunk,
                            video_key=vkey, episode_index=idx)
                    except KeyError:
                        dst_rel = f"videos/chunk-{chunk:03d}/{vkey}/episode_{idx:06d}.mp4"
                else:
                    dst_rel = f"videos/chunk-{chunk:03d}/{vkey}/episode_{idx:06d}.mp4"

                dst = out / dst_rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                saved_video_paths.setdefault(idx, {})[vkey] = str(dst)

                if frames_edited:
                    keep = [int(x) for x in editor.episode_data[src_idx]["_orig_frame_idx"].tolist()]
                    encode_tasks.append((src_path_str, keep, str(dst)))
                else:
                    copy_tasks.append((str(src), str(dst)))

        # ── 直接复制未修改的视频 ──
        total_copy = len(copy_tasks)
        if total_copy:
            report("copy_videos", "正在复制未修改视频", f"共 {total_copy} 个视频待复制...", 0, total_copy)
        for i, (src, dst) in enumerate(copy_tasks, 1):
            shutil.copy2(src, dst)
            report("copy_videos", "正在复制未修改视频", f"已复制 {i}/{total_copy} 个视频", i, total_copy)
        log.info(f"已复制 {len(copy_tasks)} 个未修改视频")

        # ── 多线程并行重编码修改过的视频 ──
        if encode_tasks:
            src_params = self._probe_video_params(encode_tasks[0][0])
            workers = min(len(encode_tasks), max(1, (os.cpu_count() or 4) // 2))
            report(
                "encode_videos",
                "正在重编码修改后视频",
                f"共 {len(encode_tasks)} 个视频，使用 {workers} 路并行...",
                0,
                len(encode_tasks),
            )
            log.info(
                f"开始视频重编码: {len(encode_tasks)} 个视频, "
                f"{workers} 路并行 (codec={src_params.get('codec','?')} "
                f"keyint={src_params.get('keyint','?')})")

            failed = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {
                    pool.submit(
                        self._reencode_video, src, keep, dst, src_params
                    ): (src, dst)
                    for src, keep, dst in encode_tasks
                }
                for i, future in enumerate(as_completed(future_map), 1):
                    src, dst = future_map[future]
                    try:
                        ok = future.result()
                    except Exception as e:
                        log.warning(f"编码异常: {e}")
                        ok = False
                    if not ok:
                        shutil.copy2(src, dst)
                        failed += 1
                    log.info(f"视频编码进度: {i}/{len(encode_tasks)}")
                    report(
                        "encode_videos",
                        "正在重编码修改后视频",
                        f"已处理 {i}/{len(encode_tasks)} 个视频",
                        i,
                        len(encode_tasks),
                    )

            if failed:
                log.warning(f"{failed} 个视频重编码失败, 已回退复制原始文件")

        # ── stats.json + episodes_stats.jsonl ──
        stats_detail = "正在重算 state/action 等数值统计..."
        if not skip_video_stats:
            stats_detail = "正在重算 state/action 与图像/视频统计..."
        report("compute_stats", "正在计算统计信息", stats_detail, 0, len(save_jobs))
        original_state = {
            "episodes_meta": self.episodes_meta,
            "episode_data": self.episode_data,
            "_orig_indices": self._orig_indices,
            "_orig_video_files": self._orig_video_files,
            "_orig_ep_lengths": self._orig_ep_lengths,
            "tasks": self.tasks,
            "info": self.info,
        }

        merged_episode_data = {}
        merged_orig_indices = {}
        merged_orig_video_files = {}
        merged_orig_ep_lengths = {}
        for job in save_jobs:
            out_idx = job["output_episode_index"]
            merged_episode_data[out_idx] = prepared_episode_data[out_idx]
            merged_orig_indices[out_idx] = out_idx
            merged_orig_video_files[out_idx] = saved_video_paths.get(out_idx, {})
            merged_orig_ep_lengths[out_idx] = len(prepared_episode_data[out_idx])

        try:
            self.episodes_meta = merged_episode_meta
            self.episode_data = merged_episode_data
            self._orig_indices = merged_orig_indices
            self._orig_video_files = merged_orig_video_files
            self._orig_ep_lengths = merged_orig_ep_lengths
            self.tasks = merged_tasks
            self.info = merged_info

            stats_video_paths = None if skip_video_stats else saved_video_paths
            global_stats, ep_stats = self.compute_stats(
                stats_video_paths, report, skip_video_stats=skip_video_stats)
            with open(meta_dir / "stats.json", "w") as f:
                json.dump(global_stats, f, indent=2, ensure_ascii=False)
            with open(meta_dir / "episodes_stats.jsonl", "w") as f:
                for s in ep_stats:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")
        finally:
            self.episodes_meta = original_state["episodes_meta"]
            self.episode_data = original_state["episode_data"]
            self._orig_indices = original_state["_orig_indices"]
            self._orig_video_files = original_state["_orig_video_files"]
            self._orig_ep_lengths = original_state["_orig_ep_lengths"]
            self.tasks = original_state["tasks"]
            self.info = original_state["info"]

        self.modified = False
        report("done", "保存完成", f"数据集已保存到: {out}", 1, 1)
        log.info(f"数据集已保存到: {out}")
        return True


# ═══════════════════════ Flask 路由 ═══════════════════════

_training_check_service = TrainingCheckService(
    DatasetEditor,
    lambda: globals().get("_joint_config_override"),
    log,
)
_stats_verify_service = StatsVerifyService()
_health_check_service = HealthCheckService(
    DatasetEditor,
    lambda: globals().get("_joint_config_override"),
    image_analyzer_cls=img_analyzer.ImageAnalyzer,
    logger=log,
)

@app.route("/")
def portal():
    return render_template("portal.html")


@app.route("/visualize")
def visualize():
    return render_template("index.html")


@app.route("/ros2-convert")
def ros2_convert():
    return render_template("ros2_convert.html")


@app.route("/data-analysis")
def data_analysis():
    return render_template("analysis.html")


@app.route("/converter")
def converter_page():
    return render_template("converter.html")


@app.route("/batch-tools")
def batch_tools_page():
    return render_template("batch_tools.html")


@app.route("/field-editor")
def field_editor_page():
    return render_template("field_editor.html")


@app.route("/video-codec")
def video_codec_page():
    return render_template("video_codec.html")


@app.route("/image-analysis")
def image_analysis_page():
    return render_template("image_analysis.html")


@app.route("/verify-stats")
def verify_stats_page():
    return render_template("verify_stats.html")


@app.route("/docs/<path:doc_name>")
def serve_doc(doc_name):
    docs_dir = (Path(__file__).parent / "docs").resolve()
    target = (docs_dir / doc_name).resolve()
    try:
        target.relative_to(docs_dir)
    except ValueError:
        abort(403)
    if not target.exists() or not target.is_file():
        abort(404)
    mime = "text/markdown; charset=utf-8" if target.suffix.lower() == ".md" else "text/plain; charset=utf-8"
    return send_file(str(target), mimetype=mime)


@app.route("/api/browse")
def api_browse():
    """浏览服务端目录结构，返回子目录列表。跨平台支持。"""
    import platform
    req_path = request.args.get("path", "").strip()

    # Windows: 空路径时列出盘符
    if not req_path and platform.system() == "Windows":
        import string
        drives = []
        for letter in string.ascii_uppercase:
            dp = Path(f"{letter}:\\")
            if dp.exists():
                drives.append({"name": f"{letter}:\\", "path": str(dp)})
        return jsonify({"current": "", "parent": "", "dirs": drives})

    base = Path(req_path) if req_path else Path("/")
    if not base.is_dir():
        return jsonify({"error": f"路径不存在或不是目录: {req_path}"}), 400

    parent = str(base.parent) if base != base.parent else ""
    dirs = []
    try:
        for entry in sorted(base.iterdir(), key=lambda e: e.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": str(entry)})
    except PermissionError:
        pass

    return jsonify({"current": str(base), "parent": parent, "dirs": dirs})


@app.route("/api/load", methods=["POST"])
def api_load():
    global _editor
    data = request.get_json()
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"error": "请输入数据集路径"}), 400

    p = Path(path)
    if not p.exists():
        return jsonify({"error": f"路径不存在: {path}"}), 400
    if not (p / "meta" / "info.json").exists():
        return jsonify({"error": "无效的 LeRobot 数据集 (缺少 meta/info.json)"}), 400

    try:
        _editor = DatasetEditor(path, joint_config=_joint_config_override)
        return jsonify({
            "success": True,
            "summary": _editor.get_summary(),
            "episodes": _editor.get_episodes(),
            "joint_names": _editor.joint_names,
            "joint_groups": _editor.joint_groups,
        })
    except Exception as e:
        log.exception("加载数据集失败")
        return jsonify({"error": str(e)}), 500


@app.route("/api/analysis/load", methods=["POST"])
def api_analysis_load():
    global _analysis_editor
    data = request.get_json() or {}
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"error": "请输入数据集路径"}), 400

    p = Path(path)
    if not p.exists():
        return jsonify({"error": f"路径不存在: {path}"}), 400
    if not (p / "meta" / "info.json").exists():
        return jsonify({"error": "无效的 LeRobot 数据集 (缺少 meta/info.json)"}), 400

    try:
        _analysis_editor = DatasetEditor(path, joint_config=_joint_config_override)
        report = _analysis_editor.build_joint_analysis_report()
        return jsonify({"success": True, **report})
    except Exception as e:
        log.exception("分析页加载数据集失败")
        return jsonify({"error": str(e)}), 500


@app.route("/api/analysis/smooth_actions", methods=["POST"])
def api_analysis_smooth_actions():
    global _analysis_editor
    data = request.get_json() or {}
    path = data.get("path", "").strip()
    output = data.get("output_path", "").strip()
    window = int(data.get("window", 5) or 5)
    skip_video_stats = bool(data.get("skip_video_stats", True))
    overwrite = bool(data.get("overwrite", False))

    if not path:
        return jsonify({"error": "请输入数据集路径"}), 400
    if not output:
        return jsonify({"error": "请指定平滑后数据集输出路径"}), 400

    p = Path(path).resolve()
    out = Path(output).resolve()
    if not p.exists():
        return jsonify({"error": f"路径不存在: {path}"}), 400
    if not (p / "meta" / "info.json").exists():
        return jsonify({"error": "无效的 LeRobot 数据集 (缺少 meta/info.json)"}), 400
    if out == p:
        return jsonify({"error": "输出路径不能与原数据集相同，请保存到一个新目录"}), 400
    if out.exists() and not overwrite:
        return jsonify({"error": "输出路径已存在。如需覆盖，请勾选允许覆盖输出目录"}), 400

    try:
        set_save_progress("prepare", "正在准备动作平滑", "正在加载数据集并计算突变点...", 0, 1, True)
        editor = DatasetEditor(str(p), joint_config=_joint_config_override)
        smoothing_report = editor.smooth_action_jumps(window=window)
        if smoothing_report["anomaly_count"] <= 0:
            set_save_progress("done", "未发现突变点", "没有需要写回的 action 平滑修正", 1, 1, False)
            _analysis_editor = editor
            report = editor.build_joint_analysis_report()
            return jsonify({
                "success": True,
                "path": str(p),
                "output_path": None,
                "smoothing": smoothing_report,
                "report": report,
                "message": "未发现需要平滑的 action 突变点，未写出新数据集",
            })

        editor.save_as(str(out), set_save_progress, skip_video_stats=skip_video_stats)
        _analysis_editor = DatasetEditor(str(out), joint_config=_joint_config_override)
        report = _analysis_editor.build_joint_analysis_report()
        set_save_progress("done", "动作平滑完成", f"平滑数据集已保存到: {out}", 1, 1, False)
        return jsonify({
            "success": True,
            "path": str(p),
            "output_path": str(out),
            "smoothing": smoothing_report,
            "report": report,
        })
    except Exception as e:
        set_save_progress("error", "动作平滑失败", str(e), 0, 0, False)
        log.exception("动作平滑写回失败")
        return jsonify({"error": str(e)}), 500


@app.route("/training-check")
def training_check_page():
    return render_template("training_check.html")



@app.route("/api/training-check/inspect", methods=["POST"])
def api_training_check_inspect():
    data = request.get_json() or {}
    root = (data.get("path") or "").strip()
    if not root:
        return jsonify({"error": "path 不能为空"}), 400
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists():
        return jsonify({"error": f"路径不存在: {root_path}"}), 400
    try:
        return jsonify(_training_check_service.inspect_dataset(root_path))
    except Exception as e:
        log.exception("训练可用性检查 inspect 失败")
        return jsonify({"error": str(e)}), 500


@app.route("/api/training-check/run", methods=["POST"])
def api_training_check_run():
    data = request.get_json() or {}
    root = (data.get("path") or "").strip()
    if not root:
        return jsonify({"error": "path 不能为空"}), 400
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists():
        return jsonify({"error": f"路径不存在: {root_path}"}), 400
    try:
        report = _training_check_service.run_training_usability_check(
            root_path,
            profile=(data.get("profile") or "general"),
            include_videos=bool(data.get("include_videos", False)),
            max_issue_examples=int(data.get("max_issue_examples", 5) or 5),
        )
        return jsonify(report)
    except Exception as e:
        log.exception("训练可用性检查失败")
        return jsonify({"error": str(e)}), 500


@app.route("/api/training-check/fix", methods=["POST"])
def api_training_check_fix():
    data = request.get_json() or {}
    root = (data.get("path") or "").strip()
    output = (data.get("output_path") or "").strip()
    if not root:
        return jsonify({"error": "path 不能为空"}), 400
    if not output:
        return jsonify({"error": "output_path 不能为空"}), 400
    src_path = Path(root).expanduser().resolve()
    dst_path = Path(output).expanduser().resolve()
    if not src_path.exists():
        return jsonify({"error": f"路径不存在: {src_path}"}), 400
    try:
        result = _training_check_service.fix_training_dataset_format(
            src_path,
            dst_path,
            overwrite=bool(data.get("overwrite", False)),
            profile=(data.get("profile") or "general"),
        )
        return jsonify(result)
    except Exception as e:
        log.exception("训练数据集格式修复失败")
        return jsonify({"error": str(e)}), 500


# ═══════════════════════ 数据集健康度评分 API ═══════════════════════

_health_progress_lock = threading.Lock()
_health_progress: dict = {
    "running": False,
    "stage": "",
    "title": "",
    "detail": "",
    "current": 0,
    "total": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "result": None,
}


def _set_health_progress(**kwargs):
    with _health_progress_lock:
        _health_progress.update(kwargs)


def _health_progress_cb(stage, title, current, total):
    with _health_progress_lock:
        _health_progress["stage"] = stage
        _health_progress["title"] = title
        _health_progress["current"] = int(current)
        _health_progress["total"] = int(total)


def _get_health_progress():
    with _health_progress_lock:
        data = dict(_health_progress)
    total = max(0, int(data.get("total", 0) or 0))
    current = max(0, int(data.get("current", 0) or 0))
    data["percent"] = max(0, min(100, round(current * 100 / total))) if total > 0 else 0
    started_at = data.get("started_at")
    finished_at = data.get("finished_at")
    if started_at:
        end = finished_at or time.time()
        data["elapsed_sec"] = max(0.0, end - started_at)
    return data


@app.route("/health-check")
def health_check_page():
    return render_template("health_check.html")


@app.route("/api/health-check/run", methods=["POST"])
def api_health_check_run():
    data = request.get_json() or {}
    root = (data.get("path") or "").strip()
    if not root:
        return jsonify({"error": "path 不能为空"}), 400
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists():
        return jsonify({"error": f"路径不存在: {root_path}"}), 400

    include_image = bool(data.get("include_image_quality", True))
    image_samples = int(data.get("image_sample_episodes", 3) or 3)

    with _health_progress_lock:
        if _health_progress.get("running"):
            return jsonify({"error": "已有健康度检查任务在进行中"}), 400
        _health_progress.clear()
        _health_progress.update({
            "running": True,
            "stage": "init",
            "title": "初始化",
            "detail": "",
            "current": 0,
            "total": 7,
            "started_at": time.time(),
            "finished_at": None,
            "error": None,
            "result": None,
        })

    def worker():
        try:
            report = _health_check_service.run_health_check(
                root_path,
                include_image_quality=include_image,
                image_sample_episodes=image_samples,
                progress_cb=_health_progress_cb,
            )
            _set_health_progress(
                running=False,
                finished_at=time.time(),
                stage="done",
                title="健康度检查完成",
                result=report,
            )
        except Exception as e:
            log.exception("健康度检查失败")
            _set_health_progress(
                running=False,
                finished_at=time.time(),
                error=str(e),
                stage="error",
                title="检查失败",
                detail=str(e),
            )

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"success": True, "message": "健康度检查任务已启动"})


@app.route("/api/health-check/progress")
def api_health_check_progress():
    return jsonify(_get_health_progress())


@app.route("/api/health-check/cancel", methods=["POST"])
def api_health_check_cancel():
    _set_health_progress(
        running=False, finished_at=time.time(),
        error="用户取消", stage="cancelled", title="已取消",
    )
    return jsonify({"success": True})


@app.route("/api/merge/inspect", methods=["POST"])
def api_merge_inspect():
    if _editor is None:
        return jsonify({"error": "请先加载主数据集"}), 400

    data = request.get_json() or {}
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"error": "请输入待拼接数据集路径"}), 400

    p = Path(path)
    if not p.exists():
        return jsonify({"error": f"路径不存在: {path}"}), 400
    if not (p / "meta" / "info.json").exists():
        return jsonify({"error": "无效的 LeRobot 数据集 (缺少 meta/info.json)"}), 400

    try:
        candidate = DatasetEditor(path, joint_config=_joint_config_override)
        _editor.assert_merge_compatible(candidate)
        return jsonify({
            "success": True,
            "summary": candidate.get_summary(),
            "basename": p.name,
        })
    except Exception as e:
        log.exception("检查拼接数据集失败")
        return jsonify({"error": str(e)}), 500


@app.route("/api/episodes")
def api_episodes():
    if _editor is None:
        return jsonify({"error": "未加载数据集"}), 400
    return jsonify({"episodes": _editor.get_episodes(), "summary": _editor.get_summary()})


@app.route("/api/integrity")
def api_integrity():
    if _editor is None:
        return jsonify({"error": "未加载数据集"}), 400
    try:
        report = _editor.check_integrity()
        return jsonify({"success": True, **report})
    except Exception as e:
        log.exception("一致性检查失败")
        return jsonify({"error": str(e)}), 500


@app.route("/api/episode/<int:ep_idx>")
def api_episode(ep_idx):
    if _editor is None:
        return jsonify({"error": "未加载数据集"}), 400
    data = _editor.get_episode_data(ep_idx)
    if data is None:
        return jsonify({"error": f"Episode {ep_idx} 不存在"}), 404
    return jsonify(data)


@app.route("/api/video")
def api_video():
    if _editor is None:
        abort(400)
    path = request.args.get("path", "")
    p = Path(path).resolve()
    try:
        p.relative_to(_editor.original_root)
    except ValueError:
        abort(403)
    if not p.exists():
        abort(404)
    return send_file(str(p), mimetype="video/mp4")


@app.route("/api/urdf/upload", methods=["POST"])
def api_urdf_upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "请至少上传一个 URDF 相关文件"}), 400

    manifest_raw = request.form.get("manifest", "").strip()
    try:
        manifest = json.loads(manifest_raw) if manifest_raw else []
    except json.JSONDecodeError:
        return jsonify({"error": "上传清单格式无效"}), 400

    if manifest and len(manifest) != len(files):
        return jsonify({"error": "上传文件与清单数量不一致"}), 400

    package_id = uuid.uuid4().hex
    package_dir = Path(tempfile.mkdtemp(prefix=f"urdf_{package_id}_"))

    saved_paths = []
    urdf_candidates = []
    for idx, storage in enumerate(files):
        fallback_name = Path(storage.filename or f"upload_{idx}").name
        rel_path = (
            manifest[idx].get("path")
            if idx < len(manifest) and isinstance(manifest[idx], dict)
            else None
        )
        rel_path = _safe_upload_rel_path(rel_path, fallback_name)
        dst = package_dir / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        storage.save(dst)
        saved_paths.append(rel_path)
        if dst.suffix.lower() == ".urdf":
            urdf_candidates.append(rel_path)

    if not urdf_candidates:
        shutil.rmtree(package_dir, ignore_errors=True)
        return jsonify({"error": "未检测到 .urdf 文件"}), 400

    root_rel = request.form.get("root_file", "").strip()
    root_rel = _safe_upload_rel_path(root_rel, urdf_candidates[0]) if root_rel else ""
    if not root_rel or root_rel not in urdf_candidates:
        root_rel = sorted(urdf_candidates)[0]

    root_path = package_dir / root_rel
    try:
        info = _inspect_urdf(root_path)
    except Exception as e:
        shutil.rmtree(package_dir, ignore_errors=True)
        return jsonify({"error": f"URDF 解析失败: {e}"}), 400

    _urdf_assets[package_id] = {
        "dir": package_dir,
        "root_file": root_rel,
        "files": saved_paths,
        **info,
    }

    return jsonify({
        "success": True,
        "package_id": package_id,
        "robot_name": info["robot_name"],
        "root_file": root_rel,
        "joint_names": info["joint_names"],
        "movable_joint_names": info["movable_joint_names"],
        "joint_info": info.get("joint_info", {}),
        "asset_root": f"/api/urdf_asset/{package_id}",
        "root_url": f"/api/urdf_asset/{package_id}/{root_rel}",
    })


@app.route("/api/urdf/load-from-dir", methods=["POST"])
def api_urdf_load_from_dir():
    """从服务端目录加载 URDF，无需上传文件。

    请求 JSON: { "path": "/abs/path/to/urdf_dir_or_file" }
    如果是目录，自动搜索 .urdf 文件；如果是 .urdf 文件，直接使用。
    """
    data = request.get_json(silent=True) or {}
    raw_path = (data.get("path") or "").strip()
    if not raw_path:
        return jsonify({"error": "请提供 URDF 目录或文件路径"}), 400

    target = Path(raw_path)
    if target.is_dir():
        urdf_candidates = sorted(p for p in target.rglob("*.urdf") if not p.name.startswith("_served_"))
        if not urdf_candidates:
            return jsonify({"error": f"目录中未找到 .urdf 文件: {target}"}), 400
        root_path = urdf_candidates[0]
    elif target.is_file() and target.suffix.lower() == ".urdf":
        root_path = target
    else:
        return jsonify({"error": "路径不存在或不是 .urdf 文件/目录"}), 400

    # 读取 URDF 内容
    try:
        urdf_text = root_path.read_text(encoding="utf-8")
    except Exception:
        urdf_text = root_path.read_text(encoding="utf-8", errors="ignore")

    import re as _re
    has_package_prefix = 'package://' in urdf_text

    # 确定 package_dir: mesh 文件实际所在的根目录
    package_dir = root_path.parent.resolve()

    if has_package_prefix:
        # package://pkg_name/path 风格
        match = _re.search(r'package://([^/]+)/', urdf_text)
        if match:
            pkg_name = match.group(1)
            for candidate in [root_path.parent, root_path.parent.parent, root_path.parent.parent.parent]:
                if candidate.name == pkg_name and candidate.is_dir():
                    package_dir = candidate.resolve()
                    break
                child = candidate / pkg_name
                if child.is_dir():
                    package_dir = child.resolve()
                    break
    else:
        # 检查是否有 ../ 相对路径（mesh 在 URDF 的上级目录）
        mesh_matches = _re.findall(r'filename="([^"]+\.(?:stl|STL|dae|DAE|obj|OBJ|glb|GLB|gltf|GLTF))"', urdf_text)
        if mesh_matches:
            # 找到最深的公共相对前缀，如 ../meshes/ → URDF 需要放在 package_dir 根
            max_up = 0
            for mm in mesh_matches:
                up_count = mm.count('../')
                max_up = max(max_up, up_count)
            if max_up > 0:
                # 向上找到包含 mesh 文件的目录
                candidate = root_path.parent
                for _ in range(max_up):
                    candidate = candidate.parent
                package_dir = candidate.resolve()

    final_root_rel = root_path.relative_to(package_dir).as_posix()

    def _rewrite_mesh_filename(match):
        quote = match.group(1)
        raw = match.group(2).replace("\\", "/")
        lower = raw.lower()
        if not lower.endswith((".stl", ".dae", ".obj", ".glb", ".gltf")):
            return match.group(0)
        if raw.startswith(("http://", "https://", "data:")):
            return match.group(0)
        if raw.startswith("package://"):
            rewritten_path = _re.sub(r"^package://[^/]+/", "", raw)
            return f"filename={quote}{rewritten_path}{quote}"
        mesh_abs = (root_path.parent / raw).resolve()
        try:
            rewritten_path = mesh_abs.relative_to(package_dir).as_posix()
        except ValueError:
            rewritten_path = raw
        return f"filename={quote}{rewritten_path}{quote}"

    rewritten = _re.sub(r'filename=(["\'])([^"\']+)\1', _rewrite_mesh_filename, urdf_text)
    if rewritten != urdf_text or final_root_rel != root_path.name:
        rewritten_name = f"_served_{root_path.stem}.urdf"
        (package_dir / rewritten_name).write_text(rewritten, encoding="utf-8")
        final_root_rel = rewritten_name

    try:
        info = _inspect_urdf(root_path)
    except Exception as e:
        return jsonify({"error": f"URDF 解析失败: {e}"}), 400

    package_id = uuid.uuid4().hex

    _urdf_assets[package_id] = {
        "dir": package_dir,
        "root_file": final_root_rel,
        **info,
    }

    return jsonify({
        "success": True,
        "package_id": package_id,
        "robot_name": info["robot_name"],
        "root_file": final_root_rel,
        "joint_names": info["joint_names"],
        "movable_joint_names": info["movable_joint_names"],
        "joint_info": info.get("joint_info", {}),
        "asset_root": f"/api/urdf_asset/{package_id}",
        "root_url": f"/api/urdf_asset/{package_id}/{final_root_rel}",
        "package_name": None,
    })


@app.route("/api/urdf_asset/<package_id>/<path:rel_path>")
def api_urdf_asset(package_id, rel_path):
    package = _urdf_assets.get(package_id)
    if package is None:
        abort(404)

    base_dir = Path(package["dir"]).resolve()
    safe_rel = _safe_upload_rel_path(rel_path, Path(rel_path).name)
    target = (base_dir / safe_rel).resolve()
    try:
        target.relative_to(base_dir)
    except ValueError:
        abort(403)

    if not target.exists() or not target.is_file():
        abort(404)
    return send_file(str(target))


@app.route("/api/delete_episodes", methods=["POST"])
def api_delete_episodes():
    if _editor is None:
        return jsonify({"error": "未加载数据集"}), 400
    data = request.get_json()
    indices = data.get("indices", [])
    if not indices:
        return jsonify({"error": "未指定要删除的 episode"}), 400

    remaining = _editor.delete_episodes(indices)
    return jsonify({
        "success": True,
        "remaining_episodes": remaining,
        "episodes": _editor.get_episodes(),
        "summary": _editor.get_summary(),
    })


@app.route("/api/delete_frames", methods=["POST"])
def api_delete_frames():
    if _editor is None:
        return jsonify({"error": "未加载数据集"}), 400
    data = request.get_json()
    ep_idx = data.get("episode_index")
    frame_indices = data.get("frame_indices", [])
    if ep_idx is None or not frame_indices:
        return jsonify({"error": "参数不完整"}), 400

    remaining = _editor.delete_frames(ep_idx, frame_indices)
    ep_data = _editor.get_episode_data(ep_idx) if remaining > 0 else None
    return jsonify({
        "success": True,
        "remaining_frames": remaining,
        "episode_data": ep_data,
        "episodes": _editor.get_episodes(),
        "summary": _editor.get_summary(),
    })


@app.route("/api/analyze_deletion", methods=["POST"])
def api_analyze_deletion():
    if _editor is None:
        return jsonify({"error": "未加载数据集"}), 400
    data = request.get_json()
    ep_idx = data.get("episode_index")
    frame_indices = data.get("frame_indices", [])
    if ep_idx is None or not frame_indices:
        return jsonify({"error": "参数不完整"}), 400
    k_sigma = data.get("k_sigma", 3.0)
    active_joints = data.get("active_joint_indices")
    result = _editor.analyze_deletion(
        ep_idx, frame_indices, k_sigma, active_joints)
    return jsonify(result)


@app.route("/api/save_progress")
def api_save_progress():
    return jsonify(get_save_progress())


@app.route("/api/save", methods=["POST"])
def api_save():
    if _editor is None:
        return jsonify({"error": "未加载数据集"}), 400
    data = request.get_json() or {}
    output = data.get("output_path", "").strip()
    append_paths = data.get("append_paths") or []
    skip_video_stats = bool(data.get("skip_video_stats", False))
    if not output:
        return jsonify({"error": "请指定保存路径"}), 400
    if not isinstance(append_paths, list):
        return jsonify({"error": "append_paths 格式无效"}), 400

    try:
        set_save_progress("prepare", "正在准备保存", "正在初始化保存任务...", 0, 1, True)
        _editor.save_as(
            output,
            set_save_progress,
            append_paths=append_paths,
            skip_video_stats=skip_video_stats,
        )
        set_save_progress("done", "保存完成", f"数据集已保存到: {output}", 1, 1, False)
        return jsonify({"success": True, "path": output})
    except Exception as e:
        set_save_progress("error", "保存失败", str(e), 0, 0, False)
        log.exception("保存失败")
        return jsonify({"error": str(e)}), 500


def _batch_options_from_request(data):
    def opt_int(name, default=None):
        val = data.get(name, default)
        if val in ("", None):
            return default
        return int(val)

    def opt_float(name, default):
        val = data.get(name, default)
        if val in ("", None):
            return default
        return float(val)

    return {
        "auto_length_iqr": bool(data.get("auto_length_iqr", False)),
        "iqr_multiplier": opt_float("iqr_multiplier", 1.5),
        "trim_static_edges": bool(data.get("trim_static_edges", False)),
        "motion_threshold": opt_float("motion_threshold", 10.0),
        "margin_frames": opt_int("margin_frames", 0),
        "min_static_frames": opt_int("min_static_frames", 1),
        "joint_indices": batch_tools.parse_joint_indices(data.get("joint_indices")),
        "motion_metric": data.get("motion_metric") or "euclidean",
    }


def _build_batch_editor_and_plan(data, include_curves=False):
    input_path = str(data.get("input_path", "")).strip()
    if not input_path:
        raise ValueError("请指定输入数据集路径")
    opts = _batch_options_from_request(data)
    if (
        not opts["auto_length_iqr"]
        and not opts["trim_static_edges"]
    ):
        raise ValueError("至少需要启用 IQR 长度自动删除或静止段裁剪")
    editor = DatasetEditor(input_path)
    plan = batch_tools.build_batch_plan(
        editor,
        **opts,
        include_curves=include_curves,
        max_curve_episodes=opt_int_from_data(data, "max_curve_episodes", 80),
        max_curve_points=opt_int_from_data(data, "max_curve_points", 260),
        max_curve_dims=opt_int_from_data(data, "max_curve_dims", 8),
    )
    return editor, plan


def _read_saved_stats_keys(output_path):
    stats_path = Path(output_path) / "meta" / "stats.json"
    if not stats_path.exists():
        return []
    try:
        with open(stats_path, "r") as f:
            stats = json.load(f)
        if isinstance(stats, dict):
            return sorted(str(k) for k in stats.keys())
    except Exception:
        log.warning(f"读取 stats keys 失败: {stats_path}", exc_info=True)
    return []


def opt_int_from_data(data, name, default):
    val = data.get(name, default)
    if val in ("", None):
        return default
    return int(val)


@app.route("/api/batch_tools/preview", methods=["POST"])
def api_batch_tools_preview():
    try:
        _editor_local, plan = _build_batch_editor_and_plan(
            request.get_json() or {}, include_curves=True)
        return jsonify({"success": True, "plan": plan})
    except Exception as e:
        log.exception("批处理预览失败")
        return jsonify({"error": str(e)}), 400


@app.route("/api/batch_tools/episode_curve", methods=["POST"])
def api_batch_tools_episode_curve():
    data = request.get_json() or {}
    try:
        ep_idx = int(data.get("episode_index"))
        editor, plan = _build_batch_editor_and_plan(data, include_curves=False)
        trim_row = None
        for row in plan.get("static_trims", []):
            if int(row.get("episode_index")) == ep_idx:
                trim_row = row
                break
        length_row = None
        for row in plan.get("length_deletions", []):
            if int(row.get("episode_index")) == ep_idx:
                length_row = row
                break
        reason = "static edge trim"
        if length_row:
            reason = "IQR length deletion"
        curve = batch_tools.build_episode_curve_preview(
            editor,
            ep_idx,
            trim_row=trim_row,
            reason=reason,
            max_points=opt_int_from_data(data, "max_curve_points", 260),
            max_dims=opt_int_from_data(data, "max_curve_dims", 8),
        )
        return jsonify({"success": True, "curve": curve})
    except Exception as e:
        log.exception("加载 episode 曲线失败")
        return jsonify({"error": str(e)}), 400


@app.route("/api/batch_tools/run", methods=["POST"])
def api_batch_tools_run():
    data = request.get_json() or {}
    output_path = str(data.get("output_path", "")).strip()
    if not output_path:
        return jsonify({"error": "请指定输出数据集路径"}), 400
    try:
        input_path = Path(str(data.get("input_path", "")).strip()).resolve()
        out_path = Path(output_path).resolve()
        if input_path == out_path:
            return jsonify({"error": "输出路径不能和输入路径相同，请另存为新目录"}), 400

        editor, plan = _build_batch_editor_and_plan(data, include_curves=False)
        if plan["keep_episodes"] <= 0 and not data.get("allow_empty", False):
            return jsonify({"error": "当前设置会删除全部 episode；如确实需要，请勾选允许删空"}), 400
        set_save_progress("prepare", "正在准备批量裁剪", "正在应用批处理计划...", 0, 1, True)
        result = batch_tools.apply_batch_plan(editor, plan)
        editor.save_as(
            str(out_path),
            set_save_progress,
            skip_video_stats=bool(data.get("skip_video_stats", False)),
        )
        stats_keys = _read_saved_stats_keys(out_path)
        set_save_progress("done", "保存完成", f"数据集已保存到: {out_path}", 1, 1, False)
        return jsonify({
            "success": True,
            "path": str(out_path),
            "plan": plan,
            "result": result,
            "stats_keys": stats_keys,
            "skip_video_stats": bool(data.get("skip_video_stats", False)),
        })
    except Exception as e:
        set_save_progress("error", "批处理执行失败", str(e), 0, 0, False)
        log.exception("批处理执行失败")
        return jsonify({"error": str(e)}), 500


# ═══════════════════════ 字段编辑器 API ═══════════════════════

def _load_field_editor(data):
    """从请求中构造 DatasetEditor（不写盘，用于 preview / dry-run）。"""
    input_path = str(data.get("input_path", "")).strip()
    if not input_path:
        raise ValueError("请指定输入数据集路径")
    return DatasetEditor(input_path)


@app.route("/api/field_editor/preview", methods=["POST"])
def api_field_editor_preview():
    """返回当前数据集所有字段的预览信息。"""
    try:
        editor = _load_field_editor(request.get_json() or {})
        result = field_editor.build_preview(editor)
        return jsonify({"success": True, **result})
    except Exception as e:
        log.exception("字段编辑器预览失败")
        return jsonify({"error": str(e)}), 400


@app.route("/api/field_editor/preview_rename", methods=["POST"])
def api_field_editor_preview_rename():
    """dry-run：预览重命名效果，不写盘。"""
    try:
        data = request.get_json() or {}
        editor = _load_field_editor(data)
        renames = field_editor.parse_rename_pairs(data.get("renames"))
        if not renames:
            return jsonify({"error": "未提供有效的重命名规则"}), 400
        result = field_editor.preview_rename(
            editor, renames,
            rename_names=bool(data.get("rename_names", True)),
        )
        return jsonify({"success": True, "result": result})
    except Exception as e:
        log.exception("重命名预览失败")
        return jsonify({"error": str(e)}), 400


@app.route("/api/field_editor/preview_add", methods=["POST"])
def api_field_editor_preview_add():
    """dry-run：预览添加字段效果，不写盘。"""
    try:
        data = request.get_json() or {}
        editor = _load_field_editor(data)
        shape_raw = data.get("shape")
        if shape_raw in ("", None):
            shape = None
        elif isinstance(shape_raw, list):
            shape = [int(x) for x in shape_raw if x not in ("", None)]
        else:
            shape = [int(shape_raw)]
        result = field_editor.preview_add(
            editor,
            str(data.get("field_name", "")).strip(),
            dtype=str(data.get("dtype", "float32") or "float32"),
            shape=shape,
            default=data.get("default", 0.0),
            names=data.get("names"),
        )
        return jsonify({"success": True, "result": result})
    except Exception as e:
        log.exception("添加字段预览失败")
        return jsonify({"error": str(e)}), 400


@app.route("/api/field_editor/preview_delete", methods=["POST"])
def api_field_editor_preview_delete():
    """dry-run：预览删除字段效果，不写盘。"""
    try:
        data = request.get_json() or {}
        editor = _load_field_editor(data)
        field_names = data.get("field_names") or []
        if isinstance(field_names, str):
            field_names = [field_names]
        if not field_names:
            return jsonify({"error": "未指定要删除的字段"}), 400
        result = field_editor.preview_delete(
            editor,
            [str(f).strip() for f in field_names if str(f).strip()],
            allow_delete_protected=bool(data.get("allow_delete_protected", False)),
        )
        return jsonify({"success": True, "result": result})
    except Exception as e:
        log.exception("删除字段预览失败")
        return jsonify({"error": str(e)}), 400


@app.route("/api/field_editor/preview_assign", methods=["POST"])
def api_field_editor_preview_assign():
    """dry-run：预览批量赋值效果，不写盘。"""
    try:
        data = request.get_json() or {}
        editor = _load_field_editor(data)
        ep_indices = data.get("episode_indices")
        if ep_indices and isinstance(ep_indices, list):
            ep_indices = [int(i) for i in ep_indices]
        result = field_editor.preview_assign(
            editor,
            str(data.get("target", "")).strip(),
            mode=str(data.get("mode", "constant") or "constant"),
            value=data.get("value"),
            source=data.get("source"),
            expression=data.get("expression"),
            episode_indices=ep_indices,
        )
        return jsonify({"success": True, "result": result})
    except Exception as e:
        log.exception("赋值预览失败")
        return jsonify({"error": str(e)}), 400


@app.route("/api/field_editor/preview_rename_names", methods=["POST"])
def api_field_editor_preview_rename_names():
    """dry-run：预览修改维度名效果，不写盘。"""
    try:
        data = request.get_json() or {}
        editor = _load_field_editor(data)
        field_name = str(data.get("field_name", "")).strip()
        new_names = data.get("new_names") or []
        if isinstance(new_names, str):
            new_names = [s.strip() for s in new_names.split(",") if s.strip()]
        if not field_name:
            return jsonify({"error": "请指定字段名"}), 400
        if not new_names:
            return jsonify({"error": "请提供新的维度名列表"}), 400
        result = field_editor.preview_rename_names(editor, field_name, new_names)
        return jsonify({"success": True, "result": result})
    except Exception as e:
        log.exception("维度名预览失败")
        return jsonify({"error": str(e)}), 400


@app.route("/api/field_editor/rename_names", methods=["POST"])
def api_field_editor_rename_names():
    """修改维度名并保存到新目录。"""
    data = request.get_json() or {}
    output_path = str(data.get("output_path", "")).strip()
    if not output_path:
        return jsonify({"error": "请指定输出数据集路径"}), 400
    try:
        input_path = Path(str(data.get("input_path", "")).strip()).resolve()
        out_path = Path(output_path).resolve()
        if input_path == out_path:
            return jsonify({"error": "输出路径不能和输入路径相同，请另存为新目录"}), 400

        editor = _load_field_editor(data)
        field_name = str(data.get("field_name", "")).strip()
        new_names = data.get("new_names") or []
        if isinstance(new_names, str):
            new_names = [s.strip() for s in new_names.split(",") if s.strip()]
        if not field_name:
            return jsonify({"error": "请指定字段名"}), 400
        if not new_names:
            return jsonify({"error": "请提供新的维度名列表"}), 400

        set_save_progress("prepare", "正在修改维度名", f"修改 {field_name}", 0, 1, True)
        result = field_editor.apply_rename_names(editor, field_name, new_names)
        editor.save_as(
            str(out_path),
            set_save_progress,
            skip_video_stats=bool(data.get("skip_video_stats", False)),
        )
        stats_keys = _read_saved_stats_keys(out_path)
        set_save_progress("done", "保存完成", f"数据集已保存到: {out_path}", 1, 1, False)
        return jsonify({
            "success": True,
            "path": str(out_path),
            "result": result,
            "stats_keys": stats_keys,
        })
    except Exception as e:
        set_save_progress("error", "修改维度名失败", str(e), 0, 0, False)
        log.exception("修改维度名失败")
        return jsonify({"error": str(e)}), 500


# ═══════════════════════ 视频编码转换 API ═══════════════════════

@app.route("/api/video_transcode/ffmpeg_info", methods=["GET"])
def api_video_transcode_ffmpeg_info():
    """检测 ffmpeg / ffprobe 是否可用以及支持的编码器。"""
    info = {"ffmpeg": False, "ffprobe": False, "encoders": []}
    for exe in ("ffmpeg", "ffprobe"):
        try:
            subprocess.run([exe, "-version"], capture_output=True, timeout=5)
            info[exe] = True
        except FileNotFoundError:
            info[exe] = False
        except Exception:
            info[exe] = False
    if info["ffmpeg"]:
        try:
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=5,
            )
            txt = r.stdout or ""
            info["encoders"] = sorted(
                {
                    "libsvtav1" if "libsvtav1" in txt else None,
                    "libaom-av1" if "libaom-av1" in txt else None,
                    "libx264" if "libx264" in txt else None,
                    "libx265" if "libx265" in txt else None,
                } - {None}
            )
        except Exception:
            info["encoders"] = []
    return jsonify({"success": True, **info})


@app.route("/api/video_transcode/scan", methods=["POST"])
def api_video_transcode_scan():
    """扫描数据集视频，返回每个视频的编码/分辨率/帧数等。"""
    try:
        data = request.get_json() or {}
        path = Path(str(data.get("input_path", "")).strip())
        if not path.exists():
            return jsonify({"error": f"路径不存在: {path}"}), 400
        result = video_transcoder.scan_dataset_videos(path)
        return jsonify({"success": True, **result})
    except Exception as e:
        log.exception("视频扫描失败")
        return jsonify({"error": str(e)}), 400


@app.route("/api/video_transcode/run", methods=["POST"])
def api_video_transcode_run():
    """批量转码数据集视频到目标编码。"""
    data = request.get_json() or {}
    output_path = str(data.get("output_path", "")).strip()
    if not output_path:
        return jsonify({"error": "请指定输出数据集路径"}), 400
    try:
        input_path = Path(str(data.get("input_path", "")).strip()).resolve()
        out_path = Path(output_path).resolve()
        if input_path == out_path:
            return jsonify({"error": "输出路径不能和输入路径相同，请另存为新目录"}), 400

        target_codec = str(data.get("target_codec", "av1") or "av1").lower()
        only_codec = data.get("only_codec")
        if only_codec and isinstance(only_codec, str):
            only_codec = [only_codec]
        extra_args = data.get("extra_args")

        set_save_progress("prepare", "准备转码", f"目标编码: {target_codec}", 0, 1, True)
        result = video_transcoder.transcode_dataset(
            input_path,
            out_path,
            target_codec,
            only_codec=only_codec,
            extra_args=extra_args,
            skip_verify=bool(data.get("skip_verify", False)),
            progress_cb=set_save_progress,
        )
        set_save_progress("done", "转码完成",
                          f"成功 {result['transcoded']}/{result['total']}"
                          f"（跳过 {result['skipped']}，失败 {result['failed']}）",
                          1, 1, False)
        return jsonify({"success": True, **result})
    except Exception as e:
        set_save_progress("error", "转码失败", str(e), 0, 0, False)
        log.exception("视频转码失败")
        return jsonify({"error": str(e)}), 500


@app.route("/api/field_editor/rename", methods=["POST"])
def api_field_editor_rename():
    """重命名字段并保存到新目录。"""
    data = request.get_json() or {}
    output_path = str(data.get("output_path", "")).strip()
    if not output_path:
        return jsonify({"error": "请指定输出数据集路径"}), 400
    try:
        input_path = Path(str(data.get("input_path", "")).strip()).resolve()
        out_path = Path(output_path).resolve()
        if input_path == out_path:
            return jsonify({"error": "输出路径不能和输入路径相同，请另存为新目录"}), 400

        editor = _load_field_editor(data)
        renames = field_editor.parse_rename_pairs(data.get("renames"))
        if not renames:
            return jsonify({"error": "未提供有效的重命名规则"}), 400

        set_save_progress("prepare", "正在重命名字段", "应用重命名规则...", 0, 1, True)
        result = field_editor.apply_rename(
            editor,
            renames,
            rename_names=bool(data.get("rename_names", True)),
        )
        if not result["applied"]:
            set_save_progress("error", "重命名失败", "没有可应用的重命名", 0, 0, False)
            return jsonify({
                "error": "没有字段被重命名",
                "detail": result["skipped"],
            }), 400

        editor.save_as(
            str(out_path),
            set_save_progress,
            skip_video_stats=bool(data.get("skip_video_stats", False)),
        )
        stats_keys = _read_saved_stats_keys(out_path)
        set_save_progress("done", "保存完成", f"数据集已保存到: {out_path}", 1, 1, False)
        return jsonify({
            "success": True,
            "path": str(out_path),
            "result": result,
            "stats_keys": stats_keys,
        })
    except Exception as e:
        set_save_progress("error", "重命名执行失败", str(e), 0, 0, False)
        log.exception("字段重命名失败")
        return jsonify({"error": str(e)}), 500


@app.route("/api/field_editor/add", methods=["POST"])
def api_field_editor_add():
    """添加新字段并保存到新目录。"""
    data = request.get_json() or {}
    output_path = str(data.get("output_path", "")).strip()
    if not output_path:
        return jsonify({"error": "请指定输出数据集路径"}), 400
    try:
        input_path = Path(str(data.get("input_path", "")).strip()).resolve()
        out_path = Path(output_path).resolve()
        if input_path == out_path:
            return jsonify({"error": "输出路径不能和输入路径相同，请另存为新目录"}), 400

        editor = _load_field_editor(data)
        shape_raw = data.get("shape")
        if shape_raw in ("", None):
            shape = None
        elif isinstance(shape_raw, list):
            shape = [int(x) for x in shape_raw if x not in ("", None)]
        else:
            shape = [int(shape_raw)]

        set_save_progress("prepare", "正在添加字段", f"添加 {data.get('field_name')}", 0, 1, True)
        result = field_editor.apply_add(
            editor,
            str(data.get("field_name", "")).strip(),
            dtype=str(data.get("dtype", "float32") or "float32"),
            shape=shape,
            default=data.get("default", 0.0),
            names=data.get("names"),
        )
        editor.save_as(
            str(out_path),
            set_save_progress,
            skip_video_stats=bool(data.get("skip_video_stats", False)),
        )
        stats_keys = _read_saved_stats_keys(out_path)
        set_save_progress("done", "保存完成", f"数据集已保存到: {out_path}", 1, 1, False)
        return jsonify({
            "success": True,
            "path": str(out_path),
            "result": result,
            "stats_keys": stats_keys,
        })
    except Exception as e:
        set_save_progress("error", "添加字段失败", str(e), 0, 0, False)
        log.exception("添加字段失败")
        return jsonify({"error": str(e)}), 500


@app.route("/api/field_editor/delete", methods=["POST"])
def api_field_editor_delete():
    """删除字段并保存到新目录。"""
    data = request.get_json() or {}
    output_path = str(data.get("output_path", "")).strip()
    if not output_path:
        return jsonify({"error": "请指定输出数据集路径"}), 400
    try:
        input_path = Path(str(data.get("input_path", "")).strip()).resolve()
        out_path = Path(output_path).resolve()
        if input_path == out_path:
            return jsonify({"error": "输出路径不能和输入路径相同，请另存为新目录"}), 400

        editor = _load_field_editor(data)
        field_names = data.get("field_names") or []
        if isinstance(field_names, str):
            field_names = [field_names]
        if not field_names:
            return jsonify({"error": "未指定要删除的字段"}), 400

        set_save_progress("prepare", "正在删除字段", "应用删除...", 0, 1, True)
        result = field_editor.apply_delete(
            editor,
            [str(f).strip() for f in field_names if str(f).strip()],
            allow_delete_protected=bool(data.get("allow_delete_protected", False)),
        )
        if not result["deleted"]:
            set_save_progress("error", "删除失败", "没有可删除的字段", 0, 0, False)
            return jsonify({
                "error": "没有字段被删除",
                "detail": result["skipped"],
            }), 400

        editor.save_as(
            str(out_path),
            set_save_progress,
            skip_video_stats=bool(data.get("skip_video_stats", False)),
        )
        stats_keys = _read_saved_stats_keys(out_path)
        set_save_progress("done", "保存完成", f"数据集已保存到: {out_path}", 1, 1, False)
        return jsonify({
            "success": True,
            "path": str(out_path),
            "result": result,
            "stats_keys": stats_keys,
        })
    except Exception as e:
        set_save_progress("error", "删除字段失败", str(e), 0, 0, False)
        log.exception("删除字段失败")
        return jsonify({"error": str(e)}), 500


@app.route("/api/field_editor/assign", methods=["POST"])
def api_field_editor_assign():
    """批量给字段赋值并保存到新目录。"""
    data = request.get_json() or {}
    output_path = str(data.get("output_path", "")).strip()
    if not output_path:
        return jsonify({"error": "请指定输出数据集路径"}), 400
    try:
        input_path = Path(str(data.get("input_path", "")).strip()).resolve()
        out_path = Path(output_path).resolve()
        if input_path == out_path:
            return jsonify({"error": "输出路径不能和输入路径相同，请另存为新目录"}), 400

        editor = _load_field_editor(data)
        ep_indices = data.get("episode_indices")
        if ep_indices and isinstance(ep_indices, list):
            ep_indices = [int(i) for i in ep_indices]

        set_save_progress("prepare", "正在批量赋值", f"赋值 {data.get('target')}", 0, 1, True)
        result = field_editor.apply_assign(
            editor,
            str(data.get("target", "")).strip(),
            mode=str(data.get("mode", "constant") or "constant"),
            value=data.get("value"),
            source=data.get("source"),
            expression=data.get("expression"),
            episode_indices=ep_indices,
        )
        editor.save_as(
            str(out_path),
            set_save_progress,
            skip_video_stats=bool(data.get("skip_video_stats", False)),
        )
        stats_keys = _read_saved_stats_keys(out_path)
        set_save_progress("done", "保存完成", f"数据集已保存到: {out_path}", 1, 1, False)
        return jsonify({
            "success": True,
            "path": str(out_path),
            "result": result,
            "stats_keys": stats_keys,
        })
    except Exception as e:
        set_save_progress("error", "批量赋值失败", str(e), 0, 0, False)
        log.exception("批量赋值失败")
        return jsonify({"error": str(e)}), 500


# ═══════════════════════ ROS2 转换 API ═══════════════════════

import ros2_converter as r2c

# 全局: 每个转换项目的状态
_ros2_projects: dict[str, r2c.ProjectState] = {}
_ros2_progress_lock = threading.Lock()
_ros2_progress: dict = {
    "running": False,
    "step": "",
    "detail": "",
    "percent": 0,
    "current": 0,
    "total": 0,
    "unit": "",
    "started_at": None,
    "finished_at": None,
    "results": [],
    "errors": [],
}


def set_ros2_progress(**kwargs):
    with _ros2_progress_lock:
        _ros2_progress.update(kwargs)


def get_ros2_progress():
    with _ros2_progress_lock:
        data = dict(_ros2_progress)

    started_at = data.get("started_at")
    finished_at = data.get("finished_at")
    now = time.time()

    if started_at:
        end_ts = finished_at or now
        elapsed = max(0.0, end_ts - started_at)
        data["elapsed_sec"] = elapsed

        current = max(0, int(data.get("current", 0) or 0))
        total = max(0, int(data.get("total", 0) or 0))
        if total > 0:
            if data.get("percent") in (None, 0) and current > 0:
                data["percent"] = max(0, min(100, round(current * 100 / total)))
            if current > 0 and elapsed > 0:
                rate = current / elapsed
                data["rate_per_sec"] = rate
                remaining = max(0, total - current)
                if remaining > 0:
                    data["eta_sec"] = remaining / rate
                else:
                    data["eta_sec"] = 0.0
            else:
                data["rate_per_sec"] = 0.0
                data["eta_sec"] = None
    else:
        data["elapsed_sec"] = None
        data["eta_sec"] = None
        data["rate_per_sec"] = None

    return data


def _get_or_create_project(project_dir: str) -> r2c.ProjectState:
    if project_dir not in _ros2_projects:
        _ros2_projects[project_dir] = r2c.ProjectState(project_dir)
    return _ros2_projects[project_dir]


@app.route("/api/ros2/scan", methods=["POST"])
def api_ros2_scan():
    """Step 1: 扫描目录中的 bag 文件"""
    data = request.get_json()
    root = data.get("path", "").strip()
    if not root:
        return jsonify({"error": "请指定扫描目录"}), 400

    try:
        bags = r2c.scan_bags(root)
        return jsonify({"success": True, "bags": bags, "count": len(bags)})
    except Exception as e:
        log.exception("扫描 bag 目录失败")
        return jsonify({"error": str(e)}), 500


@app.route("/api/ros2/topics", methods=["POST"])
def api_ros2_topics():
    """Step 2: 从指定 bag 中发现 topic"""
    data = request.get_json()
    bag_path = data.get("bag_path", "").strip()
    if not bag_path:
        return jsonify({"error": "请指定 bag 路径"}), 400

    try:
        result = r2c.discover_topics(bag_path)
        return jsonify({"success": True, **result})
    except ImportError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        log.exception("发现 topic 失败")
        return jsonify({"error": str(e)}), 500


@app.route("/api/ros2/save_config", methods=["POST"])
def api_ros2_save_config():
    """Step 3: 保存用户配置"""
    data = request.get_json()
    project_dir = data.get("project_dir", "").strip()
    config = data.get("config", {})
    if not project_dir:
        return jsonify({"error": "请指定项目目录"}), 400

    proj = _get_or_create_project(project_dir)
    proj.save_step("step3_config", config)
    return jsonify({"success": True})


@app.route("/api/ros2/align", methods=["POST"])
def api_ros2_align():
    """Step 4: 执行时间戳对齐"""
    import threading

    data = request.get_json()
    project_dir = data.get("project_dir", "").strip()
    bags = data.get("bags", [])  # [{path, name}]
    config = data.get("config", {})

    if not project_dir or not bags or not config:
        return jsonify({"error": "参数不完整"}), 400

    proj = _get_or_create_project(project_dir)

    def do_align():
        try:
            with _ros2_progress_lock:
                _ros2_progress.clear()
                _ros2_progress.update({
                    "running": True,
                    "step": "align",
                    "detail": "正在对齐...",
                    "percent": 0,
                    "current": 0,
                    "total": len(bags),
                    "unit": "bag",
                    "started_at": time.time(),
                    "finished_at": None,
                    "results": [],
                    "errors": [],
                    "current_label": "",
                })
            total = len(bags)
            for i, bag in enumerate(bags):
                set_ros2_progress(
                    detail=f"正在对齐 {bag['name']} ({i+1}/{total})",
                    percent=round(i / total * 100),
                    current=i,
                    current_label=bag["name"],
                )
                try:
                    result = r2c.align_one_episode(
                        bag["path"], config, i, project_dir)
                    with _ros2_progress_lock:
                        _ros2_progress["results"].append(result)
                except Exception as e:
                    with _ros2_progress_lock:
                        _ros2_progress["errors"].append(
                            {"episode": i, "bag": bag["name"], "error": str(e)})
                    log.exception(f"对齐 episode {i} 失败")
                finally:
                    set_ros2_progress(
                        current=i + 1,
                        percent=round((i + 1) / total * 100),
                    )

            set_ros2_progress(
                percent=100,
                detail="对齐完成",
                running=False,
                finished_at=time.time(),
            )

            # 持久化结果
            proj.save_step("step4_align", {
                "results": get_ros2_progress()["results"],
                "errors": get_ros2_progress()["errors"],
            })
        except Exception as e:
            log.exception("对齐线程异常退出")
            with _ros2_progress_lock:
                _ros2_progress["errors"].append({"step": "align", "error": str(e)})
            set_ros2_progress(
                running=False,
                finished_at=time.time(),
                detail=f"对齐失败: {e}",
            )

    threading.Thread(target=do_align, daemon=True).start()
    return jsonify({"success": True, "message": "对齐任务已启动"})


@app.route("/api/ros2/convert", methods=["POST"])
def api_ros2_convert():
    """Step 5: 转换为 LeRobot v2.1"""
    import threading

    data = request.get_json()
    project_dir = data.get("project_dir", "").strip()
    output_dir = data.get("output_dir", "").strip()
    config = data.get("config", {})

    if not project_dir or not output_dir or not config:
        return jsonify({"error": "参数不完整"}), 400

    proj = _get_or_create_project(project_dir)
    align_data = proj.load_step("step4_align")
    if not align_data:
        return jsonify({"error": "请先完成时间戳对齐 (Step 4)"}), 400

    aligned_results = align_data.get("results", [])
    if not aligned_results:
        return jsonify({"error": "没有已对齐的 episode"}), 400

    def do_convert():
        try:
            jobs = []
            next_frame_offset = 0
            skipped_errors = []
            for i, ar in enumerate(aligned_results):
                aligned_path = ar.get("output_path", "")
                if not aligned_path or not Path(aligned_path).exists():
                    skipped_errors.append(
                        {"episode": i, "error": f"缺少对齐文件: {aligned_path or '(empty)'}"})
                    continue
                jobs.append({
                    "episode_idx": i,
                    "aligned_path": aligned_path,
                    "frame_offset": next_frame_offset,
                })
                next_frame_offset += int(ar.get("frame_count", 0) or 0)

            total_episodes = len(jobs)
            total = total_episodes + 1  # 最后一格留给元数据写入
            requested_workers = int(config.get("convert_workers", 1) or 1)
            workers = min(total_episodes, max(1, requested_workers)) if total_episodes else 1
            with _ros2_progress_lock:
                _ros2_progress.clear()
                _ros2_progress.update({
                    "running": True,
                    "step": "convert",
                    "detail": "正在准备并行转换...",
                    "percent": 0,
                    "current": 0,
                    "total": total,
                    "unit": "step",
                    "started_at": time.time(),
                    "finished_at": None,
                    "results": [],
                    "errors": list(skipped_errors),
                    "current_label": "",
                })
            if skipped_errors:
                set_ros2_progress(
                    detail=f"已跳过 {len(skipped_errors)} 个缺少对齐文件的 episode，正在启动并行转换...",
                )

            if not jobs:
                set_ros2_progress(
                    running=False,
                    finished_at=time.time(),
                    detail="没有可转换的 episode",
                    percent=100,
                    current=0,
                )
                return

            episodes = []
            completed = 0
            set_ros2_progress(
                detail=f"正在并行转换，共 {total_episodes} 个 episode，使用 {workers} 个线程",
                current=0,
                percent=0,
                current_label="parallel-start",
            )

            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {
                    pool.submit(
                        r2c.convert_episode,
                        job["aligned_path"],
                        output_dir,
                        job["episode_idx"],
                        config,
                        job["frame_offset"],
                    ): job
                    for job in jobs
                }

                for future in as_completed(future_map):
                    job = future_map[future]
                    episode_idx = job["episode_idx"]
                    try:
                        ep = future.result()
                        episodes.append(ep)
                        with _ros2_progress_lock:
                            _ros2_progress["results"].append({
                                "episode_idx": ep["episode_idx"],
                                "frame_count": ep["frame_count"],
                            })
                    except Exception as e:
                        with _ros2_progress_lock:
                            _ros2_progress["errors"].append(
                                {"episode": episode_idx, "error": str(e)})
                        log.exception(f"转换 episode {episode_idx} 失败")
                    finally:
                        completed += 1
                        set_ros2_progress(
                            detail=(
                                f"正在并行转换: 已完成 {completed}/{total_episodes} 个 episode "
                                f"(线程数 {workers})"
                            ),
                            current=completed,
                            percent=round(completed / total * 100),
                            current_label=f"Episode {episode_idx}",
                        )

            episodes.sort(key=lambda ep: ep["episode_idx"])

            # 写入元数据
            set_ros2_progress(
                detail="正在写入元数据...",
                current=total - 1,
                current_label="metadata",
            )
            try:
                r2c.write_metadata(output_dir, episodes, config)
            except Exception as e:
                with _ros2_progress_lock:
                    _ros2_progress["errors"].append({"step": "metadata", "error": str(e)})

            set_ros2_progress(
                percent=100,
                current=total,
                detail="转换完成",
                running=False,
                finished_at=time.time(),
                current_label="done",
            )

            proj.save_step("step5_convert", {
                "output_dir": output_dir,
                "episodes": [{"episode_idx": e["episode_idx"],
                              "frame_count": e["frame_count"]} for e in episodes],
            })
        except Exception as e:
            log.exception("转换线程异常退出")
            with _ros2_progress_lock:
                _ros2_progress["errors"].append({"step": "convert", "error": str(e)})
            set_ros2_progress(
                running=False,
                finished_at=time.time(),
                detail=f"转换失败: {e}",
            )

    threading.Thread(target=do_convert, daemon=True).start()
    return jsonify({"success": True, "message": "转换任务已启动"})


@app.route("/api/ros2/progress")
def api_ros2_progress():
    """轮询长任务进度"""
    return jsonify(get_ros2_progress())


@app.route("/api/ros2/resume", methods=["POST"])
def api_ros2_resume():
    """检查项目已完成的步骤，用于断点续做"""
    data = request.get_json()
    project_dir = data.get("project_dir", "").strip()
    if not project_dir:
        return jsonify({"error": "请指定项目目录"}), 400

    proj = _get_or_create_project(project_dir)
    progress = proj.get_progress()

    # 加载已有配置
    saved_config = proj.load_step("step3_config")
    saved_scan = proj.load_step("step1_scan")
    saved_topics = proj.load_step("step2_topics")

    return jsonify({
        "success": True,
        "progress": progress,
        "config": saved_config,
        "scan": saved_scan,
        "topics": saved_topics,
    })


# ═══════════════════════ LeRobot 版本转换 API ═══════════════════════

import lerobot_converter as lconv

_convert_progress_lock = threading.Lock()
_convert_progress: dict = {
    "running": False,
    "stage": "",
    "title": "",
    "detail": "",
    "current": 0,
    "total": 0,
    "percent": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "result": None,
    "source": "",
    "target": "",
    "target_version": "",
}


def _set_convert_progress(**kwargs):
    with _convert_progress_lock:
        _convert_progress.update(kwargs)


def _get_convert_progress():
    with _convert_progress_lock:
        data = dict(_convert_progress)
    started_at = data.get("started_at")
    finished_at = data.get("finished_at")
    now = time.time()
    total = max(0, int(data.get("total", 0) or 0))
    current = max(0, int(data.get("current", 0) or 0))
    if total > 0:
        data["percent"] = max(0, min(100, round(current * 100 / total)))
    if started_at:
        end = finished_at or now
        data["elapsed_sec"] = max(0.0, end - started_at)
        if current > 0 and data["elapsed_sec"] > 0 and total > current:
            rate = current / data["elapsed_sec"]
            data["eta_sec"] = (total - current) / rate if rate > 0 else None
        else:
            data["eta_sec"] = 0 if (total > 0 and current >= total) else None
    else:
        data["elapsed_sec"] = None
        data["eta_sec"] = None
    return data


def _convert_progress_cb(payload: dict) -> None:
    """由转换核心模块回调: {stage, title, detail, current, total}."""
    with _convert_progress_lock:
        if payload.get("stage"):
            _convert_progress["stage"] = payload["stage"]
        if payload.get("title"):
            _convert_progress["title"] = payload["title"]
        if "detail" in payload:
            _convert_progress["detail"] = payload["detail"] or ""
        if "current" in payload:
            _convert_progress["current"] = int(payload["current"])
        if "total" in payload:
            _convert_progress["total"] = int(payload["total"])


# ───────── stats 校验 (独立门户) 的进度跟踪 ─────────

_verify_progress_lock = threading.Lock()
_verify_progress: dict = {
    "running": False,
    "stage": "",
    "title": "",
    "detail": "",
    "current": 0,
    "total": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "result": None,
    "path": "",
}


def _set_verify_progress(**kwargs):
    with _verify_progress_lock:
        _verify_progress.update(kwargs)


def _verify_progress_cb(payload: dict) -> None:
    with _verify_progress_lock:
        if payload.get("stage"):
            _verify_progress["stage"] = payload["stage"]
        if payload.get("title"):
            _verify_progress["title"] = payload["title"]
        if "detail" in payload:
            _verify_progress["detail"] = payload["detail"] or ""
        if "current" in payload:
            _verify_progress["current"] = int(payload["current"])
        if "total" in payload:
            _verify_progress["total"] = int(payload["total"])


def _get_verify_progress():
    with _verify_progress_lock:
        data = dict(_verify_progress)
    started_at = data.get("started_at")
    finished_at = data.get("finished_at")
    now = time.time()
    total = max(0, int(data.get("total", 0) or 0))
    current = max(0, int(data.get("current", 0) or 0))
    data["percent"] = max(0, min(100, round(current * 100 / total))) if total > 0 else 0
    if started_at:
        end = finished_at or now
        data["elapsed_sec"] = max(0.0, end - started_at)
        if current > 0 and data["elapsed_sec"] > 0 and total > current:
            rate = current / data["elapsed_sec"]
            data["eta_sec"] = (total - current) / rate if rate > 0 else None
        else:
            data["eta_sec"] = 0 if (total > 0 and current >= total) else None
    else:
        data["elapsed_sec"] = None
        data["eta_sec"] = None
    return data


@app.route("/api/convert/inspect", methods=["POST"])
def api_convert_inspect():
    data = request.get_json() or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "请输入数据集路径"}), 400
    try:
        info = lconv.inspect_dataset(path)
        return jsonify({"success": True, "info": info})
    except Exception as e:  # pylint: disable=broad-except
        log.exception("扫描数据集失败")
        return jsonify({"error": str(e)}), 400


@app.route("/api/convert/start", methods=["POST"])
def api_convert_start():
    data = request.get_json() or {}
    src = (data.get("source") or "").strip()
    dst = (data.get("target") or "").strip()
    target_version = (data.get("target_version") or "").strip()
    data_mb = int(data.get("data_file_size_mb") or lconv.DEFAULT_DATA_FILE_SIZE_MB)
    video_mb = int(data.get("video_file_size_mb") or lconv.DEFAULT_VIDEO_FILE_SIZE_MB)
    recompute_stats = bool(data.get("recompute_stats", False))
    video_stride = max(1, int(data.get("video_stride") or 1))
    include_video_stats = bool(data.get("include_video_stats", True))

    if not src or not dst or not target_version:
        return jsonify({"error": "source / target / target_version 均不能为空"}), 400

    src_path = Path(src).expanduser().resolve()
    dst_path = Path(dst).expanduser().resolve()
    if not src_path.exists():
        return jsonify({"error": f"源路径不存在: {src_path}"}), 400
    if src_path == dst_path:
        return jsonify({"error": "目标目录不能与源目录相同"}), 400
    try:
        dst_path.relative_to(src_path)
        return jsonify({"error": "目标目录不能位于源目录内部"}), 400
    except ValueError:
        pass

    with _convert_progress_lock:
        if _convert_progress.get("running"):
            return jsonify({"error": "已有转换任务在进行中，请稍后再试"}), 400
        _convert_progress.clear()
        _convert_progress.update({
            "running": True,
            "stage": "init",
            "title": "初始化",
            "detail": "",
            "current": 0,
            "total": 1,
            "percent": 0,
            "started_at": time.time(),
            "finished_at": None,
            "error": None,
            "result": None,
            "source": str(src_path),
            "target": str(dst_path),
            "target_version": target_version,
        })

    try:
        src_version = lconv.detect_codebase_version(src_path)
    except Exception as e:  # pylint: disable=broad-except
        _set_convert_progress(running=False, finished_at=time.time(), error=str(e))
        return jsonify({"error": str(e)}), 400

    direction = (src_version, target_version)
    supported = {
        (lconv.V21, lconv.V30),
        (lconv.V30, lconv.V21),
        (lconv.V21, lconv.V20),
    }
    if direction not in supported:
        msg = f"不支持的转换方向: {src_version} → {target_version}"
        _set_convert_progress(running=False, finished_at=time.time(), error=msg)
        return jsonify({"error": msg}), 400

    def worker():
        try:
            if direction == (lconv.V21, lconv.V30):
                result = lconv.convert_v21_to_v30(
                    src_path, dst_path, _convert_progress_cb,
                    data_file_size_mb=data_mb,
                    video_file_size_mb=video_mb,
                    recompute_stats=recompute_stats,
                    video_stride=video_stride,
                    include_video_stats=include_video_stats,
                )
            elif direction == (lconv.V30, lconv.V21):
                result = lconv.convert_v30_to_v21(
                    src_path, dst_path, _convert_progress_cb)
            else:
                result = lconv.convert_v21_to_v20(
                    src_path, dst_path, _convert_progress_cb,
                    recompute_stats=recompute_stats,
                    video_stride=video_stride,
                    include_video_stats=include_video_stats,
                )

            _set_convert_progress(
                running=False,
                finished_at=time.time(),
                stage="done",
                title="转换完成",
                detail=f"输出: {result.get('output', dst_path)}",
                current=_convert_progress.get("total", 1) or 1,
                total=_convert_progress.get("total", 1) or 1,
                result=result,
            )
        except Exception as e:  # pylint: disable=broad-except
            log.exception("转换失败")
            _set_convert_progress(
                running=False,
                finished_at=time.time(),
                stage="error",
                title="转换失败",
                detail=str(e),
                error=str(e),
            )

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({
        "success": True,
        "message": "转换任务已启动",
        "source_version": src_version,
        "target_version": target_version,
    })


@app.route("/api/convert/progress")
def api_convert_progress():
    return jsonify(_get_convert_progress())



@app.route("/api/convert/verify_stats", methods=["POST"])
def api_convert_verify_stats():
    """同步对比(兼容 converter 页面的 Step 4 校验面板)。大型视频集建议改用异步接口。"""
    data = request.get_json() or {}
    root = (data.get("path") or "").strip()
    if not root:
        return jsonify({"error": "path 不能为空"}), 400
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists():
        return jsonify({"error": f"路径不存在: {root_path}"}), 400
    try:
        report = _stats_verify_service.run_verify_stats(
            root_path,
            stride=max(1, int(data.get("video_stride") or 1)),
            include_videos=bool(data.get("include_video_stats", True)),
            tol=float(data.get("max_abs_diff") or 1e-4),
            progress_cb=lambda p: None,
        )
    except Exception as e:  # pylint: disable=broad-except
        return jsonify({"error": str(e)}), 400
    return jsonify(report)


@app.route("/api/verify-stats/inspect", methods=["POST"])
def api_verify_stats_inspect():
    """快速扫描给定目录, 返回可校验的基本信息(不真正算 stats)。"""
    data = request.get_json() or {}
    root = (data.get("path") or "").strip()
    if not root:
        return jsonify({"error": "path 不能为空"}), 400
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists():
        return jsonify({"error": f"路径不存在: {root_path}"}), 400
    try:
        summary = lconv.inspect_dataset(str(root_path))
    except Exception as e:  # pylint: disable=broad-except
        return jsonify({"error": str(e)}), 400

    info = json.loads((root_path / "meta" / "info.json").read_text(encoding="utf-8"))
    has_eps_stats = (root_path / "meta" / "episodes_stats.jsonl").exists()
    has_stats_json = (root_path / "meta" / "stats.json").exists()

    ep_parquets = list((root_path / "data").glob("chunk-*/episode_*.parquet"))
    video_keys = [k for k, v in (info.get("features") or {}).items()
                  if (v or {}).get("dtype") == "video"]
    video_counts = {}
    for cam in video_keys:
        video_counts[cam] = len(list((root_path / "videos").glob(f"chunk-*/{cam}/episode_*.mp4")))

    return jsonify({
        "success": True,
        "summary": summary,
        "eligible": summary.get("codebase_version") == lconv.V21,
        "has_episodes_stats_jsonl": has_eps_stats,
        "has_stats_json": has_stats_json,
        "num_episode_parquets": len(ep_parquets),
        "video_keys": video_keys,
        "video_episode_counts": video_counts,
    })


@app.route("/api/verify-stats/start", methods=["POST"])
def api_verify_stats_start():
    data = request.get_json() or {}
    root = (data.get("path") or "").strip()
    if not root:
        return jsonify({"error": "path 不能为空"}), 400
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists():
        return jsonify({"error": f"路径不存在: {root_path}"}), 400

    stride = max(1, int(data.get("video_stride") or 1))
    include_videos = bool(data.get("include_video_stats", True))
    tol = float(data.get("max_abs_diff") or 1e-4)

    with _verify_progress_lock:
        if _verify_progress.get("running"):
            return jsonify({"error": "已有校验任务在进行中"}), 400
        _verify_progress.clear()
        _verify_progress.update({
            "running": True,
            "stage": "init",
            "title": "初始化",
            "detail": "",
            "current": 0,
            "total": 1,
            "percent": 0,
            "started_at": time.time(),
            "finished_at": None,
            "error": None,
            "result": None,
            "path": str(root_path),
        })

    def worker():
        try:
            report = _stats_verify_service.run_verify_stats(
                root_path, stride=stride,
                include_videos=include_videos, tol=tol,
                progress_cb=_verify_progress_cb,
            )
            _set_verify_progress(
                running=False, finished_at=time.time(),
                stage="done", title="校验完成",
                detail=f"{len(report.get('recomputed_keys', []))} 个 feature 重算完成",
                current=_verify_progress.get("total", 1) or 1,
                total=_verify_progress.get("total", 1) or 1,
                result=report,
            )
        except Exception as e:  # pylint: disable=broad-except
            log.exception("stats 校验失败")
            _set_verify_progress(
                running=False, finished_at=time.time(),
                error=str(e), stage="error", title="失败", detail=str(e),
            )

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"success": True, "message": "校验任务已启动", "path": str(root_path)})


@app.route("/api/verify-stats/progress")
def api_verify_stats_progress():
    return jsonify(_get_verify_progress())


@app.route("/api/verify-stats/cancel", methods=["POST"])
def api_verify_stats_cancel():
    # 目前无法真正打断 ffmpeg 管道, 但可以让前端停止轮询
    _set_verify_progress(running=False, finished_at=time.time(),
                         error="用户取消", stage="cancelled", title="已取消")
    return jsonify({"success": True})


@app.route("/api/convert/tree", methods=["POST"])
def api_convert_tree():
    data = request.get_json() or {}
    out = {}
    for side in ("left", "right"):
        p = (data.get(side) or "").strip()
        if not p:
            out[side] = None
            continue
        try:
            out[side] = lconv.build_tree(p)
        except Exception as e:  # pylint: disable=broad-except
            out[side] = {"error": str(e), "path": p}
    if data.get("include_diff") and data.get("left") and data.get("right"):
        try:
            out["diff"] = lconv.compare_datasets(data["left"], data["right"])
        except Exception as e:  # pylint: disable=broad-except
            out["diff"] = {"error": str(e)}
    return jsonify({"success": True, **out})


@app.route("/api/convert/file_preview")
def api_convert_file_preview():
    path = (request.args.get("path") or "").strip()
    if not path:
        return jsonify({"error": "缺少 path 参数"}), 400
    try:
        payload = lconv.preview_file(path)
        return jsonify({"success": True, "file": payload})
    except Exception as e:  # pylint: disable=broad-except
        return jsonify({"error": str(e)}), 400


@app.route("/api/convert/video_file")
def api_convert_video_file():
    path = (request.args.get("path") or "").strip()
    if not path:
        abort(400)
    p = Path(path).expanduser().resolve()
    if not p.exists() or not p.is_file():
        abort(404)
    if p.suffix.lower() not in {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}:
        abort(400)
    return send_file(str(p), mimetype="video/mp4")


# ═══════════════════════ 图像质量分析 API ═══════════════════════


@app.route("/api/image-analysis/load", methods=["POST"])
def api_image_analysis_load():
    global _img_analyzer
    data = request.get_json() or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "请输入数据集路径"}), 400

    p = Path(path).expanduser().resolve()
    if not p.exists():
        return jsonify({"error": f"路径不存在: {path}"}), 400
    if not (p / "meta" / "info.json").exists():
        return jsonify({"error": "无效的 LeRobot 数据集 (缺少 meta/info.json)"}), 400

    try:
        _img_analyzer = img_analyzer.ImageAnalyzer(str(p))
        info = _img_analyzer.get_dataset_info()
        return jsonify({"success": True, **info})
    except Exception as e:
        log.exception("图像分析: 加载数据集失败")
        return jsonify({"error": str(e)}), 500


@app.route("/api/image-analysis/start", methods=["POST"])
def api_image_analysis_start():
    global _img_analyzer
    if _img_analyzer is None:
        return jsonify({"error": "请先加载数据集"}), 400

    data = request.get_json() or {}
    camera = (data.get("camera") or "").strip()
    if not camera:
        return jsonify({"error": "请选择相机"}), 400

    episodes = data.get("episodes")

    with _img_analysis_lock:
        if _img_analysis_progress.get("running"):
            return jsonify({"error": "已有分析任务在进行中"}), 400
        _img_analysis_progress.clear()
        _img_analysis_progress.update({
            "running": True,
            "stage": "init",
            "title": "初始化",
            "detail": "",
            "current": 0,
            "total": 1,
            "percent": 0,
            "started_at": time.time(),
            "finished_at": None,
            "error": None,
            "result": None,
        })

    analyzer = _img_analyzer

    def worker():
        try:
            report = analyzer.analyze(
                camera, episodes=episodes,
                progress_cb=_img_analysis_progress_cb,
            )
            _set_img_analysis_progress(
                running=False, finished_at=time.time(),
                stage="done", title="分析完成",
                detail=f"{report.get('episodes_analyzed', 0)} 个 episode 已分析",
                current=_img_analysis_progress.get("total", 1) or 1,
                total=_img_analysis_progress.get("total", 1) or 1,
                result=report,
            )
        except Exception as e:
            log.exception("图像质量分析失败")
            _set_img_analysis_progress(
                running=False, finished_at=time.time(),
                error=str(e), stage="error", title="分析失败", detail=str(e),
            )

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"success": True, "message": "分析任务已启动"})


@app.route("/api/image-analysis/progress")
def api_image_analysis_progress():
    return jsonify(_get_img_analysis_progress())


@app.route("/api/image-analysis/episode-detail")
def api_image_analysis_episode_detail():
    if _img_analyzer is None:
        return jsonify({"error": "未加载数据集"}), 400
    ep_idx = request.args.get("episode", type=int)
    if ep_idx is None:
        return jsonify({"error": "缺少 episode 参数"}), 400
    detail = _img_analyzer.get_episode_detail(ep_idx)
    if detail is None:
        return jsonify({"error": f"Episode {ep_idx} 无缓存数据"}), 404
    return jsonify({"success": True, **detail})


@app.route("/api/image-analysis/frame")
def api_image_analysis_frame():
    if _img_analyzer is None:
        abort(400)
    camera = (request.args.get("camera") or "").strip()
    ep_idx = request.args.get("episode", type=int)
    frame_idx = request.args.get("frame", type=int)
    if not camera or ep_idx is None or frame_idx is None:
        abort(400)

    jpeg_data = _img_analyzer.extract_frame_jpeg(camera, ep_idx, frame_idx)
    if jpeg_data is None:
        abort(404)

    from io import BytesIO
    return send_file(BytesIO(jpeg_data), mimetype="image/jpeg",
                     download_name=f"ep{ep_idx}_frame{frame_idx}.jpg")


# ═══════════════════════ 入口 ═══════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LeRobot 数据集编辑器")
    parser.add_argument("--port", type=int, default=7860, help="端口 (默认 7860)")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--joint-config", type=str, default=None,
                        help="关节配置文件路径 (JSON), 覆盖自动检测")
    args = parser.parse_args()

    _joint_config_override = args.joint_config

    print(f"\n  ═══ LeRobot v2.1 数据集编辑器 ═══")
    print(f"  浏览器访问: http://localhost:{args.port}")
    if _joint_config_override:
        print(f"  关节配置: {_joint_config_override}")
    print()
    app.run(host=args.host, port=args.port, debug=False, threaded=True)

