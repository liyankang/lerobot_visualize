"""
LeRobot 数据集健康度评分服务

聚合已有的完整性检查、关节时序分析、图像质量分析、物理约束、时间对齐
等多维度指标，加权计算一个 0-100 的综合健康度分数，并给出每个维度的
子分数、等级和具体问题描述清单。

依赖:
    - app.DatasetEditor: 关节统计 / 平滑性 / 约束 / 时间对齐
    - image_analyzer.ImageAnalyzer: 图像质量指标 (可选)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


# ══════════════════════ 评分等级 ══════════════════════

GRADE_THRESHOLDS = (
    (85, "A", "优秀"),
    (70, "B", "良好"),
    (55, "C", "一般"),
    (40, "D", "较差"),
    (0, "F", "不合格"),
)


def grade_for_score(score):
    """根据 0-100 分数返回 (等级字母, 中文描述, 颜色)。"""
    for threshold, letter, label in GRADE_THRESHOLDS:
        if score >= threshold:
            colors = {"A": "#2e7d32", "B": "#388e3c", "C": "#f9a825",
                      "D": "#ef6c00", "F": "#c62828"}
            return letter, label, colors.get(letter, "#666")
    return "F", "不合格", "#c62828"


def level_for_score(score):
    """将子维度分数转为红黄绿等级。"""
    if score >= 75:
        return "good"
    if score >= 50:
        return "warn"
    return "bad"


# ══════════════════════ 权重配置 ══════════════════════

# 各维度在总分中的权重 (sum = 1.0)
DIMENSION_WEIGHTS = {
    "integrity": 0.20,      # 视频/parquet 结构一致性
    "completeness": 0.15,   # 元数据完整性 (info/episodes/tasks/stats)
    "smoothness": 0.20,     # 关节轨迹平滑性 (jerk/spike)
    "coverage": 0.15,       # state 分布覆盖度 (sigma 覆盖率)
    "constraint": 0.10,     # 物理约束 (joint/velocity 超限)
    "alignment": 0.10,      # 时间对齐 (state-action lag, timestamp jitter)
    "image_quality": 0.10,  # 图像质量综合分 (可选)
}


# ══════════════════════ 辅助函数 ══════════════════════

def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(v)))


def _safe_mean(values, default=0.0):
    arr = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=np.float64)
    if arr.size == 0:
        return float(default)
    return float(arr.mean())


def _safe_max(values, default=0.0):
    arr = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=np.float64)
    if arr.size == 0:
        return float(default)
    return float(arr.max())


# ══════════════════════ 核心服务 ══════════════════════

class HealthCheckService:
    """数据集健康度评分聚合器。

    依赖 DatasetEditor 和 ImageAnalyzer 类，通过依赖注入传入，
    避免循环导入。
    """

    def __init__(self, dataset_editor_cls, joint_config_getter, image_analyzer_cls=None, logger=None):
        self.DatasetEditor = dataset_editor_cls
        self.ImageAnalyzer = image_analyzer_cls
        self._joint_config_getter = joint_config_getter
        self.log = logger or logging.getLogger(__name__)

    def _joint_config(self):
        getter = self._joint_config_getter
        return getter() if callable(getter) else None

    # ─── 各维度评分 ───

    def _score_integrity(self, editor, progress_cb=None):
        """维度 1: 视频/parquet 结构一致性。复用 editor.check_integrity()。"""
        if progress_cb:
            progress_cb("integrity", "正在检查视频与数据一致性...", 1, 7)

        try:
            report = editor.check_integrity()
        except Exception as e:
            self.log.warning(f"完整性检查失败: {e}")
            return {
                "available": False,
                "score": 0.0,
                "issues": [f"完整性检查执行失败: {e}"],
                "detail": {},
            }

        error_count = report.get("error_count", 0)
        warning_count = report.get("warning_count", 0)
        total_videos = max(1, report.get("total_videos_checked", 1))
        ffprobe_missing = report.get("ffprobe_missing", False)

        issues = []
        if ffprobe_missing:
            issues.append("未检测到 ffprobe，无法做视频一致性检查，请安装 ffmpeg/ffprobe")
        if error_count > 0:
            issues.append(f"{error_count} 个视频与 parquet 存在严重不一致 (帧数/fps/时长)")
        if warning_count > 0:
            issues.append(f"{warning_count} 个视频探测失败或存在警告")

        error_ratio = error_count / total_videos
        warning_ratio = warning_count / total_videos

        if ffprobe_missing:
            score = 30.0
        elif error_ratio > 0:
            score = _clamp(80 * (1 - error_ratio * 2) - warning_ratio * 10)
        else:
            score = _clamp(100 - warning_ratio * 15)

        return {
            "available": True,
            "score": round(score, 1),
            "level": level_for_score(score),
            "issues": issues,
            "detail": {
                "total_videos_checked": total_videos,
                "error_count": error_count,
                "warning_count": warning_count,
                "ffprobe_missing": ffprobe_missing,
                "affected_episodes": report.get("affected_episodes", []),
            },
        }

    def _score_completeness(self, root_path, editor, progress_cb=None):
        """维度 2: 元数据文件完整性。"""
        if progress_cb:
            progress_cb("completeness", "正在检查元数据完整性...", 2, 7)

        meta_dir = root_path / "meta"
        expected_files = {
            "info.json": meta_dir / "info.json",
            "episodes.jsonl": meta_dir / "episodes.jsonl",
            "tasks.jsonl": meta_dir / "tasks.jsonl",
            "stats.json": meta_dir / "stats.json",
        }
        optional_files = {
            "episodes_stats.jsonl": meta_dir / "episodes_stats.jsonl",
        }

        missing = []
        present = {}
        for name, path in expected_files.items():
            if path.exists():
                present[name] = True
            else:
                missing.append(name)
                present[name] = False

        optional_present = {}
        for name, path in optional_files.items():
            optional_present[name] = path.exists()

        issues = []
        if missing:
            issues.append(f"缺少必要元数据文件: {', '.join(missing)}")

        # 检查 episodes_meta 与 parquet 数量是否一致
        ep_count = len(editor.episodes_meta)
        data_count = len(editor.episode_data)
        if ep_count != data_count:
            issues.append(f"episodes.jsonl 记录数 ({ep_count}) 与 parquet 文件数 ({data_count}) 不一致")

        if missing:
            score = _clamp(100 - 30 * len(missing))
        elif ep_count != data_count:
            score = 60.0
        else:
            score = 100.0
            if not optional_present.get("episodes_stats.jsonl"):
                score = 90.0  # 缺少可选的 episodes_stats 轻微扣分

        return {
            "available": True,
            "score": round(score, 1),
            "level": level_for_score(score),
            "issues": issues,
            "detail": {
                "required_files": present,
                "optional_files": optional_present,
                "episode_meta_count": ep_count,
                "parquet_count": data_count,
            },
        }

    def _score_smoothness(self, editor, progress_cb=None):
        """维度 3: 关节轨迹平滑性。复用 build_joint_analysis_report()。"""
        if progress_cb:
            progress_cb("smoothness", "正在分析关节轨迹平滑性...", 3, 7)

        try:
            report = editor.build_joint_analysis_report()
        except Exception as e:
            self.log.warning(f"平滑性分析失败: {e}")
            return {"available": False, "score": 0.0, "issues": [f"分析失败: {e}"], "detail": {}}

        smoothness_module = report.get("smoothness", {})
        sources = smoothness_module.get("sources", {}) if smoothness_module.get("available") else {}

        issues = []
        all_jerks = []
        all_spikes = []
        all_velocities = []

        for source_key in ("state", "action"):
            src = sources.get(source_key)
            if not src:
                continue
            mean_jerk = src.get("mean_jerk")
            if mean_jerk is not None:
                all_jerks.append(mean_jerk)
            for joint in src.get("top_jerk_joints", []):
                if joint.get("spike_ratio") is not None:
                    all_spikes.append(joint["spike_ratio"])
                if joint.get("jerk") is not None:
                    all_jerks.append(joint["jerk"])
            for joint in src.get("top_velocity_joints", []):
                if joint.get("velocity_abs_max") is not None:
                    all_velocities.append(joint["velocity_abs_max"])

        # spike_ratio: 异常帧占比，越低越好
        mean_spike = _safe_mean(all_spikes)
        # jerk 越低越平滑，但绝对值依赖数据尺度，这里主要看 spike_ratio
        if all_spikes:
            # spike_ratio 通常 < 0.1 为好，> 0.3 为差
            score = _clamp(100 - (mean_spike / 0.15) * 100)
            if mean_spike > 0.05:
                issues.append(f"动作平均异常帧比例 {mean_spike*100:.1f}%，存在较多加速度突变")
            if mean_spike > 0.15:
                issues.append(f"异常帧比例 {mean_spike*100:.1f}% 过高，建议使用动作平滑工具")
        else:
            score = 70.0
            issues.append("未检测到可用的 spike_ratio 数据")

        return {
            "available": True,
            "score": round(score, 1),
            "level": level_for_score(score),
            "issues": issues,
            "detail": {
                "mean_spike_ratio": round(mean_spike, 4),
                "top_jerk_joints": sources.get("state", {}).get("top_jerk_joints", [])[:3]
                    if sources.get("state") else [],
                "top_velocity_joints": sources.get("action", {}).get("top_velocity_joints", [])[:3]
                    if sources.get("action") else [],
            },
        }

    def _score_coverage(self, editor, progress_cb=None):
        """维度 4: state 分布覆盖度。基于 sigma 区间覆盖率。"""
        if progress_cb:
            progress_cb("coverage", "正在分析数据分布覆盖度...", 4, 7)

        try:
            report = editor.build_joint_analysis_report()
        except Exception as e:
            return {"available": False, "score": 0.0, "issues": [f"分析失败: {e}"], "detail": {}}

        joint_groups = report.get("joint_groups", [])
        sigma1_ratios = []
        sigma2_ratios = []

        for group in joint_groups:
            for joint in group.get("joints", []):
                for source_key in ("state", "action"):
                    src = joint.get(source_key)
                    if not src:
                        continue
                    s1 = (src.get("sigma_1") or {}).get("coverage_ratio")
                    s2 = (src.get("sigma_2") or {}).get("coverage_ratio")
                    if s1 is not None:
                        sigma1_ratios.append(s1)
                    if s2 is not None:
                        sigma2_ratios.append(s2)

        issues = []
        if not sigma1_ratios:
            return {"available": False, "score": 0.0,
                    "issues": ["缺少 state/action 分布数据"], "detail": {}}

        mean_s1 = _safe_mean(sigma1_ratios)
        mean_s2 = _safe_mean(sigma2_ratios)

        # 正态分布: 1σ ≈ 68%, 2σ ≈ 95%
        # 覆盖率过低 → 分布太集中 (可能缺少多样性)
        # 覆盖率过高 → 有大量异常值拉大方差
        s1_dev = abs(mean_s1 - 0.68)
        score = _clamp(100 - s1_dev * 200)

        if mean_s1 < 0.5:
            issues.append(f"1σ 覆盖率仅 {mean_s1*100:.0f}%，数据分布过于集中，多样性可能不足")
        elif mean_s1 > 0.85:
            issues.append(f"1σ 覆盖率达 {mean_s1*100:.0f}%，可能存在大量异常值拉大方差")
        if mean_s2 < 0.85:
            issues.append(f"2σ 覆盖率仅 {mean_s2*100:.0f}%，尾部数据覆盖不足")

        return {
            "available": True,
            "score": round(score, 1),
            "level": level_for_score(score),
            "issues": issues,
            "detail": {
                "mean_sigma1_coverage": round(mean_s1, 3),
                "mean_sigma2_coverage": round(mean_s2, 3),
                "expected_sigma1": 0.68,
                "expected_sigma2": 0.95,
                "joint_count": len(sigma1_ratios),
            },
        }

    def _score_constraint(self, editor, progress_cb=None):
        """维度 5: 物理约束 (关节限位 / 速度限位 超限比例)。"""
        if progress_cb:
            progress_cb("constraint", "正在检查物理约束...", 5, 7)

        try:
            report = editor.build_joint_analysis_report()
        except Exception as e:
            return {"available": False, "score": 0.0, "issues": [f"分析失败: {e}"], "detail": {}}

        constraint_module = report.get("constraints", {})
        if not constraint_module.get("available"):
            return {
                "available": False,
                "score": 75.0,
                "level": "warn",
                "issues": ["未从 joint_config.json 或 info.json 解析到 joint/velocity limit，无法检测超限"],
                "detail": {},
            }

        issues = []
        angle_violations = constraint_module.get("top_angle_violations", [])
        velocity_violations = constraint_module.get("top_velocity_violations", [])

        all_angle_ratios = [v.get("ratio") for v in angle_violations if v.get("ratio") is not None]
        all_vel_ratios = [v.get("ratio") for v in velocity_violations if v.get("ratio") is not None]

        max_angle = _safe_max(all_angle_ratios)
        max_vel = _safe_max(all_vel_ratios)

        # 超限比例越高分越低
        score = _clamp(100 - max_angle * 500 - max_vel * 300)

        if max_angle > 0.01:
            issues.append(f"关节角度超限比例最高 {max_angle*100:.1f}%")
        if max_vel > 0.05:
            issues.append(f"关节速度超限比例最高 {max_vel*100:.1f}%")
        if not issues and (all_angle_ratios or all_vel_ratios):
            issues.append("存在轻微超限，但总体可控")

        return {
            "available": True,
            "score": round(score, 1),
            "level": level_for_score(score),
            "issues": issues,
            "detail": {
                "joint_limit_count": constraint_module.get("joint_limit_count", 0),
                "velocity_limit_count": constraint_module.get("velocity_limit_count", 0),
                "max_angle_violation_ratio": round(max_angle, 4),
                "max_velocity_violation_ratio": round(max_vel, 4),
                "top_angle_violations": angle_violations[:3],
                "top_velocity_violations": velocity_violations[:3],
            },
        }

    def _score_alignment(self, editor, progress_cb=None):
        """维度 6: 时间对齐 (timestamp jitter + state-action lag)。"""
        if progress_cb:
            progress_cb("alignment", "正在分析时间对齐...", 6, 7)

        try:
            report = editor.build_joint_analysis_report()
        except Exception as e:
            return {"available": False, "score": 0.0, "issues": [f"分析失败: {e}"], "detail": {}}

        alignment = report.get("alignment", {})
        timestamp_info = alignment.get("timestamp", {})
        lag_info = (alignment.get("state_action_lag") or {})

        issues = []
        score = 100.0

        # timestamp jitter
        if timestamp_info.get("available"):
            nominal = timestamp_info.get("nominal_dt", 0)
            jitter_mean = timestamp_info.get("jitter_mean", 0)
            jitter_max = timestamp_info.get("jitter_max", 0)
            if nominal > 0:
                jitter_ratio = jitter_mean / nominal
                if jitter_ratio > 0.1:
                    score -= 30
                    issues.append(f"时间戳抖动较大: 平均 jitter = {jitter_ratio*100:.1f}% of dt")
                elif jitter_ratio > 0.05:
                    score -= 15
                    issues.append(f"时间戳存在轻微抖动 ({jitter_ratio*100:.1f}% of dt)")
                if jitter_max / nominal > 0.5:
                    score -= 10
                    issues.append(f"存在异常帧间隔 (max jitter {jitter_max*1000:.1f}ms)")
        else:
            score -= 20
            issues.append("缺少 timestamp 序列数据")

        # state-action lag
        if lag_info.get("available"):
            median_lag = abs(lag_info.get("median_lag_frames", 0))
            max_lag = lag_info.get("max_abs_lag_frames", 0)
            mean_corr = lag_info.get("mean_abs_correlation", 0)
            if median_lag >= 2:
                score -= 25
                issues.append(f"state-action 中位滞后 {median_lag} 帧，可能存在控制延迟")
            elif median_lag >= 1:
                score -= 10
            if mean_corr < 0.7:
                score -= 15
                issues.append(f"state-action 平均相关性 {mean_corr:.2f}，对齐较差")
        # lag 不可用不扣分 (某些数据集没有 action)

        score = _clamp(score)
        return {
            "available": True,
            "score": round(score, 1),
            "level": level_for_score(score),
            "issues": issues,
            "detail": {
                "timestamp": {
                    "nominal_dt": timestamp_info.get("nominal_dt"),
                    "jitter_mean": timestamp_info.get("jitter_mean"),
                    "jitter_max": timestamp_info.get("jitter_max"),
                },
                "state_action_lag": {
                    "median_lag_frames": lag_info.get("median_lag_frames"),
                    "max_abs_lag_frames": lag_info.get("max_abs_lag_frames"),
                    "mean_abs_correlation": lag_info.get("mean_abs_correlation"),
                } if lag_info.get("available") else None,
            },
        }

    def _score_image_quality(self, root_path, editor, progress_cb=None, sample_episodes=None):
        """维度 7: 图像质量综合分 (可选，需要解码视频)。"""
        if progress_cb:
            progress_cb("image_quality", "正在分析图像质量...", 7, 7)

        if self.ImageAnalyzer is None:
            return {
                "available": False,
                "score": 80.0,
                "level": "warn",
                "issues": ["图像质量分析模块未启用 (未安装 opencv-python)"],
                "detail": {},
            }

        try:
            analyzer = self.ImageAnalyzer(str(root_path))
            info = analyzer.get_dataset_info()
            cameras = info.get("cameras", [])
            if not cameras:
                return {
                    "available": False,
                    "score": 80.0,
                    "level": "warn",
                    "issues": ["数据集没有视频/相机通道，跳过图像质量评分"],
                    "detail": {},
                }

            camera = cameras[0]
            episodes = sample_episodes or info.get("episode_indices", [])[:5]

            # 轻量分析: 只采样少量 episode 的前 N 帧
            img_report = analyzer.analyze(
                camera, episodes=episodes,
                progress_cb=lambda *a: None,
            )

            overall = img_report.get("summary", {})
            avg_score = overall.get("quality_score", 0)
            problem_ratio = overall.get("problem_frame_ratio", 0)

            issues = []
            if avg_score < 50:
                issues.append(f"图像综合质量评分仅 {avg_score:.0f}，建议检查相机对焦/曝光")
            elif avg_score < 70:
                issues.append(f"图像质量一般 ({avg_score:.0f}分)，存在部分模糊/过曝帧")
            if problem_ratio > 0.1:
                issues.append(f"问题帧占比 {problem_ratio*100:.0f}%，可能影响视觉策略训练")

            score = _clamp(avg_score)

            return {
                "available": True,
                "score": round(score, 1),
                "level": level_for_score(score),
                "issues": issues,
                "detail": {
                    "camera": camera,
                    "sampled_episodes": len(episodes),
                    "avg_quality_score": round(avg_score, 1),
                    "problem_frame_ratio": round(problem_ratio, 3),
                },
            }
        except Exception as e:
            self.log.warning(f"图像质量分析失败: {e}")
            return {
                "available": False,
                "score": 70.0,
                "level": "warn",
                "issues": [f"图像质量分析执行失败: {e}"],
                "detail": {},
            }

    # ─── 主入口 ───

    def run_health_check(self, dataset_path, include_image_quality=True,
                         image_sample_episodes=3, progress_cb=None):
        """执行完整的健康度检查，返回综合报告。

        Args:
            dataset_path: LeRobot v2.1 数据集根目录
            include_image_quality: 是否包含图像质量维度 (耗时较长)
            image_sample_episodes: 图像质量分析采样多少个 episode
            progress_cb: 进度回调 (stage, title, current, total)
        """
        root = Path(dataset_path).expanduser().resolve()
        if not root.exists():
            raise FileNotFoundError(f"路径不存在: {root}")
        if not (root / "meta" / "info.json").exists():
            raise ValueError(f"无效的 LeRobot 数据集 (缺少 meta/info.json): {root}")

        if progress_cb:
            progress_cb("load", "正在加载数据集...", 0, 7)

        editor = self.DatasetEditor(str(root), joint_config=self._joint_config())

        dimensions = {}
        dimensions["integrity"] = self._score_integrity(editor, progress_cb)
        dimensions["completeness"] = self._score_completeness(root, editor, progress_cb)
        dimensions["smoothness"] = self._score_smoothness(editor, progress_cb)
        dimensions["coverage"] = self._score_coverage(editor, progress_cb)
        dimensions["constraint"] = self._score_constraint(editor, progress_cb)
        dimensions["alignment"] = self._score_alignment(editor, progress_cb)

        if include_image_quality:
            dimensions["image_quality"] = self._score_image_quality(
                root, editor, progress_cb,
                sample_episodes=list(range(min(image_sample_episodes, len(editor.episode_data))))
                if editor.episode_data else None,
            )
        else:
            dimensions["image_quality"] = {
                "available": False,
                "score": None,
                "level": "skip",
                "issues": ["用户选择跳过图像质量分析"],
                "detail": {},
            }

        # 计算加权总分
        total_score = 0.0
        total_weight = 0.0
        for dim_key, weight in DIMENSION_WEIGHTS.items():
            dim = dimensions.get(dim_key)
            if dim and dim.get("score") is not None and dim.get("available", True):
                total_score += dim["score"] * weight
                total_weight += weight

        final_score = round(total_score / total_weight, 1) if total_weight > 0 else 0
        grade_letter, grade_label, grade_color = grade_for_score(final_score)

        # 汇总所有问题
        all_issues = []
        for dim_key, dim in dimensions.items():
            for issue in dim.get("issues", []):
                all_issues.append({"dimension": dim_key, "message": issue})

        summary = editor.get_summary()

        if progress_cb:
            progress_cb("done", "健康度检查完成", 7, 7)

        return {
            "dataset_path": str(root),
            "overall_score": final_score,
            "grade": grade_letter,
            "grade_label": grade_label,
            "grade_color": grade_color,
            "dataset_summary": {
                "total_episodes": summary.get("total_episodes", 0),
                "total_frames": summary.get("total_frames", 0),
                "fps": summary.get("fps", 30),
                "robot_type": summary.get("robot_type", "unknown"),
                "cameras": summary.get("cameras", []),
            },
            "dimensions": dimensions,
            "weights": DIMENSION_WEIGHTS,
            "issues": all_issues,
            "issue_count": len(all_issues),
        }
