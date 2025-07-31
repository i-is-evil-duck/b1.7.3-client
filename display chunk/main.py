import json
import base64
import zlib
import struct
import numpy as np
import os # For file system operations
import re # For regular expressions to parse filenames

# Panda3D imports
from direct.showbase.ShowBase import ShowBase
from panda3d.core import GeomVertexFormat, GeomVertexData, GeomVertexWriter, GeomTriangles, Geom, GeomNode, NodePath, Vec3, Point3, LVector3f, LPoint3f, CardMaker, TransparencyAttrib

# --- Constants ---
# Minecraft chunk dimensions
CHUNK_WIDTH = 16
CHUNK_HEIGHT = 256 # Minecraft world height (Minecraft's max build height)
CHUNK_DEPTH = 16
BLOCK_SIZE = 1.0 # Size of one cube in OpenGL/Panda3D units

# --- Global World Data Structure ---
# Stores chunks as { (chunk_x, chunk_z): np.ndarray(shape=(16, 256, 16), dtype=np.uint16) }
# Each element in the numpy array represents a block ID at (local_x, local_y, local_z)
world_chunks = {}

# --- Block Colors (simple mapping for visualization) ---
# You can expand this dictionary with more Minecraft block IDs and their corresponding colors.
# Format: (R, G, B, Alpha) - Alpha is for transparency (0.0 = fully transparent, 1.0 = fully opaque)
BLOCK_COLORS = {
    0: (0.0, 0.0, 0.0, 0.0), # Air (transparent, not rendered)
    1: (0.5, 0.3, 0.1, 1.0), # Dirt (brown)
    2: (0.0, 0.8, 0.0, 1.0), # Grass Block (top green, sides brown) - simplified to green
    3: (0.5, 0.5, 0.5, 1.0), # Stone (grey)
    4: (0.6, 0.6, 0.6, 1.0), # Cobblestone
    5: (0.7, 0.4, 0.2, 1.0), # Wooden Planks
    7: (0.2, 0.2, 0.2, 1.0), # Bedrock (dark grey)
    8: (0.0, 0.0, 1.0, 0.5), # Flowing Water (semi-transparent blue)
    9: (0.0, 0.0, 1.0, 0.5), # Still Water (semi-transparent blue)
    10: (1.0, 0.5, 0.0, 0.5), # Flowing Lava (semi-transparent orange)
    11: (1.0, 0.5, 0.0, 0.5), # Still Lava (semi-transparent orange)
    12: (0.9, 0.8, 0.6, 1.0), # Sand
    13: (0.4, 0.3, 0.2, 1.0), # Gravel
    14: (0.8, 0.6, 0.0, 1.0), # Gold Ore
    15: (0.3, 0.3, 0.3, 1.0), # Iron Ore
    16: (0.1, 0.1, 0.1, 1.0), # Coal Ore
    17: (0.4, 0.3, 0.0, 1.0), # Wood Log (brown)
    18: (0.1, 0.6, 0.1, 0.8), # Leaves (semi-transparent green)
    # Default color for unknown block IDs (magenta)
}

# --- Helper Functions for World Data Management ---

def get_block(abs_x, abs_y, abs_z):
    """
    Gets the block ID at given absolute world coordinates.
    Args:
        abs_x (int): Absolute X coordinate.
        abs_y (int): Absolute Y coordinate.
        abs_z (int): Absolute Z coordinate.
    Returns:
        int: The block ID, or 0 (air) if the chunk is not loaded or coordinates are out of bounds.
    """
    chunk_x = abs_x // CHUNK_WIDTH
    chunk_z = abs_z // CHUNK_DEPTH
    local_x = abs_x % CHUNK_WIDTH
    local_y = abs_y # Y is already absolute within the 256 height
    local_z = abs_z % CHUNK_DEPTH

    chunk_key = (chunk_x, chunk_z)
    if chunk_key in world_chunks:
        if 0 <= local_y < CHUNK_HEIGHT:
            return world_chunks[chunk_key][local_x, local_y, local_z]
    return 0 # Return air if chunk not loaded or out of bounds

