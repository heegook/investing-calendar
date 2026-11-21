"""설정 및 환경변수 로딩"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


@lru_cache(maxsize=None)
def load_yaml(name: str) -> dict[str, Any]:
    with open(PROJECT_ROOT / "config" / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def settings() -> dict[str, Any]:
    return load_yaml("settings.yaml")


def scoring_weights() -> dict[str, Any]:
    return load_yaml("scoring_weights.yaml")


def path(key: str) -> Path:
    p = PROJECT_ROOT / settings()["paths"][key]
    if key != "database":
        p.mkdir(parents=True, exist_ok=True)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
    return p


def env(name: str) -> str | None:
    v = os.getenv(name)
    return v.strip() if v and v.strip() else None


def setup_logging(name: str = "kvsa") -> logging.Logger:
    log_dir = path("log_dir")
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh = logging.FileHandler(log_dir / f"{name}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger
