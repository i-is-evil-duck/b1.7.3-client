import socket
import struct
import threading
import time
import zlib
import signal
import sys

# === Configuration ===
SERVER_HOST = "mc.evilduckz.net"
SERVER_PORT = 25565
USERNAME = "TestBot"
RECONNECT_DELAY_SECONDS = 5 # How long to wait before attempting to reconnect

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

# === Signal Handler for graceful shutdown ===
def signal_handler(sig, frame):
    global global_socket, running_client
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
    sys.exit(0)

# Register the signal handler
signal.signal(signal.SIGINT, signal_handler)

# === Helper Functions ===
def debug_send(sock, data):
    """Sends raw bytes over the socket."""
    sock.sendall(data)

def send_packet(sock, packet_id, data=b''):
    """Constructs and sends a Minecraft packet."""
    full_packet = struct.pack('>B', packet_id) + data
    print(f"[Send] ID: 0x{packet_id:02X}, Length: {len(full_packet)} Bytes: {full_packet.hex()}")
    debug_send(sock, full_packet)

def send_periodic_keep_alives(sock, interval=15):
    global running_client
    keep_alive_id_counter = 0
    while running_client:
        try:
            if sock._closed: # Check if socket is closed before sending
                print("[KeepAlive Sender] Socket is closed. Exiting thread.")
                break
            send_packet(sock, 0x00, struct.pack('>i', keep_alive_id_counter))
            print(f"[KeepAlive Sender] Sent 0x00 ID: {keep_alive_id_counter}")
            keep_alive_id_counter += 1
            time.sleep(interval)
        except Exception as e:
            if running_client: # Only log as error if client is still supposed to be running
                print(f"[KeepAlive Sender Error] {e}")
            running_client = False # Signal main loop to stop or reconnect
            break