def set_block(abs_x, abs_y, abs_z, block_id):
    """
    Sets the block ID at given absolute world coordinates.
    If the chunk does not exist, it will be initialized with air blocks.
    Args:
        abs_x (int): Absolute X coordinate.
        abs_y (int): Absolute Y coordinate.
        abs_z (int): Absolute Z coordinate.
        block_id (int): The new block ID to set.
    """
    chunk_x = abs_x // CHUNK_WIDTH
    chunk_z = abs_z // CHUNK_DEPTH
    local_x = abs_x % CHUNK_WIDTH
    local_y = abs_y
    local_z = abs_z % CHUNK_DEPTH

    chunk_key = (chunk_x, chunk_z)
    if chunk_key not in world_chunks:
        # Initialize a new chunk with air blocks if it doesn't exist
        world_chunks[chunk_key] = np.zeros((CHUNK_WIDTH, CHUNK_HEIGHT, CHUNK_DEPTH), dtype=np.uint16)
    
    if 0 <= local_y < CHUNK_HEIGHT:
        world_chunks[chunk_key][local_x, local_y, local_z] = block_id

# --- Packet Processing Functions ---

def process_packet_0x32(packet):
    """
    Processes Pre-Chunk (0x32) packet.
    Initializes or unloads a chunk in the world_chunks data structure.
    """
    chunk_x = packet['chunk_x']
    chunk_z = packet['chunk_z']
    mode = bool(packet['mode']) # Convert int back to boolean

    chunk_key = (chunk_x, chunk_z)

    if mode: # Initialize chunk
        if chunk_key not in world_chunks:
            # Initialize a new chunk with air blocks (0)
            world_chunks[chunk_key] = np.zeros((CHUNK_WIDTH, CHUNK_HEIGHT, CHUNK_DEPTH), dtype=np.uint16)
            print(f"Initialized empty chunk at ({chunk_x}, {chunk_z}) due to 0x32 packet.")
        else:
            print(f"Chunk ({chunk_x}, {chunk_z}) already exists. Acknowledged 0x32 packet.")
    else: # Unload chunk
        if chunk_key in world_chunks:
            del world_chunks[chunk_key]
            # If the chunk was rendered, remove its NodePath from the scene
            # 'app' is the global instance of MinecraftRenderer
            if chunk_key in app.world_nodes: 
                app.world_nodes[chunk_key].removeNode()
                del app.world_nodes[chunk_key]
            print(f"Unloaded chunk at ({chunk_x}, {chunk_z}) due to 0x32 packet.")
        else:
            print(f"Chunk ({chunk_x}, {chunk_z}) not found for unloading via 0x32 packet.")


