#!/usr/bin/env python3
"""
LeRobot 数据集视频编码转换工具。

功能：
  1. 扫描数据集 videos/ 目录下所有 mp4，用 ffprobe 读取
     codec / 分辨率 / 帧数 / 时长 / fps / pix_fmt。
  2. 按目标编码（av1 / h264）批量转码，输出到新目录。
  3. 同步更新 info.json 中视频相关字段（video.codec / pix_fmt）。
  4. 转码后可选校验完整性（decode 每个视频前 N 帧是否报错）。

本模块不依赖 Flask，只依赖 ffmpeg / ffprobe（走 PATH）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ProgressFn = Callable[..., None]

# 目标编码 → ffmpeg encoder 名 + 推荐参数
TARGET_ENCODERS = {
    "av1": {
        "encoder": "libsvtav1",
        # SVT-AV1 参数：preset 12 平衡速度与压缩率；crf 30 视觉无损偏高质量；
        # g=2 便于训练时随机访问（与 LeRobot 官方默认接近）
        "default_args": ["-preset", "12", "-crf", "30", "-g", "2",
                         "-pix_fmt", "yuv420p"],
    },
    "h264": {
        "encoder": "libx264",
        "default_args": ["-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                         "-g", "2"],
    },
    "h265": {
        "encoder": "libx265",
        "default_args": ["-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
                         "-g", "2"],
    },
}


def find_video_files(root: Path) -> List[Dict[str, Any]]:
    """扫描数据集根目录下的 videos/ 目录，返回视频文件列表。

    返回结构：[{path, rel_path, episode_index, video_key, camera_name}]
    """
    root = Path(root)
    video_dir = root / "videos"
    result: List[Dict[str, Any]] = []
    if not video_dir.exists():
        return result

    for vf in sorted(video_dir.rglob("*.mp4")):
        ep_idx = _parse_episode_index(vf.stem)
        rel_parts = vf.relative_to(video_dir).parts
        video_key = None
        if len(rel_parts) >= 3 and rel_parts[0].startswith("chunk-"):
            # v2.1 chunk-first: videos/chunk-000/<key>/episode_xxxxxx.mp4
            video_key = rel_parts[1]
        elif len(rel_parts) >= 3 and rel_parts[1].startswith("chunk-"):
            # key-first: videos/<key>/chunk-000/file-xxx.mp4
            video_key = rel_parts[0]
        else:
            video_key = vf.parent.name

        result.append({
            "path": str(vf),
            "rel_path": str(vf.relative_to(root)).replace("\\", "/"),
            "episode_index": ep_idx,
            "video_key": video_key,
            "camera_name": video_key.split(".")[-1] if "." in video_key else video_key,
        })
    return result


def _parse_episode_index(stem: str) -> Optional[int]:
    """从文件名 stem 里提取 episode 索引（episode_000000 → 0）。"""
    import re
    m = re.search(r"(\d+)", stem)
    return int(m.group(1)) if m else None


def probe_video(path: str) -> Dict[str, Any]:
    """用 ffprobe 读取视频元信息。

    返回 {codec, width, height, fps, duration, nb_frames, pix_fmt, has_audio}。
    失败返回 {error: "..."}。
    """
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams", "-select_streams", "v:0",
                "-show_format",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode != 0:
            return {"error": f"ffprobe 退出码 {r.returncode}"}
        data = json.loads(r.stdout or "{}")
    except FileNotFoundError:
        return {"error": "未找到 ffprobe，请先安装并加入 PATH"}
    except subprocess.TimeoutExpired:
        return {"error": "ffprobe 超时"}
    except Exception as e:
        return {"error": str(e)}

    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    if not streams:
        return {"error": "无视频流"}
    s = streams[0]

    def _parse_fps(raw):
        if not raw or raw == "0/0":
            return None
        try:
            if "/" in raw:
                num, den = raw.split("/")
                den = float(den) or 1.0
                return float(num) / den
            return float(raw)
        except Exception:
            return None

    fps = _parse_fps(s.get("avg_frame_rate")) or _parse_fps(s.get("r_frame_rate"))
    duration = float(s.get("duration") or fmt.get("duration") or 0)
    nb_frames_raw = s.get("nb_frames")
    try:
        nb_frames = int(nb_frames_raw) if nb_frames_raw else int(round(fps * duration)) if fps and duration else 0
    except (TypeError, ValueError):
        nb_frames = 0

    return {
        "codec": s.get("codec_name"),
        "width": int(s.get("width") or 0),
        "height": int(s.get("height") or 0),
        "fps": round(fps, 3) if fps else None,
        "duration": round(duration, 3),
        "nb_frames": nb_frames,
        "pix_fmt": s.get("pix_fmt"),
        "has_audio": any((x.get("codec_type") == "audio") for x in (data.get("streams") or [])),
        "size_bytes": int(fmt.get("size") or 0),
    }


def scan_dataset_videos(root: Path) -> Dict[str, Any]:
    """扫描数据集所有视频，返回汇总信息。"""
    files = find_video_files(root)
    items: List[Dict[str, Any]] = []
    codec_counts: Dict[str, int] = {}
    total_size = 0
    for f in files:
        info = probe_video(f["path"])
        item = {**f, **info}
        items.append(item)
        if "codec" in info and info["codec"]:
            codec_counts[info["codec"]] = codec_counts.get(info["codec"], 0) + 1
        total_size += int(info.get("size_bytes") or 0)

    return {
        "root": str(root),
        "video_count": len(items),
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "codec_summary": codec_counts,
        "is_mixed": len([c for c in codec_counts if c]) > 1,
        "items": items,
        "video_keys": sorted({x["video_key"] for x in items}),
    }


def transcode_one(
    src: str,
    dst: str,
    target_codec: str,
    *,
    extra_args: Optional[List[str]] = None,
    keep_audio: bool = False,
    overwrite: bool = True,
) -> Dict[str, Any]:
    """转码单个视频到目标编码。

    返回 {ok, src, dst, codec_before, codec_after, duration, error?}。
    """
    src_p = Path(src)
    dst_p = Path(dst)
    dst_p.parent.mkdir(parents=True, exist_ok=True)

    before = probe_video(src)
    codec_before = before.get("codec")

    target = TARGET_ENCODERS.get(target_codec)
    if not target:
        return {"ok": False, "src": src, "dst": dst,
                "error": f"不支持的目标编码: {target_codec}"}

    encoder = target["encoder"]
    args = list(target["default_args"])
    if extra_args:
        args += list(extra_args)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    cmd += ["-i", str(src_p)]
    cmd += ["-c:v", encoder] + args
    if not keep_audio:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart"]
    cmd += [str(dst_p)]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            # 清理半成品
            dst_p.unlink(missing_ok=True)
            err_tail = (r.stderr or "").strip().splitlines()[-3:]
            return {
                "ok": False, "src": src, "dst": dst,
                "codec_before": codec_before,
                "error": "ffmpeg 失败: " + " | ".join(err_tail),
            }
    except FileNotFoundError:
        return {"ok": False, "src": src, "dst": dst,
                "error": "未找到 ffmpeg，请先安装"}
    except subprocess.TimeoutExpired:
        dst_p.unlink(missing_ok=True)
        return {"ok": False, "src": src, "dst": dst,
                "codec_before": codec_before, "error": "转码超时"}
    except Exception as e:
        dst_p.unlink(missing_ok=True)
        return {"ok": False, "src": src, "dst": dst,
                "codec_before": codec_before, "error": str(e)}

    after = probe_video(str(dst_p))
    return {
        "ok": True,
        "src": src,
        "dst": dst,
        "codec_before": codec_before,
        "codec_after": after.get("codec"),
        "duration": after.get("duration"),
        "size_before": before.get("size_bytes"),
        "size_after": after.get("size_bytes"),
    }


def verify_video_decode(path: str, sample_frames: int = 30) -> Dict[str, Any]:
    """校验视频是否能正常解码前 N 帧。"""
    try:
        cmd = [
            "ffmpeg", "-hide_banner", "-v", "error",
            "-i", path,
            "-vframes", str(sample_frames),
            "-f", "null", "-",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return {"ok": False, "path": path,
                    "error": (r.stderr or "").strip().splitlines()[-1] or "decode error"}
        return {"ok": True, "path": path, "sample_frames": sample_frames}
    except Exception as e:
        return {"ok": False, "path": path, "error": str(e)}


def transcode_dataset(
    input_root: Path,
    output_root: Path,
    target_codec: str,
    *,
    only_codec: Optional[List[str]] = None,
    extra_args: Optional[List[str]] = None,
    skip_verify: bool = False,
    skip_non_video: bool = False,
    progress_cb: Optional[ProgressFn] = None,
) -> Dict[str, Any]:
    """把整个数据集的视频转码到目标编码，另存为新数据集。

    流程：
      1. 复制整个数据集到 output_root（如已存在先删除）。
      2. 扫描视频，过滤出需要转码的（codec != target 或命中 only_codec）。
      3. ThreadPoolExecutor 并行转码，进度回调上报。
      4. 更新 info.json 中视频字段。
      5. 可选：校验解码。

    返回 {input, output, total, transcoded, skipped, failed, results, info_updated}.
    """
    input_root = Path(input_root)
    output_root = Path(output_root)

    def _report(stage, title, detail="", current=0, total=0):
        if progress_cb:
            try:
                progress_cb(stage, title, detail, current, total, True)
            except Exception:
                pass

    # ── 1. 复制数据集 ──
    _report("copy", "复制数据集", f"{input_root} → {output_root}", 0, 1)
    if output_root.exists():
        shutil.rmtree(output_root)
    shutil.copytree(input_root, output_root)

    # ── 2. 扫描输出目录视频 ──
    _report("scan", "扫描视频文件", "", 0, 1)
    scan = scan_dataset_videos(output_root)
    items = scan["items"]
    _report("scan", "扫描完成",
            f"共 {len(items)} 个视频，编码分布: {scan['codec_summary']}",
            1, 1)

    # 把目标 codec 名规范化（av1 / h264 / h265）以便比较
    target_norm = _normalize_codec_name(target_codec)

    # 决定要转码哪些
    def _need_transcode(item):
        if "error" in item:
            return False
        codec = _normalize_codec_name(item.get("codec") or "")
        if only_codec:
            return codec in [_normalize_codec_name(c) for c in only_codec]
        return codec != target_norm

    todo = [it for it in items if _need_transcode(it)]
    skip_count = len(items) - len(todo)

    if not todo:
        _report("done", "无需转码",
                f"所有视频已经是 {target_codec}，共 {skip_count} 个", 1, 1)
        return {
            "input": str(input_root),
            "output": str(output_root),
            "total": len(items),
            "transcoded": 0,
            "skipped": skip_count,
            "failed": 0,
            "results": [],
            "info_updated": False,
        }

    # ── 3. 并行转码 ──
    workers = max(1, min(len(todo), (os.cpu_count() or 4) // 2))
    _report("transcode", "正在转码",
            f"共 {len(todo)} 个视频，{workers} 路并行", 0, len(todo))

    results: List[Dict[str, Any]] = []
    failed = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(
                transcode_one, it["path"], it["path"], target_codec,
                extra_args=extra_args,
            ): it
            for it in todo
        }
        for fut in as_completed(future_map):
            it = future_map[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {"ok": False, "src": it["path"],
                     "error": str(e)}
            results.append({
                "path": it.get("rel_path") or it["path"],
                "video_key": it.get("video_key"),
                "episode_index": it.get("episode_index"),
                **r,
            })
            if not r.get("ok"):
                failed += 1
            done += 1
            _report("transcode", "正在转码",
                    f"已处理 {done}/{len(todo)}" +
                    (f"（失败 {failed}）" if failed else ""),
                    done, len(todo))

    # ── 4. 更新 info.json ──
    info_updated = False
    info_path = output_root / "meta" / "info.json"
    if info_path.exists():
        try:
            _report("update_info", "更新 info.json", "", 0, 1)
            info_data = json.loads(info_path.read_text(encoding="utf-8"))
            info_updated = _update_info_video_codec(
                info_data, target_codec, target_norm,
            )
            if info_updated:
                info_path.write_text(
                    json.dumps(info_data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            _report("update_info", "info.json 已更新", "", 1, 1)
        except Exception as e:
            _report("update_info", "更新 info.json 失败", str(e), 1, 1)

    # ── 5. 可选校验 ──
    if not skip_verify:
        _report("verify", "校验视频完整性", f"共 {len(todo)} 个", 0, len(todo))
        v_done = 0
        v_failed = 0
        verify_results: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(verify_video_decode, r["dst"]): r
                for r in results if r.get("ok") and r.get("dst")
            }
            for fut in as_completed(futs):
                r = futs[fut]
                try:
                    v = fut.result()
                except Exception as e:
                    v = {"ok": False, "path": r.get("dst"), "error": str(e)}
                verify_results.append(v)
                if not v.get("ok"):
                    v_failed += 1
                v_done += 1
                _report("verify", "校验视频完整性",
                        f"已校验 {v_done}/{len(futs)}" +
                        (f"（损坏 {v_failed}）" if v_failed else ""),
                        v_done, len(futs))

    _report("done", "转码完成",
            f"成功 {len(todo) - failed}/{len(todo)}，跳过 {skip_count}",
            1, 1)

    return {
        "input": str(input_root),
        "output": str(output_root),
        "total": len(items),
        "transcoded": len(todo) - failed,
        "skipped": skip_count,
        "failed": failed,
        "results": results,
        "info_updated": info_updated,
    }


def _normalize_codec_name(name: str) -> str:
    """统一编码名比较，h264/hevc 等常见别名归一化。"""
    n = (name or "").lower().strip()
    aliases = {
        "h264": "h264", "avc": "h264", "libx264": "h264", "mpeg4": "h264",
        "hevc": "h265", "h265": "h265", "libx265": "h265",
        "av1": "av1", "libsvtav1": "av1", "libaom-av1": "av1", "aom": "av1",
        "vp9": "vp9", "libvpx-vp9": "vp9",
    }
    return aliases.get(n, n)


def _update_info_video_codec(
    info_data: Dict[str, Any],
    target_codec: str,
    target_norm: str,
) -> bool:
    """更新 info.json 中视频相关字段。

    处理两种存放方式：
      A. feature 内嵌 video_info / info 字典
      B. 顶层无 codec 字段（LeRobot v2.1 主流）

    同时更新 video.pix_fmt 为 yuv420p（转码时固定）。
    返回是否有变更。
    """
    changed = False
    features = info_data.get("features") or {}

    # 编码名映射回 info.json 里 LeRobot 惯用的写法
    # h264 / av1 / h265
    codec_value = target_norm
    pix_fmt_value = "yuv420p"

    for key, meta in features.items():
        if not isinstance(meta, dict):
            continue
        dtype = str(meta.get("dtype", "")).lower()
        if dtype not in ("video", "image"):
            continue

        # 方式1: feature.video_info
        vinfo = meta.get("video_info")
        if isinstance(vinfo, dict):
            if vinfo.get("video.codec") != codec_value:
                vinfo["video.codec"] = codec_value
                changed = True
            if vinfo.get("video.pix_fmt") != pix_fmt_value:
                vinfo["video.pix_fmt"] = pix_fmt_value
                changed = True

        # 方式2: feature.info
        finfo = meta.get("info")
        if isinstance(finfo, dict):
            if finfo.get("video.codec") != codec_value:
                finfo["video.codec"] = codec_value
                changed = True
            if finfo.get("video.pix_fmt") != pix_fmt_value:
                finfo["video.pix_fmt"] = pix_fmt_value
                changed = True

    return changed
