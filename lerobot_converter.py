#!/usr/bin/env python3
"""LeRobot 数据集版本转换核心模块

支持的方向:
    v2.1 → v3.0   (合并 parquet / mp4, 生成 meta/stats.json 与 meta/episodes/*.parquet)
    v3.0 → v2.1   (拆分合并文件, 还原 episodes.jsonl / episodes_stats.jsonl)
    v2.1 → v2.0   (把 episodes_stats.jsonl 聚合成全局 stats.json, 删除前者)

格式细节参考 docs/lerobot_format_conversion.md。
"""

from __future__ import annotations

import copy
import json
import logging
import math
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

# ────────────────────────────────── 常量 ──────────────────────────────────

V20 = "v2.0"
V21 = "v2.1"
V30 = "v3.0"

DEFAULT_CHUNK_SIZE = 1000
DEFAULT_DATA_FILE_SIZE_MB = 100
DEFAULT_VIDEO_FILE_SIZE_MB = 500
DEFAULT_QUANTILES = [0.01, 0.10, 0.50, 0.90, 0.99]

LEGACY_DATA_PATH = "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
LEGACY_VIDEO_PATH = "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4"
V30_DATA_PATH = "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
V30_VIDEO_PATH = "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"

ProgressCb = Callable[[dict], None]


# ────────────────────────────────── 通用工具 ──────────────────────────────────