def process_packet_0x33(packet):
    """
    Processes Map Chunk (0x33) packet.
    Decodes base64 block data and applies it to the specified region within a chunk.
    """
    # World coordinates for the start of the region
    start_block_x_world = packet['start_block_x_world']
    start_block_y_world = packet['start_block_y_world']
    start_block_z_world = packet['start_block_z_world']

    # Sizes of the region (actual size, add 1)
    size_x = packet['size_x'] + 1
    size_y = packet['size_y'] + 1 # This will often be 128 for full sections
    size_z = packet['size_z'] + 1

    block_types_b64 = packet['block_types_b64']
    # metadata_b64 = packet.get('metadata_b64', '') # Get with default empty string if not present
    # block_light_b64 = packet.get('block_light_b64', '')
    # sky_light_b64 = packet.get('sky_light_b64', '')

    try:
        block_types_data = base64.b64decode(block_types_b64)
        # We are only using block types for rendering for now.
        # If you want to use metadata/light, you'd decode them similarly:
        # metadata_data = base64.b64decode(metadata_b64)
        # block_light_data = base64.b64decode(block_light_b64)
        # sky_light_data = base64.b64decode(sky_light_b64)

        # Determine the chunk key based on the world coordinates of the region
        target_chunk_x = start_block_x_world // CHUNK_WIDTH
        target_chunk_z = start_block_z_world // CHUNK_DEPTH
        chunk_key = (target_chunk_x, target_chunk_z)

        if chunk_key not in world_chunks:
            # If 0x33 arrives before 0x32 for this chunk, initialize it with air
            print(f"  [WARN] Received 0x33 for unknown chunk ({target_chunk_x}, {target_chunk_z}). Initializing with air.")
            world_chunks[chunk_key] = np.zeros((CHUNK_WIDTH, CHUNK_HEIGHT, CHUNK_DEPTH), dtype=np.uint16)

        current_chunk_array = world_chunks[chunk_key]

        # Iterate over the region defined by the packet
        for lx_region in range(size_x):
            for lz_region in range(size_z):
                for ly_region in range(size_y):
                    # Calculate the index within the incoming block_types_data
                    # This indexing matches the Minecraft protocol's internal layout for 0x33
                    incoming_data_index = ly_region + (lz_region * size_y) + (lx_region * size_y * size_z)
                    
                    if incoming_data_index >= len(block_types_data):
                        print(f"  [WARN] Ran out of block type data for 0x33 packet at index {incoming_data_index}. Stopping parsing for this packet.")
                        break # Break from innermost loop
                    
                    block_id_byte = block_types_data[incoming_data_index]
                    
                    # Calculate the absolute world coordinates for this block
                    abs_x = start_block_x_world + lx_region
                    abs_y = start_block_y_world + ly_region
                    abs_z = start_block_z_world + lz_region

                    # Convert to local chunk coordinates (0-15 for X, Z; 0-255 for Y)
                    local_x_in_chunk = abs_x % CHUNK_WIDTH
                    local_z_in_chunk = abs_z % CHUNK_DEPTH
                    local_y_in_chunk = abs_y # Y is already absolute within the chunk's 256 height

                    # Ensure the calculated local coordinates are within the 16x256x16 chunk bounds
                    if 0 <= local_x_in_chunk < CHUNK_WIDTH and \
                       0 <= local_y_in_chunk < CHUNK_HEIGHT and \
                       0 <= local_z_in_chunk < CHUNK_DEPTH:
                        current_chunk_array[local_x_in_chunk, local_y_in_chunk, local_z_in_chunk] = block_id_byte
                    else:
                        # This should ideally not happen if packet data is correct and CHUNK_HEIGHT is 256
                        print(f"  [WARN] Calculated local chunk coordinate ({local_x_in_chunk}, {local_y_in_chunk}, {local_z_in_chunk}) out of bounds for chunk {chunk_key}. Skipping block.")
                else: # This 'else' belongs to the innermost 'for' loop
                    continue # Continue to next lz_region if no break from inner loop
                break # Break from lz_region loop if innermost loop broke
            else: # This 'else' belongs to the lz_region 'for' loop
                continue # Continue to next lx_region if no break from middle loop
            break # Break from lx_region loop if middle loop broke

        print(f"Processed 0x33 update for chunk ({target_chunk_x}, {target_chunk_z}) covering region from ({start_block_x_world},{start_block_y_world},{start_block_z_world}) to ({start_block_x_world+size_x-1},{start_block_y_world+size_y-1},{start_block_z_world+size_z-1}).")

    except KeyError as e:
        print(f"Error: Missing key in 0x33 packet: {e}. Skipping packet.")
    except base64.binascii.Error as e:
        print(f"Error: Base64 decoding failed for 0x33 packet: {e}. Skipping packet.")
    except Exception as e:
        print(f"An unexpected error occurred processing 0x33 packet: {e}. Skipping packet.")

def process_packet_0x34(packet):
    """
    Processes Block Change (0x34) packet.
    Updates a single block at absolute world coordinates.
    """
    abs_x = packet['x']
    abs_y = packet['y']
    abs_z = packet['z']
    block_id = packet['block_id']
    set_block(abs_x, abs_y, abs_z, block_id)
    print(f"Processed block change at ({abs_x}, {abs_y}, {abs_z}) to block ID {block_id}")

def process_packet_0x35(packet):
    """
    Processes Explosion (0x35) packet.
    Sets all affected blocks to air (block ID 0).
    """
    # For simplicity, we just set affected blocks to air
    for block_coords in packet['affected_blocks']:
        abs_x, abs_y, abs_z = block_coords
        set_block(abs_x, abs_y, abs_z, 0) # Set to air
    print(f"Processed explosion affecting {len(packet['affected_blocks'])} blocks.")


