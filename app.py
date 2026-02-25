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
import shutil
import logging
import argparse
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_file, abort
import pandas as pd
import numpy as np

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
                    self.episode_data[ep_idx] = df
                    self._orig_indices[ep_idx] = ep_idx
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
        """删除 episode 中的指定帧 (同时删除对应 state, action)"""
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

    # ─── 统计 ───

    def compute_stats(self):
        """计算全局 mean / std / min / max"""
        collector = {}
        for df in self.episode_data.values():
            for col in ("observation.state", "action"):
                if col not in df.columns:
                    continue
                vals = df[col].tolist()
                valid = [self._to_list(v) for v in vals if v is not None]
                valid = [v for v in valid if len(v) > 0]
                if valid:
                    collector.setdefault(col, []).extend(valid)

        stats = {}
        for col, data in collector.items():
            arr = np.array(data, dtype=np.float64)
            if arr.ndim == 2 and arr.shape[0] > 0:
                stats[col] = {
                    "mean": arr.mean(0).tolist(),
                    "std":  arr.std(0).tolist(),
                    "min":  arr.min(0).tolist(),
                    "max":  arr.max(0).tolist(),
                    "count": int(arr.shape[0]),
                }
        return stats

    def compute_episode_stats(self):
        """计算每个 episode 的统计数据"""
        results = []
        for em in self.episodes_meta:
            idx = em["episode_index"]
            if idx not in self.episode_data:
                continue
            df = self.episode_data[idx]
            s = {"episode_index": idx}
            for col in ("observation.state", "action"):
                if col not in df.columns:
                    continue
                vals = [self._to_list(v) for v in df[col].tolist() if v is not None]
                valid = [v for v in vals if len(v) > 0]
                if valid:
                    arr = np.array(valid, dtype=np.float64)
                    s[col] = {
                        "mean": arr.mean(0).tolist(),
                        "std":  arr.std(0).tolist(),
                        "min":  arr.min(0).tolist(),
                        "max":  arr.max(0).tolist(),
                    }
            results.append(s)
        return results

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

        # ── stats.json (全局统计) ──
        stats = self.compute_stats()
        with open(meta_dir / "stats.json", "w") as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)

        # ── episodes_stats.jsonl (逐 episode 统计) ──
        ep_stats = self.compute_episode_stats()
        with open(meta_dir / "episodes_stats.jsonl", "w") as f:
            for s in ep_stats:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")

        # ── Parquet 数据 ──
        data_tpl = self.info.get(
            "data_path",
            "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet")
        chunks_size = self.info.get("chunks_size", 1000)

        for em in self.episodes_meta:
            idx = em["episode_index"]
            if idx not in self.episode_data:
                continue
            chunk = idx // chunks_size
            try:
                rel = data_tpl.format(
                    episode_chunk=chunk, chunk_index=chunk, episode_index=idx)
            except KeyError:
                rel = f"data/chunk-{chunk:03d}/episode_{idx:06d}.parquet"
            pq_path = out / rel
            pq_path.parent.mkdir(parents=True, exist_ok=True)
            self.episode_data[idx].to_parquet(pq_path, index=False)

        # ── 视频 (从原始位置复制, 重命名) ──
        video_tpl = self.info.get("video_path", "")
        features = self.info.get("features", {})
        video_keys = [k for k in features if "images" in k]

        for em in self.episodes_meta:
            idx = em["episode_index"]
            orig_idx = self._orig_indices.get(idx, idx)
            orig_videos = self._orig_video_files.get(orig_idx, {})
            chunk_new = idx // chunks_size

            for cam_name, src_path_str in orig_videos.items():
                src = Path(src_path_str)
                if not src.exists():
                    continue

                # 找到完整的 video key (如 observation.images.cam_high)
                vkey = None
                for k in video_keys:
                    if k.endswith(cam_name):
                        vkey = k
                        break
                if vkey is None:
                    vkey = f"observation.images.{cam_name}"

                # 构建目标路径
                if video_tpl:
                    try:
                        dst_rel = video_tpl.format(
                            episode_chunk=chunk_new, chunk_index=chunk_new,
                            video_key=vkey, episode_index=idx)
                    except KeyError:
                        dst_rel = f"videos/chunk-{chunk_new:03d}/{vkey}/episode_{idx:06d}.mp4"
                else:
                    dst_rel = f"videos/chunk-{chunk_new:03d}/{vkey}/episode_{idx:06d}.mp4"

                dst = out / dst_rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

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
    # 安全检查: 确保路径在原始数据集根目录下
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
