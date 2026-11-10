# SPDX-License-Identifier: GPL-3.0-only
# DAT format reference: https://github.com/skwasjer/SilentHunter

bl_info = {
    "name": "Silent Hunter DAT Exporter",
    "author": "OpenAI",
    "version": (1, 0, 0),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar > Silent Hunter",
    "description": "Import and export Silent Hunter DAT models, materials, and textures",
    "category": "Import-Export",
}

import bpy
import os
import re
import shutil
import struct
import subprocess
import tempfile
from mathutils import Euler, Matrix, Vector
from bpy.props import StringProperty, EnumProperty, PointerProperty, BoolProperty
from bpy.types import Operator, Panel, PropertyGroup


def _read_chunks(filepath):
    with open(filepath, "rb") as f:
        data = f.read()
    if len(data) < 12 or struct.unpack_from("<I", data, 0)[0] != 0x716D0DA4:
        raise ValueError("This does not look like a Silent Hunter DAT file.")
    chunks = []
    pos = 12
    while pos < len(data):
        if pos + 12 > len(data):
            raise ValueError("Truncated chunk header at byte %d." % pos)
        magic, subtype, size = struct.unpack_from("<iii", data, pos)
        start, end = pos + 12, pos + 12 + size
        if size < 0 or end > len(data):
            raise ValueError("Invalid chunk size at byte %d." % pos)
        chunks.append((magic, subtype, data[start:end]))
        pos = end
    return chunks


def _read_model(payload):
    view = memoryview(payload)
    offset = 8  # 64-bit model id
    if len(view) < 13:
        raise ValueError("Model chunk is too short.")
    offset += 1  # model type
    vertex_count = struct.unpack_from("<i", view, offset)[0]
    offset += 4
    if vertex_count < 0 or offset + vertex_count * 12 + 4 > len(view):
        raise ValueError("Invalid vertex count in model chunk.")
    vertices = list(struct.iter_unpack("<3f", view[offset:offset + vertex_count * 12]))
    offset += vertex_count * 12
    face_count = struct.unpack_from("<i", view, offset)[0]
    offset += 4
    if face_count < 0 or offset + face_count * 13 + 4 > len(view):
        raise ValueError("Invalid face count in model chunk.")
    faces, face_uvs, face_materials = [], [], []
    for _ in range(face_count):
        triangle = struct.unpack_from("<3H", view, offset); offset += 6
        faces.append((triangle[0], triangle[2], triangle[1]))
        uv_triangle = struct.unpack_from("<3H", view, offset); offset += 6
        face_uvs.append((uv_triangle[0], uv_triangle[2], uv_triangle[1]))
        face_materials.append(view[offset]); offset += 1
    uv_count = struct.unpack_from("<i", view, offset)[0]
    offset += 4
    if uv_count < 0 or offset + uv_count * 8 > len(view):
        raise ValueError("Invalid UV count in model chunk.")
    uvs = list(struct.iter_unpack("<2f", view[offset:offset + uv_count * 8]))
    offset += uv_count * 8
    uv_maps = {1: face_uvs}
    normals = []
    while offset < len(view):
        descriptor = bytes(view[offset:offset + 4]).decode("ascii", "replace")
        offset += 4
        if descriptor == "TMAP":
            map_count = view[offset]; offset += 1
            for _ in range(map_count):
                channel = view[offset]; offset += 1
                count = face_count * 3
                if offset + count * 2 > len(view):
                    raise ValueError("Truncated TMAP indices in model chunk.")
                indices = list(struct.unpack_from("<%dH" % count, view, offset)) if count else []
                offset += count * 2
                uv_maps[channel] = [(indices[i], indices[i + 2], indices[i + 1])
                                    for i in range(0, count, 3)]
        elif descriptor == "NORM":
            normals = list(struct.iter_unpack("<3f", view[offset:offset + vertex_count * 12]))
            offset += vertex_count * 12
            if offset > len(view):
                raise ValueError("Truncated normals in model chunk.")
        else:
            raise ValueError("Unknown model mesh descriptor %r." % descriptor)
    return vertices, faces, face_uvs, face_materials, uvs, uv_maps, normals


