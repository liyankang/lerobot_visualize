from __future__ import annotations
import json
import logging
import shutil
from pathlib import Path

import numpy as np
import pandas as pd


class TrainingCheckService:
    def __init__(self, dataset_editor_cls, joint_config_getter, logger=None):
        self.DatasetEditor = dataset_editor_cls
        self._joint_config_getter = joint_config_getter
        self.log = logger or logging.getLogger(__name__)

    def _joint_config(self):
        getter = self._joint_config_getter
        return getter() if callable(getter) else None

    @staticmethod
    def _training_check_item(level, check_id, title, detail, **extra):
        item = {
            "level": level,
            "id": check_id,
            "title": title,
            "detail": detail,
        }
        item.update(extra)
        return item

    @staticmethod
    def _json_type_name(value):
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        if isinstance(value, str):
            return "string"
        if isinstance(value, list):
            return "list"
        if isinstance(value, dict):
            return "dict"
        return type(value).__name__

    def _numeric_vector_matrix(self, df, column):
        if column not in df.columns:
            return None, "missing"
        rows = []
        for value in df[column].tolist():
            row = self.DatasetEditor._to_list(value)
            if not row:
                return None, "non_numeric_or_empty"
            rows.append(row)
        if not rows:
            return np.empty((0, 0), dtype=np.float64), None
        widths = {len(row) for row in rows}
        if len(widths) != 1:
            return None, f"inconsistent_widths:{sorted(widths)}"
        try:
            return np.asarray(rows, dtype=np.float64), None
        except Exception as exc:  # pylint: disable=broad-except
            return None, f"cast_failed:{exc}"

    @staticmethod
    def _write_jsonl(path, rows):
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def inspect_dataset(self, root_path: Path) -> dict:
        root_path = Path(root_path)
        info_path = root_path / "meta" / "info.json"
        if not info_path.exists():
            raise ValueError("无效的 LeRobot 数据集: 缺少 meta/info.json")
        info = json.loads(info_path.read_text(encoding="utf-8"))
        parquets = sorted((root_path / "data").rglob("*.parquet")) if (root_path / "data").exists() else []
        video_files = sorted((root_path / "videos").rglob("*.mp4")) if (root_path / "videos").exists() else []
        return {
            "success": True,
            "path": str(root_path),
            "summary": {
                "codebase_version": info.get("codebase_version", "unknown"),
                "fps": info.get("fps"),
                "robot_type": info.get("robot_type"),
                "total_episodes": info.get("total_episodes"),
                "total_frames": info.get("total_frames"),
                "feature_count": len(info.get("features", {}) or {}),
            },
            "files": {
                "episode_parquets": len(parquets),
                "videos": len(video_files),
                "has_episodes_jsonl": (root_path / "meta" / "episodes.jsonl").exists(),
                "has_tasks_jsonl": (root_path / "meta" / "tasks.jsonl").exists(),
                "has_stats_json": (root_path / "meta" / "stats.json").exists(),
                "has_episodes_stats_jsonl": (root_path / "meta" / "episodes_stats.jsonl").exists(),
            },
            "features": sorted((info.get("features", {}) or {}).keys()),
        }

    def _finalize_training_report(self, root_path: Path, profile: str, checks: list, details: dict) -> dict:
        counts = {
            "error": sum(1 for item in checks if item.get("level") == "error"),
            "warn": sum(1 for item in checks if item.get("level") == "warn"),
            "pass": sum(1 for item in checks if item.get("level") == "pass"),
            "info": sum(1 for item in checks if item.get("level") == "info"),
        }
        status = "error" if counts["error"] else ("warn" if counts["warn"] else "pass")
        return {
            "success": True,
            "path": str(root_path),
            "profile": profile,
            "status": status,
            "summary": counts,
            "checks": checks,
            "details": details,
        }

    def run_training_usability_check(self, root_path: Path, *, profile="general",
                                     include_videos=False, max_issue_examples=5) -> dict:
        root_path = Path(root_path)
        checks = []
        max_issue_examples = max(1, int(max_issue_examples or 5))

        def add(level, check_id, title, detail, **extra):
            checks.append(self._training_check_item(level, check_id, title, detail, **extra))

        meta_dir = root_path / "meta"
        info_path = meta_dir / "info.json"
        if not info_path.exists():
            add("error", "structure.info", "缺少 meta/info.json", "这不是有效的 LeRobot 数据集根目录。")
            return self._finalize_training_report(root_path, profile, checks, {})

        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            add("pass", "structure.info", "info.json 可读取", f"codebase_version={info.get('codebase_version', 'unknown')}")
        except Exception as exc:  # pylint: disable=broad-except
            add("error", "structure.info_parse", "info.json 解析失败", str(exc))
            return self._finalize_training_report(root_path, profile, checks, {})

        required_meta = ["episodes.jsonl", "tasks.jsonl", "stats.json", "episodes_stats.jsonl"]
        for name in required_meta:
            path = meta_dir / name
            add("pass" if path.exists() else "error", f"structure.{name}", f"{name} {'存在' if path.exists() else '缺失'}", str(path))

        version = info.get("codebase_version", "unknown")
        add("pass" if version == "v2.1" else "warn", "structure.version", "LeRobot 版本", f"当前 codebase_version={version}。本检查按 v2.1 训练数据约定执行。")

        try:
            editor = self.DatasetEditor(str(root_path), joint_config=self._joint_config())
        except Exception as exc:  # pylint: disable=broad-except
            add("error", "load.dataset", "数据集加载失败", str(exc))
            return self._finalize_training_report(root_path, profile, checks, {"codebase_version": version})

        summary = editor.get_summary()
        features = info.get("features", {}) or {}
        numeric_features = [
            key for key, meta in features.items()
            if isinstance(meta, dict) and meta.get("dtype") not in ("image", "video")
        ]

        info_episode_total = info.get("total_episodes")
        actual_episode_total = len(editor.episode_data)
        add("pass" if info_episode_total == actual_episode_total else "error",
            "structure.total_episodes", "episode 数量一致性",
            f"info.total_episodes={info_episode_total}, 实际 parquet episodes={actual_episode_total}")

        actual_frames = sum(len(df) for df in editor.episode_data.values())
        info_frames = info.get("total_frames")
        add("pass" if info_frames == actual_frames else "error",
            "structure.total_frames", "frame 总数一致性",
            f"info.total_frames={info_frames}, 实际 parquet frames={actual_frames}")

        episode_indices = sorted(editor.episode_data.keys())
        expected_indices = list(range(len(episode_indices)))
        add("pass" if episode_indices == expected_indices else "error",
            "structure.episode_index_contiguous", "episode_index 连续性",
            "episode_index 从 0 连续递增。" if episode_indices == expected_indices else f"实际索引样例: {episode_indices[:20]}")

        for required_col in ("observation.state", "action"):
            declared = required_col in features
            present_count = sum(1 for df in editor.episode_data.values() if required_col in df.columns)
            add("pass" if declared and present_count == actual_episode_total else "error",
                f"feature.{required_col}.presence", f"{required_col} 字段存在性",
                f"info 声明={declared}, parquet 含字段 episode={present_count}/{actual_episode_total}")

        frame_bad = []
        timestamp_bad = []
        index_bad = []
        fps = float(info.get("fps", 30) or 30)
        expected_dt = 1.0 / max(fps, 1e-9)
        global_indices = []
        for ep_idx, df in sorted(editor.episode_data.items()):
            n = len(df)
            if "frame_index" not in df.columns:
                frame_bad.append(f"ep{ep_idx}: missing")
            else:
                vals = pd.to_numeric(df["frame_index"], errors="coerce").to_numpy()
                if vals.size != n or np.any(~np.isfinite(vals)) or not np.array_equal(vals.astype(int), np.arange(n)):
                    frame_bad.append(f"ep{ep_idx}: not 0..{n - 1}")

            if "timestamp" not in df.columns:
                timestamp_bad.append(f"ep{ep_idx}: missing")
            else:
                ts = pd.to_numeric(df["timestamp"], errors="coerce").to_numpy(dtype=np.float64)
                if ts.size != n or np.any(~np.isfinite(ts)):
                    timestamp_bad.append(f"ep{ep_idx}: non-numeric")
                elif n >= 2:
                    diffs = np.diff(ts)
                    if np.any(diffs <= 0):
                        timestamp_bad.append(f"ep{ep_idx}: non-monotonic")
                    elif expected_dt > 0:
                        drift = abs(float(np.mean(diffs)) - expected_dt) / expected_dt
                        if drift > 0.05:
                            timestamp_bad.append(f"ep{ep_idx}: dt drift {drift * 100:.2f}%")

            if "index" in df.columns:
                idx_vals = pd.to_numeric(df["index"], errors="coerce").to_numpy()
                global_indices.extend([int(v) for v in idx_vals if np.isfinite(v)])
            else:
                index_bad.append(f"ep{ep_idx}: missing")

        add("pass" if not frame_bad else "error", "temporal.frame_index", "frame_index 连续性",
            "所有 episode 均为 0..N-1。" if not frame_bad else "; ".join(frame_bad[:max_issue_examples]))
        add("pass" if not timestamp_bad else "error", "temporal.timestamp", "timestamp 单调与 FPS",
            f"timestamp 单调，平均 dt 接近 1/fps={expected_dt:.6f}s。" if not timestamp_bad else "; ".join(timestamp_bad[:max_issue_examples]))
        expected_global = list(range(actual_frames))
        add("pass" if not index_bad and global_indices == expected_global else "warn",
            "temporal.global_index", "全局 index 连续性",
            "index 全局连续。" if not index_bad and global_indices == expected_global else "index 缺失或不连续，部分 dataloader/工具可能依赖它。")

        dtype_details = []
        for feature_key in ("observation.state", "action"):
            expected_dim = editor._infer_feature_dim(feature_key)
            bad_examples = []
            nan_examples = []
            observed_shapes = set()
            for ep_idx, df in sorted(editor.episode_data.items()):
                matrix, err = self._numeric_vector_matrix(df, feature_key)
                if err:
                    bad_examples.append(f"ep{ep_idx}: {err}")
                    continue
                observed_shapes.add(tuple(matrix.shape[1:]))
                if matrix.size and np.any(~np.isfinite(matrix)):
                    nan_examples.append(f"ep{ep_idx}: NaN/Inf")
            shape_ok = observed_shapes == {(int(expected_dim),)} if expected_dim else len(observed_shapes) == 1
            add("pass" if not bad_examples and shape_ok else "error",
                f"dtype.{feature_key}", f"{feature_key} 数值类型与 shape",
                (f"expected_dim={expected_dim}, observed_shapes={sorted(map(list, observed_shapes))}"
                 if not bad_examples else "; ".join(bad_examples[:max_issue_examples])))
            add("pass" if not nan_examples else "error", f"dtype.{feature_key}.finite", f"{feature_key} NaN/Inf",
                "未发现 NaN/Inf。" if not nan_examples else "; ".join(nan_examples[:max_issue_examples]))
            dtype_details.append({
                "feature": feature_key,
                "expected_dim": expected_dim,
                "observed_shapes": sorted([list(shape) for shape in observed_shapes]),
            })

        task_index_to_task = {}
        task_errors = []
        for row_idx, task in enumerate(editor.tasks):
            if not isinstance(task, dict):
                task_errors.append(f"tasks.jsonl line {row_idx}: type={self._json_type_name(task)}")
                continue
            task_index = task.get("task_index", row_idx)
            task_text = task.get("task")
            normalized_task_index = None
            if not isinstance(task_index, int):
                task_errors.append(f"task_index={task_index} type={self._json_type_name(task_index)}")
            try:
                normalized_task_index = int(task_index)
            except (TypeError, ValueError):
                task_errors.append(f"task_index={task_index} cannot cast to int")
            if not isinstance(task_text, str) or not task_text.strip():
                task_errors.append(f"task_index={task_index}: task type={self._json_type_name(task_text)}")
            elif normalized_task_index is not None:
                task_index_to_task[normalized_task_index] = task_text
        add("pass" if not task_errors else "error", "task.tasks_jsonl", "tasks.jsonl task 类型",
            f"{len(task_index_to_task)} 条 task 文本可用。" if not task_errors else "; ".join(task_errors[:max_issue_examples]),
            fixable=bool(task_errors))

        missing_task_refs = []
        parquet_task_type_bad = []
        for ep_idx, df in sorted(editor.episode_data.items()):
            em = next((item for item in editor.episodes_meta if item.get("episode_index") == ep_idx), {})
            ep_task_index = em.get("task_index")
            if ep_task_index is not None:
                try:
                    normalized_ep_task_index = int(ep_task_index)
                except (TypeError, ValueError):
                    missing_task_refs.append(f"episodes.jsonl ep{ep_idx}: task_index type={self._json_type_name(ep_task_index)}")
                else:
                    if normalized_ep_task_index not in task_index_to_task:
                        missing_task_refs.append(f"episodes.jsonl ep{ep_idx}: task_index={ep_task_index}")
            if "task_index" in df.columns:
                for value in pd.unique(df["task_index"].dropna()):
                    try:
                        idx = int(value)
                    except (TypeError, ValueError):
                        missing_task_refs.append(f"parquet ep{ep_idx}: task_index type={self._json_type_name(value)}")
                        continue
                    if idx not in task_index_to_task:
                        missing_task_refs.append(f"parquet ep{ep_idx}: task_index={idx}")
            if "task" in df.columns and len(df):
                sample_values = df["task"].dropna().head(10).tolist()
                bad_values = [value for value in sample_values if not isinstance(value, str)]
                if bad_values:
                    parquet_task_type_bad.append(f"ep{ep_idx}: task column sample type={self._json_type_name(bad_values[0])}")

        language_profile = str(profile).lower() in ("pi05", "pi0", "pi0.5", "openvla", "vla")
        add("pass" if not missing_task_refs else "error", "task.task_index_refs", "task_index 引用完整性",
            "episode/parquet task_index 均能映射到 tasks.jsonl。" if not missing_task_refs else "; ".join(missing_task_refs[:max_issue_examples]))
        add("pass" if not parquet_task_type_bad else ("error" if language_profile else "warn"), "task.parquet_task_type",
            "parquet task 列类型",
            "未发现非法 parquet task 列。" if not parquet_task_type_bad else "; ".join(parquet_task_type_bad[:max_issue_examples]),
            fixable=bool(parquet_task_type_bad))

        stats_path = meta_dir / "stats.json"
        stats = {}
        if stats_path.exists():
            try:
                stats = json.loads(stats_path.read_text(encoding="utf-8"))
            except Exception as exc:  # pylint: disable=broad-except
                add("error", "stats.parse", "stats.json 解析失败", str(exc))

        required_stats = ("mean", "std", "min", "max", "count", "q01", "q10", "q50", "q90", "q99")
        for feature_key in numeric_features:
            missing = [metric for metric in required_stats if metric not in (stats.get(feature_key) or {})]
            level = "pass" if not missing else ("error" if feature_key in ("observation.state", "action") else "warn")
            add(level, f"stats.{feature_key}", f"{feature_key} stats 完整性",
                "mean/std/min/max/count/q01/q10/q50/q90/q99 均存在。" if not missing else f"缺少: {', '.join(missing)}",
                fixable=bool(missing))
            expected_dim = editor._infer_feature_dim(feature_key)
            type_errors = []
            feature_stats = stats.get(feature_key) or {}
            for metric in required_stats:
                if metric not in feature_stats:
                    continue
                try:
                    arr = np.asarray(feature_stats[metric], dtype=np.float64)
                except Exception as exc:  # pylint: disable=broad-except
                    type_errors.append(f"{metric}: cast_failed({exc})")
                    continue
                if arr.size and np.any(~np.isfinite(arr)):
                    type_errors.append(f"{metric}: NaN/Inf")
                if expected_dim and metric != "count":
                    flat_size = int(arr.reshape(-1).size)
                    if flat_size not in (1, int(expected_dim)):
                        type_errors.append(f"{metric}: shape={list(arr.shape)}, expected_dim={expected_dim}")
            add("pass" if not type_errors else ("error" if feature_key in ("observation.state", "action") else "warn"),
                f"stats.{feature_key}.dtype", f"{feature_key} stats 数据类型",
                "stats 数值可解析，shape 与 feature dim 兼容。" if not type_errors else "; ".join(type_errors[:max_issue_examples]),
                fixable=bool(type_errors))

        eps_stats_path = meta_dir / "episodes_stats.jsonl"
        eps_stats_rows = self.DatasetEditor._read_jsonl(eps_stats_path) if eps_stats_path.exists() else []
        add("pass" if len(eps_stats_rows) == actual_episode_total else "warn",
            "stats.episodes_stats_count", "episodes_stats.jsonl 行数",
            f"episodes_stats rows={len(eps_stats_rows)}, episodes={actual_episode_total}",
            fixable=len(eps_stats_rows) != actual_episode_total)

        tasks_parquet_path = meta_dir / "tasks.parquet"
        if tasks_parquet_path.exists():
            try:
                tasks_df = pd.read_parquet(tasks_parquet_path)
                index_values = [str(value) for value in tasks_df.index.tolist()]
                default_range_index = index_values == [str(i) for i in range(len(tasks_df))]
                has_task_column = "task" in tasks_df.columns
                if default_range_index and has_task_column:
                    add("error" if language_profile else "warn", "task.tasks_parquet_index", "tasks.parquet task 索引",
                        "tasks.parquet 把 task 存成普通列且 index 是整数；LeRobotDataset 使用 tasks.iloc[task_idx].name 时会取到整数。",
                        fixable=True)
                elif default_range_index:
                    add("error", "task.tasks_parquet_index", "tasks.parquet task 索引",
                        "tasks.parquet index 是整数且没有 task 列，无法可靠恢复任务文本。", fixable=False)
                else:
                    add("pass", "task.tasks_parquet_index", "tasks.parquet task 索引",
                        "task 字符串位于 DataFrame index，兼容 tasks.iloc[task_idx].name。")
            except Exception as exc:  # pylint: disable=broad-except
                add("error", "task.tasks_parquet_read", "tasks.parquet 读取失败", str(exc))

        action_matrix, action_names, _ = editor._collect_feature_series("action", "action_joint")
        if action_matrix.size:
            std = np.std(action_matrix, axis=0)
            near_static = [action_names[i] for i, value in enumerate(std) if float(value) < 1e-9]
            add("pass" if not near_static else "warn", "semantic.action_static", "action 静止维度",
                "action 各维均有变化。" if not near_static else f"近似不动维度: {', '.join(near_static[:max_issue_examples])}")
            max_delta = 0.0
            for _ep_idx, df in sorted(editor.episode_data.items()):
                matrix, err = self._numeric_vector_matrix(df, "action")
                if err or matrix.shape[0] < 2:
                    continue
                local = np.linalg.norm(np.diff(matrix, axis=0), axis=1)
                if local.size:
                    max_delta = max(max_delta, float(np.max(local)))
            add("pass" if max_delta <= 10 else "warn", "semantic.action_jump", "action 帧间欧氏跳变",
                f"最大帧间动作距离={max_delta:.6g}，阈值参考=10。")

        if include_videos:
            video_keys = [
                key for key, meta in features.items()
                if isinstance(meta, dict) and meta.get("dtype") in ("image", "video")
            ]
            missing_videos = []
            for ep_idx in sorted(editor.episode_data.keys()):
                videos = editor._orig_video_files.get(ep_idx, {})
                for key in video_keys:
                    cam = key.split(".")[-1]
                    has_match = key in videos or cam in videos
                    if not has_match:
                        missing_videos.append(f"ep{ep_idx}: {key}")
            add("pass" if not missing_videos else "warn", "video.files", "视频文件存在性",
                f"检查了 {len(video_keys)} 个 video/image feature。" if not missing_videos else "; ".join(missing_videos[:max_issue_examples]))

        details = {
            "summary": summary,
            "profile": profile,
            "dtype": dtype_details,
            "numeric_features": numeric_features,
        }
        return self._finalize_training_report(root_path, profile, checks, details)

    def _load_task_rows_for_fix(self, root_path: Path) -> list[dict]:
        tasks_jsonl = root_path / "meta" / "tasks.jsonl"
        rows = []
        if tasks_jsonl.exists():
            for row_idx, row in enumerate(self.DatasetEditor._read_jsonl(tasks_jsonl)):
                if not isinstance(row, dict):
                    continue
                task = row.get("task")
                if not isinstance(task, str) or not task.strip():
                    continue
                raw_idx = row.get("task_index", row_idx)
                try:
                    task_index = int(raw_idx)
                except (TypeError, ValueError):
                    continue
                rows.append({"task_index": task_index, "task": task.strip()})
        if rows:
            rows.sort(key=lambda item: item["task_index"])
            return rows

        tasks_parquet = root_path / "meta" / "tasks.parquet"
        if tasks_parquet.exists():
            df = pd.read_parquet(tasks_parquet)
            if "task_index" in df.columns:
                idx_col = pd.to_numeric(df["task_index"], errors="coerce").tolist()
            else:
                idx_col = list(range(len(df)))
            index_values = [str(value) for value in df.index.tolist()]
            default_range_index = index_values == [str(i) for i in range(len(df))]
            if not default_range_index:
                task_col = index_values
            elif "task" in df.columns:
                task_col = df["task"].astype(str).tolist()
            else:
                task_col = []
            for raw_idx, task in zip(idx_col, task_col):
                if pd.isna(raw_idx) or not str(task).strip():
                    continue
                rows.append({"task_index": int(raw_idx), "task": str(task).strip()})
        rows.sort(key=lambda item: item["task_index"])
        return rows

    def _rewrite_tasks_files_for_training(self, root_path: Path) -> list[str]:
        actions = []
        rows = self._load_task_rows_for_fix(root_path)
        if not rows:
            return actions
        meta_dir = root_path / "meta"
        self._write_jsonl(meta_dir / "tasks.jsonl", rows)
        actions.append(f"重写 tasks.jsonl ({len(rows)} tasks)")
        tasks_df = (
            pd.DataFrame(rows)[["task_index", "task"]]
            .sort_values("task_index")
            .reset_index(drop=True)
            .set_index("task", drop=True)
        )
        tasks_df.to_parquet(meta_dir / "tasks.parquet", index=True)
        actions.append("重建 tasks.parquet: task 字符串写入 DataFrame index")
        return actions

    def _episode_meta_lookup(self, root_path: Path) -> dict:
        lookup = {}
        path = root_path / "meta" / "episodes.jsonl"
        if not path.exists():
            return lookup
        for row in self.DatasetEditor._read_jsonl(path):
            if not isinstance(row, dict):
                continue
            try:
                lookup[int(row.get("episode_index"))] = dict(row)
            except (TypeError, ValueError):
                continue
        return lookup

    def _rewrite_episode_parquet_format(self, root_path: Path) -> list[str]:
        actions = []
        info = json.loads((root_path / "meta" / "info.json").read_text(encoding="utf-8"))
        fps = float(info.get("fps", 30) or 30)
        episode_meta = self._episode_meta_lookup(root_path)
        parquets = sorted((root_path / "data").rglob("*.parquet")) if (root_path / "data").exists() else []
        global_index = 0
        updated = 0
        dropped_task_col = 0

        for pq_path in parquets:
            ep_idx = self.DatasetEditor._parse_episode_index(pq_path.stem)
            if ep_idx is None:
                continue
            df = pd.read_parquet(pq_path)
            n = len(df)
            changed = False
            if "task" in df.columns and "task_index" in df.columns:
                df = df.drop(columns=["task"])
                dropped_task_col += 1
                changed = True
            expected_episode = np.full(n, int(ep_idx), dtype=np.int64)
            if "episode_index" not in df.columns or not np.array_equal(
                pd.to_numeric(df["episode_index"], errors="coerce").fillna(-1).to_numpy(dtype=np.int64),
                expected_episode,
            ):
                df["episode_index"] = expected_episode
                changed = True
            expected_frame = np.arange(n, dtype=np.int64)
            if "frame_index" not in df.columns or not np.array_equal(
                pd.to_numeric(df["frame_index"], errors="coerce").fillna(-1).to_numpy(dtype=np.int64),
                expected_frame,
            ):
                df["frame_index"] = expected_frame
                changed = True
            expected_index = np.arange(global_index, global_index + n, dtype=np.int64)
            if "index" not in df.columns or not np.array_equal(
                pd.to_numeric(df["index"], errors="coerce").fillna(-1).to_numpy(dtype=np.int64),
                expected_index,
            ):
                df["index"] = expected_index
                changed = True
            expected_ts = np.arange(n, dtype=np.float64) / max(fps, 1e-9)
            ts_needs_rewrite = True
            if "timestamp" in df.columns:
                ts = pd.to_numeric(df["timestamp"], errors="coerce").to_numpy(dtype=np.float64)
                ts_needs_rewrite = (
                    ts.size != n
                    or np.any(~np.isfinite(ts))
                    or (n >= 2 and np.any(np.diff(ts) <= 0))
                )
            if ts_needs_rewrite:
                df["timestamp"] = expected_ts
                changed = True
            meta_task_index = episode_meta.get(ep_idx, {}).get("task_index")
            if "task_index" not in df.columns and meta_task_index is not None:
                try:
                    df["task_index"] = int(meta_task_index)
                    changed = True
                except (TypeError, ValueError):
                    pass
            elif "task_index" in df.columns:
                numeric_task = pd.to_numeric(df["task_index"], errors="coerce")
                if numeric_task.isna().any() and meta_task_index is not None:
                    try:
                        df["task_index"] = int(meta_task_index)
                        changed = True
                    except (TypeError, ValueError):
                        pass
                else:
                    casted = numeric_task.astype("int64")
                    if not np.array_equal(casted.to_numpy(), df["task_index"].to_numpy()):
                        df["task_index"] = casted
                        changed = True
            if changed:
                df.to_parquet(pq_path, index=False)
                updated += 1
            global_index += n

        if updated:
            actions.append(f"重写 episode parquet 格式字段 ({updated} files)")
        if dropped_task_col:
            actions.append(f"删除 parquet 非标准 task 列 ({dropped_task_col} files)")
        return actions

    def _rewrite_episode_metadata_for_training(self, root_path: Path) -> list[str]:
        actions = []
        meta_dir = root_path / "meta"
        info_path = meta_dir / "info.json"
        if not info_path.exists():
            return actions
        info = json.loads(info_path.read_text(encoding="utf-8"))
        episode_meta = self._episode_meta_lookup(root_path)
        parquets = sorted((root_path / "data").rglob("*.parquet")) if (root_path / "data").exists() else []

        episodes = []
        total_frames = 0
        for pq_path in parquets:
            ep_idx = self.DatasetEditor._parse_episode_index(pq_path.stem)
            if ep_idx is None:
                continue
            df = pd.read_parquet(pq_path)
            old = episode_meta.get(ep_idx, {})
            row = dict(old)
            row["episode_index"] = int(ep_idx)
            row["length"] = int(len(df))
            if "task_index" in df.columns and len(df):
                try:
                    row["task_index"] = int(pd.to_numeric(df["task_index"], errors="coerce").dropna().iloc[0])
                except Exception:  # pylint: disable=broad-except
                    pass
            episodes.append(row)
            total_frames += len(df)

        episodes.sort(key=lambda row: int(row["episode_index"]))
        with open(meta_dir / "episodes.jsonl", "w", encoding="utf-8") as f:
            for row in episodes:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        actions.append(f"重写 episodes.jsonl ({len(episodes)} episodes)")

        info["total_episodes"] = len(episodes)
        info["total_frames"] = int(total_frames)
        info["data_path"] = "data/chunk-{chunk_index:03d}/episode_{episode_index:06d}.parquet"
        info["video_path"] = "videos/chunk-{chunk_index:03d}/{video_key}/episode_{episode_index:06d}.mp4"
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2, ensure_ascii=False)
        actions.append("更新 info.json total_episodes/total_frames/path 模板")
        return actions

    def _recompute_numeric_stats_for_training(self, root_path: Path) -> list[str]:
        editor = self.DatasetEditor(str(root_path), joint_config=self._joint_config())
        global_stats, episode_stats = editor.compute_stats(skip_video_stats=True)
        meta_dir = root_path / "meta"
        with open(meta_dir / "stats.json", "w", encoding="utf-8") as f:
            json.dump(global_stats, f, indent=2, ensure_ascii=False)
        with open(meta_dir / "episodes_stats.jsonl", "w", encoding="utf-8") as f:
            for row in episode_stats:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return ["重算数值 stats.json / episodes_stats.jsonl (跳过视频统计)"]

    def fix_training_dataset_format(self, src_path: Path, dst_path: Path, *, overwrite=False,
                                    profile="general") -> dict:
        src_path = Path(src_path).resolve()
        dst_path = Path(dst_path).resolve()
        if src_path == dst_path:
            raise ValueError("修复输出路径不能与原数据集相同")
        if dst_path.exists():
            if not overwrite:
                raise ValueError("输出路径已存在。如需覆盖，请勾选允许覆盖输出目录")
            shutil.rmtree(dst_path)
        shutil.copytree(src_path, dst_path)

        actions = []
        info_path = dst_path / "meta" / "info.json"
        info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {}
        version = info.get("codebase_version", "unknown")
        actions.extend(self._rewrite_tasks_files_for_training(dst_path))
        if version == "v2.1":
            actions.extend(self._rewrite_episode_parquet_format(dst_path))
            actions.extend(self._rewrite_episode_metadata_for_training(dst_path))
            actions.extend(self._recompute_numeric_stats_for_training(dst_path))
        elif version == "v3.0":
            actions.append("v3.0 数据集仅修复 tasks.parquet 索引格式，未改写合并数据文件")
        else:
            actions.append(f"未知版本 {version}，仅尝试修复 task 元数据")
        report = self.run_training_usability_check(dst_path, profile=profile, include_videos=False, max_issue_examples=5)
        return {
            "success": True,
            "source_path": str(src_path),
            "output_path": str(dst_path),
            "actions": actions,
            "report": report,
        }
