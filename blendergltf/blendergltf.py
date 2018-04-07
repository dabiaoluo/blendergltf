import collections
from distutils.version import StrictVersion as Version
import functools
import itertools
import json
import struct

import bpy
import mathutils

from .exporters import (
    Buffer,
    Reference,
    SimpleID,
    get_bone_name,

    BaseExporter,
    CameraExporter,
    ImageExporter,
    NodeExporter,
)


__all__ = ['export_gltf']


DEFAULT_SETTINGS = {
    'gltf_output_dir': '',
    'gltf_name': 'gltf',
    'gltf_export_binary': False,
    'buffers_embed_data': True,
    'buffers_combine_data': False,
    'nodes_export_hidden': False,
    'nodes_global_matrix': mathutils.Matrix.Identity(4),
    'nodes_global_matrix_apply': True,
    'nodes_selected_only': False,
    'blocks_prune_unused': True,
    'meshes_apply_modifiers': True,
    'meshes_interleave_vertex_data': True,
    'meshes_vertex_color_alpha': True,
    'images_data_storage': 'COPY',
    'asset_version': '2.0',
    'asset_copyright': '',
    'asset_profile': 'WEB',
    'images_allow_srgb': False,
    'extension_exporters': [],
    'animations_object_export': 'ACTIVE',
    'animations_armature_export': 'ELIGIBLE',
    'animations_shape_key_export': 'ELIGIBLE',
    'hacks_streaming': False,
}


# Texture formats
GL_ALPHA = 6406
GL_RGB = 6407
GL_RGBA = 6408
GL_LUMINANCE = 6409
GL_LUMINANCE_ALPHA = 6410

# Texture filtering
GL_NEAREST = 9728
GL_LINEAR = 9729
GL_LINEAR_MIPMAP_LINEAR = 9987

# Texture wrapping
GL_CLAMP_TO_EDGE = 33071
GL_MIRRORED_REPEAT = 33648
GL_REPEAT = 10497

# sRGB texture formats (not actually part of WebGL 1.0 or glTF 1.0)
GL_SRGB = 0x8C40
GL_SRGB_ALPHA = 0x8C42

OES_ELEMENT_INDEX_UINT = 'OES_element_index_uint'

PROFILE_MAP = {
    'WEB': {'api': 'WebGL', 'version': '1.0'},
    'DESKTOP': {'api': 'OpenGL', 'version': '3.0'}
}


class Vertex:
    __slots__ = (
        "co",
        "normal",
        "uvs",
        "colors",
        "loop_indices",
        "index",
        "weights",
        "joint_indexes",
        )

    def __init__(self, mesh, loop):
        vert_idx = loop.vertex_index
        loop_idx = loop.index
        self.co = mesh.vertices[vert_idx].co[:]
        self.normal = loop.normal[:]
        self.uvs = tuple(layer.data[loop_idx].uv[:] for layer in mesh.uv_layers)
        self.colors = tuple(layer.data[loop_idx].color[:] for layer in mesh.vertex_colors)
        self.loop_indices = [loop_idx]

        # Take the four most influential groups
        groups = sorted(
            mesh.vertices[vert_idx].groups,
            key=lambda group: group.weight,
            reverse=True
        )
        if len(groups) > 4:
            groups = groups[:4]

        self.weights = [group.weight for group in groups]
        self.joint_indexes = [group.group for group in groups]

        if len(self.weights) < 4:
            for _ in range(len(self.weights), 4):
                self.weights.append(0.0)
                self.joint_indexes.append(0)

        self.index = 0

    def __hash__(self):
        return hash((self.co, self.normal, self.uvs, self.colors))

    def __eq__(self, other):
        equals = (
            (self.co == other.co) and
            (self.normal == other.normal) and
            (self.uvs == other.uvs) and
            (self.colors == other.colors)
        )

        if equals:
            indices = self.loop_indices + other.loop_indices
            self.loop_indices = indices
            other.loop_indices = indices
        return equals


def togl(matrix):
    return [i for col in matrix.col for i in col]


def _decompose(matrix):
    loc, rot, scale = matrix.decompose()
    loc = loc.to_tuple()
    rot = (rot.x, rot.y, rot.z, rot.w)
    scale = scale.to_tuple()

    return loc, rot, scale


def export_camera(state, camera):
    return CameraExporter.export(state, camera)


def export_material(state, material):
    gltf = {
        'name': material.name,
    }

    if state['version'] < Version('2.0'):
        return gltf

    if hasattr(material, 'pbr_export_settings'):
        gltf['doubleSided'] = not material.game_settings.use_backface_culling
        pbr_settings = material.pbr_export_settings
        pbr = {
            'baseColorFactor': pbr_settings.base_color_factor[:],
            'metallicFactor': pbr_settings.metallic_factor,
            'roughnessFactor': pbr_settings.roughness_factor,
        }

        gltf['alphaMode'] = pbr_settings.alpha_mode
        if gltf['alphaMode'] == 'MASK':
            gltf['alphaCutoff'] = pbr_settings.alpha_cutoff

        input_textures = [texture.name for texture in state['input']['textures']]
        base_color_text = pbr_settings.base_color_texture
        if base_color_text and base_color_text in input_textures:
            pbr['baseColorTexture'] = {
                'texCoord': pbr_settings.base_color_text_index,
            }
            pbr['baseColorTexture']['index'] = Reference(
                'textures',
                pbr_settings.base_color_texture,
                pbr['baseColorTexture'],
                'index'
            )
            state['references'].append(pbr['baseColorTexture']['index'])

        metal_rough_text = pbr_settings.metal_roughness_texture
        if metal_rough_text and metal_rough_text in input_textures:
            pbr['metallicRoughnessTexture'] = {
                'texCoord': pbr_settings.metal_rough_text_index,
            }
            pbr['metallicRoughnessTexture']['index'] = Reference(
                'textures',
                pbr_settings.metal_roughness_texture,
                pbr['metallicRoughnessTexture'],
                'index'
            )
            state['references'].append(pbr['metallicRoughnessTexture']['index'])

        gltf['pbrMetallicRoughness'] = pbr

        gltf['emissiveFactor'] = pbr_settings.emissive_factor[:]

        emissive_text = pbr_settings.emissive_texture
        if emissive_text and emissive_text in input_textures:
            gltf['emissiveTexture'] = {
                'texCoord': pbr_settings.emissive_text_index,
            }
            gltf['emissiveTexture']['index'] = Reference(
                'textures',
                pbr_settings.emissive_texture,
                gltf['emissiveTexture'],
                'index'
            )
            state['references'].append(gltf['emissiveTexture']['index'])

        normal_text = pbr_settings.normal_texture
        if normal_text and normal_text in input_textures:
            gltf['normalTexture'] = {
                'texCoord': pbr_settings.normal_text_index,
            }
            gltf['normalTexture']['index'] = Reference(
                'textures',
                pbr_settings.normal_texture,
                gltf['normalTexture'],
                'index'
            )
            state['references'].append(gltf['normalTexture']['index'])

        occlusion_text = pbr_settings.occlusion_texture
        if occlusion_text and occlusion_text in input_textures:
            gltf['occlusionTexture'] = {
                'texCoord': pbr_settings.occlusion_text_index,
            }
            gltf['occlusionTexture']['index'] = Reference(
                'textures',
                pbr_settings.occlusion_texture,
                gltf['occlusionTexture'],
                'index'
            )
            state['references'].append(gltf['occlusionTexture']['index'])

    return gltf