def _read_material(payload):
    if len(payload) < 12:
        return None
    mat_id = struct.unpack_from("<Q", payload, 0)[0]
    # MaterialChunk writes opacity followed by B, G, R, and the original
    # texture filename (when present) starts after its 36-byte fixed header.
    a, b, g, r = payload[8:12]
    texture = payload[36:].split(b"\0", 1)[0].decode("latin1", "replace") if len(payload) > 36 else ""
    spec_mode = payload[12] if len(payload) > 12 else 0
    spec_b, spec_g, spec_r = payload[13:16] if len(payload) >= 16 else (0, 0, 0)
    strength = payload[16] if len(payload) > 16 else 0
    gloss = payload[17] if len(payload) > 17 else 0
    emission = payload[18] if len(payload) > 18 else 0
    return mat_id, {
        "color": (r / 255.0, g / 255.0, b / 255.0, a / 255.0),
        "texture": texture,
        "specular": (spec_r / 255.0, spec_g / 255.0, spec_b / 255.0),
        "spec_mode": spec_mode,
        "strength": strength / 255.0,
        "gloss": gloss / 255.0,
        "emission": emission / 255.0,
    }


def _read_texture_map(payload):
    if len(payload) < 44:
        return None
    _map_id, parent_id = struct.unpack_from("<QQ", payload, 0)
    map_type = payload[16:24].split(b"\0", 1)[0].decode("ascii", "replace")
    channel = struct.unpack_from("<i", payload, 24)[0]
    texture = payload[44:].split(b"\0", 1)[0].decode("latin1", "replace")
    return parent_id, {"type": map_type, "channel": channel, "texture": texture}


def _read_node(subtype, payload):
    if len(payload) < 54:
        raise ValueError("Node chunk is too short.")
    node_id, parent_id, model_id = struct.unpack_from("<QQQ", payload, 0)
    visible = payload[25] != 0
    tx, ty, tz, rx, ry, rz = struct.unpack_from("<6f", payload, 26)
    count = struct.unpack_from("<i", payload, 50)[0]
    if count < 0 or count > (len(payload) - 54) // 8:
        raise ValueError("Invalid node material list.")
    material_ids = list(struct.unpack_from("<%dQ" % count, payload, 54)) if count else []
    return {
        "id": node_id, "parent": parent_id, "model": model_id,
        "visible": visible, "translation": (tx, ty, tz),
        "rotation": (rx, ry, rz), "materials": material_ids,
        "subtype": subtype,
    }


def _texture_root(source):
    data_dir = os.path.dirname(os.path.abspath(source))
    while data_dir and os.path.basename(data_dir).casefold() != "data":
        parent = os.path.dirname(data_dir)
        if parent == data_dir:
            return os.path.join(os.path.dirname(os.path.abspath(source)), "Textures")
        data_dir = parent
    return os.path.join(data_dir, "Textures")


def _texture_index(source):
    texture_root = _texture_root(source)
    index = {}
    if os.path.isdir(texture_root):
        for folder, _dirs, files in os.walk(texture_root):
            for filename in files:
                key = filename.casefold()
                index.setdefault(key, []).append(os.path.join(folder, filename))
    return index


def _resolve_texture(name, index):
    if not name:
        return None
    exact = index.get(os.path.basename(name).casefold(), [])
    if exact:
        return sorted(exact, key=lambda p: ("tnormal" not in p.casefold(), p.casefold()))[0]
    stem = os.path.splitext(os.path.basename(name))[0].casefold()
    candidates = [p for files in index.values() for p in files if os.path.splitext(os.path.basename(p))[0].casefold() == stem]
    if not candidates:
        return None
    wanted_ext = os.path.splitext(name)[1].casefold()
    return sorted(candidates, key=lambda p: (os.path.splitext(p)[1].casefold() != wanted_ext,
                                              "tnormal" not in p.casefold(), p.casefold()))[0]


def _load_packed_image(filepath, cache):
    if filepath not in cache:
        image = bpy.data.images.load(filepath, check_existing=True)
        image.pack()
        cache[filepath] = image
    return cache[filepath]


def _load_missing_image(name, texture_root, cache):
    filename = os.path.basename(name)
    cache_key = "MISSING:" + filename.casefold()
    if cache_key not in cache:
        expected_path = os.path.join(texture_root, filename)
        image = bpy.data.images.new(filename, width=1, height=1, alpha=True)
        image.source = "FILE"
        image.filepath = expected_path
        image.filepath_raw = expected_path
        image["DAT missing texture"] = True
        cache[cache_key] = image
    return cache[cache_key]


