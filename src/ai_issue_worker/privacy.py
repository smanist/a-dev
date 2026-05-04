from __future__ import annotations

import os
import re
from pathlib import Path


def sanitize_user_paths(text: str) -> str:
    """Mask local user-home path prefixes before text leaves the machine."""
    if not text:
        return text

    sanitized = text
    home_paths = {str(Path.home())}
    real_home = os.path.realpath(str(Path.home()))
    if real_home:
        home_paths.add(real_home)
    for home in sorted(home_paths, key=len, reverse=True):
        if home and home != os.sep:
            sanitized = sanitized.replace(home, "####")

    sanitized = re.sub(r"/Users/[^/\s`'\"<>)]+", "####", sanitized)
    sanitized = re.sub(r"/home/[^/\s`'\"<>)]+", "####", sanitized)
    sanitized = re.sub(r"[A-Za-z]:\\Users\\[^\\\s`'\"<>)]+", "####", sanitized)
    return sanitized
