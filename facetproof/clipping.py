from .algebra import add, scale, sub


def plane_distance(plane, vertex):
    return sum(plane[i] * vertex[i] for i in range(3)) + plane[3]


def _interpolate(previous, current, ratio):
    point = add(previous[0], scale(sub(current[0], previous[0]), ratio))
    interpolated = [point]
    for previous_attrs, current_attrs in zip(previous[1:], current[1:]):
        interpolated.append([p + (c - p) * ratio
                             for p, c in zip(previous_attrs, current_attrs)])
    return interpolated


def clip_polygon(vertices, plane):
    """vertices are [position, attr0, attr1, ...] tuples of equal-length lists."""
    if not vertices:
        return []
    output = []
    previous = vertices[-1]
    before = plane_distance(plane, previous[0])
    for current in vertices:
        after = plane_distance(plane, current[0])
        if (before >= 0) != (after >= 0):
            ratio = before / (before - after)
            output.append(_interpolate(previous, current, ratio))
        if after >= 0:
            output.append(current)
        previous, before = current, after
    return output


def clip_triangle(vertices, planes):
    """Each vertex is [position, attr0, attr1, ...]; fan-out keeps the attrs."""
    polygon = vertices
    for plane in planes:
        polygon = clip_polygon(polygon, plane)
        if len(polygon) < 3:
            return []
    return [[polygon[0], polygon[i], polygon[i+1]] for i in range(1, len(polygon)-1)]