def _new_material(mat_id, record, texture_maps, texture_index, texture_root, image_cache):
    name = record["texture"] or next((m["texture"] for m in texture_maps if m["texture"]), "Material")
    material = bpy.data.materials.new(name)
    color = record["color"]
    material.diffuse_color = color
    material.use_fake_user = True
    material.use_backface_culling = True
    material["DAT material ID"] = "%016X" % mat_id
    material["sh_dat_diffuse_texture"] = record["texture"]
    nodes = material.node_tree.nodes if material.node_tree else None
    links = material.node_tree.links if material.node_tree else None
    shader = nodes.get("Principled BSDF") if nodes else None
    if not shader:
        return material
    shader.inputs["Base Color"].default_value = color
    shader.inputs["Alpha"].default_value = color[3]
    shader.inputs["Roughness"].default_value = max(0.08, 1.0 - record["gloss"])
    spec_input = shader.inputs.get("Specular IOR Level") or shader.inputs.get("Specular")
    if spec_input:
        spec_input.default_value = record["strength"]
    emission_strength = shader.inputs.get("Emission Strength")
    if emission_strength:
        emission_strength.default_value = record["emission"]

    def add_image(name, channel):
        if not name:
            return None
        image_path = _resolve_texture(name, texture_index)
        image_node = nodes.new("ShaderNodeTexImage")
        if image_path:
            image_node.image = _load_packed_image(image_path, image_cache)
        else:
            image_node.image = _load_missing_image(name, texture_root, image_cache)
        image_node.label = name
        if channel:
            uv_node = nodes.new("ShaderNodeUVMap")
            uv_node.uv_map = "UVMap_%d" % channel
            links.new(uv_node.outputs["UV"], image_node.inputs["Vector"])
        return image_node

    diffuse = add_image(record["texture"], 1)
    if diffuse:
        material["sh_dat_diffuse_image"] = diffuse.image.filepath
        links.new(diffuse.outputs["Color"], shader.inputs["Base Color"])
    for tex_map in texture_maps:
        image_node = add_image(tex_map["texture"], tex_map["channel"])
        if not image_node:
            continue
        kind = tex_map["type"].casefold()
        if kind == "bump":
            material["sh_dat_bump_image"] = image_node.image.filepath
            normal = nodes.new("ShaderNodeNormalMap")
            links.new(image_node.outputs["Color"], normal.inputs["Color"])
            links.new(normal.outputs["Normal"], shader.inputs["Normal"])
        elif kind == "specular":
            material["sh_dat_specular_image"] = image_node.image.filepath
            target = shader.inputs.get("Specular IOR Level") or shader.inputs.get("Specular")
            if target:
                links.new(image_node.outputs["Color"], target)
        elif kind == "selfillu":
            material["sh_dat_emission_image"] = image_node.image.filepath
            target = shader.inputs.get("Emission Color") or shader.inputs.get("Emission")
            if target:
                links.new(image_node.outputs["Color"], target)
    return material


def _unique_mesh_object_name(base_name):
    base_name = base_name or "Model"
    candidate = base_name
    suffix = 1
    while bpy.data.objects.get(candidate) is not None or bpy.data.meshes.get(candidate) is not None:
        candidate = "%s.%03d" % (base_name, suffix)
        suffix += 1
    return candidate


def _matrix_for_node(node, root_offset):
    tx, ty, tz = node["translation"]
    if node["parent"] == 0:
        tx -= root_offset.x; ty -= root_offset.y; tz -= root_offset.z
    rx, ry, rz = node["rotation"]
    rotation = Euler((-rx, -ry, -rz), "XYZ").to_matrix().to_4x4()
    return Matrix.Translation(Vector((tx, ty, tz))) @ rotation