def send_periodic_player_updates(sock, interval=0.05):
    global running_client, bot_x, bot_y, bot_stance, bot_z, bot_yaw, bot_pitch, bot_on_ground
    while running_client:
        try:
            if sock._closed: # Check if socket is closed before sending
                print("[Player Update Sender] Socket is closed. Exiting thread.")
                break
            player_data = struct.pack('>ddddff?', bot_x, bot_y, bot_stance, bot_z, bot_yaw, bot_pitch, bot_on_ground)
            send_packet(sock, 0x0D, player_data)
            print(f"[Player Update Sender] Sent 0x0D Pos: ({bot_x:.1f}, {bot_y:.1f}, {bot_z:.1f})")
            time.sleep(interval)
        except Exception as e:
            if running_client: # Only log as error if client is still supposed to be running
                print(f"[Player Update Sender Error] {e}")
            running_client = False # Signal main loop to stop or reconnect
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
    global bot_x, bot_y, bot_z, bot_stance, bot_yaw, bot_pitch, bot_on_ground, bot_entity_id, running_client
    
    REASON_CODES_0x46 = {
        0: "Invalid Bed (tile.bed.notValid)",
        1: "Begin raining",
        2: "End raining"
    }
    
    try:
        while running_client: # Loop while running_client is True
            pid = recv_packet_id(sock)
            print(f"\n[Recv] ID: 0x{pid:02X}")

            if pid == 0x00: # KeepAlive
                keep_alive_id = struct.unpack('>i', recv_exact(sock, 4))[0]
                print(f"[KeepAlive] ID: {keep_alive_id}")
                send_packet(sock, 0x00, struct.pack('>i', keep_alive_id))

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

            elif pid == 0x02: # Handshake Response (Server to Client)
                connection_hash = read_string_utf16(sock)
                print(f"[Handshake Echo from Server (0x02)] Connection Hash: '{connection_hash}'")

            elif pid == 0x03: # Chat message
                msg = read_string_utf16(sock)
                print(f"[Chat] {msg}")

            elif pid == 0x04: # Time Update
                world_time = struct.unpack('>q', recv_exact(sock, 8))[0]
                print(f"[TimeUpdate] World Time: {world_time}")

            elif pid == 0x05: # Entity Equipment
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                slot, item_id, damage = struct.unpack('>hhh', recv_exact(sock, 6))
                print(f"[EntityEquipment] EID: {eid}, Slot: {slot}, ItemID: {item_id}, Damage: {damage}")

            elif pid == 0x06: # Spawn Position
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                print(f"[SpawnPosition] X: {x}, Y: {y}, Z: {z}")
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

            elif pid == 0x0A: # Player (Client to Server)
                recv_exact(sock, 1) # on_ground
                print(f"[WARN] Received unexpected 0x0A Player (Client-to-Server).")

            elif pid == 0x0B: # Player Position (Client to Server)
                recv_exact(sock, 33) # x, y, stance, z, on_ground
                print(f"[WARN] Received unexpected 0x0B Player Position (Client-to-Server).")
            
            elif pid == 0x0C: # Player Look (Client to Server)
                recv_exact(sock, 9) # yaw, pitch, on_ground
                print(f"[WARN] Received unexpected 0x0C Player Look (Client-to-Server).")

            elif pid == 0x0D: # Player Position Look (Server to Client)
                x, stance, y, z = struct.unpack('>dddd', recv_exact(sock, 32)) 
                yaw, pitch = struct.unpack('>ff', recv_exact(sock, 8))
                on_ground = struct.unpack('>?', recv_exact(sock, 1))[0]
                print(f"[PositionLook] X: {x:.2f}, Y: {y:.2f}, Z: {z:.2f}, Stance: {stance:.2f}, Yaw: {yaw:.2f}, Pitch: {pitch:.2f}, OnGround: {on_ground}")
                
                bot_x, bot_y, bot_z = x, y, z
                bot_stance = stance
                bot_yaw, bot_pitch = yaw, pitch
                bot_on_ground = on_ground

                response_data = struct.pack('>ddddff?', bot_x, bot_y, bot_stance, bot_z, bot_yaw, bot_pitch, bot_on_ground)
                send_packet(sock, 0x0D, response_data)

            elif pid == 0x0E: # Player Digging (Client to Server)
                recv_exact(sock, 10) # status, x, y, z, face
                print(f"[WARN] Received unexpected 0x0E Player Digging (Client-to-Server).")

            elif pid == 0x0F: # Player Block Placement (Client to Server)
                x = struct.unpack('>i', recv_exact(sock, 4))[0]
                y = struct.unpack('>b', recv_exact(sock, 1))[0]
                z = struct.unpack('>i', recv_exact(sock, 4))[0]
                direction = struct.unpack('>b', recv_exact(sock, 1))[0]
                block_item_id = struct.unpack('>h', recv_exact(sock, 2))[0]
                if block_item_id != -1:
                    recv_exact(sock, 3) # amount, damage
                print(f"[WARN] Received unexpected 0x0F Player Block Placement (Client-to-Server).")

            elif pid == 0x10: # Holding Change (Client to Server)
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

            elif pid == 0x13: # Entity Action (Client to Server)
                recv_exact(sock, 5) # eid, action_type
                print(f"[WARN] Received unexpected 0x13 Entity Action (Client-to-Server).")

            elif pid == 0x14: # Named Entity Spawn
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                player_name = read_string_utf16(sock)
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                yaw, pitch = struct.unpack('>bb', recv_exact(sock, 2))
                current_item = struct.unpack('>h', recv_exact(sock, 2))[0]
                # No metadata for this packet in protocol version 14
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

            elif pid == 0x33: # Map Chunk
                x = struct.unpack('>i', recv_exact(sock, 4))[0]
                y_coord = struct.unpack('>h', recv_exact(sock, 2))[0]
                z = struct.unpack('>i', recv_exact(sock, 4))[0]
                size_x = struct.unpack('>b', recv_exact(sock, 1))[0]
                size_y = struct.unpack('>b', recv_exact(sock, 1))[0]
                size_z = struct.unpack('>b', recv_exact(sock, 1))[0]
                compressed_size = struct.unpack('>i', recv_exact(sock, 4))[0]
                recv_exact(sock, compressed_size) # Consume compressed data
                print(f"[MapChunk] X: {x}, Y_Coord: {y_coord}, Z: {z}, Size: ({size_x},{size_y},{size_z}), CompSize: {compressed_size}")
            
            elif pid == 0x34: # Multi Block Change
                chunk_x, chunk_z = struct.unpack('>ii', recv_exact(sock, 8))
                array_size = struct.unpack('>h', recv_exact(sock, 2))[0]
                recv_exact(sock, array_size * 2) # coordinates
                recv_exact(sock, array_size * 1) # block_types
                recv_exact(sock, array_size * 1) # metadata_array
                print(f"[MultiBlockChange] ChunkX:{chunk_x}, ChunkZ:{chunk_z}, NumChanges:{array_size}")

            elif pid == 0x35: # Block Change
                x, y, z = struct.unpack('>ibi', recv_exact(sock, 9))
                block_type, block_metadata = struct.unpack('>bb', recv_exact(sock, 2))
                print(f"[BlockChange] X:{x}, Y:{y}, Z:{z}, Type:{block_type}, Metadata:{block_metadata}")

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
                running_client = False # Signal to stop processing packets
                break # Exit the while loop
            
            else:
                print(f"[ERROR] Unhandled Packet ID: 0x{pid:02X}. Disconnecting to prevent further issues.")
                running_client = False # Signal to stop
                break

    except ConnectionError as e:
        print(f"[Connection Error] {e}")
        running_client = False # Signal to stop/reconnect
    except struct.error as e:
        print(f"[Protocol Error] Failed to unpack packet data: {e}. Possible desynchronization or incorrect packet structure assumption.")
        running_client = False # Signal to stop/reconnect
    except Exception as e:
        print(f"[General Error] {e}")
        running_client = False # Signal to stop/reconnect
    finally:
        if sock and not sock._closed:
            print("[Packet Handler] Closing socket from finally block.")
            sock.close()
        global_socket = None # Clear global_socket to indicate it's closed

