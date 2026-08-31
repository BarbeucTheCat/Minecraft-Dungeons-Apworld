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

IMPORTANT - reading the bytes: this used to hand spec_from_file_location
a plain filesystem path and let its default file loader open() it. That
only works when mcdungeons/ is an actual extracted folder on disk. When
Archipelago runs it straight out of custom_worlds/mcdungeons.apworld
(the normal case - the Launcher does NOT extract .apworld files, it
loads them as zip archives via zipimport), there is no real file at
that path to open, and it fails with a plain FileNotFoundError.

The fix: don't open() anything ourselves. __loader__ (a builtin every
module gets for free) is whatever loader actually loaded THIS file -
a zipimporter when running from the .apworld, or a normal
SourceFileLoader when running from an extracted folder/dev checkout.
Both implement get_data(path), and zipimporter's version specifically
knows how to translate a path that looks like
".../mcdungeons.apworld/mcdungeons/ZoneData.py" into the right entry
inside the archive. Reusing that same loader object - instead of
building a fresh, filesystem-only one - makes this work in both cases
without having to detect which one we're in.
"""

import pathlib
import sys
import types

_WORLD_DIR = pathlib.Path(__file__).resolve().parent.parent  # .../mcdungeons/
_PKG_NAME = "_mcdungeons_data"  # synthetic - never collides with a real install
_LOADER = __loader__  # whatever loaded _apworld_data.py itself - zip-aware or not


def _load(modname: str, filename: str):
    full_name = f"{_PKG_NAME}.{modname}"
    target_path = str(_WORLD_DIR / filename)
    source = _LOADER.get_data(target_path)  # works for zipimporter AND regular file loaders
    code = compile(source, target_path, "exec")
    module = types.ModuleType(full_name)
    module.__file__ = target_path
    sys.modules[full_name] = module  # register BEFORE exec so relative imports inside it resolve
    exec(code, module.__dict__)
    return module


if _PKG_NAME in sys.modules:
    _pkg = sys.modules[_PKG_NAME]
else:
    _pkg = types.ModuleType(_PKG_NAME)
    _pkg.__path__ = [str(_WORLD_DIR)]
    sys.modules[_PKG_NAME] = _pkg

    # ZoneData first - Locations.py's own "from .ZoneData import ..." (a
    # relative import) resolves against sys.modules[f"{_PKG_NAME}.ZoneData"],
    # which only exists once we've loaded and registered it here. Items.py
    # has the exact same relative-import dependency on ZoneData.
    _pkg.ZoneData = _load("ZoneData", "ZoneData.py")
    _pkg.Locations = _load("Locations", "Locations.py")
    _pkg.Items = _load("Items", "Items.py")

ZoneData = _pkg.ZoneData
Locations = _pkg.Locations
Items = _pkg.Items
