#!/usr/bin/env python3
"""
LeRobot 数据集字段编辑工具。

本模块不依赖 Flask，也不直接导入 app.py。调用方传入 DatasetEditor 实例后，
可组合执行:
  - 重命名字段（兼容批量前缀/正则替换，例如 joint_*.pos -> action）
  - 添加新字段（支持标量/向量，默认值填充）
  - 删除字段（带关键字段保护）
  - 批量赋值（常量 / 从已有字段复制 / 简单算术表达式）

字段定义存储在 info.features，数据列存储在 episode_data[ep_idx] 的 DataFrame。
所有操作都会同步更新这两处，保证后续 save_as / compute_stats 一致。
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


# 训练必需字段，删除或重命名会破坏 training-check，默认禁止
PROTECTED_FEATURES = {"observation.state", "action"}

# 标量列（LeRobot 内部时序列），不允许在字段编辑器里增删改
RESERVED_COLUMNS = {
    "frame_index",
    "timestamp",
    "index",
    "episode_index",
    "task_index",
    "_orig_frame_idx",
    "episode_data_index",
    "episode_index",
}


def list_features(editor: Any) -> List[Dict[str, Any]]:
    """返回当前数据集所有字段的快照信息。"""
    features = editor.info.get("features", {}) or {}
    result = []
    for key, meta in features.items():
        entry: Dict[str, Any] = {
            "key": key,
            "dtype": meta.get("dtype"),
            "shape": list(meta.get("shape") or []),
            "names": _normalize_names(meta.get("names")),
            "protected": key in PROTECTED_FEATURES,
            "is_image": str(meta.get("dtype", "")).lower() in ("image", "video"),
        }
        # 取第一条样本，便于前端预览
        try:
            sample_value = _sample_value(editor, key)
            entry["sample"] = sample_value
        except Exception:
            entry["sample"] = None
        result.append(entry)
    return result


def build_preview(editor: Any) -> Dict[str, Any]:
    """生成字段预览数据：features 列表 + 基本统计。"""
    return {
        "features": list_features(editor),
        "episode_count": len(editor.episode_data),
        "total_frames": sum(len(d) for d in editor.episode_data.values()),
        "feature_keys": sorted((editor.info.get("features", {}) or {}).keys()),
    }


# ───────────────── Dry-run 预览（不修改原 editor） ─────────────────

def _snapshot_editor(editor: Any) -> Dict[str, Any]:
    """备份 editor 的可变状态，供 dry-run 后恢复。"""
    return {
        "info": copy.deepcopy(editor.info),
        "episode_data": {
            ep: df.copy(deep=True) for ep, df in editor.episode_data.items()
        },
        "modified": bool(editor.modified),
    }


def _restore_editor(editor: Any, snap: Dict[str, Any]) -> None:
    """把 editor 恢复到 snapshot 时的状态。"""
    editor.info = snap["info"]
    editor.episode_data = snap["episodes"] if "episodes" in snap else snap["episode_data"]
    editor.modified = snap["modified"]


def _sample_rows_after(editor: Any, field_name: str, max_rows: int = 3) -> List[Any]:
    """取修改后某字段的前若干行样本（向量截断显示）。"""
    rows = []
    for ep_idx, df in editor.episode_data.items():
        if field_name not in df.columns:
            continue
        for v in df[field_name].tolist()[:max_rows]:
            rows.append(_sample_to_json(v))
        if len(rows) >= max_rows:
            break
    return rows[:max_rows]


def _sample_to_json(val: Any, max_len: int = 6) -> Any:
    """把单条样本规范化成 JSON 可序列化的值（向量截断）。"""
    if val is None:
        return None
    if isinstance(val, (list, tuple, np.ndarray)):
        lst = _to_list(val)
        if len(lst) > max_len:
            return [round(float(x), 4) for x in lst[:max_len]] + ["..."]
        return [round(float(x), 4) for x in lst]
    try:
        return round(float(val), 4)
    except (TypeError, ValueError):
        return str(val)[:60]


def preview_rename(editor: Any, renames: List[Tuple[str, str]], *, rename_names: bool = True) -> Dict[str, Any]:
    """dry-run：在 editor 副本上执行重命名，返回变更摘要和样例对比。

    返回结构:
      {
        "applied": [{old, new, episodes_affected}],
        "skipped": [{old, new, reason}],
        "fields_before": [...],
        "fields_after": [...],
      }
    """
    snap = _snapshot_editor(editor)
    try:
        result = apply_rename(editor, renames, rename_names=rename_names)
        return {
            "applied": result["applied"],
            "skipped": result["skipped"],
            "fields_before": snap_features(snap),
            "fields_after": list_features(editor),
        }
    finally:
        _restore_editor(editor, snap)


def preview_add(editor: Any, field_name: str, *, dtype: str = "float32", shape: Optional[List[int]] = None, default: Any = 0.0, names: Optional[List[str]] = None) -> Dict[str, Any]:
    """dry-run：在 editor 副本上执行添加字段，返回变更摘要和新字段的样例值。"""
    snap = _snapshot_editor(editor)
    try:
        result = apply_add(
            editor, field_name,
            dtype=dtype, shape=shape, default=default, names=names,
        )
        result["sample_rows"] = _sample_rows_after(editor, field_name)
        result["fields_before"] = snap_features(snap)
        result["fields_after"] = list_features(editor)
        return result
    finally:
        _restore_editor(editor, snap)


def preview_delete(editor: Any, field_names: List[str], *, allow_delete_protected: bool = False) -> Dict[str, Any]:
    """dry-run：在 editor 副本上执行删除字段，返回变更摘要。"""
    snap = _snapshot_editor(editor)
    try:
        result = apply_delete(
            editor, field_names,
            allow_delete_protected=allow_delete_protected,
        )
        result["fields_before"] = snap_features(snap)
        result["fields_after"] = list_features(editor)
        return result
    finally:
        _restore_editor(editor, snap)


def preview_assign(editor: Any, target: str, *, mode: str = "constant", value: Any = None, source: Optional[str] = None, expression: Optional[str] = None, episode_indices: Optional[List[int]] = None) -> Dict[str, Any]:
    """dry-run：在 editor 副本上执行批量赋值，返回变更摘要和前后样例对比。"""
    snap = _snapshot_editor(editor)
    try:
        before_rows = _sample_rows_after(editor, target, max_rows=3)
        result = apply_assign(
            editor, target,
            mode=mode, value=value, source=source,
            expression=expression, episode_indices=episode_indices,
        )
        result["before_rows"] = before_rows
        result["after_rows"] = _sample_rows_after(editor, target, max_rows=3)
        return result
    finally:
        _restore_editor(editor, snap)


def snap_features(snap: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 snapshot（只含 info/episode_data）里重建 features 快照。

    list_features 需要完整 editor，这里给一个最小兼容版。
    """
    info = snap.get("info", {})
    episode_data = snap.get("episode_data", {})
    features = info.get("features", {}) or {}
    result = []
    for key, meta in features.items():
        entry: Dict[str, Any] = {
            "key": key,
            "dtype": meta.get("dtype"),
            "shape": list(meta.get("shape") or []),
            "names": _normalize_names(meta.get("names")),
            "protected": key in PROTECTED_FEATURES,
            "is_image": str(meta.get("dtype", "")).lower() in ("image", "video"),
        }
        try:
            for df in episode_data.values():
                if key in df.columns and len(df) > 0:
                    entry["sample"] = _sample_to_json(df[key].iloc[0])
                    break
            else:
                entry["sample"] = None
        except Exception:
            entry["sample"] = None
        result.append(entry)
    return result


