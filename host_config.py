import json
import os
import tempfile
from typing import Any, Dict


def _deep_merge(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def load_config(path: str, defaults: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

    if defaults:
        merged = json.loads(json.dumps(defaults))
        _deep_merge(merged, cfg)  # cfg overrides defaults
        return merged
    return cfg


def save_config(path: str, cfg: Dict[str, Any]) -> None:
    abs_path = os.path.abspath(path)
    dir_path = os.path.dirname(abs_path)
    os.makedirs(dir_path, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=".host_config_", suffix=".json", dir=dir_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, abs_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def get_path(cfg: Dict[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_path(cfg: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = cfg
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value
