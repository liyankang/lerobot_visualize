import json
from pathlib import Path

import numpy as np

import lerobot_converter as lconv


class StatsVerifyService:
    def run_verify_stats(self, root_path: Path, *, stride: int, include_videos: bool,
                         tol: float, progress_cb) -> dict:
        """Recompute raw v2.1 stats and compare them with stored metadata."""
        root_path = Path(root_path)
        info = json.loads((root_path / "meta" / "info.json").read_text(encoding="utf-8"))
        if info.get("codebase_version") != lconv.V21:
            raise ValueError("验证功能目前仅支持 v2.1 源数据集")

        stored_stats: dict = {}
        stats_path = root_path / "meta" / "stats.json"
        if stats_path.exists():
            try:
                stored_stats = json.loads(stats_path.read_text(encoding="utf-8"))
            except Exception:  # pylint: disable=broad-except
                stored_stats = {}

        aggregated_stats = None
        eps_stats_path = root_path / "meta" / "episodes_stats.jsonl"
        if eps_stats_path.exists():
            try:
                progress_cb({
                    "stage": "aggregate",
                    "title": "聚合 episodes_stats.jsonl (参考对比)",
                    "detail": "",
                    "current": 0,
                    "total": 1,
                })
                rows = lconv._read_jsonl(eps_stats_path)  # pylint: disable=protected-access
                agg = lconv.aggregate_stats(
                    [lconv._cast_stats_to_numpy(r["stats"]) for r in rows]  # pylint: disable=protected-access
                )
                agg.pop("__warnings__", None)
                aggregated_stats = {
                    k: {m: lconv._to_python(v) for m, v in sub.items()}  # pylint: disable=protected-access
                    for k, sub in agg.items()
                }
                progress_cb({
                    "stage": "aggregate",
                    "title": "聚合 episodes_stats.jsonl (参考对比)",
                    "detail": f"{len(aggregated_stats)} 个 feature",
                    "current": 1,
                    "total": 1,
                })
            except Exception as exc:  # pylint: disable=broad-except
                aggregated_stats = {"__error__": str(exc)}

        raw = lconv.compute_raw_stats_v21(
            root_path,
            info,
            progress_cb,
            video_stride=stride,
            include_videos=include_videos,
        )
        raw_warnings = raw.pop("__warnings__", []) or []
        raw.pop("__source__", None)
        recomputed_stats = {
            k: {m: lconv._to_python(v) for m, v in sub.items()}  # pylint: disable=protected-access
            for k, sub in raw.items()
        }

        progress_cb({"stage": "diff", "title": "逐 feature 对比", "current": 0, "total": 1})
        diff_vs_stored = self._diff_two(recomputed_stats, stored_stats, tol) if stored_stats else None
        diff_vs_agg = (
            self._diff_two(recomputed_stats, aggregated_stats, tol)
            if aggregated_stats and "__error__" not in aggregated_stats else None
        )
        progress_cb({
            "stage": "diff",
            "title": "逐 feature 对比",
            "detail": "完成",
            "current": 1,
            "total": 1,
        })

        return {
            "success": True,
            "path": str(root_path),
            "tolerance": tol,
            "video_stride": stride,
            "include_video_stats": include_videos,
            "recomputed_keys": sorted(recomputed_stats.keys()),
            "stored_stats_keys": sorted(stored_stats.keys()),
            "aggregated_stats_keys": (
                sorted(aggregated_stats.keys())
                if aggregated_stats and "__error__" not in aggregated_stats else None
            ),
            "recompute_warnings": raw_warnings,
            "diff_recomputed_vs_stored": diff_vs_stored,
            "diff_recomputed_vs_aggregated": diff_vs_agg,
            "overall_recomputed_vs_stored": self._overall_match(diff_vs_stored),
            "overall_recomputed_vs_aggregated": self._overall_match(diff_vs_agg),
            "stats": {
                "recomputed": recomputed_stats,
                "stored": stored_stats,
                "aggregated": (
                    aggregated_stats
                    if aggregated_stats and "__error__" not in aggregated_stats else None
                ),
            },
        }

    @staticmethod
    def _diff_two(a: dict, b: dict, tol: float):
        out = {}
        keys = sorted(set(a.keys()) | set(b.keys()))
        for key in keys:
            av = a.get(key)
            bv = b.get(key)
            if av is None or bv is None:
                out[key] = {"__missing_in__": ("a" if av is None else "b")}
                continue
            feat = {}
            for metric in ("mean", "std", "min", "max", "count"):
                if metric not in av or metric not in bv:
                    feat[metric] = {"__missing__": True}
                    continue
                try:
                    arr_a = np.asarray(av[metric], dtype=float)
                    arr_b = np.asarray(bv[metric], dtype=float)
                except Exception:  # pylint: disable=broad-except
                    feat[metric] = {"__error__": "cast-failed"}
                    continue
                if arr_a.shape != arr_b.shape:
                    feat[metric] = {
                        "__shape_mismatch__": True,
                        "shape_a": list(arr_a.shape),
                        "shape_b": list(arr_b.shape),
                    }
                    continue
                diff = np.abs(arr_a - arr_b)
                denom = np.maximum(np.abs(arr_a), 1e-12)
                rel = (diff / denom) if arr_a.size > 0 else np.zeros_like(diff)
                feat[metric] = {
                    "max_abs": float(diff.max()) if diff.size else 0.0,
                    "max_rel": float(rel.max()) if rel.size else 0.0,
                    "match": bool(diff.max() <= tol) if diff.size else True,
                    "shape": list(arr_a.shape),
                }
            out[key] = feat
        return out

    @staticmethod
    def _overall_match(diff_map):
        if not diff_map:
            return None
        mismatches = []
        for key, metrics in diff_map.items():
            if "__missing_in__" in metrics:
                mismatches.append(f"{key} (missing)")
                continue
            for metric, detail in metrics.items():
                if isinstance(detail, dict) and detail.get("match") is False:
                    mismatches.append(f"{key}.{metric} (max_abs={detail.get('max_abs'):.6g})")
                elif isinstance(detail, dict) and (
                    "__shape_mismatch__" in detail or "__missing__" in detail
                ):
                    mismatches.append(f"{key}.{metric} (shape/missing)")
        return {"all_match": len(mismatches) == 0, "mismatches": mismatches[:200]}
