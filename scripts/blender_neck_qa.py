"""Measure localized neck and collar intersections in Blender and glTF files."""

import argparse
import json
import math
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Euler
from mathutils.bvhtree import BVHTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blender_character_qa import load


def number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number')
    return float(value)


def vector(value, label, size):
    if not isinstance(value, list) or len(value) != size:
        raise ValueError(f'{label} must contain {size} numbers')
    return tuple(number(item, label) for item in value)


def config_from(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    scale = number(data.get('metres_per_unit', 1.0), 'metres_per_unit')
    tolerance = number(data.get('shared_point_tolerance_m', 0.00001), 'shared_point_tolerance_m')
    if scale <= 0 or tolerance <= 0:
        raise ValueError('Scale and shared-point tolerance must be positive')
    regions = {}
    for label, item in data['regions'].items():
        materials = item['materials']
        lower = vector(item['min'], f'{label}.min', 3)
        upper = vector(item['max'], f'{label}.max', 3)
        if not isinstance(materials, list) or not materials or any(not isinstance(m, str) or not m for m in materials):
            raise ValueError(f'{label} requires one or more material names')
        if any(a >= b for a, b in zip(lower, upper)):
            raise ValueError(f'{label} requires min < max on all axes')
        regions[label] = (set(materials), lower, upper)
    pairs = []
    for pair in data['pairs']:
        if not isinstance(pair, list) or len(pair) != 2 or not all(name in regions for name in pair):
            raise ValueError(f'Invalid region pair: {pair}')
        if tuple(pair) in pairs or tuple(reversed(pair)) in pairs:
            raise ValueError(f'Duplicate region pair: {pair}')
        pairs.append(tuple(pair))
    if not pairs:
        raise ValueError('At least one intersection pair is required')
    motions = {'rest': {}}
    for label, motion in data.get('motions', {}).items():
        if label == 'rest' or not motion:
            raise ValueError(f'Use non-empty motions and leave rest reserved: {label}')
        rotations = {name: vector(angles, f'{label}.{name}', 3) for name, angles in motion.items()}
        if not any(any(angles) for angles in rotations.values()):
            raise ValueError(f'{label} must include a nonzero rotation')
        motions[label] = rotations
    width = None
    if 'width_sample' in data:
        item = data['width_sample']
        region = item['region']
        start = number(item['from_y'], 'from_y')
        direction = number(item['direction_y'], 'direction_y')
        depth = number(item['depth'], 'depth')
        step = number(item['step_m'], 'step_m') / scale
        left, right = vector(item['x_range'], 'x_range', 2)
        heights = [number(z, 'z') for z in item['z']]
        if region not in regions or direction not in (-1, 1) or depth <= 0 or step <= 0 or right <= left or not heights:
            raise ValueError('Invalid width sample region, direction, depth, step or range')
        count = math.floor((right - left) / step + 1e-8) + 1
        if count < 2 or count * len(heights) > 100_000 or len(set(heights)) != len(heights):
            raise ValueError('Width sample requires distinct heights and 2–100,000 rays in total')
        width = (region, start, direction, depth, [left + i * step for i in range(count)], heights)
    return regions, pairs, motions, width, scale, tolerance


def skinned_meshes(rig):
    shapes = {bone.custom_shape for bone in rig.pose.bones if bone.custom_shape}
    return [obj for obj in bpy.context.scene.objects if obj.type == 'MESH' and obj not in shapes
            and (obj.parent == rig or any(mod.type == 'ARMATURE' and mod.object == rig for mod in obj.modifiers))]


def evaluated_meshes(objects):
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    return [obj.evaluated_get(depsgraph) for obj in objects]


def mesh_positions(objects):
    points = []
    for obj in objects:
        matrix = np.asarray(obj.matrix_world, dtype=float)
        local = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
        obj.data.vertices.foreach_get('co', local)
        points.append(local.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3])
    result = np.concatenate(points)
    if not np.isfinite(result).all():
        raise ValueError('Non-finite coordinates in evaluated mesh')
    return result


def topology(objects):
    return [(len(obj.data.vertices), [tuple(poly.vertices) for poly in obj.data.polygons]) for obj in objects]


def select_triangles(objects, positions, regions):
    triangles = []
    selected = {name: [] for name in regions}
    offset = 0
    for obj in objects:
        mesh = obj.data
        mesh.calc_loop_triangles()
        names = [mat.name if mat else None for mat in mesh.materials]
        for triangle in mesh.loop_triangles:
            indices = tuple(offset + vertex for vertex in triangle.vertices)
            if triangle.material_index < len(names):
                material = names[triangle.material_index]
                for key, (materials, lower, upper) in regions.items():
                    if material in materials:
                        centroid = sum((positions[index] for index in indices)) / 3.0
                        if all(low <= value <= high for value, low, high in zip(centroid, lower, upper)):
                            selected[key].append(len(triangles))
            triangles.append(indices)
        offset += len(mesh.vertices)
    empty = [name for name, ids in selected.items() if not ids]
    if empty:
        raise ValueError(f'Regions matched no triangles: {", ".join(empty)}')
    return triangles, selected


