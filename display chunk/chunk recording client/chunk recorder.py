import socket
import struct
import threading
import time
import zlib
import signal
import sys
import tkinter as tk
from tkinter import scrolledtext
import queue
import json # Import for JSON serialization
import base64 # Import for base64 encoding binary data

# === Configuration ===
SERVER_HOST = "mc.evilduckz.net"
SERVER_PORT = 25565
USERNAME = "Boy_Kisser_OwO"
RECONNECT_DELAY_SECONDS = 5
MOVE_DISTANCE = 1.0
MIN_Y = 0.0

# === Global State (for player position/look) ===
bot_x, bot_y, bot_z = 0.0, 64.0, 0.0
bot_stance = bot_y + 1.62
bot_yaw, bot_pitch = 0.0, 0.0
bot_on_ground = False
bot_entity_id = -1
bot_dimension = 0

# === Global Socket Variable ===
global_socket = None
running_client = False # Control flag for threads

# === Thread-safe Queues ===
movement_queue = []
movement_lock = threading.Lock()
chat_queue = queue.Queue() # Queue for passing chat messages from network thread to GUI thread

# === Chunk Data Storage and Recording ===
# This dictionary will hold the current state of chunks in memory
# Key: (chunk_x, chunk_z)
# Value: {'blocks': bytearray, 'metadata': bytearray, 'block_light': bytearray, 'sky_light': bytearray}
world_chunks = {}
chunk_data_file = None # Global file handle for recording chunk data
CHUNK_DATA_FILENAME = "recorded_chunk_data.jsonl" # File to save chunk data

# === Signal Handler for graceful shutdown ===
def signal_handler(sig, frame):
    global global_socket, running_client, chunk_data_file
    print("\n[INFO] Ctrl+C detected. Attempting graceful shutdown...")
    running_client = False # Signal all threads to stop
    if global_socket:
        try:
            # Send Disconnect packet (0xFF)
            disconnect_message = "Disconnected by client (Ctrl+C)"
            disconnect_data = encode_string_utf16(disconnect_message)
            send_packet(global_socket, 0xFF, disconnect_data)
            print(f"[Send] Sent 0xFF Disconnect packet: '{disconnect_message}'")
            time.sleep(0.1) # Give a small moment for the packet to send
        except Exception as e:
            print(f"[ERROR] Failed to send disconnect packet during shutdown: {e}")
        finally:
            print("[INFO] Closing socket.")
            global_socket.close()
            global_socket = None # Clear global_socket after closing
    if chunk_data_file:
        chunk_data_file.close()
        print(f"[INFO] Closed chunk data file: {CHUNK_DATA_FILENAME}")
    sys.exit(0)

# Register the signal handler
signal.signal(signal.SIGINT, signal_handler)

# === Helper Functions ===
def debug_send(sock, data):
    """Sends raw bytes over the socket."""
    sock.sendall(data)

def send_packet(sock, packet_id, data=b''):
    """Constructs and sends a Minecraft packet."""
    if sock and not sock._closed:
        try:
            full_packet = struct.pack('>B', packet_id) + data
            print(f"[Send] ID: 0x{packet_id:02X}, Length: {len(full_packet)} Bytes: {full_packet.hex()}")
            debug_send(sock, full_packet)
        except Exception as e:
            print(f"[ERROR] Failed to send packet: {e}")
            global running_client
            running_client = False # Stop client on send error
    else:
        print("[WARN] Attempted to send packet on a closed or invalid socket.")


def send_periodic_keep_alives(sock, interval=15):
    global running_client
    keep_alive_id_counter = 0
    while running_client:
        try:
            if sock._closed: # Check if socket is closed before sending
                print("[KeepAlive Sender] Socket is closed. Exiting thread.")
                break
            send_packet(sock, 0x00, struct.pack('>i', keep_alive_id_counter))
            keep_alive_id_counter += 1
            time.sleep(interval)
        except Exception as e:
            if running_client:
                print(f"[KeepAlive Sender Error] {e}")
            running_client = False
            break

def send_periodic_player_updates(sock, interval=0.05):
    global running_client, bot_x, bot_y, bot_stance, bot_z, bot_yaw, bot_pitch, bot_on_ground
    while running_client:
        try:
            if sock._closed:
                print("[Player Update Sender] Socket is closed. Exiting thread.")
                break
            with movement_lock:
                player_data = struct.pack('>dddd?', bot_x, bot_y, bot_stance, bot_z, bot_on_ground)
            send_packet(sock, 0x0B, player_data)
            time.sleep(interval)
        except Exception as e:
            if running_client:
                print(f"[Player Update Sender Error] {e}")
            running_client = False
            break

def recv_exact(sock, length):
    """Receives an exact number of bytes from the socket."""
    data = b''
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
    s_utf16 = s.encode('utf-16be')
    return struct.pack('>h', len(s)) + s_utf16

def read_string_utf16(sock):
    """Reads a Minecraft UCS-2 (UTF-16BE) string from the socket."""
    length_bytes = recv_exact(sock, 2)
    length = struct.unpack('>h', length_bytes)[0]
    if length < 0:
        print(f"[ERROR] Attempted to read string with negative length: {length}. Possible desync.")
        raise ValueError(f"Negative string length: {length}")
    raw = recv_exact(sock, length * 2)
    return raw.decode('utf-16be')