def load_chunk_data_from_jsonl(filename="recorded_chunk_data.jsonl"):
    """
    Reads the JSONL file, parses each packet, and populates the world_chunks data structure.
    Args:
        filename (str): The path to the JSONL file containing recorded chunk data.
    """
    print(f"Attempting to process event data from '{filename}'...")
    try:
        with open(filename, 'r') as f:
            for line_num, line in enumerate(f):
                try:
                    packet = json.loads(line.strip())
                    packet_id_str = packet.get('packet_id')
                    
                    # Convert hex string to integer if it's a string like "0x32"
                    if isinstance(packet_id_str, str) and packet_id_str.startswith('0x'):
                        packet_id = int(packet_id_str, 16)
                    else:
                        packet_id = packet_id_str # Assume it's already an int

                    if packet_id == 0x32:
                        process_packet_0x32(packet) 
                    elif packet_id == 0x33:
                        process_packet_0x33(packet)
                    elif packet_id == 0x34:
                        process_packet_0x34(packet)
                    elif packet_id == 0x35:
                        process_packet_0x35(packet)
                    else:
                        print(f"Warning: Unknown packet_id: {hex(packet_id)} on line {line_num + 1}. Skipping.")
                except json.JSONDecodeError as e:
                    print(f"Error: JSON decoding failed on line {line_num + 1}: {e}. Skipping line.")
                except KeyError as e:
                    print(f"Error: Missing key in packet on line {line_num + 1}: {e}. Skipping line.")
                except Exception as e:
                    print(f"An unexpected error occurred processing packet on line {line_num + 1}: {e}. Skipping line.")
    except FileNotFoundError:
        print(f"Error: The file '{filename}' was not found. Please ensure it's in the same directory as the script.")
    except Exception as e:
        print(f"An unexpected error occurred while loading data from '{filename}': {e}")
    print(f"Finished processing event data from '{filename}'.")


# --- Panda3D Rendering Functions ---