def intersection_counts(positions, rest, tolerance, triangles, selected, pairs):
    coords = positions.tolist()
    faces = {name: [triangles[index] for index in members] for name, members in selected.items()}
    trees = {name: BVHTree.FromPolygons(coords, faces[name], all_triangles=True) for name in faces}
    counts = {}
    for a, b in pairs:
        hits = trees[a].overlap(trees[b])
        if a == b:
            hits = [(i, j) for i, j in hits if i < j and not any(
                (rest[left] - rest[right]) @ (rest[left] - rest[right]) <= tolerance * tolerance
                for left in faces[a][i] for right in faces[b][j])]
        counts[f'{a}/{b}'] = len(hits)
    return counts


def visible_width(rest, triangles, selected, sample, scale):
    region, start, direction, depth, positions, heights = sample
    tree = BVHTree.FromPolygons(rest.tolist(), triangles, all_triangles=True)
    skin = set(selected[region])
    records = {}
    for height in heights:
        hits = [x for x in positions if tree.ray_cast((x, start, height), (0, direction, 0), depth)[2] in skin]
        records[str(height)] = {'matched_rays': len(hits), 'total_rays': len(positions),
                                'width_mm': round((hits[-1] - hits[0]) * scale * 1000, 3) if hits else None}
    if not any(record['matched_rays'] for record in records.values()):
        raise ValueError('No width rays hit the visible target region')
    return records


def check_asset(path, config):
    regions, pairs, motions, sample, scale, tolerance = config
    load(path)
    rigs = [obj for obj in bpy.context.scene.objects if obj.type == 'ARMATURE']
    if len(rigs) != 1:
        raise ValueError(f'Expected exactly one armature, found {len(rigs)}')
    rig = rigs[0]
    objects = skinned_meshes(rig)
    if not objects:
        raise ValueError('No meshes are linked to the armature')
    required = {name for motion in motions.values() for name in motion}
    missing = sorted(required.difference(rig.pose.bones.keys()))
    if missing:
        raise ValueError(f'Missing pose bones: {", ".join(missing)}')
    saved = {name: rig.pose.bones[name].matrix_basis.copy() for name in sorted(required)}
    rest_meshes = evaluated_meshes(objects)
    rest = mesh_positions(rest_meshes)
    identity = topology(rest_meshes)
    triangles, selected = select_triangles(rest_meshes, rest, regions)
    width = visible_width(rest, triangles, selected, sample, scale) if sample else {}
    checks = {}
    try:
        for label, motion in motions.items():
            for name, basis in saved.items():
                angles = motion.get(name, (0, 0, 0))
                delta = Euler(tuple(map(math.radians, angles)), 'XYZ').to_matrix().to_4x4()
                rig.pose.bones[name].matrix_basis = basis @ delta
            current = evaluated_meshes(objects)
            if topology(current) != identity:
                raise ValueError(f'Evaluated mesh topology changed in pose {label}')
            intersections = intersection_counts(mesh_positions(current), rest, tolerance / scale,
                                                triangles, selected, pairs)
            checks[label] = {'local_rotation_deg': motion, 'intersections': intersections}
    finally:
        for name, basis in saved.items():
            rig.pose.bones[name].matrix_basis = basis
        bpy.context.view_layer.update()
    has_intersections = any(any(check['intersections'].values()) for check in checks.values())
    return {'file': str(path), 'status': 'fail' if has_intersections else 'pass',
            'region_triangles': {name: len(ids) for name, ids in selected.items()},
            'poses': checks, 'front_width': width}


def result_for(path, config):
    try:
        return check_asset(path, config)
    except (ValueError, RuntimeError, OSError, AssertionError, SystemExit) as error:
        return {'file': str(path), 'status': 'error', 'reason': str(error)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
    candidate, config_path, report = (p.expanduser().resolve() for p in (args.file, args.config, args.report))
    baseline = args.baseline.expanduser().resolve() if args.baseline else None
    if report.suffix.lower() != '.json' or report in (candidate, baseline, config_path):
        parser.error('Report must be a distinct JSON output file')
    try:
        config = config_from(config_path)
    except (OSError, TypeError, KeyError, AttributeError, ValueError) as error:
        parser.error(str(error))
    result = {'candidate': result_for(candidate, config)}
    if baseline:
        result['baseline'] = result_for(baseline, config)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    status = result['candidate']['status']
    baseline_status = result.get('baseline', {}).get('status')
    print(f'Neck QA: candidate={status}, baseline={baseline_status or "absent"}', flush=True)
    raise SystemExit(0 if status == 'pass' and baseline_status != 'error' else 1)


if __name__ == '__main__':
    main()
