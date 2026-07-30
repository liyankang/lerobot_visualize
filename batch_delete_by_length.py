#!/usr/bin/env python3
"""
按 episode 长度和静止边界批量过滤 LeRobot 数据集。

示例:
    python batch_delete_by_length.py --input /path/to/dataset --output /path/to/filtered --auto-length-iqr
    python batch_delete_by_length.py --input /path/to/dataset --output /path/to/filtered --auto-length-iqr --trim-static-edges
    python batch_delete_by_length.py --input /path/to/dataset --auto-length-iqr --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import dataset_batch_tools as batch_tools

def _load_dataset_editor_class():
    """只加载 app.py 中的 DatasetEditor，避开后续 Web/ROS2 路由导入副作用。"""
    app_path = Path(__file__).resolve().with_name("app.py")
    source = app_path.read_text(encoding="utf-8")
    marker = "# ═══════════════════════ Flask 路由 ═══════════════════════"
    if marker not in source:
        raise RuntimeError("无法在 app.py 中定位 DatasetEditor 定义结束位置")
    prefix = source.split(marker, 1)[0]
    namespace = {"__file__": str(app_path), "__name__": "_lerobot_visualize_app_core"}
    exec(compile(prefix, str(app_path), "exec"), namespace)
    return namespace["DatasetEditor"]


DatasetEditor = _load_dataset_editor_class()


def _print_length_rows(title, rows, limit):
    rows = list(rows)
    print(title)
    if not rows:
        print("  none")
        return
    for row in rows[:limit]:
        print(
            f"  episode {row['episode_index']:6d} | "
            f"length {row['length']:6d} | {row['reason']}"
        )
    remaining = len(rows) - limit
    if remaining > 0:
        print(f"  ... {remaining} more")


def _print_trim_rows(title, rows, limit):
    rows = list(rows)
    print(title)
    if not rows:
        print("  none")
        return
    for row in rows[:limit]:
        print(
            f"  episode {row['episode_index']:6d} | "
            f"length {row['length']:6d} | "
            f"trim start {row['trim_start']:5d}, end {row['trim_end']:5d}"
        )
    remaining = len(rows) - limit
    if remaining > 0:
        print(f"  ... {remaining} more")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按 IQR 自动删除过短/过长 episode，并可裁剪开头/结尾静止帧。"
    )
    parser.add_argument("--input", "-i", required=True, help="输入 LeRobot 数据集目录")
    parser.add_argument("--output", "-o", help="输出数据集目录；非 dry-run 时必填")
    parser.add_argument(
        "--auto-length-iqr",
        action="store_true",
        help="用 episode 长度的 IQR 自动删除过短/过长 episode",
    )
    parser.add_argument(
        "--iqr-multiplier",
        type=float,
        default=1.5,
        help="IQR 异常值倍数，边界为 Q1-k*IQR / Q3+k*IQR，默认 1.5",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将删除的 episode，不写出新数据集",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="允许输出 0 个 episode 的数据集；默认会阻止删空",
    )
    parser.add_argument(
        "--skip-video-stats",
        "--skip-video",
        action="store_true",
        help="保存时跳过 image/video 的视频帧统计，只计算 state/action 等数值统计",
    )
    parser.add_argument(
        "--trim-static-edges",
        action="store_true",
        help="裁掉每个 episode 开头和结尾的静止帧",
    )
    parser.add_argument(
        "--motion-threshold",
        type=float,
        default=1e-4,
        help="相邻帧 observation.state 变化量阈值，低于该值视为静止，默认 1e-4",
    )
    parser.add_argument(
        "--margin-frames",
        type=int,
        default=0,
        help="在检测到运动边界外额外保留的帧数，默认 0",
    )
    parser.add_argument(
        "--min-static-frames",
        type=int,
        default=1,
        help="开头或结尾静止段至少达到多少帧才裁剪，默认 1",
    )
    parser.add_argument(
        "--joint-indices",
        default=None,
        help="只用指定 state 维度判断静止，例如 0,1,2；默认使用全部维度",
    )
    parser.add_argument(
        "--motion-metric",
        choices=("max_abs", "norm"),
        default="max_abs",
        help="运动量计算方式，默认 max_abs",
    )
    parser.add_argument(
        "--list-limit",
        type=int,
        default=50,
        help="最多打印多少条待删除 episode 明细，默认 50",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if (
        not args.auto_length_iqr
        and not args.trim_static_edges
    ):
        print(
            "错误: 至少需要指定 --auto-length-iqr 或 --trim-static-edges",
            file=sys.stderr,
        )
        return 2
    if args.iqr_multiplier < 0:
        print("错误: --iqr-multiplier 不能小于 0", file=sys.stderr)
        return 2
    if not args.dry_run and not args.output:
        print("错误: 非 dry-run 模式必须指定 --output", file=sys.stderr)
        return 2

    input_path = Path(args.input).resolve()
    if not input_path.exists():
        print(f"错误: 输入数据集不存在: {input_path}", file=sys.stderr)
        return 2

    editor = DatasetEditor(str(input_path))
    joint_indices = batch_tools.parse_joint_indices(args.joint_indices)
    plan = batch_tools.build_batch_plan(
        editor,
        auto_length_iqr=args.auto_length_iqr,
        iqr_multiplier=args.iqr_multiplier,
        trim_static_edges=args.trim_static_edges,
        motion_threshold=args.motion_threshold,
        margin_frames=args.margin_frames,
        min_static_frames=args.min_static_frames,
        joint_indices=joint_indices,
        motion_metric=args.motion_metric,
    )

    print(f"输入数据集: {input_path}")
    print(f"episode 总数: {plan['total_episodes']}, frame 总数: {plan['total_frames']}")
    if args.auto_length_iqr:
        iqr = plan["length_iqr"]
        print(
            "IQR 长度边界: Q1={q1:.2f}, Q3={q3:.2f}, IQR={iqr:.2f}, "
            "lower={lower:.2f}, upper={upper:.2f}, k={multiplier:.2f}".format(**iqr)
        )
    if args.trim_static_edges:
        print(f"静止段裁剪阈值: {args.motion_threshold}")
        print(f"边界保留帧: {args.margin_frames}")
        print(f"最小静止段长度: {args.min_static_frames}")
    print(
        f"将按长度删除: {plan['delete_episode_count']} episodes, "
        f"{plan['delete_episode_frames']} frames"
    )
    print(f"将按静止段裁剪: {plan['trim_frame_count']} frames")
    print(f"将保留: {plan['keep_episodes']} episodes, {plan['keep_frames']} frames")
    _print_length_rows(
        "待按长度删除 episode:",
        plan["length_deletions"],
        max(0, args.list_limit),
    )
    _print_trim_rows(
        "待裁剪静止段 episode:",
        plan["static_trims"],
        max(0, args.list_limit),
    )

    if args.dry_run:
        print("dry-run: 未写出新数据集")
        return 0
    output_path = Path(args.output).resolve()
    if output_path == input_path:
        print("错误: --output 不能和 --input 相同，请另存为新目录", file=sys.stderr)
        return 2
    if plan["keep_episodes"] <= 0 and not args.allow_empty:
        print(
            "错误: 当前阈值会删除全部 episode；如确实需要，请加 --allow-empty",
            file=sys.stderr,
        )
        return 2
    if not plan["length_deletions"] and not plan["static_trims"]:
        print("没有 episode 命中删除条件，仍会按原样另存为新数据集。")

    batch_tools.apply_batch_plan(editor, plan)
    editor.save_as(str(output_path), skip_video_stats=args.skip_video_stats)
    print(f"已保存到: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
