from __future__ import annotations

import os
import subprocess
from os import PathLike
from typing import Any, Mapping, Sequence


def safe_subprocess_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    merged = os.environ.copy()
    merged["PYTHONIOENCODING"] = "utf-8"
    merged["PYTHONUTF8"] = "1"
    if env:
        merged.update(env)
    return merged


def safe_popen(
    cmd: Sequence[str | PathLike[str]],
    *,
    env: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> subprocess.Popen[str]:
    provided_env = env or kwargs.pop("env", None)
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.STDOUT)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "backslashreplace")
    kwargs["env"] = safe_subprocess_env(provided_env)
    return subprocess.Popen(cmd, **kwargs)


def safe_run(
    cmd: Sequence[str | PathLike[str]],
    *,
    env: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    provided_env = env or kwargs.pop("env", None)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "backslashreplace")
    kwargs["env"] = safe_subprocess_env(provided_env)
    return subprocess.run(cmd, **kwargs)
