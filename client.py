import socket
import struct
import threading
import time
import zlib # For decompressing chunk data

# === Configuration ===
SERVER_HOST = "mc.evilduckz.net"
SERVER_PORT = 25565
USERNAME = "TestBot"

# === Global State (for player position/look) ===
# We need to maintain the bot's known position/look to send back to the server.
bot_x, bot_y, bot_z = 0.0, 64.0, 0.0 # Initial reasonable defaults
bot_stance = bot_y + 1.62 # Stance is typically player_y + 1.62 (eye level)
bot_yaw, bot_pitch = 0.0, 0.0
bot_on_ground = False
bot_entity_id = -1 # Will be set by 0x01 Login Success

# === Helper Functions ===
def debug_send(sock, data):
    """Sends raw bytes over the socket."""
    # print(f"[RawSend] {len(data)} bytes: {data.hex()}")
    sock.sendall(data) # Use sendall to ensure all bytes are sent

def send_packet(sock, packet_id, data=b''):
    """Constructs and sends a Minecraft packet."""
    full_packet = struct.pack('>B', packet_id) + data
    print(f"[Send] ID: 0x{packet_id:02X}, Length: {len(full_packet)} Bytes: {full_packet.hex()}")
    debug_send(sock, full_packet)

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
    raw = recv_exact(sock, length * 2) # Length in characters, each char is 2 bytes
    return raw.decode('utf-16be')

# --- Metadata Handling ---
def read_metadata(sock):
    """Reads a variable-length metadata stream."""
    metadata = {}
    while True:
        x = struct.unpack('>b', recv_exact(sock, 1))[0] # Read the metadata field byte
        if x == 0x7F: # End of metadata stream
            break

        data_type = (x >> 5) & 0x07 # Top 3 bits
        index = x & 0x1F          # Lower 5 bits

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
        elif data_type == 5: # item stack (short id, byte count, short damage)
            item_id = struct.unpack('>h', recv_exact(sock, 2))[0]
            item_count = struct.unpack('>b', recv_exact(sock, 1))[0]
            item_damage = struct.unpack('>h', recv_exact(sock, 2))[0]
            value = {'id': item_id, 'count': item_count, 'damage': item_damage}
        elif data_type == 6: # extra entity information (int x, int y, int z)
            x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
            value = {'x': x, 'y': y, 'z': z}
        else:
            print(f"[WARN] Unknown metadata type {data_type} for index {index}. Skipping unknown bytes (may cause desync).")
            # For unhandled types, you'd need to guess how many bytes to skip.
            # This is dangerous. Raise an error to force an update.
            raise RuntimeError(f"Unhandled metadata type: {data_type} for metadata field 0x{x:02X}")
        
        metadata[index] = {'type': data_type, 'value': value}
    return metadata

