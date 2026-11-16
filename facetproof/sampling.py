"""Texture sampling and the explainable display-choice tradeoff.

Three filter modes share one perspective-correct UV stream, so their only
difference is how texels are gathered. Every mode reports the number of texel
fetches it consumed, turning the sampling-cost tradeoff into an observable
number rather than a marketing claim.

- nearest:  one tap. Maximally crisp small markings, but dense fine lines
  alias and shimmer as the camera moves, with no level-of-detail smoothing.
- bilinear: four taps at the base level. Smooth in-plane edges while keeping
  full detail, but distant fine lines still alias.
- trilinear: eight taps across two mip levels. Stable distant texture under
  camera motion, paying the most taps and softening the finest detail.
"""
import math

from .errors import require

FILTERS = ("nearest", "bilinear", "trilinear")
MAX_MIP_LEVELS = 8


def build_pyramid(texture):
    """Average-down linear texels into power-of-two mip levels (box filter)."""
    width, height = texture["width"], texture["height"]
    levels = [{"width": width, "height": height, "linear": list(texture["linear"])}]
    while len(levels) < MAX_MIP_LEVELS and (width > 1 or height > 1):
        new_width, new_height = max(1, width // 2), max(1, height // 2)
        source = levels[-1]["linear"]
        down = [0.0] * (new_width * new_height * 3)
        for y in range(new_height):
            for x in range(new_width):
                sx, sy = min(2 * x + 1, width - 1), min(2 * y + 1, height - 1)
                target = (y * new_width + x) * 3
                for channel in range(3):
                    total = (source[((2*y)*width + 2*x)*3 + channel]
                             + source[((2*y)*width + sx)*3 + channel]
                             + source[(sy*width + 2*x)*3 + channel]
                             + source[(sy*width + sx)*3 + channel])
                    down[target + channel] = total / 4
        levels.append({"width": new_width, "height": new_height, "linear": down})
        width, height = new_width, new_height
    return levels


def build_sampler(texture, texture_filter):
    require(texture_filter in FILTERS, "texture_filter", "Unknown texture filter")
    return {"texture_id": texture.get("id"), "address": texture["address"],
            "filter": texture_filter, "levels": build_pyramid(texture)}


def _wrap(index, extent, address):
    if address == "repeat":
        return index % extent
    return min(max(index, 0), extent - 1)


def _fetch(level, tx, ty, address):
    x = _wrap(tx, level["width"], address)
    y = _wrap(ty, level["height"], address)
    offset = (y * level["width"] + x) * 3
    return level["linear"][offset:offset + 3]


def _sample_level(level, u, v, address, texture_filter):
    width, height = level["width"], level["height"]
    if texture_filter == "nearest":
        tx = math.floor(u * width)
        ty = math.floor(v * height)
        return _fetch(level, tx, ty, address), 1
    fx = u * width - .5
    fy = v * height - .5
    x0, y0 = math.floor(fx), math.floor(fy)
    wx, wy = fx - x0, fy - y0
    corners = ((x0, y0, (1-wx)*(1-wy)), (x0+1, y0, wx*(1-wy)),
               (x0, y0+1, (1-wx)*wy), (x0+1, y0+1, wx*wy))
    result = [0., 0., 0.]
    for tx, ty, weight in corners:
        texel = _fetch(level, tx, ty, address)
        for channel in range(3):
            result[channel] += texel[channel] * weight
    return result, 4


def sample(sampler, u, v, duvdx, duvdy):
    """Return linear RGB and the texel taps used for this one fragment."""
    levels = sampler["levels"]
    address, texture_filter = sampler["address"], sampler["filter"]
    if texture_filter == "trilinear":
        # Standard screen-space LOD: scale the per-pixel UV gradient by the
        # base-level texel grid before taking log2. A footprint covering one
        # texel yields LOD 0; covering N texels yields log2(N), selecting the
        # matching mip. Deterministic and independent of the fragment's origin.
        base = levels[0]
        gx = (duvdx[0] * base["width"]) ** 2 + (duvdx[1] * base["height"]) ** 2
        gy = (duvdy[0] * base["width"]) ** 2 + (duvdy[1] * base["height"]) ** 2
        lod = max(0., .5 * math.log2(max(gx, gy) + 1e-12))
        low = min(int(math.floor(lod)), len(levels) - 1)
        high = min(low + 1, len(levels) - 1)
        blend = lod - low
        color_low, taps_low = _sample_level(levels[low], u, v, address, "bilinear")
        color_high, taps_high = _sample_level(levels[high], u, v, address, "bilinear")
        color = [color_low[c] * (1-blend) + color_high[c] * blend for c in range(3)]
        return color, taps_low + taps_high
    base_filter = "nearest" if texture_filter == "nearest" else "bilinear"
    return _sample_level(levels[0], u, v, address, base_filter)
