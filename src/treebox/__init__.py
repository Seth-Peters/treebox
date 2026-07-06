"""treebox: isolated, ready-to-run git worktrees for AI coding agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("treebox")
except PackageNotFoundError:  # not installed (e.g. running from a bare source tree)
    __version__ = "0+unknown"
