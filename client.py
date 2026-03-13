import socket
import struct
import threading
import time
import zlib
import signal
import sys
import tkinter as tk
from tkinter import scrolledtext, simpledialog
import queue  # Import queue for thread-safe message passing

# === Configuration ===
SERVER_HOST = "localhost"
SERVER_PORT = 25565
USERNAME = "bot1"
RECONNECT_DELAY_SECONDS = 5  # How long to wait before attempting to reconnect
MOVE_DISTANCE = 1.0  # Distance to move per key press
MIN_Y = 0.0  # Define a minimum Y coordinate to prevent falling into void/illegal stance issues

# === Global State (for player position/look) ===
bot_x, bot_y, bot_z = 0.0, 64.0, 0.0
bot_stance = bot_y + 1.62
bot_yaw, bot_pitch = 0.0, 0.0
bot_on_ground = False
bot_entity_id = -1
bot_dimension = 0

# === Global Socket Variable ===
global_socket = None
running_client = False  # Control flag for threads

# === World Data ===
world_chunks = {}  # (chunk_x, chunk_z) -> chunk data
world_chunks_lock = threading.Lock()
gravity_enabled = True
fall_delay_active = False
last_jump_time = 0
JUMP_GRACE_PERIOD = 0.6  # seconds to wait after jumping before detecting fall

# === Thread-safe Queues ===
movement_queue = []
movement_lock = threading.Lock()
chat_queue = (
    queue.Queue()
)  # Queue for passing chat messages from network thread to GUI thread
info_queue = queue.Queue()  # New: Queue for passing info updates to GUI thread


# === Signal Handler for graceful shutdown ===
def signal_handler(sig, frame):
    global global_socket, running_client
    print("\n[INFO] Ctrl+C detected. Attempting graceful shutdown...")
    running_client = False  # Signal all threads to stop
    if global_socket:
        try:
            # Send Disconnect packet (0xFF)
            disconnect_message = "Disconnected by client (Ctrl+C)"
            disconnect_data = encode_string_utf16(disconnect_message)
            send_packet(global_socket, 0xFF, disconnect_data)
            print(f"[Send] Sent 0xFF Disconnect packet: '{disconnect_message}'")
            time.sleep(0.1)  # Give a small moment for the packet to send
        except Exception as e:
            print(f"[ERROR] Failed to send disconnect packet during shutdown: {e}")
        finally:
            print("[INFO] Closing socket.")
            global_socket.close()
            global_socket = None  # Clear global_socket after closing
    sys.exit(0)


# Register the signal handler
signal.signal(signal.SIGINT, signal_handler)


# === Block Lookup ===
def get_block_at(x, y, z):
    """Returns the block ID at the given world coordinates. Returns 0 if air."""
    chunk_x = int(x) >> 4
    chunk_z = int(z) >> 4

    with world_chunks_lock:
        if (chunk_x, chunk_z) not in world_chunks:
            return 0  # Unknown = treat as air

    chunk = world_chunks.get((chunk_x, chunk_z))
    if chunk is None:
        return 0

    local_x = int(x) & 0xF
    local_y = int(y)
    local_z = int(z) & 0xF

    if local_y < 0 or local_y >= 128:
        return 0

    idx = local_x + (local_z << 4) + (local_y << 8)
    return chunk.get(idx, 0)


def check_gravity():
    """Background thread to handle automatic falling when there's empty space below."""
    global \
        bot_y, \
        bot_stance, \
        bot_on_ground, \
        running_client, \
        fall_delay_active, \
        last_jump_time

    FALL_STEP = 0.1  # Smaller steps for interpolated falling
    FALL_DELAY = 0.05  # Delay between each step

    while running_client:
        try:
            with movement_lock:
                current_y = bot_y
                current_x = bot_x
                current_z = bot_z

            time_since_jump = time.time() - last_jump_time

            if time_since_jump < JUMP_GRACE_PERIOD:
                time.sleep(0.1)
                continue

            block_below = get_block_at(current_x, current_y - 1, current_z)

            if block_below == 0 and gravity_enabled:
                if not fall_delay_active:
                    fall_delay_active = True
                    print(
                        "[Gravity] Empty block detected below! Waiting 1 second before falling..."
                    )
                    time.sleep(1)

                    # Interpolated falling - move down in small steps
                    steps = int(MOVE_DISTANCE / FALL_STEP)
                    for _ in range(steps):
                        if not gravity_enabled:
                            break
                        with movement_lock:
                            if bot_y - FALL_STEP >= MIN_Y:
                                bot_y -= FALL_STEP
                                bot_stance = bot_y + 1.62
                            else:
                                break
                        time.sleep(FALL_DELAY)

                    bot_on_ground = False
                    print(f"[Gravity] Fell to Y: {bot_y:.1f}")
                    fall_delay_active = False
            else:
                if block_below != 0:
                    fall_delay_active = False

            time.sleep(0.1)
        except Exception as e:
            print(f"[Gravity Error] {e}")
            time.sleep(0.5)


# === Helper Functions ===
def debug_send(sock, data):
    """Sends raw bytes over the socket."""
    sock.sendall(data)


