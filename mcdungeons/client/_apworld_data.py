"""
Loads Locations.py and ZoneData.py DIRECTLY from the parent apworld folder
(one level up from this client/ folder) - by file path, not via a normal
package import. Importing `mcdungeons` (or `ap_world`, its old working
name) as a real package would run its __init__.py first, which imports
BaseClasses / worlds.AutoWorld - only present inside a full Archipelago
install, which the client machine doesn't need and generally won't have.

Locations.py and ZoneData.py are the only two modules in this world with
zero AP-core dependency (pure dataclasses/typing + each other) - they're
the only ones safe to load this way. Options.py/Items.py/Regions.py all
touch BaseClasses or Options one way or another and are generation-only.

Both dungeons_reader.py and dungeons_ap_client.py import from here rather
than each hand-rolling this loader, and rather than hand-maintaining a
separate mirror of Locations.py's tables (an earlier version of this
project did exactly that, in ap_world_locations_ADDITION.py - it's gone
now; this loader reaches the ONE real copy of Locations.py directly, so
there's nothing left to keep in sync by hand).

Usage:
    from _apworld_data import Locations, ZoneData
    Locations.get_emerald_milestone_id(1000)
    ZoneData.ZONES_BY_NAME["squidcoast"]

How it loads them: a synthetic package (named "_mcdungeons_data", which never
collides with a real install) is registered whose search path is the world
folder, then each module is pulled in with a normal importlib.import_module().
Python's own import machinery does the rest: it reads the file from an
extracted folder OR straight out of custom_worlds/mcdungeons.apworld (the
Launcher does NOT extract .apworld files, they are zip archives, and the
standard zipimport hook handles that path). Nothing is compiled or run by hand
here (no dynamic code execution) and only the three fixed files below are ever
imported, all of them shipped inside this same apworld.
"""

import importlib
import pathlib
import sys
import types

_WORLD_DIR = pathlib.Path(__file__).resolve().parent.parent  # .../mcdungeons/ (or .../mcdungeons.apworld/mcdungeons/)
_PKG_NAME = "_mcdungeons_data"  # synthetic - never collides with a real install

if _PKG_NAME in sys.modules:
    _pkg = sys.modules[_PKG_NAME]
else:
    _pkg = types.ModuleType(_PKG_NAME)
    _pkg.__path__ = [str(_WORLD_DIR)]  # where `import _mcdungeons_data.X` looks for X.py
    sys.modules[_PKG_NAME] = _pkg

    # ZoneData first: Locations.py's own "from .ZoneData import ..." (a relative import) resolves to
    # _mcdungeons_data.ZoneData, and Items.py has the exact same dependency on it.
    for _name in ("ZoneData", "Locations", "Items"):
        setattr(_pkg, _name, importlib.import_module(f"{_PKG_NAME}.{_name}"))

ZoneData = _pkg.ZoneData
Locations = _pkg.Locations
Items = _pkg.Items
