"""Security Patch Monitor: a calendar of upcoming and released security patches."""

from importlib.metadata import PackageNotFoundError, version

APP_NAME = "Security Patch Monitor"

try:
    __version__ = version("security-patch-monitor")
except PackageNotFoundError:  # running from a source checkout without installation
    __version__ = "0.0.0+local"

__all__ = ["APP_NAME", "__version__"]