def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _to_python(value: Any) -> Any:
    """把 numpy / pyarrow / list / dict 统一转成可 json dump 的普通 python 类型"""
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (list, tuple)):
        return [_to_python(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_python(v) for k, v in value.items()}
    return value


def _flatten(d: dict, parent_key: str = "", sep: str = "/") -> dict:
    out = {}
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            out.update(_flatten(v, new_key, sep))
        else:
            out[new_key] = v
    return out


def _unflatten(d: dict, sep: str = "/") -> dict:
    out: dict = {}
    for k, v in d.items():
        parts = k.split(sep)
        cur = out
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = v
    return out


def _update_chunk_file_indices(chunk_idx: int, file_idx: int,
                               chunks_size: int = DEFAULT_CHUNK_SIZE) -> tuple[int, int]:
    file_idx += 1
    if file_idx >= chunks_size:
        file_idx = 0
        chunk_idx += 1
    return chunk_idx, file_idx


def _file_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


def _parquet_num_rows(path: Path) -> int:
    return pq.ParquetFile(path).metadata.num_rows


def _run(cmd: list[str], timeout: int = 3600) -> None:
    """运行外部命令，失败时抛出详细错误。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError as e:
        raise RuntimeError(f"找不到可执行文件: {cmd[0]}. 请先安装 ffmpeg") from e
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")[:600]
        raise RuntimeError(f"{cmd[0]} 失败 (code={proc.returncode}): {stderr}")


def _ffprobe_duration(path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    except FileNotFoundError as e:
        raise RuntimeError("找不到 ffprobe，请安装 ffmpeg 工具包") from e
    if proc.returncode != 0:
        return 0.0
    try:
        return float(proc.stdout.decode().strip() or 0.0)
    except ValueError:
        return 0.0


def _ffprobe_size(path: Path) -> tuple[int, int]:
    """返回视频 (width, height)。失败时返回 (0, 0)。"""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0", str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        if proc.returncode != 0:
            return (0, 0)
        out = proc.stdout.decode().strip()
        w_str, h_str = out.split("x")
        return (int(w_str), int(h_str))
    except Exception:  # pylint: disable=broad-except
        return (0, 0)


def _iter_video_frames_rgb(path: Path, stride: int = 1):
    """通过 ffmpeg pipe 流式读取视频帧 (RGB uint8, shape (H, W, 3))。

    stride>1 时用 ffmpeg 的 select 滤镜等距采样, 而不是先全解码再丢弃。
    """
    w, h = _ffprobe_size(path)
    if w <= 0 or h <= 0:
        return
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
    ]
    if stride and stride > 1:
        # select=not(mod(n\,S)), 配合 vsync vfr 才会真正丢帧而不是复制
        cmd += ["-vf", f"select=not(mod(n\\,{int(stride)}))", "-vsync", "vfr"]
    cmd += [
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-an", "-sn",
        "-",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as e:
        raise RuntimeError("找不到 ffmpeg，请安装 ffmpeg 工具包") from e

    frame_size = w * h * 3
    try:
        while True:
            buf = proc.stdout.read(frame_size)
            if not buf or len(buf) < frame_size:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
    finally:
        try:
            proc.stdout.close()
        except Exception:  # pylint: disable=broad-except
            pass
        proc.wait(timeout=10)


# ────────────────────────────────── 从原始数据重新计算 stats ──────────────────────────────────

def _compute_image_stats_from_video(video_path: Path, stride: int = 1) -> dict | None:
    """对单个视频文件, 按 LeRobot 约定计算 per-channel stats, shape (C, 1, 1)。

    mean/std/min/max 是 [0, 1] 区间的 float32; count 等于实际参与计算的帧数。
    """
    sum_c = np.zeros(3, dtype=np.float64)
    sumsq_c = np.zeros(3, dtype=np.float64)
    mn = np.full(3, np.inf, dtype=np.float64)
    mx = np.full(3, -np.inf, dtype=np.float64)
    total_px = 0
    n_frames = 0
    for frame in _iter_video_frames_rgb(video_path, stride=stride):
        f = frame.astype(np.float64) / 255.0
        flat = f.reshape(-1, 3)
        sum_c += flat.sum(axis=0)
        sumsq_c += (flat ** 2).sum(axis=0)
        mn = np.minimum(mn, flat.min(axis=0))
        mx = np.maximum(mx, flat.max(axis=0))
        total_px += flat.shape[0]
        n_frames += 1

    if total_px == 0:
        return None
    mean = sum_c / total_px
    var = np.maximum(sumsq_c / total_px - mean ** 2, 0.0)
    std = np.sqrt(var)
    return {
        "mean": mean.reshape(3, 1, 1),
        "std": std.reshape(3, 1, 1),
        "min": mn.reshape(3, 1, 1),
        "max": mx.reshape(3, 1, 1),
        "count": np.array(n_frames),
    }


def _stack_parquet_column(values: list) -> np.ndarray:
    """把 parquet 一列的原始 python 值栈成 numpy 数组。

    - 标量/int/float → shape (N,)
    - list[float]   → shape (N, D)
    - 嵌套 list     → 按第一行的形状 reshape
    - 空值 / None    → 用 np.nan 填充(仅对浮点列有效)
    """
    arr = np.asarray(values)
    if arr.dtype == object:
        try:
            arr = np.array([np.asarray(v) for v in values])
        except Exception:  # pylint: disable=broad-except
            return np.zeros((0,))
    return arr


def _compute_scalar_feature_stats(values: list) -> dict | None:
    """对非 image/video feature 计算 mean/std/min/max/count/quantiles。"""
    arr = _stack_parquet_column(values)
    if arr.size == 0:
        return None
    if not np.issubdtype(arr.dtype, np.number):
        # 非数值列(比如 task / index 等字符串) — 不参与 stats
        return None
    arr = arr.astype(np.float64, copy=False)
    count = arr.shape[0]
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    # ndim >= 2: 沿第一维 (时间) 聚合
    stats = {
        "mean": arr.mean(axis=0),
        "std":  arr.std(axis=0),
        "min":  arr.min(axis=0),
        "max":  arr.max(axis=0),
        "count": np.array(count),
    }
    for q in DEFAULT_QUANTILES:
        stats[f"q{int(q * 100):02d}"] = np.quantile(arr, q, axis=0)
    return stats


def compute_raw_stats_v21(src: Path, info: dict, progress_cb: ProgressCb,
                          *, video_stride: int = 1,
                          include_videos: bool = True) -> dict:
    """从 v2.1 源数据集的原始 parquet + mp4 重新计算 per-feature 全局 stats。

    返回: { feature_name: { "mean","std","min","max","count","qXX"... } }  (numpy 值)
    不依赖 episodes_stats.jsonl。

    实现方式: 逐 episode 扫, 每个 feature 先算 episode 级统计, 再用
    aggregate_feature_stats 并行地合并(Welford 风格), 避免一次性吃满内存。

    video_stride: 抽帧步长, 1 代表全部解码 (最慢最准), 5 表示每 5 帧取 1 帧。
    """
    features = info.get("features", {}) or {}
    video_keys = sorted(k for k, v in features.items() if (v or {}).get("dtype") == "video")
    numeric_keys = sorted(
        k for k, v in features.items()
        if (v or {}).get("dtype") in {"float32", "float64", "int32", "int64", "bool", "float", "int", "array"}
        or (isinstance((v or {}).get("shape"), list) and (v or {}).get("dtype") != "video"
            and (v or {}).get("dtype") != "image" and (v or {}).get("dtype") != "string")
    )

    ep_paths = sorted((src / "data").glob("chunk-*/episode_*.parquet"))
    if not ep_paths:
        raise FileNotFoundError(f"{src}/data 下没有 episode parquet, 无法重新计算 stats")

    per_ep_stats: dict[str, list[dict]] = defaultdict(list)

    total_steps = len(ep_paths) * (1 + (len(video_keys) if include_videos else 0))
    step = 0

    for ep_i, ep_path in enumerate(ep_paths):
        stem = ep_path.stem
        try:
            ep_idx = int(stem.split("_")[-1])
        except ValueError:
            ep_idx = ep_i

        # 1) parquet 中的数值 / 向量列
        try:
            df = pd.read_parquet(ep_path)
        except Exception as e:  # pylint: disable=broad-except
            progress_cb({"stage": "stats-raw", "title": "扫描 parquet 失败",
                         "detail": f"{ep_path.name}: {e}"})
            step += 1
            continue

        for col in df.columns:
            if col in video_keys:
                continue
            if col not in features:
                continue  # 只对 info.features 里声明过的列算
            ft = features.get(col) or {}
            dtype = ft.get("dtype")
            if dtype in {"video", "image", "string"}:
                continue
            vals = df[col].tolist()
            s = _compute_scalar_feature_stats(vals)
            if s is not None:
                per_ep_stats[col].append(s)

        step += 1
        if ep_i % 2 == 0 or ep_i == len(ep_paths) - 1:
            progress_cb({"stage": "stats-raw", "title": "重新计算 stats (扫描 parquet)",
                         "current": step, "total": total_steps,
                         "detail": f"episode {ep_idx}"})

        # 2) 视频 per-camera per-episode 统计
        if include_videos:
            for cam in video_keys:
                vp_candidates = list((src / "videos").glob(f"chunk-*/{cam}/episode_{ep_idx:06d}.mp4"))
                if not vp_candidates:
                    step += 1
                    continue
                vp = vp_candidates[0]
                try:
                    s = _compute_image_stats_from_video(vp, stride=video_stride)
                except Exception as e:  # pylint: disable=broad-except
                    progress_cb({"stage": "stats-raw", "title": "视频解码失败",
                                 "detail": f"{vp.name}: {e}"})
                    s = None
                if s is not None:
                    per_ep_stats[cam].append(s)
                step += 1
                progress_cb({"stage": "stats-raw",
                             "title": f"重新计算 stats · 视频 {cam}",
                             "current": step, "total": total_steps,
                             "detail": f"episode {ep_idx} · stride={video_stride}"})

    # 3) 聚合
    out: dict = {}
    warnings: list[str] = []
    for k, lst in per_ep_stats.items():
        if not lst:
            continue
        try:
            out[k] = aggregate_feature_stats(lst)
        except Exception as e:  # pylint: disable=broad-except
            warnings.append(f"feature '{k}' 聚合失败: {e}")
    out["__warnings__"] = warnings
    out["__source__"] = "recomputed-from-raw"
    return out


# ────────────────────────────────── 版本检测 / 数据集摘要 ──────────────────────────────────

def detect_codebase_version(root: Path) -> str:
    info_path = root / "meta" / "info.json"
    if not info_path.exists():
        raise FileNotFoundError(f"{root} 缺少 meta/info.json")
    info = _read_json(info_path)
    version = info.get("codebase_version") or "unknown"
    return version


def inspect_dataset(root_str: str) -> dict:
    root = Path(root_str).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"路径不存在: {root}")
    info = _read_json(root / "meta" / "info.json")
    version = info.get("codebase_version", "unknown")

    features = info.get("features", {}) or {}
    video_keys = [k for k, v in features.items() if (v or {}).get("dtype") == "video"]
    image_keys = [k for k, v in features.items() if (v or {}).get("dtype") == "image"]

    # episode 数量快速统计
    if version in (V20, V21):
        ep_count = info.get("total_episodes") or 0
        if not ep_count:
            eps = _read_jsonl(root / "meta" / "episodes.jsonl")
            ep_count = len(eps)
    else:
        ep_count = info.get("total_episodes") or 0

    dir_sizes = {}
    for sub in ("meta", "data", "videos", "images"):
        p = root / sub
        if p.exists():
            total = 0
            for f in p.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
            dir_sizes[sub] = total

    return {
        "root": str(root),
        "codebase_version": version,
        "fps": info.get("fps"),
        "robot_type": info.get("robot_type"),
        "total_episodes": ep_count,
        "total_frames": info.get("total_frames"),
        "total_tasks": info.get("total_tasks"),
        "chunks_size": info.get("chunks_size", DEFAULT_CHUNK_SIZE),
        "feature_keys": list(features.keys()),
        "video_keys": video_keys,
        "image_keys": image_keys,
        "dir_sizes_bytes": dir_sizes,
        "supported_targets": _supported_targets(version),
    }


def _supported_targets(version: str) -> list[str]:
    if version == V21:
        return [V30, V20]
    if version == V30:
        return [V21]
    if version == V20:
        return []
    return []


# ────────────────────────────────── stats 聚合 ──────────────────────────────────

def _cast_stats_to_numpy(stats: dict) -> dict:
    out = {}
    for key, vals in stats.items():
        out[key] = {m: np.array(v) for m, v in vals.items()}
    return out


def _normalize_count(c: Any) -> np.ndarray:
    """count 可能是 int / [N] / np.array(N) / np.array([N])，统一成标量 int ndarray。"""
    arr = np.asarray(c)
    if arr.size != 1:
        # 取第一个值作为 fallback（极少数情况下会出现形状异常）
        arr = np.asarray(arr.flatten()[0])
    return arr.astype(np.float64).reshape(())


def aggregate_feature_stats(stats_ft_list: list[dict]) -> dict:
    """官方 aggregate_feature_stats 的本地实现，并保留本工具使用的 qXX 字段。

    对 shape/count 做了容错：
    - count 允许是 scalar / [N] / np.array(N) 等，会被归一化到标量。
    - mean/std/min/max 按每个 episode 的 shape 广播，若 episode 之间 shape
      不一致会抛出 ValueError（由调用方捕获记录）。
    - qXX 按 episode count 加权聚合，与可视化保存路径保持一致。
    """
    if not stats_ft_list:
        raise ValueError("empty stats list")

    means = np.stack([np.asarray(s["mean"]) for s in stats_ft_list])
    stds = np.stack([np.asarray(s["std"]) for s in stats_ft_list])
    variances = stds ** 2
    mins = np.stack([np.asarray(s["min"]) for s in stats_ft_list])
    maxs = np.stack([np.asarray(s["max"]) for s in stats_ft_list])
    counts = np.stack([_normalize_count(s["count"]) for s in stats_ft_list])

    total_count = counts.sum(axis=0)
    if total_count <= 0:
        total_count = np.asarray(max(1.0, float(total_count)))

    counts_bc = counts.astype(np.float64)
    while counts_bc.ndim < means.ndim:
        counts_bc = np.expand_dims(counts_bc, axis=-1)

    weighted_means = means.astype(np.float64) * counts_bc
    total_mean = weighted_means.sum(axis=0) / total_count

    delta = means.astype(np.float64) - total_mean
    weighted_var = (variances.astype(np.float64) + delta ** 2) * counts_bc
    total_var = weighted_var.sum(axis=0) / total_count

    merged = {
        "min": np.min(mins, axis=0),
        "max": np.max(maxs, axis=0),
        "mean": total_mean,
        "std": np.sqrt(total_var),
        "count": total_count,
    }
    for metric in stats_ft_list[0]:
        if not metric.startswith("q"):
            continue
        if not all(metric in s for s in stats_ft_list):
            continue
        values = np.stack([np.asarray(s[metric]) for s in stats_ft_list])
        merged[metric] = (values.astype(np.float64) * counts_bc).sum(axis=0) / total_count
    return merged


def aggregate_stats(stats_list: list[dict]) -> dict:
    """逐 feature 聚合；单个 feature 失败不影响其它 feature，同时保留 warnings。"""
    keys = sorted({k for s in stats_list for k in s})
    out: dict = {}
    warnings: list[str] = []
    for k in keys:
        subset = [s[k] for s in stats_list if k in s]
        if not subset:
            continue
        try:
            out[k] = aggregate_feature_stats(subset)
        except Exception as e:  # pylint: disable=broad-except
            # 作为兜底：取第一份 stats 作为 fallback（比整体丢失要好）
            fallback = subset[0]
            try:
                out[k] = {
                    "min": np.asarray(fallback["min"]),
                    "max": np.asarray(fallback["max"]),
                    "mean": np.asarray(fallback["mean"]),
                    "std": np.asarray(fallback["std"]),
                    "count": _normalize_count(fallback["count"]),
                }
                for metric, value in fallback.items():
                    if metric.startswith("q"):
                        out[k][metric] = np.asarray(value)
                warnings.append(f"feature '{k}' 聚合失败, 已用第一份 episode 的 stats 作为 fallback: {e}")
            except Exception as e2:  # pylint: disable=broad-except
                warnings.append(f"feature '{k}' 聚合失败且 fallback 也失败: {e2}")
    out["__warnings__"] = warnings  # 下游可以选择丢弃这个 meta key
    return out


# ────────────────────────────────── 视频工具 ──────────────────────────────────

def _concat_videos(paths: list[Path], dst: Path) -> None:
    """使用 ffmpeg concat demuxer 无损合并 mp4（要求编码一致）。"""
    if not paths:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)

    listfile = dst.parent / f".{dst.stem}_concat.txt"
    lines = []
    for p in paths:
        lines.append(f"file '{p.resolve().as_posix()}'")
    listfile.write_text("\n".join(lines), encoding="utf-8")
    try:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(listfile),
            "-c", "copy", "-movflags", "+faststart",
            "-y", str(dst),
        ]
        _run(cmd)
    finally:
        try:
            listfile.unlink()
        except OSError:
            pass


def _extract_video_segment(src: Path, dst: Path, start: float, duration: float) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(start, 0):.6f}",
        "-i", str(src),
        "-t", f"{max(duration, 1e-3):.6f}",
        "-c", "copy", "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        "-y", str(dst),
    ]
    _run(cmd, timeout=600)


# ────────────────────────────────── v2.1 → v2.0 ──────────────────────────────────

def convert_v21_to_v20(src: Path, dst: Path, progress_cb: ProgressCb,
                       *, recompute_stats: bool = False,
                       video_stride: int = 1,
                       include_video_stats: bool = True) -> dict:
    """把 v2.1 数据集转成 v2.0: 生成全局 stats.json, 删除 episodes_stats.jsonl。

    recompute_stats=False (默认): 聚合 episodes_stats.jsonl (快, 但依赖源数据正确)
    recompute_stats=True         : 从原始 parquet + mp4 扫描重算 (慢, 可验证)
    """
    src = Path(src).resolve()
    dst = Path(dst).resolve()
    if src == dst:
        raise ValueError("目标目录不能与源目录相同")

    info = _read_json(src / "meta" / "info.json")
    if info.get("codebase_version") != V21:
        raise ValueError(f"源数据集 codebase_version 不是 {V21}（当前: {info.get('codebase_version')}）")

    progress_cb({"stage": "copy", "title": "复制数据文件", "current": 0, "total": 1})
    if dst.exists():
        shutil.rmtree(dst)
    _copy_tree_with_progress(src, dst, progress_cb, stage="copy", title="复制数据文件",
                             skip_files={"meta/episodes_stats.jsonl"})

    if recompute_stats:
        progress_cb({"stage": "stats-raw",
                     "title": "从原始 parquet / 视频重新计算 stats",
                     "current": 0, "total": 1,
                     "detail": f"video_stride={video_stride}, include_videos={include_video_stats}"})
        global_stats = compute_raw_stats_v21(src, info, progress_cb,
                                             video_stride=video_stride,
                                             include_videos=include_video_stats)
        stats_source = "recomputed-from-raw"
    else:
        progress_cb({"stage": "aggregate", "title": "聚合 episodes_stats → stats.json",
                     "current": 0, "total": 1})
        eps_stats_path = src / "meta" / "episodes_stats.jsonl"
        if not eps_stats_path.exists():
            raise FileNotFoundError("源 v2.1 数据集缺少 meta/episodes_stats.jsonl")
        stats_rows = _read_jsonl(eps_stats_path)
        stats_list = [_cast_stats_to_numpy(r["stats"]) for r in stats_rows]
        global_stats = aggregate_stats(stats_list)
        stats_source = "aggregated-from-episodes_stats.jsonl"

    agg_warnings = global_stats.pop("__warnings__", []) or []
    global_stats.pop("__source__", None)
    for w in agg_warnings:
        progress_cb({"stage": "warning", "title": "stats 聚合警告", "detail": w})
    global_stats_serial = {k: {m: _to_python(v) for m, v in sub.items()}
                           for k, sub in global_stats.items()}
    _write_json(dst / "meta" / "stats.json", global_stats_serial)

    target_eps_stats = dst / "meta" / "episodes_stats.jsonl"
    if target_eps_stats.exists():
        target_eps_stats.unlink()

    progress_cb({"stage": "info", "title": "更新 info.json", "current": 0, "total": 1})
    new_info = copy.deepcopy(info)
    new_info["codebase_version"] = V20
    _write_json(dst / "meta" / "info.json", new_info)

    progress_cb({"stage": "done", "title": "完成", "current": 1, "total": 1,
                 "detail": f"输出: {dst} · stats 来源: {stats_source}"})
    return {"output": str(dst), "target_version": V20,
            "stats_source": stats_source,
            "stats_feature_keys": sorted(global_stats_serial.keys()),
            "stats_warnings": agg_warnings}


def _copy_tree_with_progress(src: Path, dst: Path, progress_cb: ProgressCb,
                             stage: str, title: str,
                             skip_files: set[str] | None = None) -> None:
    skip_files = skip_files or set()
    all_files = [p for p in src.rglob("*") if p.is_file()]
    total = len(all_files)
    for i, f in enumerate(all_files):
        rel = f.relative_to(src).as_posix()
        if rel in skip_files:
            continue
        tgt = dst / rel
        tgt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, tgt)
        if (i + 1) % 20 == 0 or i == total - 1:
            progress_cb({"stage": stage, "title": title,
                         "current": i + 1, "total": total,
                         "detail": rel})


# ────────────────────────────────── v2.1 → v3.0 ──────────────────────────────────

def convert_v21_to_v30(src: Path, dst: Path, progress_cb: ProgressCb,
                       data_file_size_mb: int = DEFAULT_DATA_FILE_SIZE_MB,
                       video_file_size_mb: int = DEFAULT_VIDEO_FILE_SIZE_MB,
                       *, recompute_stats: bool = False,
                       video_stride: int = 1,
                       include_video_stats: bool = True) -> dict:
    src = Path(src).resolve()
    dst = Path(dst).resolve()
    if src == dst:
        raise ValueError("目标目录不能与源目录相同")
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)

    info = _read_json(src / "meta" / "info.json")
    if info.get("codebase_version") != V21:
        raise ValueError(f"源数据集 codebase_version 不是 {V21}（当前: {info.get('codebase_version')}）")

    features = info.get("features", {}) or {}
    video_keys = sorted(k for k, v in features.items() if (v or {}).get("dtype") == "video")
    image_keys = sorted(k for k, v in features.items() if (v or {}).get("dtype") == "image")
    chunks_size = int(info.get("chunks_size", DEFAULT_CHUNK_SIZE))

    # 1) 扫描 parquet
    ep_paths = sorted((src / "data").glob("chunk-*/episode_*.parquet"))
    if not ep_paths:
        raise FileNotFoundError("源数据集 data/ 下没有找到任何 episode parquet")

    # 2) 读取 episodes.jsonl / episodes_stats.jsonl / tasks.jsonl
    legacy_eps = _read_jsonl(src / "meta" / "episodes.jsonl")
    legacy_eps_by_idx = {int(r["episode_index"]): r for r in legacy_eps}
    legacy_stats = _read_jsonl(src / "meta" / "episodes_stats.jsonl")
    legacy_stats_by_idx = {int(r["episode_index"]): r["stats"] for r in legacy_stats}
    legacy_tasks = _read_jsonl(src / "meta" / "tasks.jsonl")

    # 3) 合并 parquet
    progress_cb({"stage": "data", "title": "合并 parquet 数据文件",
                 "current": 0, "total": len(ep_paths)})

    episodes_meta: list[dict] = []
    chunk_idx = file_idx = 0
    size_mb = 0.0
    num_frames = 0
    pending: list[Path] = []

    def _flush_data():
        nonlocal chunk_idx, file_idx, size_mb, pending
        if not pending:
            return
        tgt = dst / V30_DATA_PATH.format(chunk_index=chunk_idx, file_index=file_idx)
        tgt.parent.mkdir(parents=True, exist_ok=True)
        dfs = [pd.read_parquet(p) for p in pending]
        merged = pd.concat(dfs, ignore_index=True)
        merged.to_parquet(tgt, index=False)
        pending = []
        size_mb = 0.0

    for i, ep_path in enumerate(ep_paths):
        ep_size = _file_size_mb(ep_path)
        ep_frames = _parquet_num_rows(ep_path)

        if size_mb + ep_size >= data_file_size_mb and pending:
            _flush_data()
            chunk_idx, file_idx = _update_chunk_file_indices(chunk_idx, file_idx, chunks_size)

        # 从文件名推断 episode_index（episode_000007.parquet → 7）
        stem = ep_path.stem
        try:
            ep_idx = int(stem.split("_")[-1])
        except ValueError:
            ep_idx = i

        episodes_meta.append({
            "episode_index": ep_idx,
            "data/chunk_index": chunk_idx,
            "data/file_index": file_idx,
            "dataset_from_index": num_frames,
            "dataset_to_index": num_frames + ep_frames,
        })
        size_mb += ep_size
        num_frames += ep_frames
        pending.append(ep_path)

        if (i + 1) % 5 == 0 or i == len(ep_paths) - 1:
            progress_cb({"stage": "data", "title": "合并 parquet 数据文件",
                         "current": i + 1, "total": len(ep_paths),
                         "detail": ep_path.name})
    _flush_data()

    # 4) 合并每个 camera 的 mp4
    video_meta_by_ep: dict[int, dict] = {m["episode_index"]: {} for m in episodes_meta}
    if video_keys:
        for cam_i, cam in enumerate(video_keys):
            cam_ep_paths = sorted((src / "videos").glob(f"chunk-*/{cam}/episode_*.mp4"))
            total_cam = len(cam_ep_paths)
            progress_cb({"stage": f"video:{cam}", "title": f"合并视频 {cam}",
                         "current": 0, "total": total_cam,
                         "detail": f"camera {cam_i + 1}/{len(video_keys)}"})

            v_chunk_idx = v_file_idx = 0
            v_size_mb = 0.0
            duration_s = 0.0
            v_pending: list[Path] = []
            v_pending_eps: list[int] = []

            def _flush_video():
                nonlocal v_chunk_idx, v_file_idx, v_size_mb, v_pending, v_pending_eps
                if not v_pending:
                    return
                tgt = dst / V30_VIDEO_PATH.format(video_key=cam,
                                                  chunk_index=v_chunk_idx,
                                                  file_index=v_file_idx)
                _concat_videos(v_pending, tgt)
                for ep_idx in v_pending_eps:
                    video_meta_by_ep[ep_idx][f"videos/{cam}/chunk_index"] = v_chunk_idx
                    video_meta_by_ep[ep_idx][f"videos/{cam}/file_index"] = v_file_idx
                v_pending = []
                v_pending_eps = []
                v_size_mb = 0.0

            for j, vp in enumerate(cam_ep_paths):
                ep_size = _file_size_mb(vp)
                ep_dur = _ffprobe_duration(vp)
                # 从文件名提取 ep index
                stem = vp.stem
                try:
                    ep_idx = int(stem.split("_")[-1])
                except ValueError:
                    ep_idx = j

                if v_size_mb + ep_size >= video_file_size_mb and v_pending:
                    _flush_video()
                    v_chunk_idx, v_file_idx = _update_chunk_file_indices(
                        v_chunk_idx, v_file_idx, chunks_size)
                    duration_s = 0.0

                # 在 flush 之后才记录 from/to_timestamp（相对于当前合并文件的累计时长）
                video_meta_by_ep.setdefault(ep_idx, {})
                video_meta_by_ep[ep_idx][f"videos/{cam}/from_timestamp"] = duration_s
                video_meta_by_ep[ep_idx][f"videos/{cam}/to_timestamp"] = duration_s + ep_dur
                duration_s += ep_dur

                v_pending.append(vp)
                v_pending_eps.append(ep_idx)
                v_size_mb += ep_size

                if (j + 1) % 3 == 0 or j == total_cam - 1:
                    progress_cb({"stage": f"video:{cam}", "title": f"合并视频 {cam}",
                                 "current": j + 1, "total": total_cam,
                                 "detail": vp.name})
            _flush_video()

    # 5) 任务 -> tasks.jsonl（v3.0 官方主线仍保留 jsonl, 也可选写 parquet）
    progress_cb({"stage": "meta", "title": "写入 tasks / episodes / stats",
                 "current": 0, "total": 4})
    _write_jsonl(dst / "meta" / "tasks.jsonl",
                 [{"task_index": int(r["task_index"]), "task": r["task"]}
                  for r in sorted(legacy_tasks, key=lambda x: int(x["task_index"]))])

    # v3 LeRobotDataset 通过 tasks.iloc[task_idx].name 读取任务文本，
    # 因此 task 字符串必须是 DataFrame index，而不是普通列。
    tasks_df = (
        pd.DataFrame(legacy_tasks)[["task_index", "task"]]
        .sort_values("task_index")
        .reset_index(drop=True)
        .set_index("task", drop=True)
    )
    tasks_df.to_parquet(dst / "meta" / "tasks.parquet", index=True)

    progress_cb({"stage": "meta", "title": "写入 tasks / episodes / stats",
                 "current": 1, "total": 4, "detail": "tasks.jsonl 写入完成"})

    # 6) 合并 episodes 元数据
    task_to_index = {r["task"]: int(r["task_index"]) for r in legacy_tasks}
    ep_rows = []
    for em in episodes_meta:
        ep_idx = em["episode_index"]
        legacy = legacy_eps_by_idx.get(ep_idx, {})
        legacy_stats_entry = legacy_stats_by_idx.get(ep_idx, {})
        ep_video = video_meta_by_ep.get(ep_idx, {})

        flat_stats = {}
        for feat_name, metrics in legacy_stats_entry.items():
            for m_name, m_val in metrics.items():
                flat_stats[f"stats/{feat_name}/{m_name}"] = m_val

        # tasks 字段: 允许字符串列表；task_index 可能是第一个任务的索引
        tasks_list = legacy.get("tasks") or []
        task_idx = None
        if tasks_list:
            task_idx = task_to_index.get(tasks_list[0])

        row = {
            "episode_index": int(ep_idx),
            "length": int(legacy.get("length", em["dataset_to_index"] - em["dataset_from_index"])),
            "tasks": list(tasks_list),
            "data/chunk_index": int(em["data/chunk_index"]),
            "data/file_index": int(em["data/file_index"]),
            "dataset_from_index": int(em["dataset_from_index"]),
            "dataset_to_index": int(em["dataset_to_index"]),
            "meta/episodes/chunk_index": 0,
            "meta/episodes/file_index": 0,
        }
        if task_idx is not None:
            row["task_index"] = int(task_idx)
        row.update(ep_video)
        row.update(flat_stats)
        ep_rows.append(row)

    ep_df = pd.DataFrame(ep_rows)
    ep_parquet = dst / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
    ep_parquet.parent.mkdir(parents=True, exist_ok=True)
    ep_df.to_parquet(ep_parquet, index=False)

    progress_cb({"stage": "meta", "title": "写入 tasks / episodes / stats",
                 "current": 2, "total": 4, "detail": "episodes parquet 写入完成"})

    # 7) 聚合 stats.json
    if recompute_stats:
        progress_cb({"stage": "stats-raw",
                     "title": "从原始 parquet / 视频重新计算 stats",
                     "current": 0, "total": 1,
                     "detail": f"video_stride={video_stride}, include_videos={include_video_stats}"})
        global_stats = compute_raw_stats_v21(src, info, progress_cb,
                                             video_stride=video_stride,
                                             include_videos=include_video_stats)
        stats_source = "recomputed-from-raw"
    else:
        stats_list_np = [_cast_stats_to_numpy(r["stats"]) for r in legacy_stats]
        global_stats = aggregate_stats(stats_list_np)
        stats_source = "aggregated-from-episodes_stats.jsonl"

    agg_warnings = global_stats.pop("__warnings__", []) or []
    global_stats.pop("__source__", None)
    for w in agg_warnings:
        progress_cb({"stage": "warning", "title": "stats 聚合警告", "detail": w})
    global_stats_serial = {k: {m: _to_python(v) for m, v in sub.items()}
                           for k, sub in global_stats.items()}
    _write_json(dst / "meta" / "stats.json", global_stats_serial)
    progress_cb({"stage": "meta", "title": "写入 tasks / episodes / stats",
                 "current": 3, "total": 4, "detail":
                 f"stats.json 写入完成 (来源: {stats_source}, feature 数: {len(global_stats_serial)}, 警告 {len(agg_warnings)} 条)"})

    # 8) 更新 info.json
    new_info = copy.deepcopy(info)
    new_info["codebase_version"] = V30
    new_info.pop("total_chunks", None)
    new_info.pop("total_videos", None)
    new_info["data_files_size_in_mb"] = int(data_file_size_mb)
    new_info["video_files_size_in_mb"] = int(video_file_size_mb)
    new_info["data_path"] = V30_DATA_PATH
    new_info["video_path"] = V30_VIDEO_PATH if video_keys else None
    fps = new_info.get("fps")
    if fps is not None:
        new_info["fps"] = int(fps) if float(fps).is_integer() else float(fps)
        for key, ft in new_info.get("features", {}).items():
            if not isinstance(ft, dict):
                continue
            if ft.get("dtype") == "video":
                continue
            ft["fps"] = new_info["fps"]
    _write_json(dst / "meta" / "info.json", new_info)
    progress_cb({"stage": "meta", "title": "写入 tasks / episodes / stats",
                 "current": 4, "total": 4, "detail": "info.json 更新完成"})

    # 9) 复制 images/ 等辅助目录
    if (src / "images").is_dir():
        shutil.copytree(src / "images", dst / "images", dirs_exist_ok=True)

    progress_cb({"stage": "done", "title": "完成",
                 "current": 1, "total": 1, "detail": f"输出: {dst}"})
    return {"output": str(dst), "target_version": V30,
            "data_file_size_mb": data_file_size_mb,
            "video_file_size_mb": video_file_size_mb,
            "stats_source": stats_source,
            "stats_feature_keys": sorted(global_stats_serial.keys()),
            "stats_warnings": agg_warnings}


# ────────────────────────────────── v3.0 → v2.1 ──────────────────────────────────

def convert_v30_to_v21(src: Path, dst: Path, progress_cb: ProgressCb) -> dict:
    src = Path(src).resolve()
    dst = Path(dst).resolve()
    if src == dst:
        raise ValueError("目标目录不能与源目录相同")
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)

    info = _read_json(src / "meta" / "info.json")
    if info.get("codebase_version") != V30:
        raise ValueError(f"源数据集 codebase_version 不是 {V30}（当前: {info.get('codebase_version')}）")

    features = info.get("features", {}) or {}
    video_keys = sorted(k for k, v in features.items() if (v or {}).get("dtype") == "video")
    chunks_size = int(info.get("chunks_size", DEFAULT_CHUNK_SIZE))

    # 1) 读取 episodes metadata parquet
    ep_parquet_paths = sorted((src / "meta" / "episodes").glob("chunk-*/file-*.parquet"))
    if not ep_parquet_paths:
        raise FileNotFoundError(f"{src}/meta/episodes 下没找到任何 parquet")
    ep_records: list[dict] = []
    for p in ep_parquet_paths:
        t = pq.read_table(p)
        ep_records.extend(t.to_pylist())
    ep_records.sort(key=lambda r: int(r["episode_index"]))

    # 2) 拆分数据 parquet
    grouped_data: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for r in ep_records:
        key = (int(r["data/chunk_index"]), int(r["data/file_index"]))
        grouped_data[key].append(r)

    total_eps = len(ep_records)
    done_eps = 0
    progress_cb({"stage": "data", "title": "拆分 parquet 为逐 episode 文件",
                 "current": 0, "total": total_eps})

    for (c_idx, f_idx), rows in grouped_data.items():
        src_path = src / V30_DATA_PATH.format(chunk_index=c_idx, file_index=f_idx)
        if not src_path.exists():
            raise FileNotFoundError(f"合并 parquet 缺失: {src_path}")
        table = pq.read_table(src_path)
        rows_sorted = sorted(rows, key=lambda x: int(x["dataset_from_index"]))
        file_base = int(rows_sorted[0]["dataset_from_index"])

        for r in rows_sorted:
            ep_idx = int(r["episode_index"])
            start = int(r["dataset_from_index"]) - file_base
            stop = int(r["dataset_to_index"]) - file_base
            ep_table = table.slice(start, stop - start)
            dest_chunk = ep_idx // chunks_size
            dest_path = dst / LEGACY_DATA_PATH.format(
                episode_chunk=dest_chunk, episode_index=ep_idx)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(ep_table, dest_path)
            done_eps += 1
            if done_eps % 5 == 0 or done_eps == total_eps:
                progress_cb({"stage": "data", "title": "拆分 parquet 为逐 episode 文件",
                             "current": done_eps, "total": total_eps,
                             "detail": dest_path.name})

    # 3) 拆分视频
    if video_keys:
        for cam_i, cam in enumerate(video_keys):
            chunk_col = f"videos/{cam}/chunk_index"
            file_col = f"videos/{cam}/file_index"
            grouped_v: dict[tuple[int, int], list[dict]] = defaultdict(list)
            for r in ep_records:
                if chunk_col not in r or file_col not in r:
                    continue
                if r.get(chunk_col) is None or r.get(file_col) is None:
                    continue
                grouped_v[(int(r[chunk_col]), int(r[file_col]))].append(r)

            total_v = sum(len(v) for v in grouped_v.values())
            done_v = 0
            progress_cb({"stage": f"video:{cam}", "title": f"拆分视频 {cam}",
                         "current": 0, "total": total_v,
                         "detail": f"camera {cam_i + 1}/{len(video_keys)}"})
            for (c_idx, f_idx), rows in grouped_v.items():
                src_mp4 = src / V30_VIDEO_PATH.format(
                    video_key=cam, chunk_index=c_idx, file_index=f_idx)
                if not src_mp4.exists():
                    raise FileNotFoundError(f"合并 mp4 缺失: {src_mp4}")

                rows_sorted = sorted(
                    rows, key=lambda x: float(x[f"videos/{cam}/from_timestamp"]))
                for r in rows_sorted:
                    ep_idx = int(r["episode_index"])
                    t0 = float(r[f"videos/{cam}/from_timestamp"])
                    t1 = float(r[f"videos/{cam}/to_timestamp"])
                    dest_chunk = ep_idx // chunks_size
                    dest_path = dst / LEGACY_VIDEO_PATH.format(
                        episode_chunk=dest_chunk,
                        video_key=cam,
                        episode_index=ep_idx)
                    _extract_video_segment(src_mp4, dest_path, t0, t1 - t0)
                    done_v += 1
                    if done_v % 3 == 0 or done_v == total_v:
                        progress_cb({"stage": f"video:{cam}", "title": f"拆分视频 {cam}",
                                     "current": done_v, "total": total_v,
                                     "detail": dest_path.name})

    # 4) tasks.jsonl
    progress_cb({"stage": "meta", "title": "写入 tasks / episodes / stats",
                 "current": 0, "total": 4})
    tasks_rows = _load_v30_tasks(src)
    _write_jsonl(dst / "meta" / "tasks.jsonl", tasks_rows)
    progress_cb({"stage": "meta", "title": "写入 tasks / episodes / stats",
                 "current": 1, "total": 4, "detail": "tasks.jsonl 写入完成"})

    # 5) episodes.jsonl
    legacy_episodes = []
    for r in ep_records:
        legacy_episodes.append({
            "episode_index": int(r["episode_index"]),
            "tasks": list(r.get("tasks") or []),
            "length": int(r.get("length") or (int(r["dataset_to_index"]) - int(r["dataset_from_index"]))),
        })
    _write_jsonl(dst / "meta" / "episodes.jsonl", legacy_episodes)
    progress_cb({"stage": "meta", "title": "写入 tasks / episodes / stats",
                 "current": 2, "total": 4, "detail": "episodes.jsonl 写入完成"})

    # 6) episodes_stats.jsonl
    eps_stats_rows = []
    for r in ep_records:
        flat_stats = {k: r[k] for k in r if k.startswith("stats/")}
        # 去掉 "stats/" 前缀再 unflatten
        stripped = {k[len("stats/"):]: v for k, v in flat_stats.items()}
        nested = _unflatten(stripped)
        nested_py = _to_python(nested)
        eps_stats_rows.append({
            "episode_index": int(r["episode_index"]),
            "stats": nested_py,
        })
    _write_jsonl(dst / "meta" / "episodes_stats.jsonl", eps_stats_rows)
    progress_cb({"stage": "meta", "title": "写入 tasks / episodes / stats",
                 "current": 3, "total": 4, "detail": "episodes_stats.jsonl 写入完成"})

    # 7) info.json
    new_info = copy.deepcopy(info)
    new_info["codebase_version"] = V21
    new_info.pop("data_files_size_in_mb", None)
    new_info.pop("video_files_size_in_mb", None)
    new_info["data_path"] = LEGACY_DATA_PATH
    new_info["video_path"] = LEGACY_VIDEO_PATH if video_keys else None
    total_eps_num = new_info.get("total_episodes") or len(ep_records)
    new_info["total_chunks"] = math.ceil(total_eps_num / chunks_size) if total_eps_num else 0
    new_info["total_videos"] = total_eps_num * len(video_keys)
    # 移除 v3 给非视频 feature 写的 fps
    for key, ft in new_info.get("features", {}).items():
        if not isinstance(ft, dict):
            continue
        if ft.get("dtype") != "video":
            ft.pop("fps", None)
    _write_json(dst / "meta" / "info.json", new_info)
    progress_cb({"stage": "meta", "title": "写入 tasks / episodes / stats",
                 "current": 4, "total": 4, "detail": "info.json 更新完成"})

    # 8) 复制 images/ 等
    if (src / "images").is_dir():
        shutil.copytree(src / "images", dst / "images", dirs_exist_ok=True)

    progress_cb({"stage": "done", "title": "完成",
                 "current": 1, "total": 1, "detail": f"输出: {dst}"})
    return {"output": str(dst), "target_version": V21}


def _load_v30_tasks(src: Path) -> list[dict]:
    parquet_path = src / "meta" / "tasks.parquet"
    jsonl_path = src / "meta" / "tasks.jsonl"
    rows: list[dict] = []
    if parquet_path.exists():
        df = pq.read_table(parquet_path).to_pandas()
        if "task_index" in df.columns:
            idx_col = df["task_index"].astype(int).tolist()
        else:
            idx_col = list(range(len(df)))
        index_col = df.index.astype(str).tolist()
        default_range_index = index_col == [str(i) for i in range(len(df))]
        if not default_range_index:
            task_col = index_col
        elif "task" in df.columns:
            # 兼容旧转换器写出的坏格式: task 被保存成普通列。
            task_col = df["task"].astype(str).tolist()
        else:
            task_col = index_col
        for idx, t in zip(idx_col, task_col):
            rows.append({"task_index": int(idx), "task": str(t)})
    elif jsonl_path.exists():
        for r in _read_jsonl(jsonl_path):
            rows.append({"task_index": int(r["task_index"]), "task": str(r["task"])})
    else:
        raise FileNotFoundError(f"{src} 下既没有 tasks.parquet 也没有 tasks.jsonl")
    rows.sort(key=lambda r: r["task_index"])
    return rows


# ────────────────────────────────── 数据集对比 ──────────────────────────────────

PREVIEW_TEXT_EXT = {".json", ".jsonl", ".txt", ".md", ".yaml", ".yml", ".csv"}
PREVIEW_MAX_BYTES = 256 * 1024


def build_tree(root_str: str, max_depth: int = 8) -> dict:
    root = Path(root_str).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"路径不存在: {root}")

    def walk(path: Path, depth: int) -> dict:
        node = {"name": path.name, "path": str(path), "is_dir": path.is_dir()}
        if path.is_dir():
            children = []
            if depth < max_depth:
                try:
                    entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
                except PermissionError:
                    entries = []
                for e in entries:
                    children.append(walk(e, depth + 1))
            node["children"] = children
            node["count"] = len(children)
        else:
            try:
                node["size"] = path.stat().st_size
            except OSError:
                node["size"] = 0
        return node

    tree = walk(root, 0)
    tree["name"] = tree["name"] or str(root)
    return tree


def preview_file(path_str: str, max_bytes: int = PREVIEW_MAX_BYTES) -> dict:
    path = Path(path_str).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")

    size = path.stat().st_size
    suffix = path.suffix.lower()
    mime_kind = "binary"
    content: Any = None

    if suffix == ".parquet":
        pf = pq.ParquetFile(path)
        schema = [{"name": f.name, "type": str(f.type)} for f in pf.schema_arrow]
        num_rows = pf.metadata.num_rows
        # 读取前 20 行作为示例
        sample_table = pq.read_table(path).slice(0, 20)
        sample_df = sample_table.to_pandas()
        for col in sample_df.columns:
            # 所有非标量列转成字符串截断显示，避免 json 序列化失败
            sample_df[col] = sample_df[col].apply(
                lambda v: _to_python(v) if not hasattr(v, "tolist") else _to_python(v.tolist()))
        mime_kind = "parquet"
        content = {
            "schema": schema,
            "num_rows": int(num_rows),
            "num_columns": len(schema),
            "sample_rows": sample_df.head(20).to_dict(orient="records"),
        }
    elif suffix in PREVIEW_TEXT_EXT or suffix == "":
        mime_kind = "text"
        with path.open("rb") as f:
            raw = f.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        text = raw[:max_bytes].decode("utf-8", errors="replace")
        content = {"text": text, "truncated": truncated}
        if suffix == ".json":
            try:
                parsed = json.loads(text)
                content["parsed_sample"] = _to_python(parsed) if not isinstance(parsed, (list, dict)) else _trim_json_preview(parsed)
            except Exception:
                pass
        if suffix == ".jsonl":
            lines = [ln for ln in text.splitlines() if ln.strip()][:10]
            parsed = []
            for ln in lines:
                try:
                    parsed.append(json.loads(ln))
                except Exception:
                    break
            if parsed:
                content["parsed_sample"] = _trim_json_preview(parsed)
    elif suffix in (".mp4", ".webm", ".mov", ".avi", ".mkv"):
        mime_kind = "video"
        content = {"duration": _ffprobe_duration(path)}
    elif suffix in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
        mime_kind = "image"
        content = {}
    else:
        mime_kind = "binary"
        content = {}

    return {
        "path": str(path),
        "name": path.name,
        "size": size,
        "suffix": suffix,
        "kind": mime_kind,
        "content": content,
    }


def _trim_json_preview(obj: Any, max_items: int = 40, max_str: int = 2000) -> Any:
    if isinstance(obj, dict):
        out = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= max_items:
                out[f"...({len(obj) - max_items} more keys)"] = "..."
                break
            out[k] = _trim_json_preview(v, max_items, max_str)
        return out
    if isinstance(obj, list):
        if len(obj) > max_items:
            return [_trim_json_preview(v, max_items, max_str) for v in obj[:max_items]] + [f"...(+{len(obj) - max_items} more)"]
        return [_trim_json_preview(v, max_items, max_str) for v in obj]
    if isinstance(obj, str) and len(obj) > max_str:
        return obj[:max_str] + f"...(+{len(obj) - max_str} chars)"
    return _to_python(obj)


def compare_datasets(left_str: str, right_str: str) -> dict:
    left = Path(left_str).expanduser().resolve()
    right = Path(right_str).expanduser().resolve()

    def stat_info(root: Path) -> dict:
        info_path = root / "meta" / "info.json"
        if info_path.exists():
            try:
                info = _read_json(info_path)
                return {
                    "codebase_version": info.get("codebase_version"),
                    "total_episodes": info.get("total_episodes"),
                    "total_frames": info.get("total_frames"),
                    "fps": info.get("fps"),
                }
            except Exception as e:  # pylint: disable=broad-except
                return {"error": str(e)}
        return {}

    left_files = {p.relative_to(left).as_posix(): p.stat().st_size
                  for p in left.rglob("*") if p.is_file()} if left.exists() else {}
    right_files = {p.relative_to(right).as_posix(): p.stat().st_size
                   for p in right.rglob("*") if p.is_file()} if right.exists() else {}

    only_left = sorted(set(left_files) - set(right_files))
    only_right = sorted(set(right_files) - set(left_files))
    common = sorted(set(left_files) & set(right_files))
    common_diff = sorted([k for k in common if left_files[k] != right_files[k]])

    return {
        "left": {"root": str(left), "exists": left.exists(),
                 "file_count": len(left_files),
                 "total_size": sum(left_files.values()),
                 "info": stat_info(left)},
        "right": {"root": str(right), "exists": right.exists(),
                  "file_count": len(right_files),
                  "total_size": sum(right_files.values()),
                  "info": stat_info(right)},
        "only_in_left": only_left[:5000],
        "only_in_right": only_right[:5000],
        "common": common[:5000],
        "common_diff": common_diff[:5000],
        "common_count": len(common),
        "common_diff_count": len(common_diff),
        "only_left_count": len(only_left),
        "only_right_count": len(only_right),
    }