# --- Metadata Handling ---
def read_metadata(sock):
    """Reads a variable-length metadata stream."""
    metadata = {}
    while True:
        x = struct.unpack('>b', recv_exact(sock, 1))[0]
        if x == 0x7F:
            break

        data_type = (x >> 5) & 0x07
        index = x & 0x1F

        value = None
        if data_type == 0: # byte
            value = struct.unpack('>b', recv_exact(sock, 1))[0]
        elif data_type == 1: # short
            value = struct.unpack('>h', recv_exact(sock, 2))[0]
        elif data_type == 2: # int
            value = struct.unpack('>i', recv_exact(sock, 4))[0]
        elif data_type == 3: # float
            value = struct.unpack('>f', recv_exact(sock, 4))[0]
        elif data_type == 4: # string (UCS-2)
            value = read_string_utf16(sock)
        elif data_type == 5: # item stack
            item_id = struct.unpack('>h', recv_exact(sock, 2))[0]
            item_count = struct.unpack('>b', recv_exact(sock, 1))[0]
            item_damage = struct.unpack('>h', recv_exact(sock, 2))[0]
            value = {'id': item_id, 'count': item_count, 'damage': item_damage}
        elif data_type == 6: # extra entity information
            x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
            value = {'x': x, 'y': y, 'z': z}
        else:
            print(f"[WARN] Unknown metadata type {data_type} for index {index}. Skipping unknown bytes (may cause desync).")
            raise RuntimeError(f"Unhandled metadata type: {data_type} for metadata field 0x{x:02X}")
        
        metadata[index] = {'type': data_type, 'value': value}
    return metadata

