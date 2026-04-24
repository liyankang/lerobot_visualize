"""
LeRobot 图像质量分析引擎

从 LeRobot v2.1 数据集的视频中解码帧，计算逐帧图像质量指标（模糊度、亮度、
曝光、信息熵、对比度）以及帧间一致性指标（帧间差异、静止帧检测），并可与关节
速度数据关联分析。
"""

import json
import logging
import subprocess
import threading
from pathlib import Path

import cv2
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ══════════════════════ 阈值与评分参数 ══════════════════════

BLUR_GOOD = 200.0
BLUR_ACCEPTABLE = 50.0

BRIGHTNESS_LOW = 0.15
BRIGHTNESS_HIGH = 0.85

CONTENT_PIXEL_FLOOR = 15

ENTROPY_GOOD = 6.5
ENTROPY_ACCEPTABLE = 4.0

CONTRAST_GOOD = 0.15
CONTRAST_ACCEPTABLE = 0.05

EXPOSURE_WARN = 0.01
EXPOSURE_BAD = 0.10

FRAME_DIFF_STATIC = 0.002
FRAME_DIFF_SCENE_CHANGE = 0.30

ANALYSIS_RESOLUTION_CAP = 480

QUALITY_WEIGHTS = {
    "blur": 0.35,
    "brightness": 0.15,
    "entropy": 0.20,
    "contrast": 0.15,
    "exposure": 0.15,
}


# ══════════════════════ 评分函数 ══════════════════════

def _score_blur(value):
    if value >= BLUR_GOOD:
        return 100.0
    if value >= BLUR_ACCEPTABLE:
        return 50.0 + 50.0 * (value - BLUR_ACCEPTABLE) / (BLUR_GOOD - BLUR_ACCEPTABLE)
    return max(0.0, value / BLUR_ACCEPTABLE * 50.0)


def _score_brightness(value):
    if BRIGHTNESS_LOW <= value <= BRIGHTNESS_HIGH:
        return 100.0
    if value < BRIGHTNESS_LOW:
        return max(0.0, value / BRIGHTNESS_LOW * 100.0)
    return max(0.0, (1.0 - value) / (1.0 - BRIGHTNESS_HIGH) * 100.0)


def _score_entropy(value):
    if value >= ENTROPY_GOOD:
        return 100.0
    if value >= ENTROPY_ACCEPTABLE:
        return (value - ENTROPY_ACCEPTABLE) / (ENTROPY_GOOD - ENTROPY_ACCEPTABLE) * 100.0
    return max(0.0, value / ENTROPY_ACCEPTABLE * 50.0)


def _score_contrast(value):
    if value >= CONTRAST_GOOD:
        return 100.0
    if value >= CONTRAST_ACCEPTABLE:
        return (value - CONTRAST_ACCEPTABLE) / (CONTRAST_GOOD - CONTRAST_ACCEPTABLE) * 100.0
    return max(0.0, value / CONTRAST_ACCEPTABLE * 50.0)


def _score_exposure(overexposed, underexposed):
    bad = max(overexposed, underexposed)
    if bad < EXPOSURE_WARN:
        return 100.0
    if bad < EXPOSURE_BAD:
        return 100.0 - (bad - EXPOSURE_WARN) / (EXPOSURE_BAD - EXPOSURE_WARN) * 80.0
    return max(0.0, 20.0 - (bad - EXPOSURE_BAD) / (1.0 - EXPOSURE_BAD) * 20.0)


def compute_frame_quality(metrics):
    br_key = "brightness_content" if "brightness_content" in metrics else "brightness"
    scores = {
        "blur": _score_blur(metrics["blur"]),
        "brightness": _score_brightness(metrics[br_key]),
        "entropy": _score_entropy(metrics["entropy"]),
        "contrast": _score_contrast(metrics["contrast"]),
        "exposure": _score_exposure(metrics["overexposed"], metrics["underexposed"]),
    }
    total = sum(scores[k] * QUALITY_WEIGHTS[k] for k in QUALITY_WEIGHTS)
    return total, scores


# ══════════════════════ 辅助工具 ══════════════════════