def _build_export_collection(filepath, link_to_scene=False, flip_v=True):
    chunks = _read_chunks(filepath)
    models, materials, nodes = {}, {}, {}
    texture_maps = {}
    labels = {}
    for magic, subtype, payload in chunks:
        if magic == 1:
            model_id = struct.unpack_from("<Q", payload, 0)[0]
            models[model_id] = _read_model(payload)
        elif magic == 2:
            result = _read_material(payload)
            if result:
                materials[result[0]] = result[1]
        elif magic == 13:
            result = _read_texture_map(payload)
            if result:
                texture_maps.setdefault(result[0], []).append(result[1])
        elif magic == 4:
            node = _read_node(subtype, payload)
            nodes[node["id"]] = node
        elif magic == 8 and len(payload) >= 8:
            parent_id = struct.unpack_from("<Q", payload, 0)[0]
            label = payload[8:].split(b"\0", 1)[0].decode("latin1", "replace").strip()
            if label:
                labels[parent_id] = label
    if not models:
        raise ValueError("No model chunks were found in the DAT file.")

    stem = os.path.splitext(os.path.basename(filepath))[0]
    collection = bpy.data.collections.new("Silent Hunter - " + stem)
    if link_to_scene:
        bpy.context.scene.collection.children.link(collection)
    model_nodes = {node["model"]: node for node in nodes.values() if node["model"] in models}
    root = next((n for n in nodes.values() if n["subtype"] == 3), None)
    root_offset = Vector(root["translation"]) if root else Vector((0, 0, 0))
    world_matrices = {}

    def world_matrix(node_id, stack=None):
        if node_id in world_matrices:
            return world_matrices[node_id]
        stack = stack or set()
        if node_id in stack:
            raise ValueError("Cycle found in node hierarchy.")
        stack.add(node_id)
        node = nodes[node_id]
        local = _matrix_for_node(node, root_offset)
        parent = node["parent"]
        result = world_matrix(parent, stack) @ local if parent in nodes else local
        stack.remove(node_id)
        world_matrices[node_id] = result
        return result

    export_objects = []
    # Create a transform node for each source node. These retain the DAT hierarchy.
    empty_by_id = {}
    for node in nodes.values():
        label = labels.get(node["id"], "")
        empty_name = ("Node - " + label) if label else "Node"
        empty = bpy.data.objects.new(empty_name, None)
        empty["DAT node ID"] = "%016X" % node["id"]
        if label:
            empty["DAT label"] = label
        empty.empty_display_type = "PLAIN_AXES"
        empty.hide_render = True
        collection.objects.link(empty)
        empty_by_id[node["id"]] = empty
    for node in nodes.values():
        empty = empty_by_id[node["id"]]
        parent = empty_by_id.get(node["parent"])
        if parent:
            empty.parent = parent
            empty.matrix_parent_inverse = Matrix.Identity(4)
            # world_matrix contains root-normalized transforms; set after parenting.
            empty.matrix_world = world_matrix(node["id"])
        else:
            empty.matrix_world = world_matrix(node["id"])

    material_cache = {}
    image_cache = {}
    texture_files = _texture_index(filepath)
    texture_root = _texture_root(filepath)
    # Keep every material chunk, including unused entries, and preserve the
    # source texture filename as the Blender datablock name.
    for mat_id, record in materials.items():
        material_cache[mat_id] = _new_material(
            mat_id, record, texture_maps.get(mat_id, []), texture_files, texture_root, image_cache)

    def get_material(mat_id):
        if mat_id not in material_cache:
            material_cache[mat_id] = _new_material(mat_id, {
                "texture": "", "color": (0.58, 0.58, 0.58, 1.0),
                "specular": (0.0, 0.0, 0.0), "spec_mode": 0,
                "strength": 0.0, "gloss": 0.0, "emission": 0.0,
            }, [], texture_files, texture_root, image_cache)
        return material_cache[mat_id]

    for model_id, data in models.items():
        vertices, faces, face_uvs, face_mats, uvs, uv_maps, normals = data
        node = model_nodes.get(model_id)
        node_materials = node["materials"] if node else []
        label = labels.get(node["id"], "") if node else ""
        object_name = _unique_mesh_object_name(label or "Model")
        mesh = bpy.data.meshes.new(object_name)
        mesh.from_pydata(vertices, [], faces)
        mesh.update()
        if len(normals) == len(vertices) and normals:
            for polygon in mesh.polygons:
                polygon.use_smooth = True
            mesh.normals_split_custom_set_from_vertices(normals)
        obj = bpy.data.objects.new(object_name, mesh)
        obj["DAT model ID"] = "%016X" % model_id
        if label:
            obj["DAT label"] = label
        collection.objects.link(obj)
        if node:
            obj.parent = empty_by_id[node["id"]]
            obj.matrix_parent_inverse = Matrix.Identity(4)
            obj.matrix_world = world_matrix(node["id"])
            obj.hide_render = not node["visible"]
        # Slot order corresponds to the material ids on the owning scene node.
        max_index = max(face_mats, default=-1)
        for slot_index in range(max(max_index + 1, len(node_materials))):
            mat_id = node_materials[slot_index] if slot_index < len(node_materials) else (model_id + slot_index)
            mesh.materials.append(get_material(mat_id))
        for polygon, material_index in zip(mesh.polygons, face_mats):
            polygon.material_index = min(material_index, max(0, len(mesh.materials) - 1))
        if uvs and faces:
            for channel, channel_face_uvs in uv_maps.items():
                uv_layer = mesh.uv_layers.new(name="UVMap_%d" % channel)
                flat_uvs = []
                for poly in mesh.polygons:
                    corner_uvs = channel_face_uvs[poly.index]
                    for uv_index in corner_uvs:
                        uv = uvs[uv_index] if uv_index < len(uvs) else (0.0, 0.0)
                        # Silent Hunter's UV V origin is top-left; Blender's is bottom-left.
                        flat_uvs.extend((uv[0], 1.0 - uv[1] if flip_v else uv[1]))
                uv_layer.data.foreach_set("uv", flat_uvs)
            if mesh.uv_layers:
                mesh.uv_layers.active = mesh.uv_layers.get("UVMap_1")
        export_objects.append(obj)
    return collection, export_objects, stem


