#!/usr/bin/env python3
"""
验证脚本：对比 DatasetEditor 保存的 stats 与 lerobot 官方 API 重算的 stats。

用法 (需要在 lerobot conda 环境下运行):
    python verify_stats.py <dataset_path> [--output /tmp/verify_out]
"""

import json
import sys
import tempfile
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

DATASET_PATH = sys.argv[1] if len(sys.argv) > 1 else (
    "/home/yjh/temp/all/lerobot_v21/office_left_by_motionplanning_copy/"
    "cr100_open_door_by_motionplanning"
)

OUTPUT_PATH = sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="verify_stats_")

# ═══════════════ Step 1: 用 DatasetEditor 保存 ═══════════════

sys.path.insert(0, str(Path(__file__).parent))
import types

class _FakeFlaskApp:
    def route(self, *a, **kw):
        return lambda fn: fn

flask_mock = types.ModuleType("flask")
flask_mock.Flask = lambda *a, **kw: _FakeFlaskApp()
flask_mock.render_template = None
flask_mock.request = None
flask_mock.jsonify = None
flask_mock.send_file = None
flask_mock.abort = None
sys.modules["flask"] = flask_mock
from app import DatasetEditor

print(f"[1/4] 加载数据集: {DATASET_PATH}")
editor = DatasetEditor(DATASET_PATH)
summary = editor.get_summary()
print(f"       {summary['total_episodes']} episodes, {summary['total_frames']} frames")

print(f"[2/4] 保存到: {OUTPUT_PATH}")
editor.save_as(OUTPUT_PATH)

# 读取我们保存的 stats
our_ep_stats = []
with open(Path(OUTPUT_PATH) / "meta" / "episodes_stats.jsonl") as f:
    for line in f:
        our_ep_stats.append(json.loads(line))

with open(Path(OUTPUT_PATH) / "meta" / "stats.json") as f:
    our_global_stats = json.load(f)

# ═══════════════ Step 2: 用 lerobot 重算 ═══════════════

print("[3/4] 用 lerobot API 重新计算 stats...")

from lerobot.datasets.compute_stats import compute_episode_stats as lr_compute_episode_stats
from lerobot.datasets.compute_stats import aggregate_stats as lr_aggregate_stats

info_path = Path(OUTPUT_PATH) / "meta" / "info.json"
with open(info_path) as f:
    info = json.load(f)
features = info["features"]

data_dir = Path(OUTPUT_PATH) / "data"
parquet_files = sorted(data_dir.rglob("*.parquet"))

lr_all_ep_stats = []

for pq_file in parquet_files:
    stem = pq_file.stem
    if not stem.startswith("episode_"):
        continue
    ep_idx = int(stem.split("_", 1)[1])
    df = pd.read_parquet(pq_file)

    episode_data = {}
    for col in df.columns:
        if col not in features:
            continue
        feat_info = features[col]
        if feat_info["dtype"] in ("video", "image"):
            continue
        vals = df[col].values
        if hasattr(vals[0], 'shape') and vals[0].shape != ():
            arr = np.stack(vals)
        else:
            arr = np.array([float(v) for v in vals], dtype=np.float64)
            if arr.ndim == 1:
                arr = arr.reshape(-1, 1)
        episode_data[col] = arr.astype(np.float64)

    lr_ep_stats = lr_compute_episode_stats(episode_data, features)
    lr_all_ep_stats.append((ep_idx, lr_ep_stats))

lr_all_ep_stats.sort(key=lambda x: x[0])

lr_stats_for_agg = [s for _, s in lr_all_ep_stats]
lr_global = lr_aggregate_stats(lr_stats_for_agg)

# ═══════════════ Step 3: 对比 ═══════════════

print("[4/4] 对比结果...\n")

NON_IMAGE_KEYS = [k for k in features if features[k]["dtype"] not in ("video", "image")]

def to_flat(val):
    """将 numpy array 或 list 统一转为 flat python list"""
    if isinstance(val, np.ndarray):
        return val.flatten().tolist()
    if isinstance(val, list):
        def flatten(x):
            if isinstance(x, list):
                out = []
                for i in x:
                    out.extend(flatten(i))
                return out
            return [float(x)]
        return flatten(val)
    return [float(val)]


def compare_stat_dicts(name, ours, theirs, keys_to_check, rtol=1e-4, atol=1e-6):
    """对比两个 stats dict, 返回 (passed, failed) 计数"""
    passed = failed = 0
    for key in keys_to_check:
        if key not in ours and key not in theirs:
            continue
        if key not in ours:
            print(f"  MISS  {name}/{key}: 我们缺少此 key")
            failed += 1
            continue
        if key not in theirs:
            continue

        for metric in ("min", "max", "mean", "std", "count"):
            if metric not in ours[key] or metric not in theirs[key]:
                continue
            a = np.array(to_flat(ours[key][metric]))
            b = np.array(to_flat(theirs[key][metric]))
            if a.shape != b.shape:
                print(f"  FAIL  {name}/{key}/{metric}: shape {a.shape} vs {b.shape}")
                failed += 1
                continue
            if metric == "count":
                if not np.array_equal(a.astype(int), b.astype(int)):
                    print(f"  FAIL  {name}/{key}/count: {a} vs {b}")
                    failed += 1
                else:
                    passed += 1
                continue
            close = np.allclose(a, b, rtol=rtol, atol=atol)
            if not close:
                diff = np.abs(a - b)
                max_diff_idx = np.argmax(diff)
                print(f"  FAIL  {name}/{key}/{metric}: max_diff={diff[max_diff_idx]:.8f} "
                      f"at idx {max_diff_idx} (ours={a[max_diff_idx]:.8f}, "
                      f"theirs={b[max_diff_idx]:.8f})")
                failed += 1
            else:
                passed += 1
    return passed, failed


total_pass = total_fail = 0

# 对比每个 episode 的 stats
for our_es, (lr_ep_idx, lr_es) in zip(our_ep_stats, lr_all_ep_stats):
    assert our_es["episode_index"] == lr_ep_idx
    p, f = compare_stat_dicts(
        f"episode_{lr_ep_idx}", our_es["stats"], lr_es, NON_IMAGE_KEYS)
    total_pass += p
    total_fail += f

# 对比全局 stats
lr_global_serializable = {}
for k, v in lr_global.items():
    lr_global_serializable[k] = {
        mk: mv.tolist() if isinstance(mv, np.ndarray) else mv
        for mk, mv in v.items()
    }

p, f = compare_stat_dicts("global", our_global_stats, lr_global_serializable, NON_IMAGE_KEYS)
total_pass += p
total_fail += f

print(f"\n{'='*50}")
print(f"  PASSED: {total_pass}")
print(f"  FAILED: {total_fail}")
if total_fail == 0:
    print("  所有非图像统计数据完全匹配!")
else:
    print("  存在不匹配项，请检查上方 FAIL 输出。")
print(f"{'='*50}")

# 清理临时目录
if OUTPUT_PATH.startswith(tempfile.gettempdir()):
    shutil.rmtree(OUTPUT_PATH, ignore_errors=True)
    print(f"\n已清理临时目录: {OUTPUT_PATH}")
