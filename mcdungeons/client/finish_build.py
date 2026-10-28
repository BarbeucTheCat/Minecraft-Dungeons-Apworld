"""Run this right after building dungeons_bridge.dll: python finish_build.py [dll] [map]

It reads the build id compiled into the DLL (DBBUILD:<date> <time>) and saves the linker's .map file under that id
(dungeons_bridge_<build>.map, next to the DLL). Without it, a crash report from an older build cannot be turned into
function names once you have rebuilt. The last 25 maps are kept.

MSVC: add /MAP to the linker options (Project > Properties > Linker > Debugging > Generate Map File = Yes) so the
build writes dungeons_bridge.map next to dungeons_bridge.dll.
"""
import shutil
import sys
from pathlib import Path

from crash_report import read_dll_build, build_slug

KEEP = 25


def main(argv):
    dll = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent / "dungeons_bridge.dll"
    if not dll.exists():
        print(f"DLL not found: {dll}")
        return 1
    build = read_dll_build(dll)
    if not build:
        print(f"{dll} has no DBBUILD: marker - it was built from an older dungeons_bridge.cpp.")
        return 1
    map_path = Path(argv[2]) if len(argv) > 2 else dll.with_name("dungeons_bridge.map")
    print(f"DLL build id: {build}")
    if not map_path.exists():
        print(f"No map file at {map_path} - enable the linker option /MAP and build again.")
        return 1
    target = dll.with_name(f"dungeons_bridge_{build_slug(build)}.map")
    if target.resolve() != map_path.resolve():
        shutil.copyfile(map_path, target)
    print(f"Saved {target.name}")
    maps = sorted(dll.parent.glob("dungeons_bridge_*.map"), key=lambda p: p.stat().st_mtime)
    for old in maps[:-KEEP]:
        old.unlink()
        print(f"Removed old map {old.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
