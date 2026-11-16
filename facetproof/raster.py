from array import array
import math
from .sampling import sample as sample_texture


def edge(a, b, x, y):
    return (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])


def _weights(a, b, c, area, x, y):
    wa = edge(b, c, x + .5, y + .5) / area
    wb = edge(c, a, x + .5, y + .5) / area
    wc = 1 - wa - wb
    return wa, wb, wc


def _distance(a, b, c, wa, wb, wc, projection):
    if projection == "perspective":
        return 1 / (wa/a[2] + wb/b[2] + wc/c[2])
    return wa*a[2] + wb*b[2] + wc*c[2]


def _interpolate_uv(a, b, c, a_uv, b_uv, c_uv, wa, wb, wc, distance, projection):
    # Perspective-correct interpolation divides attributes by camera-space
    # depth before barycentric blending, then multiplies the depth back.
    # Orthographic depth is constant in screen space, so plain blending matches.
    if projection == "perspective":
        return [distance * (wa * a_uv[k] / a[2] + wb * b_uv[k] / b[2] + wc * c_uv[k] / c[2])
                for k in range(2)]
    return [wa * a_uv[k] + wb * b_uv[k] + wc * c_uv[k] for k in range(2)]


def rasterize(plan, checkpoint, window=None):
    full_width, full_height = plan["width"], plan["height"]
    x0, y0, x1, y1 = (0, 0, full_width, full_height) if window is None else window
    if not (0 <= x0 < x1 <= full_width and 0 <= y0 < y1 <= full_height):
        raise ValueError("Raster window must be a nonempty rectangle inside the canvas")
    width, height = x1-x0, y1-y0
    length = width * height
    depth = array("d", [math.inf]) * length
    ids = array("I", [0]) * length
    colors = array("f", [0.]) * (length * 3)
    for pixel in range(length):
        for channel in range(3):
            colors[pixel * 3 + channel] = plan["background"][channel]
    fragments = 0
    texture_taps = 0
    projection = plan["projection"]
    for index, triangle in enumerate(plan["triangles"]):
        checkpoint()
        a, b, c = triangle["vertices"]
        area = edge(a, b, c[0], c[1])
        if abs(area) < 1e-10:
            continue
        textured = "uvs" in triangle
        a_uv, b_uv, c_uv = triangle["uvs"] if textured else (None, None, None)
        sampler = plan["samplers"][triangle["texture_id"]] if textured else None
        left = max(x0, math.floor(min(a[0], b[0], c[0])))
        right = min(x1-1, math.ceil(max(a[0], b[0], c[0])))
        top = max(y0, math.floor(min(a[1], b[1], c[1])))
        bottom = min(y1-1, math.ceil(max(a[1], b[1], c[1])))
        for y in range(top, bottom + 1):
            if y % 32 == 0:
                checkpoint()
            for x in range(left, right + 1):
                wa, wb, wc = _weights(a, b, c, area, x, y)
                if min(wa, wb, wc) < -1e-10:
                    continue
                distance = _distance(a, b, c, wa, wb, wc, projection)
                offset = (y-y0) * width + x-x0
                previous = depth[offset]
                tie = abs(distance - previous) < 1e-9
                if distance > previous + 1e-9 or (tie and triangle["object_id"] >= ids[offset]):
                    continue
                if textured:
                    u, v = _interpolate_uv(a, b, c, a_uv, b_uv, c_uv, wa, wb, wc, distance, projection)
                    # Neighbouring screen samples drive the level-of-detail
                    # footprint, so trilinear stability responds to real motion.
                    wx = min(x + 1, x1 - 1)
                    wy = min(y + 1, y1 - 1)
                    nwa, nwb, nwc = _weights(a, b, c, area, wx, y)
                    n_dist = _distance(a, b, c, nwa, nwb, nwc, projection)
                    ux, vx = _interpolate_uv(a, b, c, a_uv, b_uv, c_uv, nwa, nwb, nwc, n_dist, projection)
                    nwa, nwb, nwc = _weights(a, b, c, area, x, wy)
                    n_dist = _distance(a, b, c, nwa, nwb, nwc, projection)
                    uy, vy = _interpolate_uv(a, b, c, a_uv, b_uv, c_uv, nwa, nwb, nwc, n_dist, projection)
                    texel, taps = sample_texture(sampler, u, v, (ux-u, vx-v), (uy-u, vy-v))
                    texture_taps += taps
                    fragment_color = [min(1., max(0., triangle["color"][k] * texel[k])) for k in range(3)]
                else:
                    fragment_color = triangle["color"]
                depth[offset] = distance
                ids[offset] = triangle["object_id"]
                colors[offset*3:offset*3+3] = array("f", fragment_color)
                fragments += 1
    checkpoint()
    return {"depth": depth, "ids": ids, "colors": colors,
            "fragments": fragments, "texture_taps": texture_taps,
            "triangles": len(plan["triangles"])}
