"""PyInstaller entry point — frozen apps run scripts as __main__, where
`from .cli import ...` has no package context."""

from cpa_stats.cli import entrypoint

if __name__ == "__main__":
    entrypoint()