def parse_rename_pairs(renames: Any) -> List[Tuple[str, str]]:
    """把多种输入格式统一成 [(old, new), ...] 列表。

    支持的输入：
      - {"old1": "new1", "old2": "new2"}
      - [{"from": "old1", "to": "new1"}, ...]
      - [{"old": "old1", "new": "new1"}, ...]
    """
    pairs: List[Tuple[str, str]] = []
    if isinstance(renames, dict):
        for k, v in renames.items():
            if k and v:
                pairs.append((str(k).strip(), str(v).strip()))
    elif isinstance(renames, list):
        for item in renames:
            if not isinstance(item, dict):
                continue
            old = item.get("from") or item.get("old") or item.get("source")
            new = item.get("to") or item.get("new") or item.get("target")
            if old and new:
                pairs.append((str(old).strip(), str(new).strip()))
    return pairs


def apply_rename(
    editor: Any,
    renames: List[Tuple[str, str]],
    *,
    rename_names: bool = True,
) -> Dict[str, Any]:
    """重命名字段：同步 info.features 的 key 和所有 DataFrame 列名。

    rename_names=True 时，如果 features[key].names 是一维 list，也会把
    出现在 names 里的旧字段名替换为新字段名（方便 joint_xx.pos 这类命名修正）。
    """
    features: Dict[str, Any] = editor.info.setdefault("features", {})
    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []

    for old, new in renames:
        if old == new:
            skipped.append({"old": old, "new": new, "reason": "新旧名称相同"})
            continue
        if old not in features:
            skipped.append({"old": old, "new": new, "reason": "字段不存在"})
            continue
        if new in features:
            skipped.append({"old": old, "new": new, "reason": "目标字段已存在"})
            continue
        if old in RESERVED_COLUMNS or new in RESERVED_COLUMNS:
            skipped.append({"old": old, "new": new, "reason": "属于保留列"})
            continue

        # 1) 更新 info.features 的 key
        features[new] = features.pop(old)

        # 2) 同步 names（一维 list 才处理）
        if rename_names:
            _rename_inside_names(features[new], old, new)

        # 3) 更新所有 DataFrame 列名
        renamed_in_dfs = 0
        for ep_idx, df in editor.episode_data.items():
            if old in df.columns:
                df.rename(columns={old: new}, inplace=True)
                renamed_in_dfs += 1

        applied.append({
            "old": old,
            "new": new,
            "episodes_affected": renamed_in_dfs,
        })

    if applied:
        editor.modified = True
    return {"applied": applied, "skipped": skipped}