# === Packet Handling ===
def handle_server(sock):
    global bot_x, bot_y, bot_z, bot_stance, bot_yaw, bot_pitch, bot_on_ground, bot_entity_id

    try:
        while True:
            pid = recv_packet_id(sock)
            print(f"\n[Recv] ID: 0x{pid:02X}")

            if pid == 0x00:
                # KeepAlive
                keep_alive_id = struct.unpack('>i', recv_exact(sock, 4))[0]
                print(f"[KeepAlive] ID: {keep_alive_id}")
                send_packet(sock, 0x00, struct.pack('>i', keep_alive_id))

            elif pid == 0x67:
                # Set Slot (Server to Client only)
                window_id = struct.unpack('>b', recv_exact(sock, 1))[0]
                slot = struct.unpack('>h', recv_exact(sock, 2))[0]
                item_id = struct.unpack('>h', recv_exact(sock, 2))[0]
                
                item_info = {'id': item_id}
                if item_id != -1:
                    item_count = struct.unpack('>b', recv_exact(sock, 1))[0]
                    item_damage = struct.unpack('>h', recv_exact(sock, 2))[0]
                    item_info['count'] = item_count
                    item_info['damage'] = item_damage
                else:
                    item_info['count'] = 0
                    item_info['damage'] = 0 # Empty slot
                    
                print(f"[SetSlot] Window ID: {window_id}, Slot: {slot}, Item: {item_info}")

            elif pid == 0x01:
                # Login Request / Login Response (Server to Client)
                bot_entity_id = struct.unpack('>i', recv_exact(sock, 4))[0]
                unknown_string = read_string_utf16(sock)
                map_seed = struct.unpack('>q', recv_exact(sock, 8))[0]
                dimension = struct.unpack('>b', recv_exact(sock, 1))[0]
                print(f"[Login Success (0x01)] Entity ID: {bot_entity_id}, Unknown String: '{unknown_string}', Map Seed: {map_seed}, Dimension: {dimension}")

            elif pid == 0x02:
                # Handshake (Server to Client)
                connection_hash = read_string_utf16(sock)
                print(f"[Handshake Echo from Server (0x02)] Connection Hash: '{connection_hash}'")

            elif pid == 0x03:
                # Chat message
                msg = read_string_utf16(sock)
                print(f"[Chat] {msg}")

            elif pid == 0x04:
                # Time Update (Server to Client only)
                world_time = struct.unpack('>q', recv_exact(sock, 8))[0]
                print(f"[TimeUpdate] World Time: {world_time}")

            elif pid == 0x05:
                # Entity Equipment (Server to Client)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                slot = struct.unpack('>h', recv_exact(sock, 2))[0]
                item_id = struct.unpack('>h', recv_exact(sock, 2))[0]
                damage = struct.unpack('>h', recv_exact(sock, 2))[0]
                print(f"[EntityEquipment] EID: {eid}, Slot: {slot}, ItemID: {item_id}, Damage: {damage}")

            elif pid == 0x06:
                # Spawn Position (Server to Client only)
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                print(f"[SpawnPosition] X: {x}, Y: {y}, Z: {z}")
                # Update bot's internal position for future movement packets
                bot_x, bot_y, bot_z = float(x), float(y), float(z)
                bot_stance = bot_y + 1.62 # Update stance based on new Y
                bot_on_ground = True # Assume on ground at spawn

            elif pid == 0x07:
                # Use Entity (Client to Server only - if received, it's unexpected)
                user_eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                target_eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                left_click = struct.unpack('>?', recv_exact(sock, 1))[0]
                print(f"[WARN] Received unexpected 0x07 Use Entity (Client-to-Server). User EID: {user_eid}, Target EID: {target_eid}, Left Click: {left_click}")

            elif pid == 0x08:
                # Update Health (Server to Client only)
                health = struct.unpack('>h', recv_exact(sock, 2))[0]
                print(f"[UpdateHealth] Health: {health}")

            elif pid == 0x09:
                # Respawn (Server to Client)
                world = struct.unpack('>b', recv_exact(sock, 1))[0]
                print(f"[Respawn] World: {world}")

            elif pid == 0x0A: # This is a Client to Server packet, so if received, it's unexpected.
                # Player (Client to Server) - Consuming bytes to avoid desync
                on_ground = struct.unpack('>?', recv_exact(sock, 1))[0]
                print(f"[WARN] Received unexpected 0x0A Player (Client-to-Server). OnGround: {on_ground}")

            elif pid == 0x0B: # This is a Client to Server packet, so if received, it's unexpected.
                # Player Position (Client to Server) - Consuming bytes to avoid desync
                x, y, stance, z = struct.unpack('>dddd', recv_exact(sock, 32))
                on_ground = struct.unpack('>?', recv_exact(sock, 1))[0]
                print(f"[WARN] Received unexpected 0x0B Player Position (Client-to-Server). X:{x}, Y:{y}, Stance:{stance}, Z:{z}, OnGround:{on_ground}")
            
            elif pid == 0x0C: # This is a Client to Server packet, so if received, it's unexpected.
                # Player Look (Client to Server) - Consuming bytes to avoid desync
                yaw, pitch = struct.unpack('>ff', recv_exact(sock, 8))
                on_ground = struct.unpack('>?', recv_exact(sock, 1))[0]
                print(f"[WARN] Received unexpected 0x0C Player Look (Client-to-Server). Yaw:{yaw}, Pitch:{pitch}, OnGround:{on_ground}")

            elif pid == 0x0D:
                # Player Position Look (Server to Client)
                # Server sends X, Stance, Y, Z for its position.
                x, stance, y, z = struct.unpack('>dddd', recv_exact(sock, 32)) 
                yaw, pitch = struct.unpack('>ff', recv_exact(sock, 8))
                on_ground = struct.unpack('>?', recv_exact(sock, 1))[0]
                print(f"[PositionLook] X: {x:.2f}, Y: {y:.2f}, Z: {z:.2f}, Stance: {stance:.2f}, Yaw: {yaw:.2f}, Pitch: {pitch:.2f}, OnGround: {on_ground}")
                
                # Update bot's internal position/look
                bot_x, bot_y, bot_z = x, y, z
                bot_stance = stance
                bot_yaw, bot_pitch = yaw, pitch
                bot_on_ground = on_ground

                # Client needs to respond with its own 0x0D with X, Y, Stance, Z order.
                # The documentation states: "When you connect to the official server, it will push a 0x0D packet. If you do not immediately respond with a 0x0D (or maybe 0x0A-0x0C) packet with similar and valid information, it will have unexpected results, such as map chunks not loading and future 0x0A-0x0D packets being ignored."
                # Sending 0x0D back is the most robust response here.
                response_data = struct.pack('>ddddff?', bot_x, bot_y, bot_stance, bot_z, bot_yaw, bot_pitch, bot_on_ground)
                send_packet(sock, 0x0D, response_data)

            elif pid == 0x0E: # This is a Client to Server packet, so if received, it's unexpected.
                # Player Digging (Client to Server) - Consuming bytes to avoid desync
                status = struct.unpack('>b', recv_exact(sock, 1))[0]
                x = struct.unpack('>i', recv_exact(sock, 4))[0]
                y = struct.unpack('>b', recv_exact(sock, 1))[0]
                z = struct.unpack('>i', recv_exact(sock, 4))[0]
                face = struct.unpack('>b', recv_exact(sock, 1))[0]
                print(f"[WARN] Received unexpected 0x0E Player Digging (Client-to-Server). Status:{status}, X:{x}, Y:{y}, Z:{z}, Face:{face}")

            elif pid == 0x0F: # This is a Client to Server packet, so if received, it's unexpected.
                # Player Block Placement (Client to Server) - Consuming bytes to avoid desync
                x = struct.unpack('>i', recv_exact(sock, 4))[0]
                y = struct.unpack('>b', recv_exact(sock, 1))[0]
                z = struct.unpack('>i', recv_exact(sock, 4))[0]
                direction = struct.unpack('>b', recv_exact(sock, 1))[0]
                block_item_id = struct.unpack('>h', recv_exact(sock, 2))[0]
                
                amount = -1
                damage = -1
                if block_item_id != -1: # Only read amount and damage if not empty hand
                    amount = struct.unpack('>b', recv_exact(sock, 1))[0]
                    damage = struct.unpack('>h', recv_exact(sock, 2))[0]
                print(f"[WARN] Received unexpected 0x0F Player Block Placement (Client-to-Server). X:{x}, Y:{y}, Z:{z}, Dir:{direction}, ItemID:{block_item_id}, Amount:{amount}, Damage:{damage}")

            elif pid == 0x10: # This is a Client to Server packet, so if received, it's unexpected.
                # Holding Change (Client to Server) - Consuming bytes to avoid desync
                slot_id = struct.unpack('>h', recv_exact(sock, 2))[0]
                print(f"[WARN] Received unexpected 0x10 Holding Change (Client-to-Server). Slot ID: {slot_id}")

            elif pid == 0x11:
                # Use Bed (Server to Client)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                in_bed_status = struct.unpack('>b', recv_exact(sock, 1))[0]
                x = struct.unpack('>i', recv_exact(sock, 4))[0]
                y = struct.unpack('>b', recv_exact(sock, 1))[0] # Y is a byte here
                z = struct.unpack('>i', recv_exact(sock, 4))[0]
                print(f"[UseBed] EID: {eid}, InBedStatus: {in_bed_status}, X: {x}, Y: {y}, Z: {z}")
            
            elif pid == 0x12:
                # Animation (Server to Client)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                animate_type = struct.unpack('>b', recv_exact(sock, 1))[0]
                print(f"[Animation] EID: {eid}, Type: {animate_type}")

            elif pid == 0x13: # This is a Client to Server packet, so if received, it's unexpected.
                # Entity Action (Client to Server) - Consuming bytes to avoid desync
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                action_type = struct.unpack('>b', recv_exact(sock, 1))[0]
                print(f"[WARN] Received unexpected 0x13 Entity Action (Client-to-Server). EID: {eid}, Action Type: {action_type}")

            elif pid == 0x14:
                # Named Entity Spawn (Server to Client only)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                player_name = read_string_utf16(sock)
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                yaw, pitch = struct.unpack('>bb', recv_exact(sock, 2))
                current_item = struct.unpack('>h', recv_exact(sock, 2))[0]
                metadata = read_metadata(sock)
                print(f"[SpawnNamedEntity] EID: {eid}, Name: '{player_name}', X:{x}, Y:{y}, Z:{z}, Yaw:{yaw}, Pitch:{pitch}, Item:{current_item}, Metadata: {metadata}")

            elif pid == 0x15:
                # Pickup Spawn (Server to Client only)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                item_id = struct.unpack('>h', recv_exact(sock, 2))[0]
                count = struct.unpack('>b', recv_exact(sock, 1))[0]
                damage = struct.unpack('>h', recv_exact(sock, 2))[0] # This is also for metadata
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                yaw, pitch, roll = struct.unpack('>bbb', recv_exact(sock, 3))
                print(f"[PickupSpawn] EID: {eid}, ItemID: {item_id}, Count: {count}, Damage/Metadata: {damage}, X:{x}, Y:{y}, Z:{z}, Yaw:{yaw}, Pitch:{pitch}, Roll:{roll}")

            elif pid == 0x16:
                # Collect Item (Server to Client only)
                collected_eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                collector_eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                print(f"[CollectItem] Collected EID: {collected_eid}, Collector EID: {collector_eid}")

            elif pid == 0x17:
                # Add Object/Vehicle (Server to Client only)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                obj_type = struct.unpack('>b', recv_exact(sock, 1))[0]
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                
                unknown_flag = struct.unpack('>i', recv_exact(sock, 4))[0]
                if unknown_flag > 0:
                    # These three shorts are only sent if unknown_flag > 0
                    unknown_short1 = struct.unpack('>h', recv_exact(sock, 2))[0]
                    unknown_short2 = struct.unpack('>h', recv_exact(sock, 2))[0]
                    unknown_short3 = struct.unpack('>h', recv_exact(sock, 2))[0]
                    print(f"[AddObject/Vehicle] EID: {eid}, Type: {obj_type}, X:{x}, Y:{y}, Z:{z}, Flag: {unknown_flag}, Extra Data: ({unknown_short1}, {unknown_short2}, {unknown_short3})")
                else:
                    print(f"[AddObject/Vehicle] EID: {eid}, Type: {obj_type}, X:{x}, Y:{y}, Z:{z}, Flag: {unknown_flag}")

            elif pid == 0x18:
                # Mob Spawn (Server to Client only)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                mob_type = struct.unpack('>b', recv_exact(sock, 1))[0]
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                yaw, pitch = struct.unpack('>bb', recv_exact(sock, 2))
                metadata = read_metadata(sock)
                print(f"[MobSpawn] EID: {eid}, Type: {mob_type}, X:{x}, Y:{y}, Z:{z}, Yaw:{yaw}, Pitch:{pitch}, Metadata: {metadata}")

            elif pid == 0x19:
                # Entity: Painting (Server to Client)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                title = read_string_utf16(sock)
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                direction = struct.unpack('>i', recv_exact(sock, 4))[0]
                print(f"[EntityPainting] EID: {eid}, Title: '{title}', X:{x}, Y:{y}, Z:{z}, Direction:{direction}")

            elif pid == 0x1B:
                # Stance update (?) (Server to Client)
                # Documentation notes: "It doesn't seem to be used (see 0x0D)."
                # But if it's sent, we must consume the bytes to maintain sync.
                # Assuming the example fields: 4 floats, 2 booleans based on typical structure for position/look.
                # The doc implies 4 floats and 2 booleans based on "???float ... ???boolean".
                # If this causes issues, we might need to verify the exact number/types.
                float1, float2, float3, float4 = struct.unpack('>ffff', recv_exact(sock, 16))
                bool1, bool2 = struct.unpack('>?', recv_exact(sock, 1))[0], struct.unpack('>?', recv_exact(sock, 1))[0]
                print(f"[StanceUpdate(0x1B)] Data: {float1}, {float2}, {float3}, {float4}, {bool1}, {bool2}")


            elif pid == 0x1C:
                # Entity Velocity (Server to Client only)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                vx = struct.unpack('>h', recv_exact(sock, 2))[0]
                vy = struct.unpack('>h', recv_exact(sock, 2))[0]
                vz = struct.unpack('>h', recv_exact(sock, 2))[0]
                print(f"[EntityVelocity] EID: {eid}, Vx: {vx}, Vy: {vy}, Vz: {vz}")

            elif pid == 0x1D:
                # Destroy Entity (Server to Client only)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                print(f"[DestroyEntity] EID: {eid}")
            
            elif pid == 0x1E:
                # Entity (Server to Client only) - No movement/look
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                print(f"[Entity] EID: {eid} (No movement/look)")

            elif pid == 0x1F:
                # Entity Relative Move (Server to Client only)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                dx, dy, dz = struct.unpack('>bbb', recv_exact(sock, 3))
                print(f"[EntityRelativeMove] EID: {eid}, dX:{dx}, dY:{dy}, dZ:{dz}")

            elif pid == 0x20:
                # Entity Look (Server to Client only)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                yaw, pitch = struct.unpack('>bb', recv_exact(sock, 2))
                print(f"[EntityLook] EID: {eid}, Yaw:{yaw}, Pitch:{pitch}")

            elif pid == 0x21:
                # Entity Look and Relative Move (Server to Client only)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                dx, dy, dz = struct.unpack('>bbb', recv_exact(sock, 3))
                yaw, pitch = struct.unpack('>bb', recv_exact(sock, 2))
                print(f"[EntityLookAndRelativeMove] EID: {eid}, dX:{dx}, dY:{dy}, dZ:{dz}, Yaw:{yaw}, Pitch:{pitch}")

            elif pid == 0x22:
                # Entity Teleport (Server to Client only)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                yaw, pitch = struct.unpack('>bb', recv_exact(sock, 2))
                print(f"[EntityTeleport] EID: {eid}, X:{x}, Y:{y}, Z:{z}, Yaw:{yaw}, Pitch:{pitch}")

            elif pid == 0x26:
                # Entity Status? (Server to Client only)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                status_byte = struct.unpack('>b', recv_exact(sock, 1))[0]
                print(f"[EntityStatus] EID: {eid}, Status: {status_byte}")

            elif pid == 0x27:
                # Attach Entity?
                entity_id = struct.unpack('>i', recv_exact(sock, 4))[0]
                vehicle_id = struct.unpack('>i', recv_exact(sock, 4))[0]
                print(f"[AttachEntity] Entity ID: {entity_id}, Vehicle ID: {vehicle_id}")

            elif pid == 0x28:
                # Entity Metadata (Server to Client)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                metadata = read_metadata(sock)
                print(f"[EntityMetadataUpdate] EID: {eid}, Metadata: {metadata}")

            elif pid == 0x32:
                # Pre-Chunk (Server to Client only)
                x, z = struct.unpack('>ii', recv_exact(sock, 8))
                mode = struct.unpack('>?', recv_exact(sock, 1))[0]
                print(f"[PreChunk] X: {x}, Z: {z}, Mode: {mode}")

            elif pid == 0x33:
                # Map Chunk (Server to Client only)
                x = struct.unpack('>i', recv_exact(sock, 4))[0]
                y_coord = struct.unpack('>h', recv_exact(sock, 2))[0] # Y is a short here!
                z = struct.unpack('>i', recv_exact(sock, 4))[0]
                size_x = struct.unpack('>b', recv_exact(sock, 1))[0]
                size_y = struct.unpack('>b', recv_exact(sock, 1))[0]
                size_z = struct.unpack('>b', recv_exact(sock, 1))[0]
                compressed_size = struct.unpack('>i', recv_exact(sock, 4))[0]
                compressed_data = recv_exact(sock, compressed_size)
                
                # You can decompress and process chunk data here if needed.
                # decompressed_data = zlib.decompress(compressed_data)
                # print(f"  Decompressed chunk data size: {len(decompressed_data)}")

                print(f"[MapChunk] X: {x}, Y_Coord: {y_coord}, Z: {z}, Size: ({size_x},{size_y},{size_z}), CompSize: {compressed_size}")
            
            elif pid == 0x34:
                # Multi Block Change (Server to Client only)
                chunk_x = struct.unpack('>i', recv_exact(sock, 4))[0]
                chunk_z = struct.unpack('>i', recv_exact(sock, 4))[0]
                array_size = struct.unpack('>h', recv_exact(sock, 2))[0]
                
                coordinates = []
                for _ in range(array_size):
                    coordinates.append(struct.unpack('>h', recv_exact(sock, 2))[0])
                
                block_types = []
                for _ in range(array_size):
                    block_types.append(struct.unpack('>b', recv_exact(sock, 1))[0])
                
                metadata_array = []
                for _ in range(array_size):
                    metadata_array.append(struct.unpack('>b', recv_exact(sock, 1))[0])
                
                print(f"[MultiBlockChange] ChunkX:{chunk_x}, ChunkZ:{chunk_z}, NumChanges:{array_size}")
                # For detailed output, you could iterate and print each change:
                # for i in range(array_size):
                #     # Decode coordinate short: top 4 bits X, next 4 bits Z, bottom 8 bits Y
                #     coord_short = coordinates[i]
                #     x_offset = (coord_short >> 12) & 0xF
                #     z_offset = (coord_short >> 8) & 0xF
                #     y_offset = coord_short & 0xFF
                #     print(f"  Change {i}: Coord ({x_offset}, {y_offset}, {z_offset}), Type:{block_types[i]}, Meta:{metadata_array[i]}")

            elif pid == 0x35:
                # Block Change (Server to Client only)
                x = struct.unpack('>i', recv_exact(sock, 4))[0]
                y = struct.unpack('>b', recv_exact(sock, 1))[0] # Y is a byte here
                z = struct.unpack('>i', recv_exact(sock, 4))[0]
                block_type = struct.unpack('>b', recv_exact(sock, 1))[0]
                block_metadata = struct.unpack('>b', recv_exact(sock, 1))[0]
                print(f"[BlockChange] X:{x}, Y:{y}, Z:{z}, Type:{block_type}, Metadata:{block_metadata}")

            elif pid == 0x36:
                # Block Action (Server to Client only) - For Note Blocks or Pistons
                x = struct.unpack('>i', recv_exact(sock, 4))[0]
                y = struct.unpack('>h', recv_exact(sock, 2))[0] # Y is short here
                z = struct.unpack('>i', recv_exact(sock, 4))[0]
                byte1 = struct.unpack('>b', recv_exact(sock, 1))[0] # Instrument Type / State
                byte2 = struct.unpack('>b', recv_exact(sock, 1))[0] # Pitch / Direction
                print(f"[BlockAction] X:{x}, Y:{y}, Z:{z}, Byte1:{byte1}, Byte2:{byte2}")

            elif pid == 0x47:
                # Thunderbolt (Server to Client only)
                eid = struct.unpack('>i', recv_exact(sock, 4))[0]
                unknown_bool = struct.unpack('>?', recv_exact(sock, 1))[0]
                x, y, z = struct.unpack('>iii', recv_exact(sock, 12))
                print(f"[Thunderbolt] EID: {eid}, UnknownBool: {unknown_bool}, X:{x}, Y:{y}, Z:{z}")
            
            elif pid == 0x68:
                # Window Items (Server to Client only)
                window_id = struct.unpack('>b', recv_exact(sock, 1))[0]
                count = struct.unpack('>h', recv_exact(sock, 2))[0]
                items = []
                for _ in range(count):
                    item_id = struct.unpack('>h', recv_exact(sock, 2))[0]
                    if item_id != -1: # -1 indicates an empty slot, no further data
                        item_count = struct.unpack('>b', recv_exact(sock, 1))[0]
                        item_damage = struct.unpack('>h', recv_exact(sock, 2))[0]
                        items.append({'id': item_id, 'count': item_count, 'damage': item_damage})
                    else:
                        items.append({'id': -1, 'count': 0, 'damage': 0}) # Represent empty slot
                print(f"[WindowItems] Window ID: {window_id}, Count: {count}, Items: {items}")

            elif pid == 0xFF:
                # Disconnect/Kick
                msg = read_string_utf16(sock)
                print(f"[Disconnect] {msg}")
                break

            else:
                print(f"[ERROR] Unhandled Packet ID: 0x{pid:02X}. Disconnecting to prevent further issues.")
                raise RuntimeError(f"Unhandled packet ID: 0x{pid:02X}")

    except ConnectionError as e:
        print(f"[Connection Error] {e}")
    except struct.error as e:
        print(f"[Protocol Error] Failed to unpack packet data: {e}. Possible desynchronization or incorrect packet structure assumption.")
    except Exception as e:
        print(f"[General Error] {e}")
    finally:
        print("[Packet Handler] Closing socket.")
        sock.close()

