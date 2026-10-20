"""Файлы параметров ArduPilot: чтение, фильтр калибровок, сравнение."""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

TOKEN_SPLIT = re.compile(r"[,\s]+")


def parse_parm(text: str) -> dict[str, float]:
    """Формат Mission Planner (NAME,VALUE) и QGC (sysid compid NAME VALUE type)."""
    params: dict[str, float] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = TOKEN_SPLIT.split(line)
        if len(tokens) >= 5 and tokens[0].isdigit() and tokens[1].isdigit():
            name, value = tokens[2], tokens[3]
        elif len(tokens) >= 2:
            name, value = tokens[0], tokens[1]
        else:
            raise ValueError(f"строка {lineno}: не похоже на параметр: {raw!r}")
        params[name] = float(value)
    return params


def read_parm(path: Path) -> dict[str, float]:
    return parse_parm(path.read_text("utf-8")) if path.exists() else {}


def format_parm(params: dict[str, float], header: str = "") -> str:
    lines = [f"# {line}" for line in header.splitlines()]
    lines += [f"{name},{value:.7g}" for name, value in sorted(params.items())]
    return "\n".join(lines) + "\n"


def load_ignore(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text("utf-8").splitlines() if line.strip() and not line.startswith("#")]


def ignored(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def expected_params(base: dict[str, float], overrides: dict[str, float], patterns: list[str]) -> dict[str, float]:
    """Эталон для конкретного робота: база без калибровок + параметры fleet."""
    merged = {k: v for k, v in base.items() if not ignored(k, patterns)}
    merged.update(overrides)
    return merged


def close(a: float, b: float, tol: float = 1e-4) -> bool:
    # ArduPilot хранит float32: 0.1 возвращается как 0.100000001
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def diff(expected: dict[str, float], actual: dict[str, float]) -> list[tuple[str, float, float | None]]:
    """Параметры, которые отличаются от эталона или отсутствуют в прошивке."""
    out = []
    for name, exp in sorted(expected.items()):
        act = actual.get(name)
        if act is None or not close(exp, act):
            out.append((name, exp, act))
    return out