def _read_exact(stream, size):
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _probe_video(video_path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "v:0",
             str(video_path)],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            return {}
        streams = json.loads(r.stdout).get("streams", [])
        if not streams:
            return {}
        s = streams[0]
        result = {
            "width": int(s.get("width", 0)),
            "height": int(s.get("height", 0)),
        }
        nb = s.get("nb_frames")
        if nb and nb != "N/A":
            result["nb_frames"] = int(nb)
        r_fps = s.get("r_frame_rate", "")
        if "/" in r_fps:
            num, den = r_fps.split("/")
            if float(den) > 0:
                result["fps"] = float(num) / float(den)
        return result
    except Exception:
        return {}


def _compute_analysis_resolution(width, height, cap=ANALYSIS_RESOLUTION_CAP):
    if max(width, height) <= cap:
        return width, height
    scale = cap / max(width, height)
    return max(1, int(width * scale)), max(1, int(height * scale))


def _compute_entropy(gray):
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten()
    hist = hist[hist > 0]
    probs = hist / hist.sum()
    return float(-np.sum(probs * np.log2(probs)))


# ══════════════════════ 核心分析类 ══════════════════════

class ImageAnalyzer:
    """LeRobot 数据集图像质量分析器。"""

    def __init__(self, dataset_path):
        self.root = Path(dataset_path)
        self.info = {}
        self.cameras = {}
        self.episode_indices = []
        self.episode_data = {}
        self._lock = threading.Lock()

        self._load()

    def _load(self):
        info_path = self.root / "meta" / "info.json"
        if not info_path.exists():
            raise FileNotFoundError(f"缺少 meta/info.json: {self.root}")
        self.info = json.loads(info_path.read_text(encoding="utf-8"))

        self._scan_videos()
        self._load_parquet()

    def _scan_videos(self):
        video_dir = self.root / "videos"
        if not video_dir.exists():
            return
        video_map = {}
        for vf in sorted(video_dir.rglob("*.mp4")):
            ep_idx = self._parse_episode_index(vf.stem)
            if ep_idx is None:
                continue
            rel_parts = vf.relative_to(video_dir).parts
            cam_key = None
            if len(rel_parts) >= 3 and rel_parts[0].startswith("chunk-"):
                cam_key = rel_parts[1]
            elif len(rel_parts) >= 3 and rel_parts[1].startswith("chunk-"):
                cam_key = rel_parts[0]
            else:
                cam_key = vf.parent.name
            if cam_key.startswith("chunk-"):
                continue
            cam_name = cam_key.split(".")[-1] if "." in cam_key else cam_key
            video_map.setdefault(cam_name, {})[ep_idx] = str(vf)

        self.cameras = video_map
        all_eps = set()
        for cam_eps in video_map.values():
            all_eps.update(cam_eps.keys())
        self.episode_indices = sorted(all_eps)

    def _load_parquet(self):
        data_dir = self.root / "data"
        if not data_dir.exists():
            return
        for pf in sorted(data_dir.rglob("*.parquet")):
            ep_idx = self._parse_episode_index(pf.stem)
            if ep_idx is None:
                continue
            try:
                df = pd.read_parquet(pf)
                self.episode_data[ep_idx] = df
            except Exception as e:
                log.warning(f"读取 parquet 失败 {pf}: {e}")

    @staticmethod
    def _parse_episode_index(stem):
        if stem.startswith("episode_"):
            try:
                return int(stem.split("_")[1])
            except (IndexError, ValueError):
                return None
        return None

    def get_dataset_info(self):
        features = self.info.get("features", {})
        video_features = [k for k, v in features.items()
                          if v.get("dtype") in ("image", "video")]
        return {
            "total_episodes": len(self.episode_indices),
            "cameras": list(self.cameras.keys()),
            "video_features": video_features,
            "fps": self.info.get("fps", 30),
            "robot_type": self.info.get("robot_type", "unknown"),
            "episode_indices": self.episode_indices,
        }

    # ─── 逐帧分析 ───

    def _analyze_episode_video(self, video_path, ep_idx, joint_velocities=None):
        """分析单个 episode 的视频，返回逐帧指标。"""
        params = _probe_video(video_path)
        width = params.get("width", 0)
        height = params.get("height", 0)
        if width <= 0 or height <= 0:
            log.warning(f"无法获取视频尺寸: {video_path}")
            return None

        out_w, out_h = _compute_analysis_resolution(width, height)

        vf_filters = []
        if out_w != width or out_h != height:
            vf_filters.append(f"scale={out_w}:{out_h}")

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(video_path),
        ]
        if vf_filters:
            cmd += ["-vf", ",".join(vf_filters)]
        cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]

        proc = None
        frame_size = out_w * out_h * 3
        frame_metrics = []
        prev_gray = None
        frame_idx = 0

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            while True:
                buf = _read_exact(proc.stdout, frame_size)
                if not buf or len(buf) != frame_size:
                    break

                rgb = np.frombuffer(buf, dtype=np.uint8).reshape(out_h, out_w, 3)
                gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

                m = {}

                lap = cv2.Laplacian(gray, cv2.CV_64F)
                m["blur"] = float(lap.var())

                m["brightness"] = float(gray.mean() / 255.0)
                content_mask = gray >= CONTENT_PIXEL_FLOOR
                if content_mask.any():
                    m["brightness_content"] = float(gray[content_mask].mean() / 255.0)
                else:
                    m["brightness_content"] = m["brightness"]
                m["dark_ratio"] = float((~content_mask).sum() / gray.size)

                total_px = gray.size
                m["overexposed"] = float((gray > 250).sum() / total_px)
                m["underexposed"] = float((gray < 5).sum() / total_px)

                m["entropy"] = _compute_entropy(gray)

                m["contrast"] = float(gray.std() / 255.0)

                if prev_gray is not None:
                    diff = cv2.absdiff(gray, prev_gray)
                    m["frame_diff"] = float(diff.mean() / 255.0)
                else:
                    m["frame_diff"] = 0.0

                quality, sub_scores = compute_frame_quality(m)
                m["quality"] = quality

                frame_metrics.append(m)
                prev_gray = gray.copy()
                frame_idx += 1

            proc.wait(timeout=600)
        except FileNotFoundError:
            log.warning("ffmpeg 未安装")
            return None
        except subprocess.TimeoutExpired:
            if proc:
                proc.kill()
            return None
        except Exception as e:
            log.warning(f"分析视频异常 {video_path}: {e}")
            return None
        finally:
            if proc:
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()

        if not frame_metrics:
            return None

        n = len(frame_metrics)
        blur_arr = np.array([m["blur"] for m in frame_metrics])
        brightness_arr = np.array([m["brightness"] for m in frame_metrics])
        brightness_content_arr = np.array([m.get("brightness_content", m["brightness"]) for m in frame_metrics])
        dark_ratio_arr = np.array([m.get("dark_ratio", 0.0) for m in frame_metrics])
        entropy_arr = np.array([m["entropy"] for m in frame_metrics])
        contrast_arr = np.array([m["contrast"] for m in frame_metrics])
        quality_arr = np.array([m["quality"] for m in frame_metrics])
        frame_diff_arr = np.array([m["frame_diff"] for m in frame_metrics])
        overexp_arr = np.array([m["overexposed"] for m in frame_metrics])
        underexp_arr = np.array([m["underexposed"] for m in frame_metrics])

        problem_indices = []
        problem_reasons = []
        for i, m in enumerate(frame_metrics):
            reasons = []
            if m["blur"] < BLUR_ACCEPTABLE:
                reasons.append("blurry")
            br = m.get("brightness_content", m["brightness"])
            if br < BRIGHTNESS_LOW:
                reasons.append("dark")
            if br > BRIGHTNESS_HIGH:
                reasons.append("bright")
            if m["entropy"] < ENTROPY_ACCEPTABLE:
                reasons.append("low_info")
            if m["overexposed"] > EXPOSURE_BAD:
                reasons.append("overexposed")
            if m["underexposed"] > EXPOSURE_BAD:
                reasons.append("underexposed")
            if i > 0 and m["frame_diff"] < FRAME_DIFF_STATIC:
                reasons.append("static")
            if i > 0 and m["frame_diff"] > FRAME_DIFF_SCENE_CHANGE:
                reasons.append("scene_change")
            if reasons:
                problem_indices.append(i)
                problem_reasons.append(reasons)

        static_count = sum(
            1 for i, m in enumerate(frame_metrics)
            if i > 0 and m["frame_diff"] < FRAME_DIFF_STATIC
        )

        velocity_blur = None
        if joint_velocities is not None and len(joint_velocities) > 0:
            vel_len = min(n, len(joint_velocities))
            velocity_blur = {
                "velocity": joint_velocities[:vel_len].tolist(),
                "blur": blur_arr[:vel_len].tolist(),
            }

        fps = self.info.get("fps", 30) or 30
        timestamps = [i / fps for i in range(n)]

        result = {
            "episode_index": ep_idx,
            "frame_count": n,
            "resolution": f"{width}x{height}",
            "quality_score": float(quality_arr.mean()),
            "avg_blur": float(blur_arr.mean()),
            "avg_brightness": float(brightness_arr.mean()),
            "avg_brightness_content": float(brightness_content_arr.mean()),
            "avg_dark_ratio": float(dark_ratio_arr.mean()),
            "avg_entropy": float(entropy_arr.mean()),
            "avg_contrast": float(contrast_arr.mean()),
            "avg_overexposed": float(overexp_arr.mean()),
            "avg_underexposed": float(underexp_arr.mean()),
            "min_blur": float(blur_arr.min()),
            "max_blur": float(blur_arr.max()),
            "static_frame_ratio": static_count / max(1, n - 1),
            "problem_frame_count": len(problem_indices),
            "problem_frame_ratio": len(problem_indices) / max(1, n),
            "timeline": {
                "timestamps": timestamps,
                "blur": blur_arr.tolist(),
                "brightness": brightness_arr.tolist(),
                "brightness_content": brightness_content_arr.tolist(),
                "dark_ratio": dark_ratio_arr.tolist(),
                "entropy": entropy_arr.tolist(),
                "contrast": contrast_arr.tolist(),
                "frame_diff": frame_diff_arr.tolist(),
                "quality": quality_arr.tolist(),
            },
            "problems": [
                {"frame": problem_indices[i], "reasons": problem_reasons[i]}
                for i in range(len(problem_indices))
            ],
            "velocity_blur": velocity_blur,
        }

        return result

    # ─── 帧图像提取 ───

    def extract_frame_jpeg(self, camera, episode_index, frame_index):
        """从视频中提取指定帧，返回 JPEG 字节数据。"""
        if camera not in self.cameras:
            return None
        video_path = self.cameras[camera].get(episode_index)
        if not video_path or not Path(video_path).exists():
            return None

        fps = max(float(self.info.get("fps", 30) or 30), 1.0)
        seek_time = max(0, frame_index / fps - 0.5)

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{seek_time:.4f}",
            "-i", str(video_path),
            "-vf", f"select=eq(n\\,{frame_index})",
            "-vsync", "0",
            "-vframes", "1",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-q:v", "3",
            "pipe:1",
        ]

        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except Exception as e:
            log.warning(f"提取帧失败 ep{episode_index} frame{frame_index}: {e}")

        cmd_fallback = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(video_path),
            "-vf", f"select=eq(n\\,{frame_index})",
            "-vsync", "0",
            "-vframes", "1",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-q:v", "3",
            "pipe:1",
        ]
        try:
            r = subprocess.run(cmd_fallback, capture_output=True, timeout=60)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except Exception as e:
            log.warning(f"回退提取帧失败 ep{episode_index} frame{frame_index}: {e}")
        return None

    # ─── 关节速度提取 ───

    def _extract_joint_velocities(self, ep_idx):
        """从 parquet 提取该 episode 每帧的最大关节绝对速度。"""
        df = self.episode_data.get(ep_idx)
        if df is None:
            return None
        col = "observation.state"
        if col not in df.columns:
            return None
        try:
            values = np.array(df[col].tolist(), dtype=np.float64)
        except Exception:
            return None
        if values.ndim != 2 or values.shape[0] < 2:
            return None

        fps = max(float(self.info.get("fps", 30) or 30), 1e-6)
        dt = 1.0 / fps
        diffs = np.diff(values, axis=0) / dt
        max_vel = np.max(np.abs(diffs), axis=1)
        max_vel = np.concatenate([[0.0], max_vel])
        return max_vel

    # ─── 主入口 ───

    def analyze(self, camera, episodes=None, progress_cb=None):
        """分析指定相机的图像质量。

        Args:
            camera: 相机名称
            episodes: 要分析的 episode 列表，None 表示全部
            progress_cb: 回调函数 (stage, title, detail, current, total)

        Returns:
            分析报告字典
        """
        if camera not in self.cameras:
            raise ValueError(f"相机 '{camera}' 不存在，可用: {list(self.cameras.keys())}")

        cam_videos = self.cameras[camera]
        if episodes is not None:
            ep_list = [e for e in episodes if e in cam_videos]
        else:
            ep_list = sorted(cam_videos.keys())

        if not ep_list:
            raise ValueError("没有可分析的 episode")

        total = len(ep_list)
        episode_results = []

        if progress_cb:
            progress_cb("analyzing", "正在分析图像质量",
                        f"共 {total} 个 episode", 0, total)

        for i, ep_idx in enumerate(ep_list):
            video_path = cam_videos[ep_idx]
            if progress_cb:
                progress_cb("analyzing", "正在分析图像质量",
                            f"Episode {ep_idx} ({i + 1}/{total})",
                            i, total)

            joint_vel = self._extract_joint_velocities(ep_idx)
            result = self._analyze_episode_video(video_path, ep_idx, joint_vel)
            if result is not None:
                episode_results.append(result)

        if progress_cb:
            progress_cb("aggregating", "正在汇总结果", "", total, total)

        report = self._build_report(camera, episode_results)

        if progress_cb:
            progress_cb("done", "分析完成",
                        f"已分析 {len(episode_results)} 个 episode",
                        total, total)

        return report

    def _build_report(self, camera, episode_results):
        if not episode_results:
            return {
                "camera": camera,
                "episodes_analyzed": 0,
                "total_frames": 0,
                "summary": {},
                "episodes": [],
                "thresholds": self._get_thresholds(),
            }

        total_frames = sum(e["frame_count"] for e in episode_results)
        quality_scores = [e["quality_score"] for e in episode_results]
        blur_values = [e["avg_blur"] for e in episode_results]
        brightness_values = [e["avg_brightness"] for e in episode_results]
        brightness_content_values = [e.get("avg_brightness_content", e["avg_brightness"]) for e in episode_results]
        dark_ratio_values = [e.get("avg_dark_ratio", 0.0) for e in episode_results]
        entropy_values = [e["avg_entropy"] for e in episode_results]
        contrast_values = [e["avg_contrast"] for e in episode_results]
        problem_total = sum(e["problem_frame_count"] for e in episode_results)

        all_velocity = []
        all_blur_for_vel = []
        for ep in episode_results:
            vb = ep.get("velocity_blur")
            if vb:
                all_velocity.extend(vb["velocity"])
                all_blur_for_vel.extend(vb["blur"])

        velocity_blur_correlation = None
        if len(all_velocity) > 10:
            vel_arr = np.array(all_velocity)
            blur_arr = np.array(all_blur_for_vel)
            finite_mask = np.isfinite(vel_arr) & np.isfinite(blur_arr)
            if finite_mask.sum() > 10:
                v_clean = vel_arr[finite_mask]
                b_clean = blur_arr[finite_mask]
                corr = np.corrcoef(v_clean, b_clean)[0, 1]
                velocity_blur_correlation = {
                    "correlation": float(corr) if np.isfinite(corr) else None,
                    "sample_count": int(finite_mask.sum()),
                }
                n_sample = min(2000, len(v_clean))
                if n_sample < len(v_clean):
                    idx = np.round(np.linspace(0, len(v_clean) - 1, n_sample)).astype(int)
                    velocity_blur_correlation["velocity"] = v_clean[idx].tolist()
                    velocity_blur_correlation["blur"] = b_clean[idx].tolist()
                else:
                    velocity_blur_correlation["velocity"] = v_clean.tolist()
                    velocity_blur_correlation["blur"] = b_clean.tolist()

        def _grade(score):
            if score >= 90:
                return "excellent"
            if score >= 75:
                return "good"
            if score >= 60:
                return "acceptable"
            if score >= 40:
                return "poor"
            return "bad"

        avg_quality = float(np.mean(quality_scores))

        episode_summaries = []
        for ep in episode_results:
            ep_summary = {
                "episode_index": ep["episode_index"],
                "frame_count": ep["frame_count"],
                "quality_score": ep["quality_score"],
                "avg_blur": ep["avg_blur"],
                "avg_brightness": ep["avg_brightness"],
                "avg_brightness_content": ep.get("avg_brightness_content", ep["avg_brightness"]),
                "avg_dark_ratio": ep.get("avg_dark_ratio", 0.0),
                "avg_entropy": ep["avg_entropy"],
                "avg_contrast": ep["avg_contrast"],
                "min_blur": ep["min_blur"],
                "static_frame_ratio": ep["static_frame_ratio"],
                "problem_frame_count": ep["problem_frame_count"],
                "problem_frame_ratio": ep["problem_frame_ratio"],
                "resolution": ep["resolution"],
            }
            episode_summaries.append(ep_summary)

        best_ep = max(episode_results, key=lambda e: e["quality_score"])
        worst_ep = min(episode_results, key=lambda e: e["quality_score"])

        representative_idx = worst_ep["episode_index"]
        representative_detail = None
        for ep in episode_results:
            if ep["episode_index"] == representative_idx:
                representative_detail = {
                    "episode_index": ep["episode_index"],
                    "timeline": ep["timeline"],
                    "problems": ep["problems"][:100],
                    "velocity_blur": ep.get("velocity_blur"),
                }
                break

        self._cached_episode_details = {
            ep["episode_index"]: {
                "timeline": ep["timeline"],
                "problems": ep["problems"],
                "velocity_blur": ep.get("velocity_blur"),
            }
            for ep in episode_results
        }

        report = {
            "camera": camera,
            "episodes_analyzed": len(episode_results),
            "total_frames": total_frames,
            "summary": {
                "quality_score": avg_quality,
                "quality_grade": _grade(avg_quality),
                "avg_blur": float(np.mean(blur_values)),
                "avg_brightness": float(np.mean(brightness_values)),
                "avg_brightness_content": float(np.mean(brightness_content_values)),
                "avg_dark_ratio": float(np.mean(dark_ratio_values)),
                "avg_entropy": float(np.mean(entropy_values)),
                "avg_contrast": float(np.mean(contrast_values)),
                "problem_frame_ratio": problem_total / max(1, total_frames),
                "problem_frame_count": problem_total,
                "best_episode": best_ep["episode_index"],
                "best_quality": best_ep["quality_score"],
                "worst_episode": worst_ep["episode_index"],
                "worst_quality": worst_ep["quality_score"],
            },
            "episodes": episode_summaries,
            "representative_detail": representative_detail,
            "velocity_blur_correlation": velocity_blur_correlation,
            "thresholds": self._get_thresholds(),
        }

        return report

    def get_episode_detail(self, episode_index):
        details = getattr(self, "_cached_episode_details", {})
        return details.get(episode_index)

    @staticmethod
    def _get_thresholds():
        return {
            "blur_good": BLUR_GOOD,
            "blur_acceptable": BLUR_ACCEPTABLE,
            "brightness_low": BRIGHTNESS_LOW,
            "brightness_high": BRIGHTNESS_HIGH,
            "entropy_good": ENTROPY_GOOD,
            "entropy_acceptable": ENTROPY_ACCEPTABLE,
            "contrast_good": CONTRAST_GOOD,
            "contrast_acceptable": CONTRAST_ACCEPTABLE,
            "exposure_warn": EXPOSURE_WARN,
            "exposure_bad": EXPOSURE_BAD,
            "frame_diff_static": FRAME_DIFF_STATIC,
            "frame_diff_scene_change": FRAME_DIFF_SCENE_CHANGE,
        }