def _write_obj(filepath, objects):
    # Small deterministic OBJ writer avoids exporter-version/API differences.
    mtl_path = os.path.splitext(filepath)[0] + ".mtl"
    output_dir = os.path.dirname(filepath)
    copied_images = {}

    def copy_texture(source_path):
        if not source_path:
            return None
        source_path = bpy.path.abspath(source_path)
        if not os.path.isfile(source_path):
            return None
        filename = os.path.basename(source_path)
        destination = os.path.join(output_dir, filename)
        if source_path not in copied_images:
            if os.path.abspath(source_path) != os.path.abspath(destination):
                if os.path.exists(destination) and os.path.getsize(destination) != os.path.getsize(source_path):
                    stem, ext = os.path.splitext(filename)
                    filename = stem + "_texture" + ext
                    destination = os.path.join(output_dir, filename)
                shutil.copy2(source_path, destination)
            copied_images[source_path] = filename
        return copied_images[source_path]

    with open(filepath, "w", encoding="utf-8") as obj_file, open(mtl_path, "w", encoding="utf-8") as mtl_file:
        obj_file.write("mtllib %s\n" % os.path.basename(mtl_path))
        material_names = {}
        for ob in objects:
            for material in ob.data.materials:
                if material:
                    safe = re.sub(r"[^A-Za-z0-9_]+", "_", material.name)
                    material_names[material.name] = safe
                    color = material.diffuse_color
                    mtl_file.write("newmtl %s\nKd %.6f %.6f %.6f\nKa 0.1 0.1 0.1\nKs 0.05 0.05 0.05\nNs 12\n" % (safe, color[0], color[1], color[2]))
                    for key, directive in (("sh_dat_diffuse_image", "map_Kd"),
                                            ("sh_dat_bump_image", "map_Bump"),
                                            ("sh_dat_specular_image", "map_Ks"),
                                            ("sh_dat_emission_image", "map_Ke")):
                        copied = copy_texture(material.get(key, ""))
                        if copied:
                            mtl_file.write("%s %s\n" % (directive, copied))
                    mtl_file.write("\n")
        vo = uo = no = 1
        for ob in objects:
            mesh = ob.data
            mesh.calc_loop_triangles()
            transform = ob.matrix_world
            normal_xform = transform.to_3x3().inverted().transposed()
            obj_file.write("\no %s\n" % re.sub(r"\s+", "_", ob.name))
            for vertex in mesh.vertices:
                v = transform @ vertex.co
                obj_file.write("v %.7f %.7f %.7f\n" % (v.x, v.y, v.z))
            uv_layer = mesh.uv_layers.active
            for loop in mesh.loops:
                uv = uv_layer.data[loop.index].uv if uv_layer else (0.0, 0.0)
                obj_file.write("vt %.7f %.7f\n" % (uv[0], uv[1]))
            for loop in mesh.loops:
                normal = (normal_xform @ loop.normal).normalized()
                obj_file.write("vn %.7f %.7f %.7f\n" % (normal.x, normal.y, normal.z))
            current_material = None
            for tri in mesh.loop_triangles:
                mat = mesh.materials[mesh.polygons[tri.polygon_index].material_index]
                mat_name = material_names.get(mat.name, "Default") if mat else "Default"
                if mat_name != current_material:
                    obj_file.write("usemtl %s\n" % mat_name)
                    current_material = mat_name
                refs = []
                for loop_index in tri.loops:
                    vertex_index = mesh.loops[loop_index].vertex_index
                    refs.append("%d/%d/%d" % (vo + vertex_index, uo + loop_index, no + loop_index))
                obj_file.write("f %s\n" % " ".join(refs))
            vo += len(mesh.vertices); uo += len(mesh.loops); no += len(mesh.loops)