def create_chunk_mesh(chunk_array, chunk_offset_x, chunk_offset_z):
    """
    Creates a Panda3D GeomNode representing the mesh for a single chunk.
    This function generates the vertices and triangles for all visible blocks in the chunk.
    Args:
        chunk_array (np.ndarray): The 3D numpy array of block IDs for the chunk.
        chunk_offset_x (int): The X coordinate of the chunk in chunk units.
        chunk_offset_z (int): The Z coordinate of the chunk in chunk units.
    Returns:
        NodePath: A NodePath containing the GeomNode for the chunk's mesh.
    """
    format = GeomVertexFormat.getV3c4() # Vertices (3 floats), Colors (4 floats - RGBA)
    vdata = GeomVertexData('chunk_data', format, Geom.UHDynamic)
    
    # Writers for vertex positions and colors
    vertex_writer = GeomVertexWriter(vdata, 'vertex')
    color_writer = GeomVertexWriter(vdata, 'color')

    # Triangles for the mesh
    prim = GeomTriangles(Geom.UHDynamic)

    # Vertices for a unit cube (Panda3D: X-right, Y-forward, Z-up)
    # Mapping Minecraft (X, Y, Z) to Panda3D (X, Z, Y)
    # Minecraft X -> Panda3D X
    # Minecraft Y (height) -> Panda3D Z (height)
    # Minecraft Z -> Panda3D Y (depth/forward)

    # Define cube vertices relative to block origin (0,0,0) in Panda3D's coordinate system
    # (x, y, z) where x is right, y is forward, z is up
    p3d_vertices = [
        (0, 0, 0), (BLOCK_SIZE, 0, 0), (BLOCK_SIZE, BLOCK_SIZE, 0), (0, BLOCK_SIZE, 0), # Bottom face (Z=0)
        (0, 0, BLOCK_SIZE), (BLOCK_SIZE, 0, BLOCK_SIZE), (BLOCK_SIZE, BLOCK_SIZE, BLOCK_SIZE), (0, BLOCK_SIZE, BLOCK_SIZE) # Top face (Z=BLOCK_SIZE)
    ]

    # Faces as lists of vertex indices (clockwise or counter-clockwise winding matters for culling)
    # Each face is defined by two triangles.
    # We'll define triangles explicitly to ensure correct winding.
    # (v0, v1, v2), (v0, v2, v3) for a quad (v0, v1, v2, v3)
    p3d_face_triangles = [
        # Front face (Y=0)
        (0, 4, 5), (0, 5, 1), 
        # Back face (Y=BLOCK_SIZE)
        (3, 2, 6), (3, 6, 7), 
        # Left face (X=0)
        (0, 3, 7), (0, 7, 4), 
        # Right face (X=BLOCK_SIZE)
        (1, 5, 6), (1, 6, 2), 
        # Top face (Z=BLOCK_SIZE)
        (4, 7, 6), (4, 6, 5), 
        # Bottom face (Z=0)
        (0, 1, 2), (0, 2, 3)  
    ]

    # Iterate through each local block coordinate within the chunk
    for local_x in range(CHUNK_WIDTH):
        for local_y in range(CHUNK_HEIGHT): # Minecraft Y (height)
            for local_z in range(CHUNK_DEPTH): # Minecraft Z (depth)
                block_id = chunk_array[local_x, local_y, local_z]
                
                if block_id != 0: # Only draw non-air blocks
                    color = BLOCK_COLORS.get(block_id, (1.0, 0.0, 1.0, 1.0)) 
                    
                    # Calculate absolute world coordinates for the block's origin in Panda3D's system
                    # Minecraft (X, Y, Z) -> Panda3D (X, Z, Y)
                    abs_p3d_x = chunk_offset_x * CHUNK_WIDTH + local_x * BLOCK_SIZE
                    abs_p3d_y = chunk_offset_z * CHUNK_DEPTH + local_z * BLOCK_SIZE # Minecraft Z becomes Panda3D Y
                    abs_p3d_z = local_y * BLOCK_SIZE # Minecraft Y becomes Panda3D Z

                    # Add vertices and triangles for the cube
                    base_vertex_index = vdata.getNumRows()
                    for vx, vy, vz in p3d_vertices:
                        vertex_writer.addData3f(abs_p3d_x + vx, abs_p3d_y + vy, abs_p3d_z + vz)
                        color_writer.addData4f(*color)

                    for tri_indices in p3d_face_triangles:
                        prim.addVertices(base_vertex_index + tri_indices[0], 
                                         base_vertex_index + tri_indices[1], 
                                         base_vertex_index + tri_indices[2])
    
    geom = Geom(vdata)
    geom.addPrimitive(prim)
    
    node = GeomNode('chunk-mesh')
    node.addGeom(geom)
    
    # Enable transparency if any block has alpha < 1.0
    if any(c[3] < 1.0 for c in BLOCK_COLORS.values()):
        node.setAttrib(TransparencyAttrib.make(TransparencyAttrib.MAlpha))

    return NodePath(node)


