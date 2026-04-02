#!/usr/bin/env python3
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
import shlex
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, render_template, request, jsonify, send_file, abort
import pandas as pd
import numpy as np

try:
    from scipy.signal import butter, filtfilt
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

# ═══════════════════════ 配置 ═══════════════════════

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# 全局编辑器实例
_editor = None

# 关节名称 (CR100 双臂灵巧手, 与 rosbag2lerobot 转换器一致)
JOINT_GROUPS = {
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

ALL_JOINT_NAMES = []
for _g in ["left_arm", "left_hand", "right_arm", "right_hand"]:
    ALL_JOINT_NAMES.extend(JOINT_GROUPS[_g])


# ═══════════════════════ DatasetEditor ═══════════════════════

class DatasetEditor:
    """LeRobot v2.1 数据集的加载、编辑和保存"""

    def __init__(self, dataset_path: str):
        self.root = Path(dataset_path).resolve()
        self.original_root = self.root
        self.info = {}
        self.episodes_meta = []
        self.tasks = []
        self.episode_data = {}          # ep_idx -> pd.DataFrame
        self._orig_indices = {}         # current_ep_idx -> original_ep_idx
        self._orig_video_files = {}     # original_ep_idx -> {cam_name: abs_path}
        self._orig_ep_lengths = {}      # original_ep_idx -> 原始帧数
        self.modified = False
        self._load()

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
                    if "frame_index" in df.columns:
                        df["_orig_frame_idx"] = df["frame_index"].copy()
                    else:
                        df["_orig_frame_idx"] = range(len(df))
                    self.episode_data[ep_idx] = df
                    self._orig_indices[ep_idx] = ep_idx
                    self._orig_ep_lengths[ep_idx] = len(df)
                except Exception as e:
                    log.warning(f"读取 {pq_file} 失败: {e}")

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
            # 从父目录名提取相机名
            cam_key = vf.parent.name
            if cam_key.startswith("chunk-"):
                continue
            cam_name = cam_key.split(".")[-1] if "." in cam_key else cam_key
            self._orig_video_files.setdefault(ep_idx, {})[cam_name] = str(vf)

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
        cameras = [k.split(".")[-1] for k in features if "images" in k]
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
            "av1": "libx264",
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
        else:
            cmd += ["-crf", "18"]
        if encoder == "libx264":
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
        return {
            "min":  arr.min(0).tolist(),
            "max":  arr.max(0).tolist(),
            "mean": arr.mean(0).tolist(),
            "std":  arr.std(0).tolist(),
            "count": [int(arr.shape[0])],
        }

    def compute_episode_stats(self):
        """计算每个 episode 的统计数据 (lerobot v2.1 格式)"""
        vector_cols = [c for c in ("observation.state", "action")
                       if any(c in df.columns for df in self.episode_data.values())]
        scalar_cols = [c for c in ("timestamp", "frame_index", "episode_index",
                                    "index", "task_index")
                       if any(c in df.columns for df in self.episode_data.values())]

        results = []
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

            results.append({"episode_index": idx, "stats": stats})
        return results

    def compute_stats(self):
        """基于 episode stats 聚合全局统计 (lerobot aggregate_stats 公式)"""
        ep_stats_list = self.compute_episode_stats()
        all_keys = {}
        for es in ep_stats_list:
            for k in es["stats"]:
                all_keys.setdefault(k, []).append(es["stats"][k])

        global_stats = {}
        for key, stats_list in all_keys.items():
            mins   = np.array([s["min"]  for s in stats_list])
            maxs   = np.array([s["max"]  for s in stats_list])
            means  = np.array([s["mean"] for s in stats_list])
            stds   = np.array([s["std"]  for s in stats_list])
            counts = np.array([s["count"][0] for s in stats_list]).reshape(-1, 1)

            total_count = counts.sum()
            total_mean = (means * counts).sum(0) / total_count
            total_var = ((stds ** 2 + (means - total_mean) ** 2) * counts).sum(0) / total_count
            total_std = np.sqrt(np.maximum(0, total_var))

            global_stats[key] = {
                "min":  mins.min(0).tolist(),
                "max":  maxs.max(0).tolist(),
                "mean": total_mean.tolist(),
                "std":  total_std.tolist(),
                "count": [int(total_count)],
            }
        return global_stats, ep_stats_list

    # ─── 保存 ───

    def save_as(self, output_path: str):
        """另存为新数据集 (含重算的统计元数据)"""
        out = Path(output_path).resolve()

        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True, exist_ok=True)

        meta_dir = out / "meta"
        meta_dir.mkdir()

        # ── info.json ──
        self._refresh_info()
        with open(meta_dir / "info.json", "w") as f:
            json.dump(self.info, f, indent=2, ensure_ascii=False)

        # ── episodes.jsonl ──
        with open(meta_dir / "episodes.jsonl", "w") as f:
            for em in self.episodes_meta:
                f.write(json.dumps(em, ensure_ascii=False) + "\n")

        # ── tasks.jsonl ──
        with open(meta_dir / "tasks.jsonl", "w") as f:
            for t in self.tasks:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")

        # ── 修正数据列 (timestamp, index) 以保证统计和 Parquet 一致 ──
        data_tpl = self.info.get(
            "data_path",
            "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet")
        video_tpl = self.info.get("video_path", "")
        chunks_size = self.info.get("chunks_size", 1000)
        features = self.info.get("features", {})
        video_keys = [k for k in features if "images" in k]
        fps = self.info.get("fps", 30)

        global_idx = 0
        for em in self.episodes_meta:
            idx = em["episode_index"]
            if idx not in self.episode_data:
                continue
            df = self.episode_data[idx]
            orig_idx = self._orig_indices.get(idx, idx)
            orig_len = self._orig_ep_lengths.get(orig_idx, len(df))
            if len(df) != orig_len:
                df["timestamp"] = [i / fps for i in range(len(df))]
            if "index" in df.columns:
                df["index"] = range(global_idx, global_idx + len(df))
            global_idx += len(df)

        # ── stats.json + episodes_stats.jsonl ──
        global_stats, ep_stats = self.compute_stats()
        with open(meta_dir / "stats.json", "w") as f:
            json.dump(global_stats, f, indent=2, ensure_ascii=False)
        with open(meta_dir / "episodes_stats.jsonl", "w") as f:
            for s in ep_stats:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")

        # ── Parquet 数据 + 收集视频任务 ──
        encode_tasks = []   # (src_path, keep_indices, dst_path)
        copy_tasks = []     # (src_path, dst_path)

        for em in self.episodes_meta:
            idx = em["episode_index"]
            if idx not in self.episode_data:
                continue

            df = self.episode_data[idx]
            chunk = idx // chunks_size
            orig_idx = self._orig_indices.get(idx, idx)
            orig_videos = self._orig_video_files.get(orig_idx, {})
            orig_len = self._orig_ep_lengths.get(orig_idx, len(df))
            frames_edited = (len(df) != orig_len)

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

            # ── 收集视频任务 ──
            for cam_name, src_path_str in orig_videos.items():
                src = Path(src_path_str)
                if not src.exists():
                    continue

                vkey = None
                for k in video_keys:
                    if k.endswith(cam_name):
                        vkey = k
                        break
                if vkey is None:
                    vkey = f"observation.images.{cam_name}"

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

                if frames_edited:
                    keep = [int(x) for x in df["_orig_frame_idx"].tolist()]
                    encode_tasks.append((src_path_str, keep, str(dst)))
                else:
                    copy_tasks.append((str(src), str(dst)))

        # ── 直接复制未修改的视频 ──
        for src, dst in copy_tasks:
            shutil.copy2(src, dst)
        log.info(f"已复制 {len(copy_tasks)} 个未修改视频")

        # ── 多线程并行重编码修改过的视频 ──
        if encode_tasks:
            src_params = self._probe_video_params(encode_tasks[0][0])
            workers = min(len(encode_tasks), max(1, (os.cpu_count() or 4) // 2))
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

            if failed:
                log.warning(f"{failed} 个视频重编码失败, 已回退复制原始文件")

        self.modified = False
        log.info(f"数据集已保存到: {out}")
        return True


# ═══════════════════════ Flask 路由 ═══════════════════════

@app.route("/")
def index():
    return render_template("index.html")


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
        _editor = DatasetEditor(path)
        return jsonify({
            "success": True,
            "summary": _editor.get_summary(),
            "episodes": _editor.get_episodes(),
            "joint_names": ALL_JOINT_NAMES,
            "joint_groups": JOINT_GROUPS,
        })
    except Exception as e:
        log.exception("加载数据集失败")
        return jsonify({"error": str(e)}), 500


@app.route("/api/episodes")
def api_episodes():
    if _editor is None:
        return jsonify({"error": "未加载数据集"}), 400
    return jsonify({"episodes": _editor.get_episodes(), "summary": _editor.get_summary()})


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


@app.route("/api/save", methods=["POST"])
def api_save():
    if _editor is None:
        return jsonify({"error": "未加载数据集"}), 400
    data = request.get_json()
    output = data.get("output_path", "").strip()
    if not output:
        return jsonify({"error": "请指定保存路径"}), 400

    try:
        _editor.save_as(output)
        return jsonify({"success": True, "path": output})
    except Exception as e:
        log.exception("保存失败")
        return jsonify({"error": str(e)}), 500


# ═══════════════════════ 入口 ═══════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LeRobot 数据集编辑器")
    parser.add_argument("--port", type=int, default=7860, help="端口 (默认 7860)")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    args = parser.parse_args()

    print(f"\n  ═══ LeRobot v2.1 数据集编辑器 ═══")
    print(f"  浏览器访问: http://localhost:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=False)