def _write_standalone_blend(source, destination, flip_v=True):
    # Save from an isolated Blender process so the user's open project is never
    # replaced and the exported file opens as a normal scene, not a library file.
    addon_path = os.path.abspath(__file__)
    runner = """
import bpy, importlib.util
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
spec = importlib.util.spec_from_file_location('sh_dat_exporter', {addon!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
collection, objects, stem = mod._build_export_collection({source!r}, link_to_scene=True, flip_v={flip_v!r})
bpy.ops.wm.save_as_mainfile(filepath={destination!r})
""".format(addon=addon_path, source=os.path.abspath(source), destination=os.path.abspath(destination), flip_v=flip_v)
    runner_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix="_sh_dat_export.py", encoding="utf-8", delete=False) as script:
            script.write(runner)
            runner_path = script.name
        result = subprocess.run(
            [bpy.app.binary_path, "--background", "--factory-startup", "--python", runner_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        if result.returncode != 0:
            detail = (result.stdout or "").strip()[-1400:]
            raise RuntimeError("Blender could not save the standalone file. " + detail)
    finally:
        if runner_path and os.path.exists(runner_path):
            os.remove(runner_path)


def _remove_temporary_collection(collection):
    object_ids = list(collection.objects)
    for obj in object_ids:
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(collection)
    for blocks in (bpy.data.meshes, bpy.data.materials):
        for block in list(blocks):
            if block.users == 0:
                blocks.remove(block)


class SHDATExportSettings(PropertyGroup):
    dat_file: StringProperty(name="DAT file", subtype="FILE_PATH")
    output_dir: StringProperty(name="Output folder", subtype="DIR_PATH")
    flip_v: BoolProperty(
        name="Flip V axis",
        description="Convert Silent Hunter's texture origin to Blender's UV origin",
        default=True)
    output_format: EnumProperty(
        name="Format",
        items=(("BLEND", "Blender", "Save a separate .blend containing the exported model"),
               ("OBJ", "OBJ + MTL", "Save Wavefront OBJ geometry and material colors"),
               ("BOTH", "Blender + OBJ", "Save both Blender and Wavefront files")),
        default="BLEND")


class SHDATExportOperator(Operator):
    bl_idname = "sh_dat.export"
    bl_label = "Start Export"
    bl_description = "Read the selected DAT and export its mesh geometry"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.sh_dat_export_settings
        source = bpy.path.abspath(settings.dat_file)
        out_dir = bpy.path.abspath(settings.output_dir)
        if not source or not os.path.isfile(source):
            self.report({"ERROR"}, "Choose an existing .dat file.")
            return {"CANCELLED"}
        if not source.lower().endswith(".dat"):
            self.report({"ERROR"}, "The selected source must have a .dat extension.")
            return {"CANCELLED"}
        if not out_dir:
            self.report({"ERROR"}, "Choose an output folder.")
            return {"CANCELLED"}
        os.makedirs(out_dir, exist_ok=True)
        collection = None
        try:
            stem = os.path.splitext(os.path.basename(source))[0]
            if settings.output_format in {"BLEND", "BOTH"}:
                blend_path = os.path.join(out_dir, stem + ".blend")
                _write_standalone_blend(source, blend_path, settings.flip_v)
            if settings.output_format in {"OBJ", "BOTH"}:
                collection, objects, _ = _build_export_collection(source, flip_v=settings.flip_v)
                _write_obj(os.path.join(out_dir, stem + ".obj"), objects)
                _remove_temporary_collection(collection)
                collection = None
            self.report({"INFO"}, "Export complete for %s." % os.path.basename(source))
            return {"FINISHED"}
        except Exception as exc:
            if collection is not None:
                _remove_temporary_collection(collection)
            self.report({"ERROR"}, "Export failed: %s" % exc)
            return {"CANCELLED"}


class SHDATImportOperator(Operator):
    bl_idname = "sh_dat.import"
    bl_label = "Import DAT into Scene"
    bl_description = "Load all model meshes, UVs, node transforms, and material colors into this Blender scene"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.sh_dat_export_settings
        source = bpy.path.abspath(settings.dat_file)
        if not source or not os.path.isfile(source):
            self.report({"ERROR"}, "Choose an existing .dat file.")
            return {"CANCELLED"}
        if not source.lower().endswith(".dat"):
            self.report({"ERROR"}, "The selected source must have a .dat extension.")
            return {"CANCELLED"}
        try:
            collection, objects, _stem = _build_export_collection(
                source, link_to_scene=True, flip_v=settings.flip_v)
            # Make imported meshes easy to find and frame in the viewport.
            bpy.ops.object.select_all(action="DESELECT")
            for obj in objects:
                obj.select_set(True)
            if objects:
                context.view_layer.objects.active = objects[0]
            self.report({"INFO"}, "Imported %d meshes in collection %s." % (len(objects), collection.name))
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, "Import failed: %s" % exc)
            return {"CANCELLED"}


class SHDATImportFileOperator(Operator):
    """File-browser entry used by Blender's File > Import menu."""
    bl_idname = "sh_dat.import_file"
    bl_label = "Silent Hunter DAT (.dat)"
    bl_description = "Choose a Silent Hunter DAT file and import its scene into Blender"

    filepath: StringProperty(name="DAT file", subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.dat", options={"HIDDEN"})
    flip_v: BoolProperty(
        name="Flip V axis",
        description="Convert DAT UVs to Blender's texture origin",
        default=True)

    def invoke(self, context, event):
        settings = context.scene.sh_dat_export_settings
        self.filepath = settings.dat_file
        self.flip_v = settings.flip_v
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        self.layout.prop(self, "flip_v")

    def execute(self, context):
        source = bpy.path.abspath(self.filepath)
        if not source or not os.path.isfile(source) or not source.lower().endswith(".dat"):
            self.report({"ERROR"}, "Choose an existing .dat file.")
            return {"CANCELLED"}
        settings = context.scene.sh_dat_export_settings
        settings.dat_file = source
        settings.flip_v = self.flip_v
        return getattr(bpy.ops.sh_dat, "import")()


def _draw_import_menu(self, context):
    self.layout.operator(SHDATImportFileOperator.bl_idname, text="Silent Hunter DAT (.dat)", icon="IMPORT")


class SHDATExportPanel(Panel):
    bl_label = "Silent Hunter DAT Exporter"
    bl_idname = "VIEW3D_PT_sh_dat_exporter"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Silent Hunter"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.sh_dat_export_settings
        layout.prop(settings, "dat_file")
        layout.operator("sh_dat.import", icon="IMPORT")
        layout.prop(settings, "flip_v")
        layout.separator()
        layout.label(text="Export a copy")
        layout.prop(settings, "output_dir")
        layout.prop(settings, "output_format")
        layout.separator()
        layout.operator("sh_dat.export", icon="EXPORT")
        layout.label(text="Reads DAT meshes, outward normals, UVs, and materials.", icon="INFO")


classes = (SHDATExportSettings, SHDATExportOperator, SHDATImportOperator,
           SHDATImportFileOperator, SHDATExportPanel)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sh_dat_export_settings = PointerProperty(type=SHDATExportSettings)
    bpy.types.TOPBAR_MT_file_import.append(_draw_import_menu)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(_draw_import_menu)
    del bpy.types.Scene.sh_dat_export_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
