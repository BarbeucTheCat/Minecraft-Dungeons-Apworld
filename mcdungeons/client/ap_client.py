"""
ap_client.py - minimal Archipelago multiworld client, just enough to
connect, authenticate, send LocationChecks, receive items, and send/
receive DeathLink. Not a full client (no DataStorage, no !hints, etc) -
purpose-built for the emerald milestone watcher, filler-reward receiver,
and DeathLink watcher in dungeons_reader.py.

Protocol reference: Archipelago's network protocol is plain JSON over
WebSocket. This implements the minimum handshake:
  1. Connect to ws://<host>:<port>
  2. Server sends "RoomInfo"
  3. Client sends "Connect" (game, slot name, password, uuid, version,
     items_handling=0b111 so it receives every item sent to this slot)
  4. Server sends "Connected" (or "ConnectionRefused" on failure), then
     immediately follows up with one or more "ReceivedItems" packets
     covering the full backlog of everything sent to this slot so far
     (even from previous sessions) - poll_received_items() picks these up
  5. Client sends "LocationChecks" with a list of location IDs whenever
     a check is earned
  6. Server sends further "ReceivedItems" packets in real time as other
     players (or this player) send items to this slot

Each ReceivedItems packet has a starting "index" and a list of "items";
the item at position N in the list has absolute index (packet index + N).
That absolute index is what the caller should persist to disk to avoid
re-applying the same reward twice across reconnects - see
dungeons_reader.py's REWARDS_FILE handling.

pip install websocket-client
"""

import json
import time
import uuid as uuid_module

try:
    import websocket
    WEBSOCKET_IMPORT_ERROR = None
except ImportError as _e:
    # See dungeons_reader.py's matching pymem guard for why this isn't
    # fatal at import time - same reasoning applies here.
    websocket = None
    WEBSOCKET_IMPORT_ERROR = _e