def export_attributes(state, mesh, mesh_name, vert_list, base_vert_list):
    is_skinned = mesh_name in state['skinned_meshes']

    color_type = Buffer.VEC3
    color_size = 3
    if state['settings']['meshes_vertex_color_alpha']:
        color_type = Buffer.VEC4
        color_size = 4

    num_uv_layers = len(mesh.uv_layers)
    num_col_layers = len(mesh.vertex_colors)
    vertex_size = (3 + 3 + num_uv_layers * 2 + num_col_layers * color_size) * 4

    buf = Buffer(mesh_name)

    num_verts = len(vert_list)

    if state['settings']['meshes_interleave_vertex_data']:
        view = buf.add_view(vertex_size * num_verts, vertex_size, Buffer.ARRAY_BUFFER)
        vdata = buf.add_accessor(view, 0, vertex_size, Buffer.FLOAT, num_verts, Buffer.VEC3)
        ndata = buf.add_accessor(view, 12, vertex_size, Buffer.FLOAT, num_verts, Buffer.VEC3)
        if not base_vert_list:
            tdata = [
                buf.add_accessor(
                    view,
                    24 + 8 * i,
                    vertex_size,
                    Buffer.FLOAT,
                    num_verts,
                    Buffer.VEC2
                )
                for i in range(num_uv_layers)
            ]
            cdata = [
                buf.add_accessor(
                    view,
                    24 + 8 * num_uv_layers + 12 * i,
                    vertex_size,
                    Buffer.FLOAT,
                    num_verts,
                    color_type
                )
                for i in range(num_col_layers)
            ]
    else:
        prop_buffer = Buffer(mesh_name + '_POSITION')
        state['buffers'].append(prop_buffer)
        state['input']['buffers'].append(SimpleID(prop_buffer.name))
        prop_view = prop_buffer.add_view(12 * num_verts, 12, Buffer.ARRAY_BUFFER)
        vdata = prop_buffer.add_accessor(prop_view, 0, 12, Buffer.FLOAT, num_verts, Buffer.VEC3)

        prop_buffer = Buffer(mesh_name + '_NORMAL')
        state['buffers'].append(prop_buffer)
        state['input']['buffers'].append(SimpleID(prop_buffer.name))
        prop_view = prop_buffer.add_view(12 * num_verts, 12, Buffer.ARRAY_BUFFER)
        ndata = prop_buffer.add_accessor(prop_view, 0, 12, Buffer.FLOAT, num_verts, Buffer.VEC3)

        if not base_vert_list:
            tdata = []
            for uv_layer in range(num_uv_layers):
                prop_buffer = Buffer('{}_TEXCOORD_{}'.format(mesh_name, uv_layer))
                state['buffers'].append(prop_buffer)
                state['input']['buffers'].append(SimpleID(prop_buffer.name))
                prop_view = prop_buffer.add_view(8 * num_verts, 8, Buffer.ARRAY_BUFFER)
                tdata.append(
                    prop_buffer.add_accessor(prop_view, 0, 8, Buffer.FLOAT, num_verts, Buffer.VEC2)
                )
            cdata = []
            for col_layer in range(num_col_layers):
                prop_buffer = Buffer('{}_COLOR_{}'.format(mesh_name, col_layer))
                state['buffers'].append(prop_buffer)
                state['input']['buffers'].append(SimpleID(prop_buffer.name))
                prop_view = prop_buffer.add_view(
                    4 * color_size * num_verts,
                    4 * color_size,
                    Buffer.ARRAY_BUFFER
                )
                cdata.append(
                    prop_buffer.add_accessor(
                        prop_view,
                        0,
                        color_size * 4,
                        Buffer.FLOAT,
                        num_verts,
                        color_type
                    )
                )

    # Copy vertex data
    if base_vert_list:
        vert_iter = [(i, v[0], v[1]) for i, v in enumerate(zip(vert_list, base_vert_list))]
        for i, vtx, base_vtx in vert_iter:
            co = [a - b for a, b in zip(vtx.co, base_vtx.co)]
            normal = [a - b for a, b in zip(vtx.normal, base_vtx.normal)]
            for j in range(3):
                vdata[(i * 3) + j] = co[j]
                ndata[(i * 3) + j] = normal[j]

    else:
        for i, vtx in enumerate(vert_list):
            vtx.index = i
            co = vtx.co
            normal = vtx.normal

            for j in range(3):
                vdata[(i * 3) + j] = co[j]
                ndata[(i * 3) + j] = normal[j]

            for j, uv in enumerate(vtx.uvs):
                tdata[j][i * 2] = uv[0]
                if state['settings']['asset_profile'] == 'WEB':
                    tdata[j][i * 2 + 1] = 1.0 - uv[1]
                else:
                    tdata[j][i * 2 + 1] = uv[1]

            for j, col in enumerate(vtx.colors):
                cdata[j][i * color_size] = col[0]
                cdata[j][i * color_size + 1] = col[1]
                cdata[j][i * color_size + 2] = col[2]

                if state['settings']['meshes_vertex_color_alpha']:
                    cdata[j][i * color_size + 3] = 1.0

    # Handle attribute references
    gltf_attrs = {}
    gltf_attrs['POSITION'] = Reference('accessors', vdata.name, gltf_attrs, 'POSITION')
    state['references'].append(gltf_attrs['POSITION'])

    gltf_attrs['NORMAL'] = Reference('accessors', ndata.name, gltf_attrs, 'NORMAL')
    state['references'].append(gltf_attrs['NORMAL'])

    if not base_vert_list:
        for i, accessor in enumerate(tdata):
            attr_name = 'TEXCOORD_' + str(i)
            gltf_attrs[attr_name] = Reference('accessors', accessor.name, gltf_attrs, attr_name)
            state['references'].append(gltf_attrs[attr_name])
        for i, accessor in enumerate(cdata):
            attr_name = 'COLOR_' + str(i)
            gltf_attrs[attr_name] = Reference('accessors', accessor.name, gltf_attrs, attr_name)
            state['references'].append(gltf_attrs[attr_name])

    state['buffers'].append(buf)
    state['input']['buffers'].append(SimpleID(buf.name))

    if is_skinned:
        skin_buf = Buffer('{}_skin'.format(mesh_name))

        skin_vertex_size = (4 + 4) * 4
        skin_view = skin_buf.add_view(
            skin_vertex_size * num_verts,
            skin_vertex_size,
            Buffer.ARRAY_BUFFER
        )
        jdata = skin_buf.add_accessor(
            skin_view,
            0,
            skin_vertex_size,
            Buffer.UNSIGNED_BYTE,
            num_verts,
            Buffer.VEC4
        )
        wdata = skin_buf.add_accessor(
            skin_view,
            16,
            skin_vertex_size,
            Buffer.FLOAT,
            num_verts,
            Buffer.VEC4
        )

        for i, vtx in enumerate(vert_list):
            joints = vtx.joint_indexes
            weights = vtx.weights

            for j in range(4):
                jdata[(i * 4) + j] = joints[j]
                wdata[(i * 4) + j] = weights[j]

        if state['version'] < Version('2.0'):
            joint_key = 'JOINT'
            weight_key = 'WEIGHT'
        else:
            joint_key = 'JOINTS_0'
            weight_key = 'WEIGHTS_0'

        gltf_attrs[joint_key] = Reference('accessors', jdata.name, gltf_attrs, joint_key)
        state['references'].append(gltf_attrs[joint_key])
        gltf_attrs[weight_key] = Reference('accessors', wdata.name, gltf_attrs, weight_key)
        state['references'].append(gltf_attrs[weight_key])

        state['buffers'].append(skin_buf)
        state['input']['buffers'].append(SimpleID(skin_buf.name))

    return buf, gltf_attrs