def send_packet(sock, packet_id, data=b""):
    """Constructs and sends a Minecraft packet."""
    if sock and not sock._closed:
        try:
            full_packet = struct.pack(">B", packet_id) + data
            print(
                f"[Send] ID: 0x{packet_id:02X}, Length: {len(full_packet)} Bytes: {full_packet.hex()}"
            )
            debug_send(sock, full_packet)
        except Exception as e:
            print(f"[ERROR] Failed to send packet: {e}")
            global running_client
            running_client = False  # Stop client on send error
    else:
        print("[WARN] Attempted to send packet on a closed or invalid socket.")


def send_periodic_keep_alives(sock, interval=15):
    global running_client
    keep_alive_id_counter = 0
    while running_client:
        try:
            if sock._closed:  # Check if socket is closed before sending
                print("[KeepAlive Sender] Socket is closed. Exiting thread.")
                break
            send_packet(sock, 0x00, struct.pack(">i", keep_alive_id_counter))
            # print(f"[KeepAlive Sender] Sent 0x00 ID: {keep_alive_id_counter}") # Reduced spam
            keep_alive_id_counter += 1
            time.sleep(interval)
        except Exception as e:
            if (
                running_client
            ):  # Only log as error if client is still supposed to be running
                print(f"[KeepAlive Sender Error] {e}")
            running_client = False  # Signal main loop to stop or reconnect
            break


def send_periodic_player_updates(sock, interval=0.05):
    global \
        running_client, \
        bot_x, \
        bot_y, \
        bot_stance, \
        bot_z, \
        bot_yaw, \
        bot_pitch, \
        bot_on_ground
    while running_client:
        try:
            if sock._closed:  # Check if socket is closed before sending
                print("[Player Update Sender] Socket is closed. Exiting thread.")
                break
            # Use 0x0B for position updates
            with movement_lock:  # Ensure thread-safe access to bot coordinates
                player_data = struct.pack(
                    ">dddd?", bot_x, bot_y, bot_stance, bot_z, bot_on_ground
                )
                # Update info queue with current position and look
                info_queue.put(
                    f"Pos: ({bot_x:.1f}, {bot_y:.1f}, {bot_z:.1f}) Yaw: {bot_yaw:.1f} Pitch: {bot_pitch:.1f}"
                )
            send_packet(sock, 0x0B, player_data)  # Send Player Position (0x0B)
            # print(f"[Player Update Sender] Sent 0x0B Pos: ({bot_x:.1f}, {bot_y:.1f}, {bot_z:.1f})") # Reduced spam
            time.sleep(interval)
        except Exception as e:
            if (
                running_client
            ):  # Only log as error if client is still supposed to be running
                print(f"[Player Update Sender Error] {e}")
            running_client = False  # Signal main loop to stop or reconnect
            break


def recv_exact(sock, length):
    """Receives an exact number of bytes from the socket."""
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("Connection closed while reading data.")
        data += chunk
    return data


def recv_packet_id(sock):
    """Receives a single byte representing the packet ID."""
    pid_byte = sock.recv(1)
    if not pid_byte:
        raise ConnectionError("Disconnected or no data received")
    return pid_byte[0]


# --- String Encoding/Decoding ---
def encode_string_utf16(s):
    """Encodes a Python string into Minecraft's UCS-2 (UTF-16BE) format."""
    s_utf16 = s.encode("utf-16be")
    return struct.pack(">h", len(s)) + s_utf16


def read_string_utf16(sock):
    """Reads a Minecraft UCS-2 (UTF-16BE) string from the socket."""
    length_bytes = recv_exact(sock, 2)
    length = struct.unpack(">h", length_bytes)[0]
    if length < 0:
        print(
            f"[ERROR] Attempted to read string with negative length: {length}. Possible desync."
        )
        raise ValueError(f"Negative string length: {length}")
    raw = recv_exact(sock, length * 2)
    return raw.decode("utf-16be")


# --- Metadata Handling ---
def read_metadata(sock):
    """Reads a variable-length metadata stream."""
    metadata = {}
    while True:
        x = struct.unpack(">b", recv_exact(sock, 1))[0]
        if x == 0x7F:
            break

        data_type = (x >> 5) & 0x07
        index = x & 0x1F

        value = None
        if data_type == 0:  # byte
            value = struct.unpack(">b", recv_exact(sock, 1))[0]
        elif data_type == 1:  # short
            value = struct.unpack(">h", recv_exact(sock, 2))[0]
        elif data_type == 2:  # int
            value = struct.unpack(">i", recv_exact(sock, 4))[0]
        elif data_type == 3:  # float
            value = struct.unpack(">f", recv_exact(sock, 4))[0]
        elif data_type == 4:  # string (UCS-2)
            value = read_string_utf16(sock)
        elif data_type == 5:  # item stack
            item_id = struct.unpack(">h", recv_exact(sock, 2))[0]
            item_count = struct.unpack(">b", recv_exact(sock, 1))[0]
            item_damage = struct.unpack(">h", recv_exact(sock, 2))[0]
            value = {"id": item_id, "count": item_count, "damage": item_damage}
        elif data_type == 6:  # extra entity information
            x, y, z = struct.unpack(">iii", recv_exact(sock, 12))
            value = {"x": x, "y": y, "z": z}
        else:
            print(
                f"[WARN] Unknown metadata type {data_type} for index {index}. Skipping unknown bytes (may cause desync)."
            )
            raise RuntimeError(
                f"Unhandled metadata type: {data_type} for metadata field 0x{x:02X}"
            )

        metadata[index] = {"type": data_type, "value": value}
    return metadata