class ArchipelagoClient:
    def __init__(self, host, port, slot_name, game_name, password="", tags=None):
        self.host = host
        self.port = port
        self.slot_name = slot_name
        self.game_name = game_name
        self.password = password
        self.tags = tags or []  # include "DeathLink" here to opt in/receive DeathLink
        self.ws = None
        self.connected = False
        self.checked_locations = set()  # locations already sent, this connection
        self.slot_data = {}  # populated after connect() if the server sends it
        self.last_deathlinks = []  # populated by poll_received_items() each call

    def connect(self, timeout=10):
        if websocket is None:
            raise RuntimeError(
                "websocket-client isn't installed in this Python environment "
                f"({WEBSOCKET_IMPORT_ERROR}). Run: pip install websocket-client"
            )
        url = f"ws://{self.host}:{self.port}"
        self.ws = websocket.create_connection(url, timeout=timeout)

        # Step 1: expect RoomInfo
        room_info = self._receive_one()
        if not room_info or room_info[0].get("cmd") != "RoomInfo":
            raise RuntimeError(f"Expected RoomInfo, got: {room_info}")

        # Step 2: send Connect
        connect_packet = {
            "cmd": "Connect",
            "game": self.game_name,
            "name": self.slot_name,
            "password": self.password,
            "uuid": str(uuid_module.uuid4()),
            "version": {"major": 0, "minor": 4, "build": 4, "class": "Version"},
            # bit 0: items other players send us: bit 1: our own items if
            # they get sent back to us (e.g. found in our own game but
            # routed through AP): bit 2: starting inventory. 0b111 = all
            # of it - we want every filler reward that lands on this slot.
            "items_handling": 0b111,
            "tags": self.tags,
            "slot_data": True,  # so callers can read yaml options (e.g. death_link)
                                 # off the returned Connected packet's "slot_data" field
        }
        self.ws.send(json.dumps([connect_packet]))

        # Step 3: expect Connected (or ConnectionRefused)
        response = self._receive_one()
        if not response:
            raise RuntimeError("No response to Connect")

        cmd = response[0].get("cmd")
        if cmd == "ConnectionRefused":
            errors = response[0].get("errors", [])
            raise RuntimeError(f"Connection refused: {errors}")
        if cmd != "Connected":
            raise RuntimeError(f"Unexpected response to Connect: {response}")

        self.connected = True
        self.slot_data = response[0].get("slot_data", {}) or {}
        return response[0]  # the Connected packet (has slot/team/checked_locations/etc)

    def _receive_one(self):
        raw = self.ws.recv()
        if not raw:
            return None
        return json.loads(raw)

    def send_location_checks(self, location_ids):
        """Sends a LocationChecks packet for the given location IDs.
        Safe to call with IDs already sent before - dedups against
        checked_locations to avoid spamming the server with repeats."""
        new_ids = [loc_id for loc_id in location_ids if loc_id not in self.checked_locations]
        if not new_ids:
            return

        self.ws.send(json.dumps([{
            "cmd": "LocationChecks",
            "locations": new_ids,
        }]))
        self.checked_locations.update(new_ids)

    def poll_received_items(self, timeout=0.05):
        """Non-blocking-ish check for incoming server packets (mainly
        ReceivedItems). Returns a list of (item_id, absolute_index) pairs
        seen this call - empty if nothing arrived within `timeout`
        seconds. Safe to call every loop iteration alongside a memory
        poll; a short timeout keeps the watcher responsive.

        Also captures any DeathLink "Bounce" packets seen during this same
        call into self.last_deathlinks (a list of their "data" dicts,
        reset at the start of every call) - since this is the only method
        that drains the socket, a caller wanting DeathLink needs to read
        that attribute after calling this rather than polling separately
        (a second independent drain would race with this one and could
        eat packets meant for the other).

        Any other packet types (e.g. PrintJSON chat/log messages) are
        silently ignored - this client doesn't do anything with them, but
        draining them out of the socket buffer is still necessary so they
        don't pile up.
        """
        self.last_deathlinks = []
        if not self.ws:
            return []

        results = []
        self.ws.settimeout(timeout)
        try:
            while True:
                raw = self.ws.recv()
                if not raw:
                    break
                for pkt in json.loads(raw):
                    cmd = pkt.get("cmd")
                    if cmd == "ReceivedItems":
                        start_index = pkt.get("index", 0)
                        for offset, item in enumerate(pkt.get("items", [])):
                            results.append((item["item"], start_index + offset))
                    elif cmd == "Bounce" and "DeathLink" in pkt.get("tags", []):
                        data = pkt.get("data", {})
                        if data.get("source") != self.slot_name:  # ignore our own echo
                            self.last_deathlinks.append(data)
        except websocket.WebSocketTimeoutException:
            pass
        except OSError:
            # some platforms raise a plain socket timeout instead of
            # WebSocketTimeoutException - treat the same way
            pass

        return results

    def send_death_link(self, cause=None):
        """Sends a DeathLink Bounce - the de facto standard payload shape
        used by every DeathLink-supporting client, so other games'
        DeathLink implementations will recognize this regardless of what
        sends it. Requires "DeathLink" to have been included in `tags` at
        connect time, or the server will simply not relay it to anyone."""
        self.ws.send(json.dumps([{
            "cmd": "Bounce",
            "data": {
                "time": time.time(),
                "source": self.slot_name,
                "cause": cause or f"{self.slot_name} lost a totem",
            },
            "tags": ["DeathLink"],
        }]))

    def send_goal_complete(self):
        """Declares this slot's goal as reached (AP client_status 30 =
        CLIENT_GOAL). Safe to call more than once - the server treats
        repeats as a no-op, but this doesn't bother deduping itself since
        goal completion is a one-time event per session in practice."""
        self.ws.send(json.dumps([{
            "cmd": "StatusUpdate",
            "status": 30,  # ClientStatus.CLIENT_GOAL
        }]))

    def close(self):
        if self.ws:
            self.ws.close()
        self.connected = False