class MinecraftRenderer(ShowBase):
    """
    Main Panda3D application class for rendering Minecraft chunks.
    """
    def __init__(self):
        ShowBase.__init__(self)

        self.disableMouse() # Disable default camera control
        
        # Set up a more suitable camera position for a top-down/isometric view
        # Minecraft (X, Y, Z) -> Panda3D (X, Z, Y)
        # Camera position: (X, Y, Z) in Panda3D coords
        # Look-at point: (X, Y, Z) in Panda3D coords
        self.camera.setPos(0, -100, 150) # Slightly back (negative Y), up (positive Z)
        self.camera.lookAt(0, 0, 50)   # Look at a point around Y=50 (Minecraft height)

        self.world_nodes = {} # To store NodePaths for each chunk for easy management

        # Process the JSONL file for all chunk data (0x32, 0x33, 0x34, 0x35)
        load_chunk_data_from_jsonl()
        print("Chunk data processing complete. Starting rendering loop.")

        self.render_all_chunks()

        # Set up basic camera controls
        self.accept('escape', self.userExit) # Exit on Escape key
        self.accept('w', self.move_camera, [0, 1, 0])
        self.accept('s', self.move_camera, [0, -1, 0])
        self.accept('a', self.move_camera, [-1, 0, 0])
        self.accept('d', self.move_camera, [1, 0, 0])
        self.accept('q', self.move_camera, [0, 0, 1]) # Move camera up
        self.accept('e', self.move_camera, [0, 0, -1]) # Move camera down
        
        # Mouse-based camera rotation (simple orbit)
        self.mouse_x = 0
        self.mouse_y = 0
        self.is_mouse_down = False
        self.accept('mouse1', self.on_mouse_down)
        self.accept('mouse1-up', self.on_mouse_up)
        self.taskMgr.add(self.camera_task, 'cameraTask')

    def move_camera(self, dx, dy, dz):
        """Moves the camera relative to its current orientation."""
        # Use getRelativeVector to move relative to camera's orientation
        # dx, dy, dz are multipliers for movement speed
        speed = 5.0
        self.camera.setPos(self.camera, LVector3f(dx * speed, dy * speed, dz * speed))

    def on_mouse_down(self):
        """Records mouse position when mouse button 1 is pressed."""
        if self.mouseWatcherNode.hasMouse():
            self.mouse_x = self.mouseWatcherNode.getMouseX()
            self.mouse_y = self.mouseWatcherNode.getMouseY()
            self.is_mouse_down = True

    def on_mouse_up(self):
        """Resets mouse state when mouse button 1 is released."""
        self.is_mouse_down = False

    def camera_task(self, task):
        """Task to handle mouse-based camera rotation."""
        if self.is_mouse_down and self.mouseWatcherNode.hasMouse():
            new_mouse_x = self.mouseWatcherNode.getMouseX()
            new_mouse_y = self.mouseWatcherNode.getMouseY()

            dx = new_mouse_x - self.mouse_x
            dy = new_mouse_y - self.mouse_y

            self.mouse_x = new_mouse_x
            self.mouse_y = new_mouse_y

            # Rotate around the look-at point (0, 0, 50)
            # This is a simplified orbit. For a true orbit, you'd calculate the vector
            # from camera to target, rotate that vector, and then set camera position.
            
            # Get current camera HPR (Heading, Pitch, Roll)
            h, p, r = self.camera.getHpr()

            # Adjust HPR based on mouse movement
            # dx affects Heading (yaw), dy affects Pitch
            self.camera.setHpr(h - dx * 100, p + dy * 100, r) 
            
            # To orbit around a point, you'd typically do something like:
            # target_pos = Point3(0, 0, 50)
            # current_pos = self.camera.getPos()
            # vec_to_target = target_pos - current_pos
            # # Rotate vec_to_target by dx, dy
            # # Then set self.camera.setPos(target_pos - rotated_vec)
            # For this example, direct HPR manipulation is simpler, but it rotates the camera itself, not orbits.
            # A full orbit would require more complex matrix transformations or a dedicated camera controller.

        return task.cont # Continue the task

    def render_all_chunks(self):
        """
        Renders all loaded chunks by creating Panda3D meshes for them.
        """
        # Iterate over a copy of world_chunks.items() to allow modification during iteration if needed
        for (chunk_x, chunk_z), chunk_array in list(world_chunks.items()): 
            # Check if chunk already has a node (e.g., if updated by 0x33/0x34 packets)
            if (chunk_x, chunk_z) in self.world_nodes:
                # Remove old node if it exists (for updates)
                self.world_nodes[(chunk_x, chunk_z)].removeNode()
                del self.world_nodes[(chunk_x, chunk_z)] # Remove from dictionary

            # Only create a mesh if the chunk still exists in world_chunks (not unloaded by 0x32)
            if (chunk_x, chunk_z) in world_chunks:
                chunk_node = create_chunk_mesh(chunk_array, chunk_x, chunk_z)
                chunk_node.reparentTo(self.render) # Attach the chunk mesh to the scene graph
                self.world_nodes[(chunk_x, chunk_z)] = chunk_node


if __name__ == "__main__":
    app = MinecraftRenderer()
    app.run()