# === Packet Handling ===
def handle_server(sock):
    global \
        bot_x, \
        bot_y, \
        bot_z, \
        bot_stance, \
        bot_yaw, \
        bot_pitch, \
        bot_on_ground, \
        bot_entity_id, \
        running_client

    REASON_CODES_0x46 = {
        0: "Invalid Bed (tile.bed.notValid)",
        1: "Begin raining",
        2: "End raining",
    }

    try:
        while running_client:  # Loop while running_client is True
            pid = recv_packet_id(sock)
            print(f"\n[Recv] ID: 0x{pid:02X}")

            if pid == 0x00:  # KeepAlive
                keep_alive_id = struct.unpack(">i", recv_exact(sock, 4))[0]
                print(f"[KeepAlive] ID: {keep_alive_id}")
                send_packet(sock, 0x00, struct.pack(">i", keep_alive_id))

            # --- CHAT PACKET (SERVER -> CLIENT) ---
            elif pid == 0x03:  # Chat message
                msg = read_string_utf16(sock)
                print(f"[Chat] {msg}")
                chat_queue.put(msg)  # Put the message in the queue for the GUI

            elif pid == 0xC8:  # Increment Statistic
                stat_id = struct.unpack(">i", recv_exact(sock, 4))[0]
                amount = struct.unpack(">b", recv_exact(sock, 1))[0]
                print(f"[IncrementStatistic] Stat ID: {stat_id}, Amount: {amount}")

            elif pid == 0x46:  # New/Invalid State
                reason_code = struct.unpack(">b", recv_exact(sock, 1))[0]
                reason_text = REASON_CODES_0x46.get(
                    reason_code, f"Unknown Reason Code {reason_code}"
                )
                print(
                    f"[New/Invalid State (0x46)] Reason Code: {reason_code} ({reason_text})"
                )

            elif pid == 0x3C:  # Explosion
                x, y, z = struct.unpack(">ddd", recv_exact(sock, 24))
                unknown_float = struct.unpack(">f", recv_exact(sock, 4))[0]
                record_count = struct.unpack(">i", recv_exact(sock, 4))[0]
                for _ in range(record_count):
                    recv_exact(sock, 3)  # dx, dy, dz bytes
                print(
                    f"[Explosion (0x3C)] X:{x:.2f}, Y:{y:.2f}, Z:{z:.2f}, Radius?: {unknown_float:.2f}, Affected Blocks Count: {record_count}"
                )

            elif pid == 0x3D:  # Sound Effect
                effect_id = struct.unpack(">i", recv_exact(sock, 4))[0]
                x, y, z = struct.unpack(">ibi", recv_exact(sock, 9))
                sound_data = struct.unpack(">i", recv_exact(sock, 4))[0]
                print(
                    f"[SoundEffect] Effect ID: {effect_id}, X:{x}, Y:{y}, Z:{z}, Data: {sound_data}"
                )

            elif pid == 0x65:  # Close Window
                window_id = struct.unpack(">b", recv_exact(sock, 1))[0]
                print(f"[CloseWindow] Window ID: {window_id}")

            elif pid == 0x67:  # Set Slot
                window_id = struct.unpack(">b", recv_exact(sock, 1))[0]
                slot = struct.unpack(">h", recv_exact(sock, 2))[0]
                item_id = struct.unpack(">h", recv_exact(sock, 2))[0]
                if item_id != -1:
                    recv_exact(sock, 3)  # count (byte), damage (short)
                print(
                    f"[SetSlot] Window ID: {window_id}, Slot: {slot}, Item ID: {item_id}"
                )

            elif pid == 0x01:  # Login Response (Server to Client)
                bot_entity_id = struct.unpack(">i", recv_exact(sock, 4))[0]
                unknown_string = read_string_utf16(sock)
                map_seed = struct.unpack(">q", recv_exact(sock, 8))[0]
                dimension = struct.unpack(">b", recv_exact(sock, 1))[0]
                print(
                    f"[Login Success (0x01)] Entity ID: {bot_entity_id}, Unknown String: '{unknown_string}', Map Seed: {map_seed}, Dimension: {dimension}"
                )
                chat_queue.put(f"--- Logged in successfully as {USERNAME} ---")

            elif pid == 0x02:  # Handshake Response (Server to Client)
                connection_hash = read_string_utf16(sock)
                print(
                    f"[Handshake Echo from Server (0x02)] Connection Hash: '{connection_hash}'"
                )

            elif pid == 0x04:  # Time Update
                world_time = struct.unpack(">q", recv_exact(sock, 8))[0]
                # print(f"[TimeUpdate] World Time: {world_time}") # Reduced spam

            elif pid == 0x05:  # Entity Equipment
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                slot, item_id, damage = struct.unpack(">hhh", recv_exact(sock, 6))
                print(
                    f"[EntityEquipment] EID: {eid}, Slot: {slot}, ItemID: {item_id}, Damage: {damage}"
                )

            elif pid == 0x06:  # Spawn Position
                x, y, z = struct.unpack(">iii", recv_exact(sock, 12))
                print(f"[SpawnPosition] X: {x}, Y: {y}, Z: {z}")
                with movement_lock:  # Protect shared variables
                    bot_x, bot_y, bot_z = float(x), float(y), float(z)
                    bot_stance = bot_y + 1.62
                    bot_on_ground = True
                info_queue.put(f"Spawned at: ({bot_x:.1f}, {bot_y:.1f}, {bot_z:.1f})")

            elif (
                pid == 0x07
            ):  # Use Entity (Client to Server only - if received, it's unexpected)
                recv_exact(sock, 9)  # eid, target_eid, left_click
                print(f"[WARN] Received unexpected 0x07 Use Entity (Client-to-Server).")

            elif pid == 0x08:  # Update Health
                health = struct.unpack(">h", recv_exact(sock, 2))[0]
                print(f"[UpdateHealth] Health: {health}")
                if health <= 0:
                    print("[Death] Health is 0 or less. Sending respawn packet...")
                    send_packet(
                        sock, 0x09, b""
                    )  # 0x09 Respawn packet has no payload from client

            elif pid == 0x09:  # Respawn (Server to Client)
                world = struct.unpack(">b", recv_exact(sock, 1))[0]
                print(f"[Respawn] World: {world}")

            elif pid == 0x0A:  # Player (Client to Server) - Received unexpectedly
                recv_exact(sock, 1)  # on_ground
                print(f"[WARN] Received unexpected 0x0A Player (Client-to-Server).")

            elif (
                pid == 0x0B
            ):  # Player Position (Client to Server) - Received unexpectedly
                recv_exact(sock, 33)  # x, y, stance, z, on_ground
                print(
                    f"[WARN] Received unexpected 0x0B Player Position (Client-to-Server)."
                )

            elif pid == 0x0C:  # Player Look (Client to Server) - Received unexpectedly
                recv_exact(sock, 9)  # yaw, pitch, on_ground
                print(
                    f"[WARN] Received unexpected 0x0C Player Look (Client-to-Server)."
                )

            elif pid == 0x0D:  # Player Position Look (Server to Client)
                x, stance, y, z = struct.unpack(">dddd", recv_exact(sock, 32))
                yaw, pitch = struct.unpack(">ff", recv_exact(sock, 8))
                on_ground = struct.unpack(">?", recv_exact(sock, 1))[0]
                print(
                    f"[PositionLook] X: {x:.2f}, Y: {y:.2f}, Z: {z:.2f}, Stance: {stance:.2f}, Yaw: {yaw:.2f}, Pitch: {pitch:.2f}, OnGround: {on_ground}"
                )

                with movement_lock:  # Protect shared variables
                    bot_x, bot_y, bot_z = x, y, z
                    bot_stance = stance
                    bot_yaw, bot_pitch = yaw, pitch
                    bot_on_ground = on_ground
                info_queue.put(
                    f"Pos: ({bot_x:.1f}, {bot_y:.1f}, {bot_z:.1f}) Yaw: {bot_yaw:.1f} Pitch: {bot_pitch:.1f}"
                )

                # Client should acknowledge by sending back its current position/look
                response_data = struct.pack(
                    ">ddddff?",
                    bot_x,
                    bot_y,
                    bot_stance,
                    bot_z,
                    bot_yaw,
                    bot_pitch,
                    bot_on_ground,
                )
                send_packet(sock, 0x0D, response_data)

            elif (
                pid == 0x0E
            ):  # Player Digging (Client to Server) - Received unexpectedly
                recv_exact(sock, 10)  # status, x, y, z, face
                print(
                    f"[WARN] Received unexpected 0x0E Player Digging (Client-to-Server)."
                )

            elif (
                pid == 0x0F
            ):  # Player Block Placement (Client to Server) - Received unexpectedly
                x = struct.unpack(">i", recv_exact(sock, 4))[0]
                y = struct.unpack(">b", recv_exact(sock, 1))[0]
                z = struct.unpack(">i", recv_exact(sock, 4))[0]
                direction = struct.unpack(">b", recv_exact(sock, 1))[0]
                block_item_id = struct.unpack(">h", recv_exact(sock, 2))[0]
                if block_item_id != -1:
                    recv_exact(sock, 3)  # amount, damage
                print(
                    f"[WARN] Received unexpected 0x0F Player Block Placement (Client-to-Server)."
                )

            elif (
                pid == 0x10
            ):  # Holding Change (Client to Server) - Received unexpectedly
                recv_exact(sock, 2)  # slot_id
                print(
                    f"[WARN] Received unexpected 0x10 Holding Change (Client-to-Server)."
                )

            elif pid == 0x11:  # Use Bed
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                in_bed_status = struct.unpack(">b", recv_exact(sock, 1))[0]
                x, y, z = struct.unpack(">ibi", recv_exact(sock, 9))
                print(
                    f"[UseBed] EID: {eid}, InBedStatus: {in_bed_status}, X: {x}, Y: {y}, Z: {z}"
                )

            elif pid == 0x12:  # Animation
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                animate_type = struct.unpack(">b", recv_exact(sock, 1))[0]
                print(f"[Animation] EID: {eid}, Type: {animate_type}")

            elif (
                pid == 0x13
            ):  # Entity Action (Client to Server) - Received unexpectedly
                recv_exact(sock, 5)  # eid, action_type
                print(
                    f"[WARN] Received unexpected 0x13 Entity Action (Client-to-Server)."
                )

            elif pid == 0x14:  # Named Entity Spawn
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                player_name = read_string_utf16(sock)
                x, y, z = struct.unpack(">iii", recv_exact(sock, 12))
                yaw, pitch = struct.unpack(">bb", recv_exact(sock, 2))
                current_item = struct.unpack(">h", recv_exact(sock, 2))[0]
                # No metadata for this packet in protocol version 14
                print(
                    f"[SpawnNamedEntity] EID: {eid}, Name: '{player_name}', X:{x}, Y:{y}, Z:{z}, Yaw:{yaw}, Pitch:{pitch}, Item:{current_item}"
                )

            elif pid == 0x15:  # Pickup Spawn
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                item_id, count, damage = struct.unpack(">hbh", recv_exact(sock, 5))
                x, y, z = struct.unpack(">iii", recv_exact(sock, 12))
                yaw, pitch, roll = struct.unpack(">bbb", recv_exact(sock, 3))
                print(
                    f"[PickupSpawn] EID: {eid}, ItemID: {item_id}, Count: {count}, Damage/Metadata: {damage}, X:{x}, Y:{y}, Z:{z}, Yaw:{yaw}, Pitch:{pitch}, Roll:{roll}"
                )

            elif pid == 0x16:  # Collect Item
                collected_eid, collector_eid = struct.unpack(">ii", recv_exact(sock, 8))
                print(
                    f"[CollectItem] Collected EID: {collected_eid}, Collector EID: {collector_eid}"
                )

            elif pid == 0x17:  # Add Object/Vehicle
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                obj_type = struct.unpack(">b", recv_exact(sock, 1))[0]
                x, y, z = struct.unpack(">iii", recv_exact(sock, 12))
                unknown_flag = struct.unpack(">i", recv_exact(sock, 4))[0]
                if unknown_flag > 0:
                    recv_exact(
                        sock, 6
                    )  # unknown_short1, unknown_short2, unknown_short3
                print(
                    f"[AddObject/Vehicle] EID: {eid}, Type: {obj_type}, X:{x}, Y:{y}, Z:{z}, Flag: {unknown_flag}"
                )

            elif pid == 0x18:  # Mob Spawn
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                mob_type = struct.unpack(">b", recv_exact(sock, 1))[0]
                x, y, z = struct.unpack(">iii", recv_exact(sock, 12))
                yaw, pitch = struct.unpack(">bb", recv_exact(sock, 2))
                metadata = read_metadata(sock)
                print(
                    f"[MobSpawn] EID: {eid}, Type: {mob_type}, X:{x}, Y:{y}, Z:{z}, Yaw:{yaw}, Pitch:{pitch}, Metadata: {metadata}"
                )

            elif pid == 0x19:  # Entity: Painting
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                title = read_string_utf16(sock)
                x, y, z = struct.unpack(">iii", recv_exact(sock, 12))
                direction = struct.unpack(">i", recv_exact(sock, 4))[0]
                print(
                    f"[EntityPainting] EID: {eid}, Title: '{title}', X:{x}, Y:{y}, Z:{z}, Direction:{direction}"
                )

            elif pid == 0x1B:  # Stance update (?)
                recv_exact(sock, 18)  # 4 floats, 2 bools
                print(f"[StanceUpdate(0x1B)] Data received.")

            elif pid == 0x1C:  # Entity Velocity
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                vx, vy, vz = struct.unpack(">hhh", recv_exact(sock, 6))
                print(f"[EntityVelocity] EID: {eid}, Vx: {vx}, Vy: {vy}, Vz: {vz}")

            elif pid == 0x1D:  # Destroy Entity
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                print(f"[DestroyEntity] EID: {eid}")

            elif pid == 0x1E:  # Entity (No movement/look)
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                print(f"[Entity] EID: {eid} (No movement/look)")

            elif pid == 0x1F:  # Entity Relative Move
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                dx, dy, dz = struct.unpack(">bbb", recv_exact(sock, 3))
                print(f"[EntityRelativeMove] EID: {eid}, dX:{dx}, dY:{dy}, dZ:{dz}")

            elif pid == 0x20:  # Entity Look
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                yaw, pitch = struct.unpack(">bb", recv_exact(sock, 2))
                print(f"[EntityLook] EID: {eid}, Yaw:{yaw}, Pitch:{pitch}")

            elif pid == 0x21:  # Entity Look and Relative Move
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                dx, dy, dz = struct.unpack(">bbb", recv_exact(sock, 3))
                yaw, pitch = struct.unpack(">bb", recv_exact(sock, 2))
                print(
                    f"[EntityLookAndRelativeMove] EID: {eid}, dX:{dx}, dY:{dy}, dZ:{dz}, Yaw:{yaw}, Pitch:{pitch}"
                )

            elif pid == 0x22:  # Entity Teleport
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                x, y, z = struct.unpack(">iii", recv_exact(sock, 12))
                yaw, pitch = struct.unpack(">bb", recv_exact(sock, 2))
                print(
                    f"[EntityTeleport] EID: {eid}, X:{x}, Y:{y}, Z:{z}, Yaw:{yaw}, Pitch:{pitch}"
                )

            elif pid == 0x26:  # Entity Status
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                status_byte = struct.unpack(">b", recv_exact(sock, 1))[0]
                print(f"[EntityStatus] EID: {eid}, Status: {status_byte}")

            elif pid == 0x27:  # Attach Entity
                entity_id, vehicle_id = struct.unpack(">ii", recv_exact(sock, 8))
                print(
                    f"[AttachEntity] Entity ID: {entity_id}, Vehicle ID: {vehicle_id}"
                )

            elif pid == 0x28:  # Entity Metadata
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                metadata = read_metadata(sock)
                print(f"[EntityMetadataUpdate] EID: {eid}, Metadata: {metadata}")

            elif pid == 0x32:  # Pre-Chunk
                x, z = struct.unpack(">ii", recv_exact(sock, 8))
                mode = struct.unpack(">?", recv_exact(sock, 1))[0]
                print(f"[PreChunk] X: {x}, Z: {z}, Mode: {mode}")

            elif pid == 0x33:  # Map Chunk
                x = struct.unpack(">i", recv_exact(sock, 4))[0]
                y_coord = struct.unpack(">h", recv_exact(sock, 2))[0]
                z = struct.unpack(">i", recv_exact(sock, 4))[0]
                size_x = struct.unpack(">b", recv_exact(sock, 1))[0]
                size_y = struct.unpack(">b", recv_exact(sock, 1))[0]
                size_z = struct.unpack(">b", recv_exact(sock, 1))[0]
                compressed_size = struct.unpack(">i", recv_exact(sock, 4))[0]
                compressed_data = recv_exact(sock, compressed_size)

                try:
                    decompressed = zlib.decompress(compressed_data)
                    chunk_data = {}

                    # Uncompress chunk data: blocks (size_x * size_y * size_z)
                    # In beta 1.7.3, chunks are stored as consecutive bytes
                    num_blocks = (size_x + 1) * (size_y + 1) * (size_z + 1)
                    block_data = decompressed[:num_blocks]

                    # Store blocks by local coordinate index
                    idx = 0
                    for cy in range(size_y + 1):
                        for cz in range(size_z + 1):
                            for cx in range(size_x + 1):
                                if idx < len(block_data):
                                    local_idx = cx + (cz << 4) + (cy << 8)
                                    chunk_data[local_idx] = block_data[idx]
                                    idx += 1

                    with world_chunks_lock:
                        world_chunks[(x, z)] = chunk_data

                except Exception as e:
                    print(f"[MapChunk] Failed to decompress: {e}")

                print(
                    f"[MapChunk] X: {x}, Y_Coord: {y_coord}, Z: {z}, Size: ({size_x},{size_y},{size_z}), CompSize: {compressed_size}"
                )

            elif pid == 0x34:  # Multi Block Change
                chunk_x, chunk_z = struct.unpack(">ii", recv_exact(sock, 8))
                array_size = struct.unpack(">h", recv_exact(sock, 2))[0]
                recv_exact(sock, array_size * 2)  # coordinates
                recv_exact(sock, array_size * 1)  # block_types
                recv_exact(sock, array_size * 1)  # metadata_array
                print(
                    f"[MultiBlockChange] ChunkX:{chunk_x}, ChunkZ:{chunk_z}, NumChanges:{array_size}"
                )

            elif pid == 0x35:  # Block Change
                x, y, z = struct.unpack(">ibi", recv_exact(sock, 9))
                block_type, block_metadata = struct.unpack(">bb", recv_exact(sock, 2))
                print(
                    f"[BlockChange] X:{x}, Y:{y}, Z:{z}, Type:{block_type}, Metadata:{block_metadata}"
                )

            elif pid == 0x36:  # Block Action
                x, y, z = struct.unpack(">ihi", recv_exact(sock, 10))
                byte1, byte2 = struct.unpack(">bb", recv_exact(sock, 2))
                print(
                    f"[BlockAction] X:{x}, Y:{y}, Z:{z}, Byte1:{byte1}, Byte2:{byte2}"
                )

            elif pid == 0x47:  # Thunderbolt
                eid = struct.unpack(">i", recv_exact(sock, 4))[0]
                unknown_bool = struct.unpack(">?", recv_exact(sock, 1))[0]
                x, y, z = struct.unpack(">iii", recv_exact(sock, 12))
                print(
                    f"[Thunderbolt] EID: {eid}, UnknownBool: {unknown_bool}, X:{x}, Y:{y}, Z:{z}"
                )

            elif pid == 0x68:  # Window Items
                window_id = struct.unpack(">b", recv_exact(sock, 1))[0]
                count = struct.unpack(">h", recv_exact(sock, 2))[0]
                for _ in range(count):
                    item_id = struct.unpack(">h", recv_exact(sock, 2))[0]
                    if item_id != -1:
                        recv_exact(sock, 3)  # count, damage
                print(f"[WindowItems] Window ID: {window_id}, Count: {count}")

            elif pid == 0xFF:  # Disconnect/Kick
                msg = read_string_utf16(sock)
                print(f"[Disconnect] {msg}")
                chat_queue.put(f"--- Disconnected: {msg} ---")
                running_client = False  # Signal to stop processing packets
                break  # Exit the while loop

            else:
                print(
                    f"[ERROR] Unhandled Packet ID: 0x{pid:02X}. Disconnecting to prevent further issues."
                )
                chat_queue.put(f"--- Unhandled Packet 0x{pid:02X}, disconnecting ---")
                running_client = False  # Signal to stop
                break

    except ConnectionError as e:
        print(f"[Connection Error] {e}")
        chat_queue.put(f"--- Connection Error: {e} ---")
        running_client = False  # Signal to stop/reconnect
    except struct.error as e:
        print(
            f"[Protocol Error] Failed to unpack packet data: {e}. Possible desynchronization or incorrect packet structure assumption."
        )
        chat_queue.put(f"--- Protocol Error: {e} ---")
        running_client = False  # Signal to stop/reconnect
    except Exception as e:
        print(f"[General Error] {e}")
        chat_queue.put(f"--- General Error: {e} ---")
        running_client = False  # Signal to stop/reconnect
    finally:
        if sock and not sock._closed:
            print("[Packet Handler] Closing socket from finally block.")
            sock.close()
        global_socket = None  # Clear global_socket to indicate it's closed


