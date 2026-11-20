"""Blender-side QA for a rigged character asset.

Usage:
    blender -b --factory-startup --python blender_character_qa.py -- \
        --file "汪果_写实精修版.glb" --report qa.json [--height 1.695] [--no-pose]

Accepts .blend (opened directly) or .glb / .gltf / .fbx (imported into an empty scene).
Checks mesh sanity, skin weights, material textures and, when finger bones are named
``<Digit>.01/.02/.03.<L|R>``, runs an isolated per-finger bend test.

Exit code 0 means every gate passed; 1 means at least one gate failed.
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path

import bpy
import numpy as np

FINGER = re.compile(r'^(?P<digit>.+)\.03\.(?P<side>[LR])$')
INFLUENCE_LIMIT = 4
WEIGHT_TOLERANCE = 1e-4
SIZE_TOLERANCE = 0.002


def arguments():
    argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument('--file', required=True)
    parser.add_argument('--report', default='character_qa.json')
    parser.add_argument('--height', type=float, default=None,
                        help='Expected height in metres. Omit to only report the measurement.')
    parser.add_argument('--pose-angle', type=float, default=-32.0)
    parser.add_argument('--no-pose', action='store_true')
    return parser.parse_args(argv)


def load(path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    suffix = path.suffix.lower()
    if suffix == '.blend':
        bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False, use_scripts=False)
    elif suffix in {'.glb', '.gltf'}:
        result = bpy.ops.import_scene.gltf(filepath=str(path))
        assert result == {'FINISHED'}, result
    elif suffix == '.fbx':
        result = bpy.ops.import_scene.fbx(filepath=str(path))
        assert result == {'FINISHED'}, result
    else:
        raise SystemExit(f'Unsupported file type: {suffix}')
    bpy.context.view_layer.update()


def find_asset():
    rigs = [o for o in bpy.context.scene.objects if o.type == 'ARMATURE']
    if len(rigs) != 1:
        return None, None, f'expected exactly one armature, found {len(rigs)}'
    rig = rigs[0]
    shapes = {b.custom_shape for b in rig.pose.bones if b.custom_shape}
    meshes = [o for o in bpy.context.scene.objects
              if o.type == 'MESH' and o not in shapes and o.parent == rig]
    if len(meshes) != 1:
        meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH' and o not in shapes]
    if len(meshes) != 1:
        return None, rig, f'expected exactly one skinned mesh, found {len(meshes)}'
    return meshes[0], rig, None


def mesh_gates(model, rig):
    mesh = model.data
    mesh.calc_loop_triangles()
    coords = np.empty(3 * len(mesh.vertices), np.float32)
    mesh.vertices.foreach_get('co', coords)
    coords = coords.reshape(-1, 3)
    triangles = np.empty(3 * len(mesh.loop_triangles), np.int32)
    mesh.loop_triangles.foreach_get('vertices', triangles)
    triangles = triangles.reshape(-1, 3)

    finite = bool(np.isfinite(coords).all())
    if len(triangles):
        p, q, r = coords[triangles].transpose(1, 0, 2)
        area = np.linalg.norm(np.cross(q - p, r - p), axis=1)
        degenerate = int(np.count_nonzero(area < 1e-12))
        unique = len(np.unique(np.sort(triangles, axis=1), axis=0))
        duplicates = len(triangles) - unique
    else:
        degenerate = duplicates = 0

    bones = {b.name for b in rig.data.bones}
    by_index = {g.index: g.name for g in model.vertex_groups}
    unweighted = 0
    orphan_groups = 0
    worst_sum = 0.0
    worst_influences = 0
    for vertex in mesh.vertices:
        weights = [g.weight for g in vertex.groups if by_index.get(g.group) in bones]
        orphan_groups += sum(1 for g in vertex.groups if by_index.get(g.group) not in bones)
        count = len(weights)
        total = sum(weights)
        worst_influences = max(worst_influences, count)
        worst_sum = max(worst_sum, abs(total - 1.0))
        if count == 0 or total <= 1e-6:
            unweighted += 1

    images = []
    for material in mesh.materials:
        if not material or not material.use_nodes:
            continue
        for node in material.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                image = node.image
                images.append({'name': image.name, 'size': list(image.size),
                               'packed': bool(image.packed_file)})
    missing = [i['name'] for i in images if min(i['size']) <= 0 or not i['packed']]

    return {
        'mesh': model.name,
        'vertices': len(mesh.vertices),
        'triangles': int(len(triangles)),
        'materials': len(mesh.materials),
        'material_names': [m.name for m in mesh.materials if m],
        'bones': len(rig.data.bones),
        'uv_layers': [layer.name for layer in mesh.uv_layers],
        'dimensions_m': [round(float(v), 5) for v in model.dimensions],
        'finite_coordinates': finite,
        'degenerate_triangles': degenerate,
        'duplicate_triangles': duplicates,
        'unweighted_vertices': unweighted,
        'max_influences': worst_influences,
        'maximum_weight_sum_error': round(float(worst_sum), 8),
        'vertices_touching_unknown_bones': orphan_groups,
        'textures': images,
        'textures_not_embedded': missing,
    }


def tip_vertices(model, rig):
    """Vertices dominated by a distal finger bone, grouped by that bone."""
    mesh = model.data
    by_index = {g.index: g.name for g in model.vertex_groups}
    tips = {}
    for vertex in mesh.vertices:
        for group in vertex.groups:
            name = by_index.get(group.group, '')
            if group.weight >= 0.94 and FINGER.match(name) and name in rig.data.bones:
                tips.setdefault(name, []).append(vertex.index)
    return tips


def evaluated(model):
    bpy.context.view_layer.update()
    data = model.evaluated_get(bpy.context.evaluated_depsgraph_get()).data
    coords = np.empty(3 * len(data.vertices), np.float32)
    data.vertices.foreach_get('co', coords)
    return coords.reshape(-1, 3)


def bend(bone, degrees):
    bone.rotation_mode = 'XYZ'
    bone.rotation_euler.x = math.radians(degrees)


def pose_gate(model, rig, angle):
    tips = tip_vertices(model, rig)
    if not tips:
        return {'status': 'not_applicable',
                'reason': 'no distal bones named <Digit>.03.<L|R> with weight >= 0.94'}
    rest = evaluated(model)
    result = {}
    failures = []
    for tip, indices in sorted(tips.items()):
        joint = tip.replace('.03.', '.02.')
        if joint not in rig.pose.bones:
            continue
        bone = rig.pose.bones[joint]
        bend(bone, angle)
        moved = np.linalg.norm(evaluated(model) - rest, axis=1) * 1000.0
        others = [v for name, group in tips.items() if name != tip for v in group]
        own = float(np.median(moved[indices]))
        spill = float(np.max(moved[others])) if others else 0.0
        result[tip] = {'tip_median_mm': round(own, 3), 'other_tips_max_mm': round(spill, 3)}
        if own < 3.0 or spill > 0.5:
            failures.append(tip)
        bend(bone, 0.0)
    bpy.context.view_layer.update()
    return {'status': 'pass' if not failures else 'fail',
            'isolated_failures': failures, 'angle_degrees': angle, 'per_bone': result}


def main():
    options = arguments()
    path = Path(options.file).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f'File not found: {path}')
    load(path)
    model, rig, error = find_asset()
    if error:
        report = {'file': str(path), 'status': 'fail', 'error': error}
        Path(options.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
        print('QA', json.dumps(report, ensure_ascii=False), flush=True)
        raise SystemExit(1)

    gates = mesh_gates(model, rig)
    failures = []
    warnings = []
    if not gates['finite_coordinates']:
        failures.append('non-finite vertex coordinates')
    if gates['degenerate_triangles']:
        failures.append(f"{gates['degenerate_triangles']} degenerate triangles")
    if gates['duplicate_triangles']:
        failures.append(f"{gates['duplicate_triangles']} duplicate triangles")
    if gates['unweighted_vertices']:
        failures.append(f"{gates['unweighted_vertices']} unweighted vertices")
    if gates['max_influences'] > INFLUENCE_LIMIT:
        failures.append(f"max influences {gates['max_influences']} > {INFLUENCE_LIMIT}")
    if gates['maximum_weight_sum_error'] > WEIGHT_TOLERANCE:
        failures.append(f"weight sum error {gates['maximum_weight_sum_error']} > {WEIGHT_TOLERANCE}")
    if gates['vertices_touching_unknown_bones']:
        failures.append(f"{gates['vertices_touching_unknown_bones']} weights target missing bones")
    if gates['textures_not_embedded']:
        failures.append(f"textures not embedded: {', '.join(gates['textures_not_embedded'])}")
    if not gates['textures']:
        warnings.append('no image texture found on any material')
    height = max(gates['dimensions_m'])
    if options.height is not None and abs(height - options.height) > SIZE_TOLERANCE:
        failures.append(f'height {height:.4f} m differs from {options.height:.4f} m')

    pose = {'status': 'skipped'} if options.no_pose else pose_gate(model, rig, options.pose_angle)
    if pose['status'] == 'fail':
        failures.append('finger bend leaks into other fingers: ' + ', '.join(pose['isolated_failures']))

    report = {'file': str(path), 'status': 'fail' if failures else ('warn' if warnings else 'pass'),
              'gates': gates, 'finger_pose': pose, 'failures': failures, 'warnings': warnings}
    Path(options.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print('QA', json.dumps({k: report[k] for k in ('status', 'failures', 'warnings')}, ensure_ascii=False),
          flush=True)
    print('REPORT', Path(options.report).resolve(), flush=True)
    raise SystemExit(1 if failures else 0)


if __name__ == '__main__':
    main()
