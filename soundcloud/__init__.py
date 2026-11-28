"""Omarchy SoundCloud backend package.

``soundcloud_app.py`` at the plugin root is the stable CLI/bar entry point.
``backend`` re-exports the public surface for tests and compatibility imports.
"""

from .cli import main

__all__ = ["main"]