def connect_and_manage_bot():
    global global_socket, running_client

    while True:  # Infinite loop for reconnection
        if (
            not running_client
        ):  # Only try to connect if not already running (or just disconnected)
            chat_queue.put(
                f"--> Attempting to connect to {SERVER_HOST}:{SERVER_PORT}..."
            )
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            global_socket = s
            running_client = True  # Set to True while attempting connection and running

            try:
                s.connect((SERVER_HOST, SERVER_PORT))
                chat_queue.put("--> Successfully connected.")

                # Handshake (0x02) - Client to Server
                send_packet(s, 0x02, encode_string_utf16(USERNAME))

                # Read server's handshake response (0x02)
                pid = recv_packet_id(s)
                if pid != 0x02:
                    msg = f"Expected 0x02 Handshake, got 0x{pid:02X}"
                    print(f"[Unexpected] {msg}")
                    chat_queue.put(f"--> {msg}")
                    running_client = False
                    continue

                connection_hash = read_string_utf16(s)
                print(
                    f"[Handshake Response] Server Connection Hash: '{connection_hash}'"
                )

                # Login Request (0x01) - Client to Server
                protocol_version = 14
                login_data = (
                    struct.pack(">i", protocol_version)
                    + encode_string_utf16(USERNAME)
                    + encode_string_utf16(connection_hash)
                    + struct.pack(">q", 0)
                    + struct.pack(">b", 0)
                )
                send_packet(s, 0x01, login_data)

                # Server Response after login
                pid = recv_packet_id(s)
                if pid == 0x01:
                    # Login Success
                    global bot_entity_id, bot_dimension
                    bot_entity_id = struct.unpack(">i", recv_exact(s, 4))[0]
                    unknown_string = read_string_utf16(s)
                    map_seed = struct.unpack(">q", recv_exact(s, 8))[0]
                    bot_dimension = struct.unpack(">b", recv_exact(s, 1))[0]
                    print(
                        f"[Login Success] EID: {bot_entity_id}, Seed: {map_seed}, Dim: {bot_dimension}"
                    )

                    server_listener_thread = threading.Thread(
                        target=handle_server, args=(s,)
                    )
                    server_listener_thread.daemon = True
                    server_listener_thread.start()

                    player_update_thread = threading.Thread(
                        target=send_periodic_player_updates, args=(s,)
                    )
                    player_update_thread.daemon = True
                    player_update_thread.start()

                    gravity_thread = threading.Thread(target=check_gravity)
                    gravity_thread.daemon = True
                    gravity_thread.start()

                    while running_client and server_listener_thread.is_alive():
                        time.sleep(1)

                    chat_queue.put(
                        "--> Bot disconnected or stopped. Attempting to restart..."
                    )

                elif pid == 0xFF:
                    msg = read_string_utf16(s)
                    print(f"[Login Failed] Kicked: {msg}")
                    chat_queue.put(f"--> Login Failed: {msg}")
                    running_client = False
                else:
                    msg = f"Expected 0x01 Login or 0xFF Kick, got 0x{pid:02X}"
                    print(f"[Unexpected] {msg}")
                    chat_queue.put(f"--> {msg}")
                    running_client = False

            except ConnectionRefusedError:
                chat_queue.put("--> Connection refused. Retrying...")
            except ConnectionError as e:
                chat_queue.put(f"--> Connection Error: {e}. Retrying...")
            except struct.error as e:
                chat_queue.put(f"--> Protocol Error: {e}. Retrying...")
            except Exception as e:
                chat_queue.put(f"--> General Error: {e}. Retrying...")
            finally:
                if s and not s._closed:
                    s.close()
                global_socket = None
                running_client = False

        if not running_client:
            print(f"Waiting {RECONNECT_DELAY_SECONDS} seconds before reconnecting...")
            time.sleep(RECONNECT_DELAY_SECONDS)