def connect_and_manage_bot():
    global global_socket, running_client

    while True: # Infinite loop for reconnection
        if not running_client: # Only try to connect if not already running (or just disconnected)
            print(f"Attempting to connect to {SERVER_HOST}:{SERVER_PORT}...")
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            global_socket = s
            running_client = True # Set to True while attempting connection and running

            try:
                s.connect((SERVER_HOST, SERVER_PORT))
                print("Successfully connected.")

                # Handshake (0x02) - Client to Server
                send_packet(s, 0x02, encode_string_utf16(USERNAME))

                # Read server's handshake response (0x02)
                pid = recv_packet_id(s)
                if pid != 0x02:
                    print(f"[Unexpected] Expected 0x02 Handshake Response, got 0x{pid:02X}. Closing connection.")
                    running_client = False
                    continue # Try again

                connection_hash = read_string_utf16(s)
                print(f"[Handshake Response] Server Connection Hash: '{connection_hash}'")

                # Login Request (0x01) - Client to Server
                protocol_version = 14
                login_data = struct.pack('>i', protocol_version) + encode_string_utf16(USERNAME) + encode_string_utf16(connection_hash) + struct.pack('>q', 0) + struct.pack('>b', 0)
                send_packet(s, 0x01, login_data)

                # Server Response after login
                pid = recv_packet_id(s)
                if pid == 0x01:
                    # Login Success
                    global bot_entity_id, bot_dimension # Ensure these are set globally
                    bot_entity_id = struct.unpack('>i', recv_exact(s, 4))[0]
                    unknown_string = read_string_utf16(s)
                    map_seed = struct.unpack('>q', recv_exact(s, 8))[0]
                    bot_dimension = struct.unpack('>b', recv_exact(s, 1))[0] # Use bot_dimension
                    print(f"[Login Success] EID: {bot_entity_id}, Seed: {map_seed}, Dim: {bot_dimension}")
                    
                    # Start threads for handling incoming packets and periodic updates
                    server_listener_thread = threading.Thread(target=handle_server, args=(s,))
                    server_listener_thread.daemon = True
                    server_listener_thread.start()

                    player_update_thread = threading.Thread(target=send_periodic_player_updates, args=(s,))
                    player_update_thread.daemon = True
                    player_update_thread.start()
                    
                    # Wait for either the server listener to stop or the running_client flag to become False
                    while running_client and server_listener_thread.is_alive():
                        time.sleep(1)
                    
                    print("[INFO] Bot disconnected or stopped. Attempting to restart...")
                    # If running_client became False within handle_server, it means disconnection.
                    # The loop will then wait RECONNECT_DELAY_SECONDS and try to reconnect.

                elif pid == 0xFF:
                    msg = read_string_utf16(s)
                    print(f"[Login Failed] Kicked: {msg}")
                    running_client = False # Set to False so the outer loop retries
                else:
                    print(f"[Unexpected] Expected 0x01 Login or 0xFF Kick, got 0x{pid:02X}. Closing connection.")
                    running_client = False # Set to False so the outer loop retries
            
            except ConnectionRefusedError:
                print("[Error] Connection refused. Retrying...")
            except ConnectionError as e:
                print(f"[Connection Error] {e}. Retrying...")
            except struct.error as e:
                print(f"[Protocol Error] {e}. Retrying...")
            except Exception as e:
                print(f"[General Error] {e}. Retrying...")
            finally:
                if s and not s._closed:
                    print("[INFO] Closing socket from connection manager.")
                    s.close()
                global_socket = None # Ensure global_socket is cleared
                running_client = False # Ensure running_client is False if an exception occurred

        # If not running_client (due to disconnect or initial failure), wait and retry
        if not running_client:
            print(f"Waiting {RECONNECT_DELAY_SECONDS} seconds before reconnecting...")
            time.sleep(RECONNECT_DELAY_SECONDS)


if __name__ == "__main__":
    connect_and_manage_bot()
