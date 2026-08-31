# Minecraft Dungeons - Archipelago Integration

An unofficial [Archipelago](https://archipelago.gg) multiworld randomizer
integration for Minecraft Dungeons. There is no mods needed only the apworld.
It use memory-reading + an injected DLL that hooks to the game to detect game events.

## Download

Grab the latest `.apworld` from the [Releases](../../releases) page.

## Setup (playing)

1. Drop the `.apworld` into your Archipelago install's `custom_worlds/` folder.
2. Install the client's Python dependencies (once) - `pymem` and `pywin32` aren't part of the base Archipelago install, since no other world needs memory-reading or named pipes:

   pip install pymem websocket-client pywin32

3. Open the Archipelago Launcher - a "Minecraft Dungeons Client" button appears automatically. Click it (with `Dungeons.exe` running, past the main menu / in a level).

That's it - the client injects `dungeons_bridge.dll` and connects on its own; there's no separate injection step or script to run by hand. It opens a normal Archipelago client window - connect the same way as any other game's client (server address, slot name).

## Antivirus false positives

`auto_inject.py` uses `CreateRemoteThread` + `LoadLibraryA` - the same
primitive game trainers, overlays, and mod loaders use, which some
antivirus heuristics flag even when nothing malicious is happening.
`dungeons_bridge.dll` is code-signed to reduce (not eliminate) this. If
your antivirus blocks it, add an exclusion for this folder, or check
the release notes for a link to submit a false-positive report.

## Development

See [`mcdungeons/client/README.md`](mcdungeons/client/README.md) for
how the client is put together internally (why it can load generator
tables without a full Archipelago install, how the DLL hook works,
what each file does).

### Building `dungeons_bridge.dll`

Requires the MinHook library and the Windows SDK. From an x64 Visual
Studio developer command prompt:

```
cl /LD /EHsc dungeons_bridge.cpp /I include /link d3d11.lib dxgi.lib user32.lib lib\libMinHook.x64.lib /OUT:dungeons_bridge.dll
```

Or open the folder as a Visual Studio project (`File > New > Project
From Existing Code`) and build normally - just make sure the include
and lib paths above are set in the project properties.

### Running the generator's tests

```
py -3.12 -m pytest worlds/mcdungeons/test/ -v
```

## License

MIT - see [LICENSE](LICENSE).