# === Packet Handling ===
def handle_server(sock):
    global bot_x, bot_y, bot_z, bot_stance, bot_yaw, bot_pitch, bot_on_ground, bot_entity_id, running_client, chunk_data_file
    
    REASON_CODES_0x46 = {
        0: "Invalid Bed (tile.bed.notValid)",
        1: "Begin raining",
        2: "End raining"
    }
    
    try:
        while running_client:
            pid = recv_packet_id(sock)
            print(f"\n[Recv] ID: 0x{pid:02X}")

            if pid == 0x00: # KeepAlive
                keep_alive_id = struct.unpack('>i', recv_exact(sock, 4))[0]
                print(f"[KeepAlive] ID: {keep_alive_id}")
                send_packet(sock, 0x00, struct.pack('>i', keep_alive_id))
            
            elif pid == 0x03: # Chat message
                msg = read_string_utf16(sock)
                print(f"[Chat] {msg}")
                chat_queue.put(msg)

            elif pid == 0xC8: # Increment Statistic
                stat_id = struct.unpack('>i', recv_exact(sock, 4))[0]
                amount = struct.unpack('>b', recv_exact(sock, 1))[0]
                print(f"[IncrementStatistic] Stat ID: {stat_id}, Amount: {amount}")

            elif pid == 0x46: # New/Invalid State
                reason_code = struct.unpack('>b', recv_exact(sock, 1))[0]
                reason_text = REASON_CODES_0x46.get(reason_code, f"Unknown Reason Code {reason_code}")
                print(f"[New/Invalid State (0x46)] Reason Code: {reason_code} ({reason_text})")

            elif pid == 0x3C: # Explosion
                x, y, z = struct.unpack('>ddd', recv_exact(sock, 24))
                unknown_float = struct.unpack('>f', recv_exact(sock, 4))[0]
                record_count = struct.unpack('>i', recv_exact(sock, 4))[0]
                for _ in range(record_count):
                    recv_exact(sock, 3) # dx, dy, dz bytes
                print(f"[Explosion (0x3C)] X:{x:.2f}, Y:{y:.2f}, Z:{z:.2f}, Radius?: {unknown_float:.2f}, Affected Blocks Count: {record_count}")

            elif pid == 0x3D: # Sound Effect
                effect_id = struct.unpack('>i', recv_exact(sock, 4))[0]
                x, y, z = struct.unpack('>ibi', recv_exact(sock, 9))
                sound_data = struct.unpack('>i', recv_exact(sock, 4))[0]
                print(f"[SoundEffect] Effect ID: {effect_id}, X:{x}, Y:{y}, Z:{z}, Data: {sound_data}")

            elif pid == 0x65: # Close Window
                window_id = struct.unpack('>b', recv_exact(sock, 1))[0]
                print(f"[CloseWindow] Window ID: {window_id}")

            elif pid == 0x67: # Set Slot
                window_id = struct.unpack('>b', recv_exact(sock, 1))[0]
                slot = struct.unpack('>h', recv_exact(sock, 2))[0]
                item_id = struct.unpack('>h', recv_exact(sock, 2))[0]
                if item_id != -1:
                    recv_exact(sock, 3) # count (byte), damage (short)
                print(f"[SetSlot] Window ID: {window_id}, Slot: {slot}, Item ID: {item_id}")

            elif pid == 0x01: # Login Response (Server to Client)
                bot_entity_id = struct.unpack('>i', recv_exact(sock, 4))[0]
                unknown_string = read_string_utf16(sock)
                map_seed = struct.unpack('>q', recv_exact(sock, 8))[0]
                dimension = struct.unpack('>b', recv_exact(sock, 1))[0]
                print(f"[Login Success (0x01)] Entity ID: {bot_entity_id}, Unknown String: '{unknown_string}', Map Seed: {map_seed}, Dimension: {dimension}")
                chat_queue.put(f"--- Logged in successfully as {USERNAME} ---")

            elif pid == 0x02: # Handshake Response (Server to Client)
                connection_hash = read_string_utf16(sock)
                print(f"[Handshake Echo from Server (0x02)] Connection Hash: '{connection_hash}'")

            elif pid == 0x04: # Time Update
                world_time = struct.unpack('>q', recv_exact(sock, 8))[0]
                # print(f"[TimeUpdate] World Time: {world_time}") # Reduced spam

            elif pid == 0x05: # Entity Equipment
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                slot, item_id, damage = struct.unpack('>hhh', recv_exact(sock, 6))
                print(f"[EntityEquipment] EID: {eid}, Slot: {slot}, ItemID: {item_id}, Damage: {damage}")

            elif pid == 0x06: # Spawn Position
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                print(f"[SpawnPosition] X: {x}, Y: {y}, Z: {z}")
                with movement_lock:
                    bot_x, bot_y, bot_z = float(x), float(y), float(z)
                    bot_stance = bot_y + 1.62
                    bot_on_ground = True

            elif pid == 0x07: # Use Entity (Client to Server only - if received, it's unexpected)
                recv_exact(sock, 9) # eid, target_eid, left_click
                print(f"[WARN] Received unexpected 0x07 Use Entity (Client-to-Server).")

            elif pid == 0x08: # Update Health
                health = struct.unpack('>h', recv_exact(sock, 2))[0]
                print(f"[UpdateHealth] Health: {health}")
                if health <= 0:
                    print("[Death] Health is 0 or less. Sending respawn packet...")
                    send_packet(sock, 0x09, b'') # 0x09 Respawn packet has no payload from client

            elif pid == 0x09: # Respawn (Server to Client)
                world = struct.unpack('>b', recv_exact(sock, 1))[0]
                print(f"[Respawn] World: {world}")

            elif pid == 0x0A: # Player (Client to Server) - Received unexpectedly
                recv_exact(sock, 1) # on_ground
                print(f"[WARN] Received unexpected 0x0A Player (Client-to-Server).")

            elif pid == 0x0B: # Player Position (Client to Server) - Received unexpectedly
                recv_exact(sock, 33) # x, y, stance, z, on_ground
                print(f"[WARN] Received unexpected 0x0B Player Position (Client-to-Server).")
            
            elif pid == 0x0C: # Player Look (Client to Server) - Received unexpectedly
                recv_exact(sock, 9) # yaw, pitch, on_ground
                print(f"[WARN] Received unexpected 0x0C Player Look (Client-to-Server).")

            elif pid == 0x0D: # Player Position Look (Server to Client)
                x, stance, y, z = struct.unpack('>dddd', recv_exact(sock, 32)) 
                yaw, pitch = struct.unpack('>ff', recv_exact(sock, 8))
                on_ground = struct.unpack('>?', recv_exact(sock, 1))[0]
                print(f"[PositionLook] X: {x:.2f}, Y: {y:.2f}, Z: {z:.2f}, Stance: {stance:.2f}, Yaw: {yaw:.2f}, Pitch: {pitch:.2f}, OnGround: {on_ground}")
                
                with movement_lock:
                    bot_x, bot_y, bot_z = x, y, z
                    bot_stance = stance
                    bot_yaw, bot_pitch = yaw, pitch
                    bot_on_ground = on_ground

                response_data = struct.pack('>ddddff?', bot_x, bot_y, bot_stance, bot_z, bot_yaw, bot_pitch, bot_on_ground)
                send_packet(sock, 0x0D, response_data)

            elif pid == 0x0E: # Player Digging (Client to Server) - Received unexpectedly
                recv_exact(sock, 10) # status, x, y, z, face
                print(f"[WARN] Received unexpected 0x0E Player Digging (Client-to-Server).")

            elif pid == 0x0F: # Player Block Placement (Client to Server) - Received unexpectedly
                x = struct.unpack('>i', recv_exact(sock, 4))[0]
                y = struct.unpack('>b', recv_exact(sock, 1))[0]
                z = struct.unpack('>i', recv_exact(sock, 4))[0]
                direction = struct.unpack('>b', recv_exact(sock, 1))[0]
                block_item_id = struct.unpack('>h', recv_exact(sock, 2))[0]
                if block_item_id != -1:
                    recv_exact(sock, 3) # amount, damage
                print(f"[WARN] Received unexpected 0x0F Player Block Placement (Client-to-Server).")

            elif pid == 0x10: # Holding Change (Client to Server) - Received unexpectedly
                recv_exact(sock, 2) # slot_id
                print(f"[WARN] Received unexpected 0x10 Holding Change (Client-to-Server).")

            elif pid == 0x11: # Use Bed
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                in_bed_status = struct.unpack('>b', recv_exact(sock, 1))[0]
                x, y, z = struct.unpack('>ibi', recv_exact(sock, 9))
                print(f"[UseBed] EID: {eid}, InBedStatus: {in_bed_status}, X: {x}, Y: {y}, Z: {z}")
            
            elif pid == 0x12: # Animation
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                animate_type = struct.unpack('>b', recv_exact(sock, 1))[0]
                print(f"[Animation] EID: {eid}, Type: {animate_type}")

            elif pid == 0x13: # Entity Action (Client to Server) - Received unexpectedly
                recv_exact(sock, 5) # eid, action_type
                print(f"[WARN] Received unexpected 0x13 Entity Action (Client-to-Server).")

            elif pid == 0x14: # Named Entity Spawn
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                player_name = read_string_utf16(sock)
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                yaw, pitch = struct.unpack('>bb', recv_exact(sock, 2))
                current_item = struct.unpack('>h', recv_exact(sock, 2))[0]
                print(f"[SpawnNamedEntity] EID: {eid}, Name: '{player_name}', X:{x}, Y:{y}, Z:{z}, Yaw:{yaw}, Pitch:{pitch}, Item:{current_item}")

            elif pid == 0x15: # Pickup Spawn
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                item_id, count, damage = struct.unpack('>hbh', recv_exact(sock, 5))
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                yaw, pitch, roll = struct.unpack('>bbb', recv_exact(sock, 3))
                print(f"[PickupSpawn] EID: {eid}, ItemID: {item_id}, Count: {count}, Damage/Metadata: {damage}, X:{x}, Y:{y}, Z:{z}, Yaw:{yaw}, Pitch:{pitch}, Roll:{roll}")

            elif pid == 0x16: # Collect Item
                collected_eid, collector_eid = struct.unpack('>ii', recv_exact(sock, 8))
                print(f"[CollectItem] Collected EID: {collected_eid}, Collector EID: {collector_eid}")

            elif pid == 0x17: # Add Object/Vehicle
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                obj_type = struct.unpack('>b', recv_exact(sock, 1))[0]
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                unknown_flag = struct.unpack('>i', recv_exact(sock, 4))[0]
                if unknown_flag > 0:
                    recv_exact(sock, 6) # unknown_short1, unknown_short2, unknown_short3
                print(f"[AddObject/Vehicle] EID: {eid}, Type: {obj_type}, X:{x}, Y:{y}, Z:{z}, Flag: {unknown_flag}")

            elif pid == 0x18: # Mob Spawn
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                mob_type = struct.unpack('>b', recv_exact(sock, 1))[0]
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                yaw, pitch = struct.unpack('>bb', recv_exact(sock, 2))
                metadata = read_metadata(sock)
                print(f"[MobSpawn] EID: {eid}, Type: {mob_type}, X:{x}, Y:{y}, Z:{z}, Yaw:{yaw}, Pitch:{pitch}, Metadata: {metadata}")

            elif pid == 0x19: # Entity: Painting
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                title = read_string_utf16(sock)
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                direction = struct.unpack('>i', recv_exact(sock, 4))[0]
                print(f"[EntityPainting] EID: {eid}, Title: '{title}', X:{x}, Y:{y}, Z:{z}, Direction:{direction}")

            elif pid == 0x1B: # Stance update (?)
                recv_exact(sock, 18) # 4 floats, 2 bools
                print(f"[StanceUpdate(0x1B)] Data received.")

            elif pid == 0x1C: # Entity Velocity
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                vx, vy, vz = struct.unpack('>hhh', recv_exact(sock, 6))
                print(f"[EntityVelocity] EID: {eid}, Vx: {vx}, Vy: {vy}, Vz: {vz}")

            elif pid == 0x1D: # Destroy Entity
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                print(f"[DestroyEntity] EID: {eid}")
            
            elif pid == 0x1E: # Entity (No movement/look)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                print(f"[Entity] EID: {eid} (No movement/look)")

            elif pid == 0x1F: # Entity Relative Move
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                dx, dy, dz = struct.unpack('>bbb', recv_exact(sock, 3))
                print(f"[EntityRelativeMove] EID: {eid}, dX:{dx}, dY:{dy}, dZ:{dz}")

            elif pid == 0x20: # Entity Look
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                yaw, pitch = struct.unpack('>bb', recv_exact(sock, 2))
                print(f"[EntityLook] EID: {eid}, Yaw:{yaw}, Pitch:{pitch}")

            elif pid == 0x21: # Entity Look and Relative Move
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                dx, dy, dz = struct.unpack('>bbb', recv_exact(sock, 3))
                yaw, pitch = struct.unpack('>bb', recv_exact(sock, 2))
                print(f"[EntityLookAndRelativeMove] EID: {eid}, dX:{dx}, dY:{dy}, dZ:{dz}, Yaw:{yaw}, Pitch:{pitch}")

            elif pid == 0x22: # Entity Teleport
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                yaw, pitch = struct.unpack('>bb', recv_exact(sock, 2))
                print(f"[EntityTeleport] EID: {eid}, X:{x}, Y:{y}, Z:{z}, Yaw:{yaw}, Pitch:{pitch}")

            elif pid == 0x26: # Entity Status
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                status_byte = struct.unpack('>b', recv_exact(sock, 1))[0]
                print(f"[EntityStatus] EID: {eid}, Status: {status_byte}")

            elif pid == 0x27: # Attach Entity
                entity_id, vehicle_id = struct.unpack('>ii', recv_exact(sock, 8))
                print(f"[AttachEntity] Entity ID: {entity_id}, Vehicle ID: {vehicle_id}")

            elif pid == 0x28: # Entity Metadata
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                metadata = read_metadata(sock)
                print(f"[EntityMetadataUpdate] EID: {eid}, Metadata: {metadata}")

            elif pid == 0x32: # Pre-Chunk
                x, z = struct.unpack('>ii', recv_exact(sock, 8))
                mode = struct.unpack('>?', recv_exact(sock, 1))[0]
                print(f"[PreChunk] X: {x}, Z: {z}, Mode: {mode}")

                # Record Pre-Chunk data to file
                if chunk_data_file:
                    record = {
                        "packet_id": "0x32",
                        "timestamp": time.time(),
                        "chunk_x": x,
                        "chunk_z": z,
                        "mode": int(mode) # Convert boolean to int for JSON
                    }
                    json.dump(record, chunk_data_file)
                    chunk_data_file.write('\n')
                    chunk_data_file.flush() # Ensure data is written immediately

                with movement_lock: # Protect access to world_chunks
                    if mode: # Initialize chunk
                        # Create a placeholder for the chunk, e.g., a 16x128x16 array of zeros
                        # The actual data will come in 0x33
                        world_chunks[(x, z)] = {
                            'blocks': bytearray(16 * 128 * 16), # Initialize with air (0)
                            'metadata': bytearray(16 * 128 * 16 // 2),
                            'block_light': bytearray(16 * 128 * 16 // 2),
                            'sky_light': bytearray(16 * 128 * 16 // 2),
                        }
                        print(f"  Initialized chunk at ({x}, {z})")
                    else: # Unload chunk
                        if (x, z) in world_chunks:
                            del world_chunks[(x, z)]
                            print(f"  Unloaded chunk at ({x}, {z})")

            elif pid == 0x33: # Map Chunk
                x = struct.unpack('>i', recv_exact(sock, 4))[0]
                y_coord = struct.unpack('>h', recv_exact(sock, 2))[0]
                z = struct.unpack('>i', recv_exact(sock, 4))[0]
                size_x = struct.unpack('>b', recv_exact(sock, 1))[0]
                size_y = struct.unpack('>b', recv_exact(sock, 1))[0]
                size_z = struct.unpack('>b', recv_exact(sock, 1))[0]
                compressed_size = struct.unpack('>i', recv_exact(sock, 4))[0]
                compressed_data = recv_exact(sock, compressed_size)
                print(f"[MapChunk] X: {x}, Y_Coord: {y_coord}, Z: {z}, Size: ({size_x},{size_y},{size_z}), CompSize: {compressed_size}")

                try:
                    # Decompress the data
                    uncompressed_data = zlib.decompress(compressed_data)

                    # Calculate actual sizes (add 1)
                    actual_sx = size_x + 1
                    actual_sy = size_y + 1
                    actual_sz = size_z + 1

                    expected_uncompressed_len = actual_sx * actual_sy * actual_sz * 2.5
                    if len(uncompressed_data) != expected_uncompressed_len:
                        print(f"[WARN] 0x33 Uncompressed data length mismatch. Expected {expected_uncompressed_len}, Got {len(uncompressed_data)}")
                        # Continue attempting to parse what's available

                    # Calculate chunk coordinates from block coordinates
                    chunk_x = x >> 4
                    chunk_z = z >> 4
                    
                    # Get or create the chunk entry in memory
                    with movement_lock:
                        if (chunk_x, chunk_z) not in world_chunks:
                            print(f"  [WARN] Received 0x33 for unknown chunk ({chunk_x}, {chunk_z}). Initializing in memory.")
                            world_chunks[(chunk_x, chunk_z)] = {
                                'blocks': bytearray(16 * 128 * 16),
                                'metadata': bytearray(16 * 128 * 16 // 2),
                                'block_light': bytearray(16 * 128 * 16 // 2),
                                'sky_light': bytearray(16 * 128 * 16 // 2),
                            }
                        
                        current_chunk_data = world_chunks[(chunk_x, chunk_z)]

                        # Offsets for different sections within the uncompressed data
                        block_type_len = actual_sx * actual_sy * actual_sz
                        metadata_len = block_type_len // 2 # Each nibble array is half the size of block type array
                        
                        block_type_start = 0
                        metadata_start = block_type_len
                        block_light_start = metadata_start + metadata_len
                        sky_light_start = block_light_start + metadata_len

                        # Extract the arrays (slice up to the actual length available)
                        block_types_data = uncompressed_data[block_type_start : min(block_type_start + block_type_len, len(uncompressed_data))]
                        metadata_data = uncompressed_data[metadata_start : min(metadata_start + metadata_len, len(uncompressed_data))]
                        block_light_data = uncompressed_data[block_light_start : min(block_light_start + metadata_len, len(uncompressed_data))]
                        sky_light_data = uncompressed_data[sky_light_start : min(sky_light_start + metadata_len, len(uncompressed_data))]

                        # Populate the chunk data structure in memory
                        for lx in range(actual_sx):
                            for lz in range(actual_sz):
                                for ly in range(actual_sy):
                                    start_x_in_chunk = x & 15
                                    start_y_in_chunk = y_coord & 127
                                    start_z_in_chunk = z & 15

                                    chunk_local_x = start_x_in_chunk + lx
                                    chunk_local_y = start_y_in_chunk + ly
                                    chunk_local_z = start_z_in_chunk + lz

                                    if not (0 <= chunk_local_x < 16 and 0 <= chunk_local_y < 128 and 0 <= chunk_local_z < 16):
                                        # print(f"[WARN] Block coordinate ({chunk_local_x},{chunk_local_y},{chunk_local_z}) out of chunk bounds.")
                                        continue # Skip if calculated local coord is out of 16x128x16 bounds

                                    chunk_index = chunk_local_y + (chunk_local_z * 128) + (chunk_local_x * 128 * 16)
                                    incoming_data_index = ly + (lz * actual_sy) + (lx * actual_sy * actual_sz)

                                    if incoming_data_index < len(block_types_data):
                                        current_chunk_data['blocks'][chunk_index] = block_types_data[incoming_data_index]
                                    
                                    nibble_array_index = incoming_data_index // 2
                                    if nibble_array_index < len(metadata_data):
                                        # Get current byte value in the chunk's nibble array
                                        current_meta_byte = current_chunk_data['metadata'][nibble_array_index]
                                        current_block_light_byte = current_chunk_data['block_light'][nibble_array_index]
                                        current_sky_light_byte = current_chunk_data['sky_light'][nibble_array_index]

                                        # Get new nibble values from incoming data
                                        new_meta_nibble = (metadata_data[nibble_array_index] >> (4 * (incoming_data_index % 2))) & 0x0F
                                        new_block_light_nibble = (block_light_data[nibble_array_index] >> (4 * (incoming_data_index % 2))) & 0x0F
                                        new_sky_light_nibble = (sky_light_data[nibble_array_index] >> (4 * (incoming_data_index % 2))) & 0x0F

                                        if incoming_data_index % 2 == 0: # Lower Y corresponds to low nibble (bits 0-3)
                                            current_chunk_data['metadata'][nibble_array_index] = (current_meta_byte & 0xF0) | new_meta_nibble
                                            current_chunk_data['block_light'][nibble_array_index] = (current_block_light_byte & 0xF0) | new_block_light_nibble
                                            current_chunk_data['sky_light'][nibble_array_index] = (current_sky_light_byte & 0xF0) | new_sky_light_nibble
                                        else: # Higher Y corresponds to high nibble (bits 4-7)
                                            current_chunk_data['metadata'][nibble_array_index] = (current_meta_byte & 0x0F) | (new_meta_nibble << 4)
                                            current_chunk_data['block_light'][nibble_array_index] = (current_block_light_byte & 0x0F) | (new_block_light_nibble << 4)
                                            current_chunk_data['sky_light'][nibble_array_index] = (current_sky_light_byte & 0x0F) | (new_sky_light_nibble << 4)

                        print(f"  Populated chunk at ({chunk_x}, {chunk_z}) with data from ({x},{y_coord},{z}) to ({x+size_x},{y_coord+size_y},{z+size_z}).")

                        # Record Map Chunk data to file
                        if chunk_data_file:
                            record = {
                                "packet_id": "0x33",
                                "timestamp": time.time(),
                                "chunk_x": chunk_x,
                                "chunk_z": chunk_z,
                                "start_block_x_world": x,
                                "start_block_y_world": y_coord,
                                "start_block_z_world": z,
                                "size_x": size_x,
                                "size_y": size_y,
                                "size_z": size_z,
                                "block_types_b64": base64.b64encode(block_types_data).decode('utf-8'),
                                "metadata_b64": base64.b64encode(metadata_data).decode('utf-8'),
                                "block_light_b64": base64.b64encode(block_light_data).decode('utf-8'),
                                "sky_light_b64": base64.b64encode(sky_light_data).decode('utf-8')
                            }
                            json.dump(record, chunk_data_file)
                            chunk_data_file.write('\n')
                            chunk_data_file.flush() # Ensure data is written immediately

                except zlib.error as ze:
                    print(f"[ZLib Error] Failed to decompress chunk data: {ze}")
                    chat_queue.put(f"--- ZLib Error: {ze} ---")
                except IndexError as ie:
                    print(f"[Indexing Error] While processing 0x33 chunk data: {ie}")
                    chat_queue.put(f"--- Indexing Error (Chunk): {ie} ---")
                except Exception as e:
                    print(f"[Error] Processing 0x33 Map Chunk: {e}")
                    chat_queue.put(f"--- Error processing chunk: {e} ---")
            
            elif pid == 0x34: # Multi Block Change
                chunk_x, chunk_z = struct.unpack('>ii', recv_exact(sock, 8))
                array_size = struct.unpack('>h', recv_exact(sock, 2))[0]
                coords_data = recv_exact(sock, array_size * 2) # coordinates
                block_types_data = recv_exact(sock, array_size * 1) # block_types
                metadata_array_data = recv_exact(sock, array_size * 1) # metadata_array
                print(f"[MultiBlockChange] ChunkX:{chunk_x}, ChunkZ:{chunk_z}, NumChanges:{array_size}")

                # Record Multi Block Change data to file
                if chunk_data_file:
                    record = {
                        "packet_id": "0x34",
                        "timestamp": time.time(),
                        "chunk_x": chunk_x,
                        "chunk_z": chunk_z,
                        "num_changes": array_size,
                        "coords_b64": base64.b64encode(coords_data).decode('utf-8'),
                        "block_types_b64": base64.b64encode(block_types_data).decode('utf-8'),
                        "metadata_b64": base64.b64encode(metadata_array_data).decode('utf-8')
                    }
                    json.dump(record, chunk_data_file)
                    chunk_data_file.write('\n')
                    chunk_data_file.flush()

            elif pid == 0x35: # Block Change
                x, y, z = struct.unpack('>ibi', recv_exact(sock, 9))
                block_type, block_metadata = struct.unpack('>bb', recv_exact(sock, 2))
                print(f"[BlockChange] X:{x}, Y:{y}, Z:{z}, Type:{block_type}, Metadata:{block_metadata}")

                # Record Block Change data to file
                if chunk_data_file:
                    record = {
                        "packet_id": "0x35",
                        "timestamp": time.time(),
                        "block_x": x,
                        "block_y": y,
                        "block_z": z,
                        "block_type": block_type,
                        "block_metadata": block_metadata
                    }
                    json.dump(record, chunk_data_file)
                    chunk_data_file.write('\n')
                    chunk_data_file.flush()

            elif pid == 0x36: # Block Action
                x, y, z = struct.unpack('>ihi', recv_exact(sock, 10))
                byte1, byte2 = struct.unpack('>bb', recv_exact(sock, 2))
                print(f"[BlockAction] X:{x}, Y:{y}, Z:{z}, Byte1:{byte1}, Byte2:{byte2}")

            elif pid == 0x47: # Thunderbolt
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                unknown_bool = struct.unpack('>?', recv_exact(sock, 1))[0]
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                print(f"[Thunderbolt] EID: {eid}, UnknownBool: {unknown_bool}, X:{x}, Y:{y}, Z:{z}")
            
            elif pid == 0x68: # Window Items
                window_id = struct.unpack('>b', recv_exact(sock, 1))[0]
                count = struct.unpack('>h', recv_exact(sock, 2))[0]
                for _ in range(count):
                    item_id = struct.unpack('>h', recv_exact(sock, 2))[0]
                    if item_id != -1:
                        recv_exact(sock, 3) # count, damage
                print(f"[WindowItems] Window ID: {window_id}, Count: {count}")

            elif pid == 0xFF: # Disconnect/Kick
                msg = read_string_utf16(sock)
                print(f"[Disconnect] {msg}")
                chat_queue.put(f"--- Disconnected: {msg} ---")
                running_client = False
                break
            
            else:
                print(f"[ERROR] Unhandled Packet ID: 0x{pid:02X}. Disconnecting to prevent further issues.")
                chat_queue.put(f"--- Unhandled Packet 0x{pid:02X}, disconnecting ---")
                running_client = False
                break

    except ConnectionError as e:
        print(f"[Connection Error] {e}")
        chat_queue.put(f"--- Connection Error: {e} ---")
        running_client = False
    except struct.error as e:
        print(f"[Protocol Error] Failed to unpack packet data: {e}. Possible desynchronization or incorrect packet structure assumption.")
        chat_queue.put(f"--- Protocol Error: {e} ---")
        running_client = False
    except Exception as e:
        print(f"[General Error] {e}")
        chat_queue.put(f"--- General Error: {e} ---")
    finally:
        if sock and not sock._closed:
            print("[Packet Handler] Closing socket from finally block.")
            sock.close()
        global_socket = None # Clear global_socket to indicate it's closed


def connect_and_manage_bot():
    global global_socket, running_client, chunk_data_file

    # Open the chunk data file at the start of the bot management thread
    try:
        chunk_data_file = open(CHUNK_DATA_FILENAME, 'a')
        print(f"[INFO] Opened chunk data file: {CHUNK_DATA_FILENAME}")
    except IOError as e:
        print(f"[ERROR] Could not open chunk data file {CHUNK_DATA_FILENAME}: {e}")
        chat_queue.put(f"--- ERROR: Could not open chunk data file: {e} ---")
        running_client = False # Prevent client from running if file cannot be opened
        return

    while True:
        if not running_client:
            chat_queue.put(f"--> Attempting to connect to {SERVER_HOST}:{SERVER_PORT}...")
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            global_socket = s
            running_client = True

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
                print(f"[Handshake Response] Server Connection Hash: '{connection_hash}'")

                # Login Request (0x01) - Client to Server
                protocol_version = 14
                login_data = struct.pack('>i', protocol_version) + encode_string_utf16(USERNAME) + encode_string_utf16(connection_hash) + struct.pack('>q', 0) + struct.pack('>b', 0)
                send_packet(s, 0x01, login_data)

                # Server Response after login
                pid = recv_packet_id(s)
                if pid == 0x01:
                    global bot_entity_id, bot_dimension
                    bot_entity_id = struct.unpack('>i', recv_exact(s, 4))[0]
                    unknown_string = read_string_utf16(s)
                    map_seed = struct.unpack('>q', recv_exact(s, 8))[0]
                    bot_dimension = struct.unpack('>b', recv_exact(s, 1))[0]
                    print(f"[Login Success] EID: {bot_entity_id}, Seed: {map_seed}, Dim: {bot_dimension}")
                    
                    server_listener_thread = threading.Thread(target=handle_server, args=(s,))
                    server_listener_thread.daemon = True
                    server_listener_thread.start()

                    player_update_thread = threading.Thread(target=send_periodic_player_updates, args=(s,))
                    player_update_thread.daemon = True
                    player_update_thread.start()
                    
                    while running_client and server_listener_thread.is_alive():
                        time.sleep(1)
                    
                    chat_queue.put("--> Bot disconnected or stopped. Attempting to restart...")
                
                elif pid == 0xFF:
                    msg = read_string_utf16(s)
                    print(f"[Login Failed] Kicked: {msg}")
                    chat_queue.put(f"--> Login Failed: {msg}")
                    running_client = False
                else:
                    msg = f"Expected 0x01 Login or 0xFF Kick, got 0x{pid:02X}"
                    print(f"--> [Unexpected] {msg}")
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

# === Tkinter GUI with Chat Implementation ===
class ChatClientGUI(tk.Frame):
    def __init__(self, master=None):
        super().__init__(master)
        self.master = master
        self.master.title("Minecraft Bot Controller")
        self.pack(fill="both", expand=True)
        self._create_widgets()
        self._bind_events()
        self.master.after(100, self._process_chat_queue) # Start checking the queue

    def _create_widgets(self):
        # Frame for chat display and input
        chat_frame = tk.Frame(self, borderwidth=2, relief="groove")
        chat_frame.pack(side="top", fill="both", expand=True, padx=5, pady=5)

        # ScrolledText for receiving messages
        self.chat_log = scrolledtext.ScrolledText(chat_frame, state='disabled', wrap=tk.WORD, bg="#f0f0f0", fg="black")
        self.chat_log.pack(side="top", fill="both", expand=True, padx=5, pady=5)

        # Frame for message entry and send button
        input_frame = tk.Frame(chat_frame)
        input_frame.pack(side="bottom", fill="x", expand=False, padx=5, pady=(0, 5))

        self.chat_entry = tk.Entry(input_frame)
        self.chat_entry.pack(side="left", fill="x", expand=True)

        self.send_button = tk.Button(input_frame, text="Send", command=self._send_chat_message)
        self.send_button.pack(side="right")

        # Label for movement instructions
        info_label = tk.Label(self, text="Focus this window. Use WASD to move, Space (Up), Shift (Down).")
        info_label.pack(side="bottom", fill="x", padx=5, pady=(0, 5))

    def _bind_events(self):
        # Bind movement keys to the master window
        self.master.bind('<KeyPress-w>', self._on_key_press)
        self.master.bind('<KeyPress-s>', self._on_key_press)
        self.master.bind('<KeyPress-a>', self._on_key_press)
        self.master.bind('<KeyPress-d>', self._on_key_press)
        self.master.bind('<KeyPress-space>', self._on_key_press)
        self.master.bind('<KeyPress-Shift_L>', self._on_key_press)
        self.master.bind('<KeyPress-Shift_R>', self._on_key_press)
        
        # Bind Return key in chat entry to send message
        self.chat_entry.bind('<Return>', self._send_chat_message)

    def _on_key_press(self, event):
        if self.master.focus_get() is self.chat_entry:
            return

        global bot_x, bot_y, bot_z, bot_stance
        with movement_lock:
            if event.keysym == 'w':
                bot_z += MOVE_DISTANCE
            elif event.keysym == 's':
                bot_z -= MOVE_DISTANCE
            elif event.keysym == 'a':
                bot_x += MOVE_DISTANCE
            elif event.keysym == 'd':
                bot_x -= MOVE_DISTANCE
            elif event.keysym == 'space':
                bot_y += MOVE_DISTANCE
                bot_stance = bot_y + 1.62
            elif event.keysym == 'Shift_L' or event.keysym == 'Shift_R':
                new_y = bot_y - MOVE_DISTANCE
                if new_y >= MIN_Y:
                    bot_y = new_y
                    bot_stance = bot_y + 1.62
                else:
                    print(f"Cannot move below MIN_Y ({MIN_Y}).")
        print(f"Bot moved. New position: (X: {bot_x:.1f}, Y: {bot_y:.1f}, Z: {bot_z:.1f})")

    def _send_chat_message(self, event=None):
        message = self.chat_entry.get()
        if message and global_socket:
            self.chat_entry.delete(0, tk.END)
            chat_packet_data = encode_string_utf16(message)
            send_packet(global_socket, 0x03, chat_packet_data)

    def _display_message(self, message):
        """Safely inserts a message into the chat log."""
        self.chat_log.configure(state='normal')
        self.chat_log.insert(tk.END, message + '\n')
        self.chat_log.configure(state='disabled')
        self.chat_log.see(tk.END) # Scroll to the bottom

    def _process_chat_queue(self):
        """Checks the queue for new messages and displays them."""
        try:
            while not chat_queue.empty():
                message = chat_queue.get_nowait()
                self._display_message(message)
        finally:
            self.master.after(100, self._process_chat_queue) # Schedule the next check

def start_gui():
    root = tk.Tk()
    root.geometry("500x350")
    app = ChatClientGUI(master=root)
    app.mainloop()

if __name__ == "__main__":
    # Start the bot connection and management in a separate thread
    bot_thread = threading.Thread(target=connect_and_manage_bot)
    bot_thread.daemon = True
    bot_thread.start()

    # Start the GUI in the main thread
    start_gui()

    # Keep the main thread alive while the client is running
    while running_client:
        time.sleep(1)
    
    print("[INFO] Main application exiting.")