def export_mesh(state, mesh):
    # glTF data
    mesh_name = mesh.name
    mesh = state['mod_meshes'].get(mesh.name, mesh)
    gltf_mesh = {
        'name': mesh_name,
        'primitives': [],
    }

    extras = BaseExporter.get_custom_properties(mesh)
    if extras:
        gltf_mesh['extras'] = extras

    mesh.calc_normals_split()
    mesh.calc_tessface()

    shape_keys = state['shape_keys'].get(mesh_name, [])

    # Remove duplicate verts with dictionary hashing (causes problems with shape keys)
    if shape_keys:
        vert_list = [Vertex(mesh, loop) for loop in mesh.loops]
    else:
        vert_list = {Vertex(mesh, loop): 0 for loop in mesh.loops}.keys()

    # Process mesh data and gather attributes
    buf, gltf_attrs = export_attributes(state, mesh, mesh_name, vert_list, None)

    # Process shape keys
    targets = []
    for shape_key_mesh in [key[1] for key in shape_keys]:
        shape_key_mesh.calc_normals_split()
        shape_key_mesh.calc_tessface()
        shape_verts = [Vertex(shape_key_mesh, loop) for loop in shape_key_mesh.loops]
        targets.append(export_attributes(
            state,
            shape_key_mesh,
            shape_key_mesh.name,
            shape_verts,
            vert_list
        )[1])
    if shape_keys:
        gltf_mesh['weights'] = [key[0] for key in shape_keys]

    # For each material, make an empty primitive set.
    # This dictionary maps material names to list of indices that form the
    # part of the mesh that the material should be applied to.
    mesh_materials = [ma for ma in mesh.materials if ma in state['input']['materials']]
    prims = {ma.name if ma else '': [] for ma in mesh_materials}
    if not prims:
        prims = {'': []}

    # Index data
    # Map loop indices to vertices
    vert_dict = {i: vertex for vertex in vert_list for i in vertex.loop_indices}

    max_vert_index = 0
    for poly in mesh.polygons:
        # Find the primitive that this polygon ought to belong to (by
        # material).
        if not mesh_materials:
            prim = prims['']
        else:
            try:
                mat = mesh_materials[poly.material_index]
            except IndexError:
                # Polygon has a bad material index, so skip it
                continue
            prim = prims[mat.name if mat else '']

        # Find the (vertex) index associated with each loop in the polygon.
        indices = [vert_dict[i].index for i in poly.loop_indices]

        # Used to determine whether a mesh must be split.
        max_vert_index = max(max_vert_index, max(indices))

        if len(indices) == 3:
            # No triangulation necessary
            prim += indices
        elif len(indices) > 3:
            # Triangulation necessary
            for i in range(len(indices) - 2):
                prim += (indices[-1], indices[i], indices[i + 1])
        else:
            # Bad polygon
            raise RuntimeError(
                "Invalid polygon with {} vertices.".format(len(indices))
            )

    if max_vert_index > 65535:
        # Use the integer index extension
        if OES_ELEMENT_INDEX_UINT not in state['gl_extensions_used']:
            state['gl_extensions_used'].append(OES_ELEMENT_INDEX_UINT)

    for mat, prim in prims.items():
        # For each primitive set add an index buffer and accessor.

        if not prim:
            # This material has not verts, do not make a 0 length buffer
            continue

        # If we got this far use integers if we have to, if this is not
        # desirable we would have bailed out by now.
        if max_vert_index > 65535:
            itype = Buffer.UNSIGNED_INT
            istride = 4
        else:
            itype = Buffer.UNSIGNED_SHORT
            istride = 2

        # Pad index buffer if necessary to maintain a size that is a multiple of 4
        view_length = istride * len(prim)
        view_length = view_length + (4 - view_length % 4)

        index_view = buf.add_view(view_length, 0, Buffer.ELEMENT_ARRAY_BUFFER)
        idata = buf.add_accessor(index_view, 0, istride, itype, len(prim),
                                 Buffer.SCALAR)

        for i, index in enumerate(prim):
            idata[i] = index

        gltf_prim = {
            'attributes': gltf_attrs,
            'mode': 4,
        }

        gltf_prim['indices'] = Reference('accessors', idata.name, gltf_prim, 'indices')
        state['references'].append(gltf_prim['indices'])

        if targets:
            gltf_prim['targets'] = targets

        # Add the material reference after checking that it is valid
        if mat:
            gltf_prim['material'] = Reference('materials', mat, gltf_prim, 'material')
            state['references'].append(gltf_prim['material'])

        gltf_mesh['primitives'].append(gltf_prim)

    return gltf_mesh