# === Connection Logic ===
def connect_to_server():
    global bot_x, bot_y, bot_z, bot_stance, bot_yaw, bot_pitch, bot_on_ground, bot_entity_id

    print(f"Connecting to {SERVER_HOST}:{SERVER_PORT}...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect((SERVER_HOST, SERVER_PORT))
        print("Connected.")

        # Handshake (0x02) - Client to Server
        send_packet(s, 0x02, encode_string_utf16(USERNAME))
        print("[Handshake] Sent.")

        # Read server's handshake response (0x02)
        pid = recv_packet_id(s)
        if pid == 0x02:
            connection_hash = read_string_utf16(s)
            print(f"[Handshake Response (0x02)] Server Connection Hash: '{connection_hash}'")
        else:
            print(f"[Unexpected] Expected 0x02 Handshake Response, got 0x{pid:02X}. Disconnecting.")
            s.close()
            return

        # Login Request (0x01) - Client to Server
        protocol_version = 14  # Beta 1.7.3 protocol version
        login_data = struct.pack('>i', protocol_version)
        login_data += encode_string_utf16(USERNAME)
        # The connection hash from 0x02 Handshake is sent here for authentication purposes.
        # If it was '-', client continues without name auth. If '+', client sends password.
        # For simplicity and given your goal is botting, send the received hash.
        # If the server hash was '-', send '-' back.
        login_data += encode_string_utf16(connection_hash) # Send the hash received from 0x02
        login_data += struct.pack('>q', 0)           # Map seed (ignored by server for client login)
        login_data += struct.pack('>b', 0)           # Dimension (0 for overworld)
        send_packet(s, 0x01, login_data)
        print("[Login] Sent.")

        # Server Response after login
        pid = recv_packet_id(s)
        if pid == 0x01:
            # Login Success (0x01) - Server to Client
            bot_entity_id = struct.unpack('>i', recv_exact(s, 4))[0]
            unknown_string = read_string_utf16(s)
            map_seed = struct.unpack('>q', recv_exact(s, 8))[0]
            dimension = struct.unpack('>b', recv_exact(s, 1))[0]
            print(f"[Login Success (0x01)] Entity ID: {bot_entity_id}, Unknown String: '{unknown_string}', Map Seed: {map_seed}, Dimension: {dimension}")

        elif pid == 0xFF:
            msg = read_string_utf16(s)
            print(f"[Login Failed (0xFF)] {msg}")
            s.close()
            return
        else:
            print(f"[Unexpected] Packet ID after login initial sequence: 0x{pid:02X}. Disconnecting.")
            s.close()
            return

        # Start listening to server packets in a separate thread
        threading.Thread(target=handle_server, args=(s,), daemon=True).start()

        # Main thread sends movement packets to keep connection alive and simulate activity
        print("[Main Thread] Bot is now running. Press Ctrl+C to exit.")
        while True:
            # Send Player Position & Look (0x0D) periodically
            # This is a good way to keep the connection alive and update position.
            # Even if the bot is "standing still", send updates regularly.
            # The server might kick if it doesn't receive movement updates.
            # For this simple bot, we'll just send the last known position.
            
            # Use 0x0D Client to Server structure: X, Y, Stance, Z, Yaw, Pitch, OnGround
            movement_data = struct.pack('>ddddff?', bot_x, bot_y, bot_stance, bot_z, bot_yaw, bot_pitch, bot_on_ground)
            send_packet(s, 0x0D, movement_data)

            time.sleep(0.5) # Send every half second (2 ticks)

    except KeyboardInterrupt:
        print("\n[Exit] Closing connection.")
    except Exception as e:
        print(f"[Connection Error] {e}")
    finally:
        s.close()

# === Entry Point ===
if __name__ == "__main__":
    connect_to_server()
