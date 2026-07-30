#!/usr/bin/env python3
"""
LeRobot 数据集批处理工具。

本模块不依赖 Flask，也不直接导入 app.py。调用方传入 DatasetEditor 实例后，
可组合执行:
  - 按 episode 长度删除过短/过长数据
  - 按 observation.state 运动量裁掉开头/结尾静止帧
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import numpy as np


def parse_joint_indices(raw: Any) -> Optional[List[int]]:
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        parts = [p.strip() for p in text.replace(";", ",").split(",")]
        return [int(p) for p in parts if p]
    if isinstance(raw, Iterable):
        result = []
        for item in raw:
            if item is None or item == "":
                continue
            result.append(int(item))
        return result or None
    return None


def episode_length(editor: Any, episode_index: int, meta: Dict[str, Any]) -> int:
    if episode_index in editor.episode_data:
        return int(len(editor.episode_data[episode_index]))
    return int(meta.get("length", 0) or 0)


def compute_length_iqr_bounds(editor: Any, iqr_multiplier: float = 1.5) -> Dict[str, Any]:
    lengths = []
    for meta in editor.episodes_meta:
        episode_index = int(meta["episode_index"])
        lengths.append(episode_length(editor, episode_index, meta))

    if not lengths:
        return {
            "enabled": True,
            "count": 0,
            "q1": None,
            "q3": None,
            "iqr": None,
            "multiplier": float(iqr_multiplier),
            "lower": None,
            "upper": None,
        }

    arr = np.asarray(lengths, dtype=np.float64)
    q1 = float(np.percentile(arr, 25))
    q3 = float(np.percentile(arr, 75))
    iqr = q3 - q1
    multiplier = float(iqr_multiplier)
    lower = q1 - multiplier * iqr
    upper = q3 + multiplier * iqr
    return {
        "enabled": True,
        "count": len(lengths),
        "q1": q1,
        "q3": q3,
        "iqr": float(iqr),
        "multiplier": multiplier,
        "lower": float(lower),
        "upper": float(upper),
        "min_length": int(np.min(arr)),
        "median_length": float(np.median(arr)),
        "max_length": int(np.max(arr)),
    }


def find_length_deletions(
    editor: Any,
    auto_iqr: bool = False,
    iqr_multiplier: float = 1.5,
) -> List[Dict[str, Any]]:
    iqr_bounds = compute_length_iqr_bounds(editor, iqr_multiplier) if auto_iqr else None
    rows = []
    for meta in editor.episodes_meta:
        episode_index = int(meta["episode_index"])
        length = episode_length(editor, episode_index, meta)
        reasons = []
        if iqr_bounds and iqr_bounds["lower"] is not None:
            lower = float(iqr_bounds["lower"])
            upper = float(iqr_bounds["upper"])
            if length < lower:
                reasons.append("length %d < IQR lower %.2f" % (length, lower))
            if length > upper:
                reasons.append("length %d > IQR upper %.2f" % (length, upper))
        if reasons:
            rows.append({
                "episode_index": episode_index,
                "length": length,
                "reason": "; ".join(reasons),
            })
    return rows


def _state_array(editor: Any, episode_index: int,
                 joint_indices: Optional[Sequence[int]]) -> Optional[np.ndarray]:
    if not hasattr(editor, "_get_state_array"):
        return None
    states = editor._get_state_array(episode_index)
    if states is None or len(states) == 0:
        return None
    states = np.asarray(states, dtype=np.float64)
    if states.ndim != 2 or states.shape[0] < 2 or states.shape[1] == 0:
        return None
    if joint_indices:
        valid = [int(i) for i in joint_indices if 0 <= int(i) < states.shape[1]]
        if not valid:
            return None
        states = states[:, valid]
    return states


def find_static_edge_trim(
    editor: Any,
    episode_index: int,
    motion_threshold: float,
    margin_frames: int = 0,
    min_static_frames: int = 1,
    joint_indices: Optional[Sequence[int]] = None,
    motion_metric: str = "max_abs",
) -> Dict[str, Any]:
    """检测单个 episode 开头/结尾应删除的静止帧。

    motion_threshold 是相邻两帧 observation.state 的变化阈值。默认 max_abs
    表示任一维度变化超过阈值即认为运动；norm 表示使用 L2 范数。
    """
    n = int(len(editor.episode_data.get(episode_index, [])))
    base = {
        "episode_index": int(episode_index),
        "length": n,
        "delete_frames": [],
        "trim_start": 0,
        "trim_end": 0,
        "keep_start": 0,
        "keep_end": max(0, n - 1),
        "reason": "",
        "status": "ok",
    }
    if n < 2:
        base["status"] = "skipped"
        base["reason"] = "episode too short"
        return base

    states = _state_array(editor, episode_index, joint_indices)
    if states is None:
        base["status"] = "skipped"
        base["reason"] = "observation.state unavailable"
        return base

    diffs = np.abs(np.diff(states, axis=0))
    if motion_metric == "norm":
        motion = np.linalg.norm(diffs, axis=1)
    else:
        motion = np.max(diffs, axis=1)
    moving = motion > float(motion_threshold)

    if not np.any(moving):
        base["status"] = "skipped"
        base["reason"] = "no moving segment found"
        base["max_motion"] = float(np.max(motion)) if len(motion) else 0.0
        return base

    moving_indices = np.flatnonzero(moving)
    first_move = int(moving_indices[0])
    last_move = int(moving_indices[-1])
    margin = max(0, int(margin_frames))
    min_static = max(1, int(min_static_frames))

    keep_start = max(0, first_move - margin)
    keep_end = min(n - 1, last_move + 1 + margin)

    start_delete = list(range(0, keep_start))
    end_delete = list(range(keep_end + 1, n))
    if len(start_delete) < min_static:
        start_delete = []
    if len(end_delete) < min_static:
        end_delete = []

    delete_frames = start_delete + end_delete
    base.update({
        "delete_frames": delete_frames,
        "trim_start": len(start_delete),
        "trim_end": len(end_delete),
        "keep_start": keep_start,
        "keep_end": keep_end,
        "first_move_transition": first_move,
        "last_move_transition": last_move,
        "max_motion": float(np.max(motion)) if len(motion) else 0.0,
        "mean_motion": float(np.mean(motion)) if len(motion) else 0.0,
    })
    if delete_frames:
        base["reason"] = "trim static edges"
    else:
        base["status"] = "skipped"
        base["reason"] = "static edge shorter than min_static_frames"
    return base


def find_static_edge_trims(
    editor: Any,
    excluded_episodes: Optional[Set[int]] = None,
    motion_threshold: float = 1e-4,
    margin_frames: int = 0,
    min_static_frames: int = 1,
    joint_indices: Optional[Sequence[int]] = None,
    motion_metric: str = "max_abs",
) -> List[Dict[str, Any]]:
    excluded = excluded_episodes or set()
    rows = []
    for meta in editor.episodes_meta:
        episode_index = int(meta["episode_index"])
        if episode_index in excluded:
            continue
        row = find_static_edge_trim(
            editor,
            episode_index,
            motion_threshold=motion_threshold,
            margin_frames=margin_frames,
            min_static_frames=min_static_frames,
            joint_indices=joint_indices,
            motion_metric=motion_metric,
        )
        if row.get("delete_frames"):
            rows.append(row)
    return rows


def _vector_column_array(editor: Any, episode_index: int,
                         column: str) -> Optional[np.ndarray]:
    df = editor.episode_data.get(episode_index)
    if df is None or column not in df.columns or len(df) == 0:
        return None
    values = [editor._to_list(v) if hasattr(editor, "_to_list") else v
              for v in df[column].tolist()]
    if not values or not all(isinstance(v, (list, tuple, np.ndarray)) and len(v) for v in values):
        return None
    try:
        arr = np.asarray(values, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if arr.ndim != 2 or arr.shape[0] == 0 or arr.shape[1] == 0:
        return None
    return arr


def _sample_indices(n_rows: int, max_points: int) -> np.ndarray:
    if n_rows <= max_points:
        return np.arange(n_rows, dtype=int)
    return np.unique(np.linspace(0, n_rows - 1, max_points).round().astype(int))


def build_episode_curve_preview(
    editor: Any,
    episode_index: int,
    trim_row: Optional[Dict[str, Any]] = None,
    reason: str = "",
    preferred_column: str = "action",
    max_points: int = 260,
    max_dims: int = 8,
) -> Dict[str, Any]:
    column = preferred_column
    arr = _vector_column_array(editor, episode_index, column)
    if arr is None:
        column = "observation.state"
        arr = _vector_column_array(editor, episode_index, column)
    if arr is None:
        return {
            "episode_index": int(episode_index),
            "source": None,
            "reason": reason,
            "error": "action/state unavailable",
        }

    n_rows, n_dims = arr.shape
    dim_count = min(max(1, int(max_dims)), n_dims)
    sample_idx = _sample_indices(n_rows, max(2, int(max_points)))
    sampled = arr[sample_idx, :dim_count]
    names = []
    try:
        fallback = "action_joint" if column == "action" else "state_joint"
        names = editor._resolve_feature_axis_names(column, n_dims, fallback)[:dim_count]
    except Exception:
        names = [f"{column}_{i}" for i in range(dim_count)]

    return {
        "episode_index": int(episode_index),
        "source": column,
        "reason": reason,
        "length": int(n_rows),
        "dim_count": int(dim_count),
        "total_dims": int(n_dims),
        "x": [int(i) for i in sample_idx.tolist()],
        "series": sampled.T.tolist(),
        "names": names,
        "trim_start": int(trim_row.get("trim_start", 0)) if trim_row else 0,
        "trim_end": int(trim_row.get("trim_end", 0)) if trim_row else 0,
        "keep_start": int(trim_row.get("keep_start", 0)) if trim_row else 0,
        "keep_end": int(trim_row.get("keep_end", n_rows - 1)) if trim_row else n_rows - 1,
    }


def build_curve_previews(
    editor: Any,
    length_deletions: Sequence[Dict[str, Any]],
    static_trims: Sequence[Dict[str, Any]],
    max_episodes: int = 80,
    max_points: int = 260,
    max_dims: int = 8,
) -> List[Dict[str, Any]]:
    trim_by_ep = {int(row["episode_index"]): row for row in static_trims}
    reasons = {}
    ordered_eps = []
    for row in length_deletions:
        ep = int(row["episode_index"])
        if ep not in reasons:
            ordered_eps.append(ep)
            reasons[ep] = "IQR length deletion"
    for row in static_trims:
        ep = int(row["episode_index"])
        if ep not in reasons:
            ordered_eps.append(ep)
            reasons[ep] = "static edge trim"

    previews = []
    for ep in ordered_eps[:max(0, int(max_episodes))]:
        previews.append(build_episode_curve_preview(
            editor,
            ep,
            trim_row=trim_by_ep.get(ep),
            reason=reasons.get(ep, ""),
            max_points=max_points,
            max_dims=max_dims,
        ))
    return previews


def build_batch_plan(
    editor: Any,
    auto_length_iqr: bool = False,
    iqr_multiplier: float = 1.5,
    trim_static_edges: bool = False,
    motion_threshold: float = 1e-4,
    margin_frames: int = 0,
    min_static_frames: int = 1,
    joint_indices: Optional[Sequence[int]] = None,
    motion_metric: str = "max_abs",
    include_curves: bool = False,
    max_curve_episodes: int = 80,
    max_curve_points: int = 260,
    max_curve_dims: int = 8,
) -> Dict[str, Any]:
    length_iqr = (
        compute_length_iqr_bounds(editor, iqr_multiplier)
        if auto_length_iqr else {"enabled": False}
    )
    length_deletions = find_length_deletions(
        editor,
        auto_iqr=auto_length_iqr,
        iqr_multiplier=iqr_multiplier,
    )
    deleted_eps = set(int(row["episode_index"]) for row in length_deletions)
    static_trims = []
    if trim_static_edges:
        static_trims = find_static_edge_trims(
            editor,
            excluded_episodes=deleted_eps,
            motion_threshold=motion_threshold,
            margin_frames=margin_frames,
            min_static_frames=min_static_frames,
            joint_indices=joint_indices,
            motion_metric=motion_metric,
        )

    total_episodes = len(editor.episodes_meta)
    total_frames = sum(len(df) for df in editor.episode_data.values())
    length_deleted_frames = sum(int(row["length"]) for row in length_deletions)
    trimmed_frames = sum(len(row["delete_frames"]) for row in static_trims)
    keep_episodes = total_episodes - len(length_deletions)
    keep_frames = total_frames - length_deleted_frames - trimmed_frames
    curve_previews = []
    if include_curves:
        curve_previews = build_curve_previews(
            editor,
            length_deletions,
            static_trims,
            max_episodes=max_curve_episodes,
            max_points=max_curve_points,
            max_dims=max_curve_dims,
        )

    return {
        "total_episodes": total_episodes,
        "total_frames": total_frames,
        "length_iqr": length_iqr,
        "length_deletions": length_deletions,
        "static_trims": static_trims,
        "delete_episode_count": len(length_deletions),
        "delete_episode_frames": length_deleted_frames,
        "trim_frame_count": trimmed_frames,
        "curve_previews": curve_previews,
        "keep_episodes": keep_episodes,
        "keep_frames": keep_frames,
    }


def apply_batch_plan(editor: Any, plan: Dict[str, Any]) -> Dict[str, Any]:
    trimmed_frames = 0
    for row in plan.get("static_trims", []):
        frames = [int(i) for i in row.get("delete_frames", [])]
        if not frames:
            continue
        remaining = editor.delete_frames(int(row["episode_index"]), frames)
        trimmed_frames += len(frames)
        row["remaining_frames"] = remaining

    delete_indices = [
        int(row["episode_index"])
        for row in plan.get("length_deletions", [])
    ]
    if delete_indices:
        editor.delete_episodes(delete_indices)

    return {
        "deleted_episodes": len(delete_indices),
        "trimmed_frames": trimmed_frames,
        "remaining_episodes": len(editor.episodes_meta),
        "remaining_frames": sum(len(df) for df in editor.episode_data.values()),
    }