def export_skins(state):
    def export_skin(obj):
        if state['version'] < Version('2.0'):
            joints_key = 'jointNames'
        else:
            joints_key = 'joints'

        arm = obj.find_armature()

        axis_mat = mathutils.Matrix.Identity(4)
        if state['settings']['nodes_global_matrix_apply']:
            axis_mat = state['settings']['nodes_global_matrix']

        bind_shape_mat = (
            axis_mat
            * arm.matrix_world.inverted()
            * obj.matrix_world
            * axis_mat.inverted()
        )

        bone_groups = [group for group in obj.vertex_groups if group.name in arm.data.bones]

        gltf_skin = {
            'name': obj.name,
        }
        gltf_skin[joints_key] = [
            Reference('objects', get_bone_name(arm.data.bones[group.name]), None, None)
            for group in bone_groups
        ]
        for i, ref in enumerate(gltf_skin[joints_key]):
            ref.source = gltf_skin[joints_key]
            ref.prop = i
            state['references'].append(ref)

        if state['version'] < Version('2.0'):
            gltf_skin['bindShapeMatrix'] = togl(mathutils.Matrix.Identity(4))
        else:
            bone_names = [get_bone_name(b) for b in arm.data.bones if b.parent is None]
            if len(bone_names) > 1:
                print('Warning: Armature {} has no root node'.format(arm.data.name))
            gltf_skin['skeleton'] = Reference('objects', bone_names[0], gltf_skin, 'skeleton')
            state['references'].append(gltf_skin['skeleton'])

        element_size = 16 * 4
        num_elements = len(bone_groups)
        buf = Buffer('IBM_{}_skin'.format(obj.name))
        buf_view = buf.add_view(element_size * num_elements, element_size, None)
        idata = buf.add_accessor(buf_view, 0, element_size, Buffer.FLOAT, num_elements, Buffer.MAT4)

        for i, group in enumerate(bone_groups):
            bone = arm.data.bones[group.name]
            mat = togl((axis_mat * bone.matrix_local).inverted() * bind_shape_mat)
            for j in range(16):
                idata[(i * 16) + j] = mat[j]

        gltf_skin['inverseBindMatrices'] = Reference(
            'accessors',
            idata.name,
            gltf_skin,
            'inverseBindMatrices'
        )
        state['references'].append(gltf_skin['inverseBindMatrices'])
        state['buffers'].append(buf)
        state['input']['buffers'].append(SimpleID(buf.name))

        state['input']['skins'].append(SimpleID(obj.name))

        return gltf_skin

    return [export_skin(obj) for obj in state['skinned_meshes'].values()]


def export_node(state, obj):
    return NodeExporter.export(state, obj)


def export_joint(state, bone):
    axis_mat = mathutils.Matrix.Identity(4)
    if state['settings']['nodes_global_matrix_apply']:
        axis_mat = state['settings']['nodes_global_matrix']

    matrix = axis_mat * bone.matrix_local
    if bone.parent:
        matrix = bone.parent.matrix_local.inverted() * bone.matrix_local

    bone_name = get_bone_name(bone)

    gltf_joint = {
        'name': bone_name,
    }
    if state['version'] < Version('2.0'):
        gltf_joint['jointName'] = Reference(
            'objects',
            bone_name,
            gltf_joint,
            'jointName'
        )
        state['references'].append(gltf_joint['jointName'])
    if bone.children:
        gltf_joint['children'] = [
            Reference('objects', get_bone_name(child), None, None) for child in bone.children
        ]
    if bone_name in state['bone_children']:
        bone_children = [
            Reference('objects', obj_name, None, None)
            for obj_name in state['bone_children'][bone_name]
        ]
        gltf_joint['children'] = gltf_joint.get('children', []) + bone_children
    for i, ref in enumerate(gltf_joint.get('children', [])):
        ref.source = gltf_joint['children']
        ref.prop = i
        state['references'].append(ref)

    (
        gltf_joint['translation'],
        gltf_joint['rotation'],
        gltf_joint['scale']
    ) = _decompose(matrix)

    return gltf_joint


def export_scene(state, scene):
    result = {
        'extras': {
            'background_color': scene.world.horizon_color[:] if scene.world else [0.05]*3,
            'frames_per_second': scene.render.fps,
        },
        'name': scene.name,
    }

    if scene.camera and scene.camera.data in state['input']['cameras']:
        result['extras']['active_camera'] = Reference(
            'cameras',
            scene.camera.name,
            result['extras'],
            'active_camera'
        )
        state['references'].append(result['extras']['active_camera'])

    extras = BaseExporter.get_custom_properties(scene)
    if extras:
        result['extras'].update(BaseExporter.get_custom_properties(scene))

    result['nodes'] = [
        Reference('objects', ob.name, None, None)
        for ob in scene.objects
        if ob in state['input']['objects'] and ob.parent is None and ob.is_visible(scene)
    ]
    for i, ref in enumerate(result['nodes']):
        ref.source = result['nodes']
        ref.prop = i
    state['references'].extend(result['nodes'])

    hidden_nodes = [
        Reference('objects', ob.name, None, None)
        for ob in scene.objects
        if ob in state['input']['objects'] and not ob.is_visible(scene)
    ]

    if hidden_nodes:
        result['extras']['hidden_nodes'] = hidden_nodes
        for i, ref in enumerate(hidden_nodes):
            ref.source = result['extras']['hidden_nodes']
            ref.prop = i
        state['references'].extend(result['extras']['hidden_nodes'])

    return result


def export_buffers(state):
    if state['settings']['buffers_combine_data']:
        buffers = [functools.reduce(
            lambda x, y: x.combine(y, state),
            state['buffers'],
            Buffer('empty')
        )]
        state['buffers'] = buffers
        state['input']['buffers'] = [SimpleID(buffers[0].name)]
    else:
        buffers = state['buffers']

    gltf = {}
    gltf['buffers'] = [buf.export_buffer(state) for buf in buffers]
    gltf['bufferViews'] = list(itertools.chain(*[buf.export_views(state) for buf in buffers]))
    gltf['accessors'] = list(itertools.chain(*[buf.export_accessors(state) for buf in buffers]))

    return gltf


def export_image(state, image):
    return ImageExporter.export(state, image)


def check_texture(texture):
    if not isinstance(texture, bpy.types.ImageTexture):
        return False

    errors = []
    if texture.image is None:
        errors.append('has no image reference')
    elif texture.image.channels not in [3, 4]:
        errors.append(
            'points to {}-channel image (must be 3 or 4)'
            .format(texture.image.channels)
        )

    if errors:
        err_list = '\n\t'.join(errors)
        print(
            'Unable to export texture {} due to the following errors:\n\t{}'
            .format(texture.name, err_list)
        )
        return False

    return True


def export_texture(state, texture):
    # Generate sampler for this texture
    gltf_sampler = {
        'name': texture.name,
    }

    # Handle wrapS and wrapT
    if texture.extension in ('REPEAT', 'CHECKER', 'EXTEND'):
        if texture.use_mirror_x:
            gltf_sampler['wrapS'] = GL_MIRRORED_REPEAT
        else:
            gltf_sampler['wrapS'] = GL_REPEAT

        if texture.use_mirror_y:
            gltf_sampler['wrapT'] = GL_MIRRORED_REPEAT
        else:
            gltf_sampler['wrapT'] = GL_REPEAT
    elif texture.extension in ('CLIP', 'CLIP_CUBE'):
        gltf_sampler['wrapS'] = GL_CLAMP_TO_EDGE
        gltf_sampler['wrapT'] = GL_CLAMP_TO_EDGE
    else:
        print('Warning: Unknown texture extension option:', texture.extension)

    # Handle minFilter and magFilter
    if texture.use_mipmap:
        gltf_sampler['minFilter'] = GL_LINEAR_MIPMAP_LINEAR
        gltf_sampler['magFilter'] = GL_LINEAR
    else:
        gltf_sampler['minFilter'] = GL_NEAREST
        gltf_sampler['magFilter'] = GL_NEAREST

    gltf_texture = {
        'name': texture.name,
    }

    state['input']['samplers'].append(SimpleID(texture.name))
    state['samplers'].append(gltf_sampler)

    gltf_texture['sampler'] = Reference('samplers', texture.name, gltf_texture, 'sampler')
    state['references'].append(gltf_texture['sampler'])

    gltf_texture['source'] = Reference('images', texture.image.name, gltf_texture, 'source')
    state['references'].append(gltf_texture['source'])

    tformat = None
    channels = texture.image.channels
    image_is_srgb = texture.image.colorspace_settings.name == 'sRGB'
    use_srgb = state['settings']['images_allow_srgb'] and image_is_srgb

    if state['version'] < Version('2.0'):
        if channels == 3:
            if use_srgb:
                tformat = GL_SRGB
            else:
                tformat = GL_RGB
        elif channels == 4:
            if use_srgb:
                tformat = GL_SRGB_ALPHA
            else:
                tformat = GL_RGBA

        gltf_texture['format'] = gltf_texture['internalFormat'] = tformat

    return gltf_texture


