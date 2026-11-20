"""treebox: isolated, ready-to-run git worktrees for AI coding agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("treebox")
except PackageNotFoundError:
    __version__ = "0+unknown"
