"""Make the repo's custom_components visible to the HA test harness.

The harness ships its own ``custom_components`` package for its fixtures, which
would otherwise hide this repository's integration. Without the harness
installed (engine tests only) this does nothing.
"""
import pathlib

try:
    import custom_components
except ImportError:  # harness not installed: only the pure engine tests will run
    custom_components = None

if custom_components is not None:
    _repo = str(pathlib.Path(__file__).parents[1] / "custom_components")
    if _repo not in list(custom_components.__path__):
        custom_components.__path__.append(_repo)