def _can_object_use_action(obj, action):
    for fcurve in action.fcurves:
        path = fcurve.data_path
        if not path.startswith('pose'):
            return obj.animation_data is not None

        if obj.type == 'ARMATURE':
            path = path.split('["')[-1]
            path = path.split('"]')[0]
            if path in [bone.name for bone in obj.data.bones]:
                return True

    return False


def export_animations(state, actions):
    def export_animation(obj, action):
        if state['version'] < Version('2.0'):
            target_key = 'id'
        else:
            target_key = 'node'

        channels = {}
        decompose = state['decompose_fn']
        axis_mat = mathutils.Matrix.Identity(4)
        if state['settings']['nodes_global_matrix_apply']:
            axis_mat = state['settings']['nodes_global_matrix']

        sce = bpy.context.scene
        prev_frame = sce.frame_current
        prev_action = obj.animation_data.action

        frame_start, frame_end = [int(x) for x in action.frame_range]
        num_frames = frame_end - frame_start + 1
        obj.animation_data.action = action

        has_location = set()
        has_rotation = set()
        has_scale = set()

        action_name = '{}_{}'.format(obj.name, action.name)

        # Check action groups to see what needs to be animated
        pose_bones = set()
        for group in action.groups:
            for channel in group.channels:
                data_path = channel.data_path
                if 'pose.bones' in data_path:
                    target_name = data_path.split('"')[1]
                    transform = data_path.split('.')[-1]
                    pose_bones.add(obj.pose.bones[target_name])
                else:
                    target_name = obj.name
                    transform = data_path.lower()
                    if obj.name not in channels:
                        channels[obj.name] = []

                if 'location' in transform:
                    has_location.add(target_name)
                if 'rotation' in transform:
                    has_rotation.add(target_name)
                if 'scale' in transform:
                    has_scale.add(target_name)
        channels.update({pbone.name: [] for pbone in pose_bones})

        # Iterate frames and bake animations
        for frame in range(frame_start, frame_end + 1):
            sce.frame_set(frame)

            if obj.name in channels:
                # Decompose here so we don't store a reference to the matrix
                loc, rot, scale = decompose(obj.matrix_local)
                if obj.name not in has_location:
                    loc = None
                if obj.name not in has_rotation:
                    rot = None
                if obj.name not in has_scale:
                    scale = None
                channels[obj.name].append((loc, rot, scale))

            for pbone in pose_bones:
                if pbone.parent:
                    mat = pbone.parent.matrix.inverted() * pbone.matrix
                else:
                    mat = axis_mat * pbone.matrix

                loc, rot, scale = _decompose(mat)

                if pbone.name not in has_location:
                    loc = None
                if pbone.name not in has_rotation:
                    rot = None
                if pbone.name not in has_scale:
                    scale = None
                channels[pbone.name].append((loc, rot, scale))

        gltf_channels = []
        gltf_parameters = {}
        gltf_samplers = []

        tbuf = Buffer('{}_time'.format(action_name))
        tbv = tbuf.add_view(num_frames * 1 * 4, 1 * 4, None)
        tdata = tbuf.add_accessor(tbv, 0, 1 * 4, Buffer.FLOAT, num_frames, Buffer.SCALAR)
        time = 0
        for i in range(num_frames):
            tdata[i] = time
            time += state['animation_dt']
        state['buffers'].append(tbuf)
        state['input']['buffers'].append(SimpleID(tbuf.name))
        time_parameter_name = '{}_time_parameter'.format(action_name)
        ref = Reference('accessors', tdata.name, gltf_parameters, time_parameter_name)
        gltf_parameters[time_parameter_name] = ref
        state['references'].append(ref)

        input_list = '{}_{}_samplers'.format(action_name, obj.name)
        state['input'][input_list] = []

        sampler_keys = []
        for targetid, chan in channels.items():
            buf = Buffer('{}_{}'.format(targetid, action_name))
            ldata = rdata = sdata = None
            paths = []
            if targetid in has_location:
                lbv = buf.add_view(num_frames * 3 * 4, 3 * 4, None)
                ldata = buf.add_accessor(lbv, 0, 3 * 4, Buffer.FLOAT, num_frames, Buffer.VEC3)
                paths.append('translation')
            if targetid in has_rotation:
                rbv = buf.add_view(num_frames * 4 * 4, 4 * 4, None)
                rdata = buf.add_accessor(rbv, 0, 4 * 4, Buffer.FLOAT, num_frames, Buffer.VEC4)
                paths.append('rotation')
            if targetid in has_scale:
                sbv = buf.add_view(num_frames * 3 * 4, 3 * 4, None)
                sdata = buf.add_accessor(sbv, 0, 3 * 4, Buffer.FLOAT, num_frames, Buffer.VEC3)
                paths.append('scale')

            if not paths:
                continue

            for i in range(num_frames):
                loc, rot, scale = chan[i]
                if ldata:
                    for j in range(3):
                        ldata[(i * 3) + j] = loc[j]
                if sdata:
                    for j in range(3):
                        sdata[(i * 3) + j] = scale[j]
                if rdata:
                    for j in range(4):
                        rdata[(i * 4) + j] = rot[j]

            state['buffers'].append(buf)
            state['input']['buffers'].append(SimpleID(buf.name))

            is_bone = False
            if targetid != obj.name:
                is_bone = True
                targetid = get_bone_name(bpy.data.armatures[obj.data.name].bones[targetid])

            for path in paths:
                sampler_name = '{}_{}_{}_sampler'.format(action_name, targetid, path)
                sampler_keys.append(sampler_name)
                parameter_name = '{}_{}_{}_parameter'.format(action_name, targetid, path)

                gltf_channel = {
                    'sampler': sampler_name,
                    'target': {
                        target_key: targetid,
                        'path': path,
                    }
                }
                gltf_channels.append(gltf_channel)
                id_ref = Reference(
                    'objects' if is_bone else 'objects',
                    targetid,
                    gltf_channel['target'],
                    target_key
                )
                state['references'].append(id_ref)
                state['input'][input_list].append(SimpleID(sampler_name))
                sampler_ref = Reference(input_list, sampler_name, gltf_channel, 'sampler')
                state['references'].append(sampler_ref)

                gltf_sampler = {
                    'input': None,
                    'interpolation': 'LINEAR',
                    'output': None,
                }
                gltf_samplers.append(gltf_sampler)

                accessor_name = {
                    'translation': ldata.name if ldata else None,
                    'rotation': rdata.name if rdata else None,
                    'scale': sdata.name if sdata else None,
                }[path]

                if state['version'] < Version('2.0'):
                    gltf_sampler['input'] = time_parameter_name
                    gltf_sampler['output'] = parameter_name
                    accessor_ref = Reference(
                        'accessors',
                        accessor_name,
                        gltf_parameters,
                        parameter_name
                    )
                    gltf_parameters[parameter_name] = accessor_ref
                else:
                    time_ref = Reference(
                        'accessors',
                        tdata.name,
                        gltf_sampler,
                        'input'
                    )
                    gltf_sampler['input'] = time_ref
                    state['references'].append(time_ref)
                    accessor_ref = Reference(
                        'accessors',
                        accessor_name,
                        gltf_sampler,
                        'output'
                    )
                    gltf_sampler['output'] = accessor_ref

                state['references'].append(accessor_ref)

        gltf_action = {
            'name': action_name,
            'channels': gltf_channels,
            'samplers': gltf_samplers,
        }

        if state['version'] < Version('2.0'):
            gltf_action['samplers'] = {
                '{}_{}'.format(input_list, i[0]): i[1]
                for i in zip(sampler_keys, gltf_action['samplers'])
            }
            gltf_action['parameters'] = gltf_parameters

        sce.frame_set(prev_frame)
        obj.animation_data.action = prev_action

        return gltf_action

    def export_shape_key_animation(obj, action):
        action_name = '{}_{}'.format(obj.name, action.name)
        fcurves = action.fcurves
        frame_range = action.frame_range
        frame_count = int(frame_range[1]) - int(frame_range[0])
        for fcurve in fcurves:
            fcurve.convert_to_samples(*frame_range)
        samples = {
            fcurve.data_path.split('"')[1]: [point.co[1] for point in fcurve.sampled_points]
            for fcurve in fcurves
        }
        shape_keys = [
            block for block in obj.data.shape_keys.key_blocks
            if block != block.relative_key
        ]
        empty_data = [0.0] * frame_count

        weight_data = zip(*[samples.get(key.name, empty_data) for key in shape_keys])
        weight_data = itertools.chain.from_iterable(weight_data)
        dt_data = [state['animation_dt'] * i for i in range(frame_count)]

        anim_buffer = Buffer('{}_{}'.format(obj.name, action_name))
        state['buffers'].append(anim_buffer)
        state['input']['buffers'].append(SimpleID(anim_buffer.name))

        time_view = anim_buffer.add_view(frame_count * 1 * 4, 1 * 4, None)
        time_acc = anim_buffer.add_accessor(
            time_view,
            0,
            1 * 4,
            Buffer.FLOAT,
            frame_count,
            Buffer.SCALAR
        )
        for i, dt in enumerate(dt_data):
            time_acc[i] = dt

        key_count = len(shape_keys)
        weight_view = anim_buffer.add_view(frame_count * key_count * 4, 4, None)
        weight_acc = anim_buffer.add_accessor(
            weight_view,
            0,
            1 * 4,
            Buffer.FLOAT,
            frame_count * key_count,
            Buffer.SCALAR
        )
        for i, weight in enumerate(weight_data):
            weight_acc[i] = weight

        channel = {
            'sampler': 0,
            'target': {
                'path': 'weights',
            },
        }
        channel['target']['node'] = Reference('objects', obj.name, channel['target'], 'node')
        state['references'].append(channel['target']['node'])

        sampler = {
            'interpolation': 'LINEAR',
        }
        sampler['input'] = Reference('accessors', time_acc.name, sampler, 'input')
        state['references'].append(sampler['input'])
        sampler['output'] = Reference('accessors', weight_acc.name, sampler, 'output')
        state['references'].append(sampler['output'])

        gltf_action = {
            'name': action_name,
            'channels': [channel],
            'samplers': [sampler],
        }

        return gltf_action

    armature_objects = [obj for obj in state['input']['objects'] if obj.type == 'ARMATURE']
    regular_objects = [obj for obj in state['input']['objects'] if obj.type != 'ARMATURE']
    shape_key_objects = [
        obj for obj in state['input']['objects']
        if obj.type == 'MESH' and obj.data.shape_keys
    ]

    gltf_actions = []

    def export_eligible(objects):
        for obj in objects:
            gltf_actions.extend([
                export_animation(obj, action)
                for action in actions
                if _can_object_use_action(obj, action)
            ])

    def export_active(objects):
        for obj in objects:
            if obj.animation_data and obj.animation_data.action:
                gltf_actions.append(export_animation(obj, obj.animation_data.action))

    armature_setting = state['settings']['animations_armature_export']
    object_setting = state['settings']['animations_object_export']
    shape_key_setting = state['settings']['animations_shape_key_export']

    if armature_setting == 'ACTIVE':
        export_active(armature_objects)
    elif armature_setting == 'ELIGIBLE':
        export_eligible(armature_objects)
    else:
        print(
            'WARNING: Unrecognized setting for animations_armature_export:',
            '{}'.format(armature_setting)
        )

    if object_setting == 'ACTIVE':
        export_active(regular_objects)
    elif object_setting == 'ELIGIBLE':
        export_eligible(regular_objects)
    else:
        print(
            'WARNING: Unrecognized setting for animations_object_export:',
            '{}'.format(object_setting)
        )

    if shape_key_setting == 'ACTIVE':
        for obj in shape_key_objects:
            action = obj.data.shape_keys.animation_data.action
            gltf_actions.append(export_shape_key_animation(obj, action))
    elif shape_key_setting == 'ELIGIBLE':
        for obj in shape_key_objects:
            eligible_actions = []
            shape_keys = set([
                block.name for block in obj.data.shape_keys.key_blocks
                if block != block.relative_key
            ])
            for action in actions:
                fcurve_keys = set([fcurve.data_path.split('"')[1] for fcurve in action.fcurves])
                if fcurve_keys <= shape_keys:
                    eligible_actions.append(action)
            for action in eligible_actions:
                gltf_actions.append(export_shape_key_animation(obj, action))
    else:
        print(
            'WARNING: Unrecognized setting for animations_shape_key_export:',
            '{}'.format(object_setting)
        )

    return gltf_actions