# === Tkinter GUI with Chat Implementation (REVISED) ===
class ChatClientGUI(tk.Frame):
    def __init__(self, master=None):
        super().__init__(master)
        self.master = master
        self.master.title("Minecraft Bot Controller")
        self.pack(fill="both", expand=True)
        self._create_widgets()
        self._bind_events()
        self.master.after(
            100, self._process_chat_queue
        )  # Start checking the chat queue
        self.master.after(
            100, self._process_info_queue
        )  # New: Start checking the info queue

    def _create_widgets(self):
        # Frame for info display
        info_frame = tk.Frame(self, borderwidth=2, relief="groove")
        info_frame.pack(side="top", fill="x", expand=False, padx=5, pady=5)

        self.pos_yaw_pitch_label = tk.Label(
            info_frame, text="Pos: (X.X, Y.Y, Z.Z) Yaw: X.X Pitch: X.X", anchor="w"
        )
        self.pos_yaw_pitch_label.pack(side="top", fill="x", padx=5, pady=2)

        # Frame for chat display and input
        chat_frame = tk.Frame(self, borderwidth=2, relief="groove")
        chat_frame.pack(side="top", fill="both", expand=True, padx=5, pady=5)

        # ScrolledText for receiving messages
        self.chat_log = scrolledtext.ScrolledText(
            chat_frame, state="disabled", wrap=tk.WORD, bg="#f0f0f0", fg="black"
        )
        self.chat_log.pack(side="top", fill="both", expand=True, padx=5, pady=5)

        # Frame for message entry and send button
        input_frame = tk.Frame(chat_frame)
        input_frame.pack(side="bottom", fill="x", expand=False, padx=5, pady=(0, 5))

        self.chat_entry = tk.Entry(input_frame)
        self.chat_entry.pack(side="left", fill="x", expand=True)

        self.send_button = tk.Button(
            input_frame, text="Send", command=self._send_chat_message
        )
        self.send_button.pack(side="right")

        # Label for movement instructions
        info_label = tk.Label(
            self, text="WASD=Move, Space=Up, Shift=Down, G=Toggle Gravity"
        )
        info_label.pack(side="bottom", fill="x", padx=5, pady=(0, 5))

    def _bind_events(self):
        # Bind movement keys to the master window
        self.master.bind("<KeyPress-w>", self._on_key_press)
        self.master.bind("<KeyPress-s>", self._on_key_press)
        self.master.bind("<KeyPress-a>", self._on_key_press)
        self.master.bind("<KeyPress-d>", self._on_key_press)
        self.master.bind("<KeyPress-space>", self._on_key_press)
        self.master.bind("<KeyPress-Shift_L>", self._on_key_press)
        self.master.bind("<KeyPress-Shift_R>", self._on_key_press)
        self.master.bind("<KeyPress-g>", self._toggle_gravity)

        # Bind Return key in chat entry to send message
        self.chat_entry.bind("<Return>", self._send_chat_message)

    def _toggle_gravity(self, event):
        global gravity_enabled
        gravity_enabled = not gravity_enabled
        status = "enabled" if gravity_enabled else "disabled"
        print(f"[Gravity] Auto-fall {status}")
        self.pos_yaw_pitch_label.config(
            text=f"Gravity: {status.upper()} | Pos: ({bot_x:.1f}, {bot_y:.1f}, {bot_z:.1f})"
        )

    def _on_key_press(self, event):
        # FIX #1: Ignore movement if the chat entry is focused
        if self.master.focus_get() is self.chat_entry:
            return

        global bot_x, bot_y, bot_z, bot_stance, bot_yaw, bot_pitch, last_jump_time
        with movement_lock:
            # Simple movement for demonstration, not accounting for yaw/pitch
            if event.keysym == "w":
                bot_z += MOVE_DISTANCE
            elif event.keysym == "s":
                bot_z -= MOVE_DISTANCE
            elif event.keysym == "a":
                bot_x += MOVE_DISTANCE  # Inverted 'a' based on your current code
            elif event.keysym == "d":
                bot_x -= MOVE_DISTANCE  # Inverted 'd' based on your current code
            elif event.keysym == "space":
                bot_y += MOVE_DISTANCE
                bot_stance = bot_y + 1.62
                last_jump_time = time.time()
            elif event.keysym == "Shift_L" or event.keysym == "Shift_R":
                new_y = bot_y - MOVE_DISTANCE
                if new_y >= MIN_Y:
                    bot_y = new_y
                    bot_stance = bot_y + 1.62
                else:
                    print(f"Cannot move below MIN_Y ({MIN_Y}).")
        print(
            f"Bot moved. New position: (X: {bot_x:.1f}, Y: {bot_y:.1f}, Z: {bot_z:.1f})"
        )
        # Update the info label immediately on movement
        self.pos_yaw_pitch_label.config(
            text=f"Pos: ({bot_x:.1f}, {bot_y:.1f}, {bot_z:.1f}) Yaw: {bot_yaw:.1f} Pitch: {bot_pitch:.1f}"
        )

    def _send_chat_message(self, event=None):
        message = self.chat_entry.get()
        if message and global_socket:
            self.chat_entry.delete(0, tk.END)
            # Packet 0x03 is used for Client->Server chat as well
            chat_packet_data = encode_string_utf16(message)
            send_packet(global_socket, 0x03, chat_packet_data)
            # FIX #2: REMOVED the line that displayed the message locally
            # self._display_message(f"<{USERNAME}> {message}") # This line was removed

    def _display_message(self, message):
        """Safely inserts a message into the chat log."""
        self.chat_log.configure(state="normal")
        self.chat_log.insert(tk.END, message + "\n")
        self.chat_log.configure(state="disabled")
        self.chat_log.see(tk.END)  # Scroll to the bottom

    def _process_chat_queue(self):
        """Checks the chat queue for new messages and displays them."""
        try:
            while not chat_queue.empty():
                message = chat_queue.get_nowait()
                self._display_message(message)
        finally:
            self.master.after(
                100, self._process_chat_queue
            )  # Schedule the next chat check

    def _process_info_queue(self):
        """Checks the info queue for new updates and displays them in the info label."""
        try:
            while not info_queue.empty():
                info_message = info_queue.get_nowait()
                self.pos_yaw_pitch_label.config(text=info_message)
        finally:
            self.master.after(
                100, self._process_info_queue
            )  # Schedule the next info check


def start_gui():
    root = tk.Tk()
    root.geometry("500x350")
    app = ChatClientGUI(master=root)
    app.mainloop()


if __name__ == "__main__":
    # Start the bot connection and management in a separate thread
    bot_thread = threading.Thread(target=connect_and_manage_bot)
    bot_thread.daemon = True  # Allow the bot thread to close when main thread exits
    bot_thread.start()

    # Start the GUI in the main thread
    start_gui()
