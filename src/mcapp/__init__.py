import subprocess
from importlib.metadata import version


def _get_version() -> str:
    """Get version from git describe (includes dev tag), falling back to package metadata."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags"],  # noqa: S607 - fixed internal command
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0:
            tag = result.stdout.strip()
            if tag.startswith("v"):
                return tag[1:]
            return tag
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return version("mcapp")


__version__ = _get_version()