def insert_root_nodes(state, root_matrix):
    for i, scene in enumerate(state['output']['scenes']):
        # Generate a new root node for each scene
        root_node = {
            'children': scene['nodes'],
            'matrix': root_matrix,
            'name': '{}_root'.format(scene['name']),
        }
        state['output']['nodes'].append(root_node)
        ref_name = '__scene_root_{}_'.format(i)
        state['input']['objects'].append(SimpleID(ref_name))

        # Replace scene node lists to just point to the new root nodes
        scene['nodes'] = []
        scene['nodes'].append(Reference('objects', ref_name, scene['nodes'], 0))
        state['references'].append(scene['nodes'][0])


def build_string_refmap(input_data):
    in_out_map = {
        'objects': 'nodes',
        'bones': 'nodes',
        'lamps': 'lights'
    }
    refmap = {}
    for key, value in input_data.items():
        refmap.update({
            (key, data.name): '{}_{}'.format(in_out_map.get(key, key), data.name)
            for data in value
        })
    return refmap


def build_int_refmap(input_data):
    refmap = {}
    for key, value in input_data.items():
        refmap.update({(key, data.name): i for i, data in enumerate(value)})
    return refmap


def export_gltf(scene_delta, settings=None):
    # Fill in any missing settings with defaults
    if not settings:
        settings = {}
    for key, value in DEFAULT_SETTINGS.items():
        settings.setdefault(key, value)

    res_x = bpy.context.scene.render.resolution_x
    res_y = bpy.context.scene.render.resolution_y
    # Initialize export state
    state = {
        'version': Version(settings['asset_version']),
        'settings': settings,
        'animation_dt': 1.0 / bpy.context.scene.render.fps,
        'mod_meshes_obj': {},
        'aspect_ratio': res_x / res_y,
        'mod_meshes': {},
        'shape_keys': {},
        'skinned_meshes': {},
        'dupli_nodes': [],
        'bone_children': {},
        'extensions_used': [],
        'gl_extensions_used': [],
        'buffers': [],
        'samplers': [],
        'input': {
            'buffers': [],
            'accessors': [],
            'bufferViews': [],
            'objects': [],
            'bones': [],
            'anim_samplers': [],
            'samplers': [],
            'scenes': [],
            'skins': [],
            'materials': [],
            'dupli_ids': [],
        },
        'output': {
            'extensions': [],
        },
        'references': [],
        'files': {},
        'decompose_fn': _decompose,
        'decompose_mesh_fn': _decompose,
    }
    state['input'].update({key: list(value) for key, value in scene_delta.items()})

    # Filter out empty meshes
    if 'meshes' in state['input']:
        state['input']['meshes'] = [mesh for mesh in state['input']['meshes'] if mesh.loops]
        if 'objects' in state['input']:
            state['input']['objects'] = [
                obj for obj in state['input']['objects']
                if obj.type != 'MESH' or obj.data in state['input']['meshes']
            ]

    # Make sure any temporary meshes do not have animation data baked in
    default_scene = bpy.context.scene
    if not settings['hacks_streaming']:
        saved_pose_positions = [armature.pose_position for armature in bpy.data.armatures]
        for armature in bpy.data.armatures:
            armature.pose_position = 'REST'
        if saved_pose_positions:
            for obj in bpy.data.objects:
                if obj.type == 'ARMATURE':
                    obj.update_tag()
        default_scene.frame_set(default_scene.frame_current)

    mesh_list = []
    mod_obs = [
        ob for ob in state['input']['objects']
        if [mod for mod in ob.modifiers if mod.type != 'ARMATURE']
    ]
    for mesh in state['input'].get('meshes', []):
        if mesh.shape_keys and mesh.shape_keys.use_relative:
            relative_key = mesh.shape_keys.key_blocks[0].relative_key
            keys = [key for key in mesh.shape_keys.key_blocks if key != relative_key]

            # Gather weight values
            weights = [key.value for key in keys]

            # Clear weight values
            for key in keys:
                key.value = 0.0
            mesh_users = [obj for obj in state['input']['objects'] if obj.data == mesh]

            # Mute modifiers if necessary
            muted_modifiers = []
            original_modifier_states = []
            if not settings['meshes_apply_modifiers']:
                muted_modifiers = itertools.chain.from_iterable(
                    [obj.modifiers for obj in mesh_users]
                )
                original_modifier_states = [mod.show_viewport for mod in muted_modifiers]
                for modifier in muted_modifiers:
                    modifier.show_viewport = False

            for user in mesh_users:
                base_mesh = user.to_mesh(default_scene, True, 'PREVIEW')
                mesh_name = base_mesh.name
                state['mod_meshes_obj'][user.name] = base_mesh

                if mesh_name not in state['shape_keys']:
                    key_meshes = []
                    for key, weight in zip(keys, weights):
                        key.value = key.slider_max
                        key_meshes.append((
                            weight,
                            user.to_mesh(default_scene, True, 'PREVIEW')
                        ))
                        key.value = 0.0
                    state['shape_keys'][mesh_name] = key_meshes

            # Reset weight values
            for key, weight in zip(keys, weights):
                key.value = weight

            # Unmute modifiers
            for modifier, state in zip(muted_modifiers, original_modifier_states):
                modifier.show_viewport = state
        elif settings['meshes_apply_modifiers']:
            mod_users = [ob for ob in mod_obs if ob.data == mesh]

            # Only convert meshes with modifiers, otherwise each non-modifier
            # user ends up with a copy of the mesh and we lose instancing
            state['mod_meshes_obj'].update(
                {ob.name: ob.to_mesh(default_scene, True, 'PREVIEW') for ob in mod_users}
            )

            # Add unmodified meshes directly to the mesh list
            if len(mod_users) < mesh.users:
                mesh_list.append(mesh)
        else:
            mesh_list.append(mesh)

    mesh_list.extend(state['mod_meshes_obj'].values())
    state['input']['meshes'] = mesh_list

    apply_global_matrix = (
        settings['nodes_global_matrix'] != mathutils.Matrix.Identity(4)
        and settings['nodes_global_matrix_apply']
    )
    if apply_global_matrix:
        global_mat = settings['nodes_global_matrix']
        global_scale_mat = mathutils.Matrix([[abs(j) for j in i] for i in global_mat])

        def decompose_apply(matrix):
            loc, rot, scale = matrix.decompose()

            loc.rotate(global_mat)
            loc = loc.to_tuple()

            rot.rotate(global_mat)
            rot = (rot.x, rot.y, rot.z, rot.w)

            scale.rotate(global_scale_mat)
            scale = scale.to_tuple()

            return loc, rot, scale

        def decompose_mesh_apply(matrix):
            loc, rot, scale = matrix.decompose()

            loc.rotate(global_mat)
            loc = loc.to_tuple()

            rot = mathutils.Vector(list(rot.to_euler()))
            rot.rotate(global_mat)
            rot = mathutils.Euler(rot, 'XYZ').to_quaternion()
            rot = (rot.x, rot.y, rot.z, rot.w)

            scale.rotate(global_scale_mat)
            scale = scale.to_tuple()

            return loc, rot, scale
        state['decompose_fn'] = decompose_apply
        state['decompose_mesh_fn'] = decompose_mesh_apply

        transformed_meshes = [mesh.copy() for mesh in mesh_list]
        for mesh in transformed_meshes:
            mesh.transform(global_mat, shape_keys=False)
        state['mod_meshes'].update(
            {mesh.name: xformed_mesh for xformed_mesh, mesh in zip(transformed_meshes, mesh_list)}
        )
        for shape_key_list in state['shape_keys'].values():
            for shape_key in shape_key_list:
                shape_key[1].transform(global_mat, shape_keys=False)

    # Restore armature pose positions
    for i, armature in enumerate(bpy.data.armatures):
        armature.pose_position = saved_pose_positions[i]

    exporter = collections.namedtuple('exporter', [
        'gltf_key',
        'blender_key',
        'export',
        'check',
        'default',
    ])

    # If check function can return False, make sure a default function is provided
    exporters = [
        CameraExporter,
        ImageExporter,
        NodeExporter,
        # Make sure meshes come after nodes to detect which meshes are skinned
        exporter('materials', 'materials', export_material, lambda x: True, None),
        exporter('meshes', 'meshes', export_mesh, lambda x: True, None),
        exporter('scenes', 'scenes', export_scene, lambda x: True, None),
        exporter(
            'textures', 'textures', export_texture, check_texture,
            lambda x: {'name': x.name}
        ),
    ]

    state['output'] = {
        exporter.gltf_key: [
            exporter.export(state, data)
            if exporter.check(state, data)
            else exporter.default(state, data)
            for data in state['input'].get(exporter.blender_key, [])
        ] for exporter in exporters
    }

    # Export top level data
    gltf = {
        'asset': {
            'version': settings['asset_version'],
            'generator': 'blendergltf v1.2.0',
            'copyright': settings['asset_copyright'],
        }
    }
    if state['version'] < Version('2.0'):
        gltf['asset']['profile'] = PROFILE_MAP[settings['asset_profile']]

    # Export samplers
    state['output']['samplers'] = state['samplers']

    # Export animations
    state['output']['animations'] = export_animations(state, state['input'].get('actions', []))
    state['output']['skins'] = export_skins(state)
    state['output']['nodes'].extend([
        export_joint(state, sid.data) for sid in state['input']['bones']
    ])

    # Move bones to nodes for updating references
    state['input']['objects'].extend(state['input']['bones'])
    state['input']['bones'] = []

    # Export dupli-groups
    state['output']['nodes'].extend(state['dupli_nodes'])
    state['input']['objects'].extend(state['input']['dupli_ids'])
    state['input']['dupli_ids'] = []

    # Export default scene
    default_scene = None
    for scene in state['input']['scenes']:
        if scene == bpy.context.scene:
            default_scene = scene
    if default_scene:
        scene_ref = Reference('scenes', bpy.context.scene.name, gltf, 'scene')
        scene_ref.value = 0
        state['references'].append(scene_ref)

    # Export extensions
    state['refmap'] = build_int_refmap(state['input'])
    for ext_exporter in settings['extension_exporters']:
        ext_exporter.export(state)

    # Insert root nodes if axis conversion is needed
    root_node_needed = (
        settings['nodes_global_matrix'] != mathutils.Matrix.Identity(4)
        and not settings['nodes_global_matrix_apply']
    )
    if root_node_needed:
        insert_root_nodes(state, togl(settings['nodes_global_matrix']))

    if state['buffers']:
        state['output'].update(export_buffers(state))
    state['output'] = {key: value for key, value in state['output'].items() if value != []}
    if state['extensions_used']:
        gltf.update({'extensionsUsed': state['extensions_used']})
    if state['version'] < Version('2.0'):
        gltf.update({'glExtensionsUsed': state['gl_extensions_used']})

    # Convert lists to dictionaries
    if state['version'] < Version('2.0'):
        extensions = state['output'].get('extensions', [])
        state['output'] = {
            key: {
                '{}_{}'.format(key, data['name']): data for data in value
            } for key, value in state['output'].items()
            if key != 'extensions'
        }
        if extensions:
            state['output']['extensions'] = extensions
    gltf.update(state['output'])

    # Gather refmap inputs
    reference_inputs = state['input']
    if settings['hacks_streaming']:
        reference_inputs.update({
            'actions': list(bpy.data.actions),
            'cameras': list(bpy.data.cameras),
            'lamps': list(bpy.data.lamps),
            'images': list(bpy.data.images),
            'materials': list(bpy.data.materials),
            'meshes': list(bpy.data.meshes),
            'objects': list(bpy.data.objects),
            'scenes': list(bpy.data.scenes),
            'textures': list(bpy.data.textures),
        })

    # Resolve references
    if state['version'] < Version('2.0'):
        refmap = build_string_refmap(reference_inputs)
        ref_default = 'INVALID'
    else:
        refmap = build_int_refmap(reference_inputs)
        ref_default = -1
    for ref in state['references']:
        ref.source[ref.prop] = refmap.get((ref.blender_type, ref.blender_name), ref_default)
        if ref.source[ref.prop] == ref_default:
            print(
                'Warning: {} contains an invalid reference to {}'
                .format(ref.source, (ref.blender_type, ref.blender_name))
            )

    # Remove any temporary meshes
    temp_mesh_collections = (
        state['mod_meshes'].values(),
        state['mod_meshes_obj'].values(),
        [
            shape_key_pair[1] for shape_key_pair
            in itertools.chain.from_iterable(state['shape_keys'].values())
        ]
    )
    for mesh in itertools.chain(*temp_mesh_collections):
        bpy.data.meshes.remove(mesh)

    # Transform gltf data to binary
    if settings['gltf_export_binary']:
        json_data = json.dumps(gltf, sort_keys=True, check_circular=False).encode()
        json_length = len(json_data)
        json_pad = (' ' * (4 - json_length % 4)).encode()
        json_pad = json_pad if len(json_pad) != 4 else b''
        json_length += len(json_pad)
        json_format = '<II{}s{}s'.format(len(json_data), len(json_pad))
        chunks = [struct.pack(json_format, json_length, 0x4e4f534a, json_data, json_pad)]

        if settings['buffers_embed_data']:
            buffers = [data for path, data in state['files'].items() if path.endswith('.bin')]

            # Get padded lengths
            lengths = [len(buffer) for buffer in buffers]
            lengths = [
                length + ((4 - length % 4) if length % 4 != 0 else 0)
                for length in lengths
            ]

            chunks.extend([
                struct.pack('<II{}s'.format(length), length, 0x004E4942, buffer)
                for buffer, length in zip(buffers, lengths)
            ])

            state['files'] = {
                path: data for path, data in state['files'].items()
                if not path.endswith('.bin')
            }

        version = 2
        size = 12
        for chunk in chunks:
            size += len(chunk)
        header = struct.pack('<4sII', b'glTF', version, size)

        gltf = bytes(0).join([header, *chunks])

    # Write secondary files
    for path, data in state['files'].items():
        with open(path, 'wb') as fout:
            fout.write(data)

    return gltf