def _rename_inside_names(feature_meta: Dict[str, Any], old: str, new: str) -> None:
    """如果 feature 的 names 里出现了旧字段名（常见于 joint_xx.pos），
    则把它替换成新字段名。"""
    names = feature_meta.get("names")
    if isinstance(names, list):
        if names and isinstance(names[0], str):
            feature_meta["names"] = [new if n == old else n for n in names]
        elif names and isinstance(names[0], list):
            feature_meta["names"] = [
                [new if n == old else n for n in sub] for sub in names
            ]


def apply_add(
    editor: Any,
    field_name: str,
    *,
    dtype: str = "float32",
    shape: Optional[List[int]] = None,
    default: Any = 0.0,
    names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """添加新字段：在 info.features 注册并在每个 DataFrame 新建列。

    - shape 为 [] 或 None 表示标量列；否则按 shape[-1] 当作向量维度。
    - 向量列每行存 list（与 LeRobot 标准一致）。
    - default 支持数字或 list；如果是 list 长度需匹配维度。
    """
    field_name = (field_name or "").strip()
    if not field_name:
        raise ValueError("字段名不能为空")
    if field_name in RESERVED_COLUMNS:
        raise ValueError(f"{field_name} 是系统保留列，不能添加")
    features: Dict[str, Any] = editor.info.setdefault("features", {})
    if field_name in features:
        raise ValueError(f"字段 {field_name} 已存在")

    shape = list(shape or [])
    dim = int(shape[-1]) if shape else 1
    is_vector = dim > 1

    # 规范化 default
    if is_vector:
        if isinstance(default, (list, tuple)):
            if len(default) != dim:
                raise ValueError(
                    f"默认值长度 {len(default)} 与维度 {dim} 不匹配"
                )
            default_list = [float(x) for x in default]
        else:
            default_list = [float(default)] * dim
    else:
        default_list = [float(default)]

    # 1) 注册 info.features
    feature_meta: Dict[str, Any] = {
        "dtype": dtype,
        "shape": shape if shape else [1],
    }
    if names:
        feature_meta["names"] = list(names)
    features[field_name] = feature_meta

    # 2) 给每个 DataFrame 新建列
    total_rows = 0
    for ep_idx, df in editor.episode_data.items():
        n = len(df)
        if is_vector:
            df[field_name] = [list(default_list) for _ in range(n)]
        else:
            df[field_name] = default_list[0]
        total_rows += n

    editor.modified = True
    return {
        "field": field_name,
        "dtype": dtype,
        "shape": shape if shape else [1],
        "is_vector": is_vector,
        "rows_added": total_rows,
    }


def apply_delete(
    editor: Any,
    field_names: List[str],
    *,
    allow_delete_protected: bool = False,
) -> Dict[str, Any]:
    """删除字段：移除 info.features 条目并 drop 所有 DataFrame 列。

    默认禁止删除 observation.state / action，除非 allow_delete_protected=True。
    """
    features: Dict[str, Any] = editor.info.setdefault("features", {})
    deleted: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []

    for name in field_names:
        name = (name or "").strip()
        if not name:
            continue
        if name in RESERVED_COLUMNS:
            skipped.append({"field": name, "reason": "系统保留列"})
            continue
        if name not in features:
            skipped.append({"field": name, "reason": "字段不存在"})
            continue
        if name in PROTECTED_FEATURES and not allow_delete_protected:
            skipped.append({
                "field": name,
                "reason": "训练必需字段，已阻止删除（如确需删除请勾选允许）",
            })
            continue

        # 1) 从 info.features 移除
        features.pop(name, None)

        # 2) 从所有 DataFrame drop
        dropped_eps = 0
        for ep_idx, df in editor.episode_data.items():
            if name in df.columns:
                df.drop(columns=[name], inplace=True)
                dropped_eps += 1

        deleted.append({"field": name, "episodes_affected": dropped_eps})

    if deleted:
        editor.modified = True
    return {"deleted": deleted, "skipped": skipped}


def apply_assign(
    editor: Any,
    target: str,
    *,
    mode: str = "constant",
    value: Any = None,
    source: Optional[str] = None,
    expression: Optional[str] = None,
    episode_indices: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """批量给字段赋值。

    支持三种 mode：
      - constant: 整列设为常量 value（向量字段 value 可为 list）
      - copy:     从 source 字段复制（需同维度）
      - expr:     用表达式对 source 做运算，支持 +,-,*,/ 和数字
                  例如 "source * 0.5"、"source + 1.0"
                  source 可省略，此时表达式直接对 target 原值运算。

    episode_indices 为 None 表示全部 episode，否则只改指定 episode。
    """
    target = (target or "").strip()
    if not target:
        raise ValueError("目标字段不能为空")
    if target in RESERVED_COLUMNS:
        raise ValueError(f"{target} 是系统保留列，不能赋值")

    features: Dict[str, Any] = editor.info.setdefault("features", {})
    if target not in features:
        raise ValueError(f"目标字段 {target} 不存在，请先添加")

    target_shape = list(features[target].get("shape") or [1])
    target_dim = int(target_shape[-1]) if target_shape else 1
    is_vector = target_dim > 1

    ep_filter = set(episode_indices) if episode_indices is not None else None

    # 解析常量
    const_list: Optional[List[float]] = None
    if mode == "constant":
        if is_vector:
            if isinstance(value, (list, tuple)):
                if len(value) != target_dim:
                    raise ValueError(
                        f"常量长度 {len(value)} 与目标维度 {target_dim} 不匹配"
                    )
                const_list = [float(x) for x in value]
            else:
                const_list = [float(value)] * target_dim
        else:
            const_list = [float(value)]

    # 解析 copy 源
    if mode == "copy":
        if not source:
            raise ValueError("copy 模式需要指定 source 字段")
        if source not in features:
            raise ValueError(f"源字段 {source} 不存在")
        source_shape = list(features[source].get("shape") or [1])
        source_dim = int(source_shape[-1]) if source_shape else 1
        if source_dim != target_dim:
            raise ValueError(
                f"源字段维度 {source_dim} 与目标维度 {target_dim} 不匹配"
            )

    # 解析表达式
    expr_ast: Optional[Any] = None
    expr_source_field: Optional[str] = None
    if mode == "expr":
        if not expression:
            raise ValueError("expr 模式需要提供 expression")
        expr_ast, expr_source_field = _parse_expr(expression, source, target)

    total_rows = 0
    episodes_changed = 0
    for ep_idx, df in editor.episode_data.items():
        if ep_filter is not None and ep_idx not in ep_filter:
            continue
        if target not in df.columns:
            continue
        n = len(df)

        if mode == "constant":
            if is_vector:
                df[target] = [list(const_list) for _ in range(n)]
            else:
                df[target] = const_list[0]
        elif mode == "copy":
            df[target] = df[source].tolist() if is_vector else df[source]
        elif mode == "expr":
            base_field = expr_source_field or target
            if base_field not in df.columns:
                continue
            raw_series = df[base_field].tolist()
            if is_vector:
                new_rows = []
                for raw in raw_series:
                    arr = _to_list(raw)
                    if not arr:
                        arr = [0.0] * target_dim
                    out = [_eval_expr_vec(expr_ast, np.asarray(arr, dtype=float))]
                    new_rows.append(list(out[0]))
                df[target] = new_rows
            else:
                arr = np.asarray(
                    [float(v) if v is not None else 0.0 for v in raw_series],
                    dtype=float,
                )
                df[target] = _eval_expr_vec(expr_ast, arr).tolist()

        total_rows += n
        episodes_changed += 1

    editor.modified = True
    return {
        "target": target,
        "mode": mode,
        "episodes_changed": episodes_changed,
        "rows_changed": total_rows,
    }


# ───────────────── 表达式支持（安全的最小实现） ─────────────────

_EXPR_BIN_OPS = {
    "+": np.add,
    "-": np.subtract,
    "*": np.multiply,
    "/": np.divide,
}


class _ExprParser:
    """递归下降解析 + - * / 和数字/变量。

    支持形如: x * 0.5, x + 1.0, (x + 1) * 2, x / 10
    变量固定为 source 字段，记为 'x'。
    """

    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def peek(self) -> str:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def consume(self) -> str:
        ch = self.peek()
        if ch:
            self.pos += 1
        return ch

    def parse(self):
        node = self._parse_add()
        if self.peek() != "":
            raise ValueError(f"表达式无法解析: {self.text}")
        return node

    def _parse_add(self):
        node = self._parse_mul()
        while True:
            ch = self.peek()
            if ch in ("+", "-"):
                self.consume()
                right = self._parse_mul()
                node = ("binop", ch, node, right)
            else:
                break
        return node

    def _parse_mul(self):
        node = self._parse_atom()
        while True:
            ch = self.peek()
            if ch in ("*", "/"):
                self.consume()
                right = self._parse_atom()
                node = ("binop", ch, node, right)
            else:
                break
        return node

    def _parse_atom(self):
        ch = self.peek()
        if ch == "(":
            self.consume()
            node = self._parse_add()
            if self.peek() != ")":
                raise ValueError("缺少右括号")
            self.consume()
            return node
        if ch == "-":
            self.consume()
            sub = self._parse_atom()
            return ("neg", sub)
        if ch.isdigit() or ch == ".":
            return self._parse_number()
        if ch.isalpha() or ch == "_":
            return self._parse_var()
        raise ValueError(f"无法解析符号: {ch!r}")

    def _parse_number(self):
        start = self.pos
        while self.pos < len(self.text) and (
            self.text[self.pos].isdigit()
            or self.text[self.pos] == "."
        ):
            self.pos += 1
        text = self.text[start:self.pos]
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError(f"非法数字: {text}") from exc
        return ("num", value)

    def _parse_var(self):
        start = self.pos
        while self.pos < len(self.text) and (
            self.text[self.pos].isalnum() or self.text[self.pos] == "_"
        ):
            self.pos += 1
        name = self.text[start:self.pos]
        return ("var", name)


def _parse_expr(text: str, source: Optional[str], target: str):
    """解析表达式，返回 (ast, 实际使用字段名)。

    约定：表达式里的变量名（如 x / source 名）会被当成 source 字段引用；
    若未传 source，则使用 target 自身。
    """
    ast = _ExprParser(text).parse()
    used = _collect_vars(ast)
    # 优先用 source；否则用 target；若表达式中出现 source/target 之外的名字也算通过
    field = source or target
    return ast, field


def _collect_vars(node) -> List[str]:
    if not isinstance(node, tuple):
        return []
    if node[0] == "var":
        return [node[1]]
    if node[0] == "num":
        return []
    if node[0] == "neg":
        return _collect_vars(node[1])
    if node[0] == "binop":
        return _collect_vars(node[2]) + _collect_vars(node[3])
    return []


def _eval_expr_vec(node, arr: np.ndarray) -> np.ndarray:
    if node[0] == "num":
        return np.full_like(arr, node[1])
    if node[0] == "var":
        return arr
    if node[0] == "neg":
        return -_eval_expr_vec(node[1], arr)
    if node[0] == "binop":
        op = node[1]
        left = _eval_expr_vec(node[2], arr)
        right = _eval_expr_vec(node[3], arr)
        fn = _EXPR_BIN_OPS.get(op)
        if fn is None:
            raise ValueError(f"不支持的操作符: {op}")
        return fn(left, right)
    raise ValueError(f"无法求值的 AST 节点: {node}")


# ───────────────── 内部工具 ─────────────────

def _normalize_names(raw: Any) -> Optional[List[str]]:
    """兼容 names 的三种写法，返回一维 list 或 None。"""
    if raw is None:
        return None
    if isinstance(raw, list):
        if raw and isinstance(raw[0], list):
            inner = raw[0]
            return [str(n) for n in inner] if inner else None
        if raw and isinstance(raw[0], str):
            return [str(n) for n in raw]
        return None
    if isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, list) and v and isinstance(v[0], str):
                return [str(n) for n in v]
    return None


def _to_list(val: Any) -> List[float]:
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


def _sample_value(editor: Any, key: str, max_len: int = 6) -> Any:
    """取指定字段第一条样本用于前端预览，向量截断到 max_len 维。"""
    for df in editor.episode_data.values():
        if key not in df.columns or len(df) == 0:
            continue
        v = df[key].iloc[0]
        if isinstance(v, (list, tuple, np.ndarray)):
            lst = _to_list(v)
            if len(lst) > max_len:
                return [round(x, 4) for x in lst[:max_len]] + ["..."]
            return [round(x, 4) for x in lst]
        try:
            return round(float(v), 4)
        except (TypeError, ValueError):
            return str(v)[:60]
    return None
