"""Version-one plate semantics, independent of the producer's Python code."""
import hashlib
import json
import math
import re
import struct
from .archive import CHANNELS, MAX_SIDE, read_members
from .errors import InvalidBundle, require
from .png import decode_rgb

TRUST = {
    "verified": "Internal byte integrity and metadata/channel consistency only",
    "external_authenticity": "Not established: an attacker can replace content and all bundle-local hashes",
    "review_validity": "Unknown: the ZIP contains no proof records or live quarantine state",
    "render_correctness": "Not established: channels are decoded and cross-checked, not independently re-rendered",
}
SUPPORTED_PLATE = {"facet-plate-1", "facet-plate-2"}
SUPPORTED_SEAL = {"facet-seal-1", "facet-seal-2"}
TEXTURE_FILTERS = {"nearest", "bilinear", "trilinear"}
FILTER_TRADEOFF = {
    "nearest": {"texel_taps_per_textured_fragment": 1,
                "edge_sharpness": "highest", "motion_stability": "lowest",
                "loss": "dense fine lines alias and shimmer under small camera motion"},
    "bilinear": {"texel_taps_per_textured_fragment": 4,
                 "edge_sharpness": "high", "motion_stability": "medium",
                 "loss": "in-plane edges are smooth but distant fine lines still alias"},
    "trilinear": {"texel_taps_per_textured_fragment": 8,
                  "edge_sharpness": "softened", "motion_stability": "highest",
                  "loss": "mip averaging blurs the finest detail at grazing or distant angles"},
}


def _object(value, field):
    require(isinstance(value, dict), "metadata_shape", field + " must be an object")
    return value


def _integer(value, low, high, field):
    require(type(value) is int and low <= value <= high, "metadata_value", field + " is out of range")
    return value


def _text(value, field):
    require(isinstance(value, str) and bool(value), "metadata_value", field + " must be text")
    return value


def _checksum(value, field):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value),
            "metadata_hash", field + " must be a lowercase SHA-256")
    return value


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise InvalidBundle("metadata_json", "Metadata is not finite canonical JSON") from exc


def _digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _json(raw, field):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "metadata_json", "Duplicate JSON object key")
            value[key] = item
        return value
    def constant(_):
        raise InvalidBundle("metadata_json", "Non-finite JSON constant")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        canonical(value)  # Also catches overflowing exponents and bad surrogates.
    except InvalidBundle:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidBundle("metadata_json", "Invalid " + field) from exc
    return _object(value, field)


def _objects(seal, objects):
    closure = seal.get("closure")
    require(isinstance(closure, list) and 1 <= len(closure) <= 80,
            "closure", "Expected a nonempty frozen placement closure")
    by_key = {}
    for entry in closure:
        entry = _object(entry, "closure entry")
        placement = _object(entry.get("placement"), "placement")
        key = _text(placement.get("key"), "placement.key")
        require(key not in by_key, "closure", "Frozen placement keys must be unique")
        require(placement.get("visible") is True, "closure", "Frozen closure must contain visible placements")
        _object(entry.get("mesh"), "frozen mesh")
        _object(entry.get("pigment"), "frozen pigment")
        for field in ("mesh_revision", "pigment_revision"):
            _integer(entry.get(field), 1, 2**63-2, field)
        by_key[key] = {"key": key,
                       "mesh_id": _text(placement.get("mesh_id"), "placement.mesh_id"),
                       "pigment_id": _text(placement.get("pigment_id"), "placement.pigment_id")}
    expected = {str(i): by_key[key] for i, key in enumerate(sorted(by_key), 1)}
    require(_object(objects, "objects") == expected, "object_mapping", "Object mapping disagrees with the frozen closure")
    return expected


def _textures(payload, frozen, members, plate_format, request):
    """Verify embedded source art and the display choice against real bytes."""
    textures = payload.get("textures", [])
    texture_filter = payload.get("texture_filter")
    if plate_format == "facet-plate-1":
        require(textures == [] and texture_filter is None,
                "texture", "Version-one plates cannot carry texture data")
        return {"textures": [], "texture_filter": None}
    require(isinstance(textures, list) and len(textures) <= 16, "texture", "Invalid texture manifest")
    require(texture_filter in TEXTURE_FILTERS, "texture_filter", "Unknown or missing texture filter")
    require(request.get("texture_filter") == texture_filter,
            "texture_filter", "Request and plate disagree on the display choice")
    frozen_by_id = {entry["texture"]["id"]: entry
                    for entry in frozen.get("closure", []) if entry.get("texture")}
    verified = []
    for texture in textures:
        texture = _object(texture, "plate texture")
        texture_id = _text(texture.get("texture_id"), "texture.texture_id")
        descriptor = _object(texture.get("descriptor"), "texture.descriptor")
        require(set(descriptor) == {"sha256", "size"}, "descriptor", "Unexpected texture descriptor fields")
        checksum = _checksum(descriptor.get("sha256"), "texture.descriptor.sha256")
        member_name = "textures/" + checksum + ".png"
        require(member_name in members, "texture", "Bundle is missing a referenced texture image")
        image = members[member_name]
        require(len(image) == descriptor["size"], "texture", "Texture size mismatch")
        require(hashlib.sha256(image).hexdigest() == checksum, "texture", "Texture checksum mismatch")
        # The frozen seal must be the provenance of every delivered texture.
        require(texture_id in frozen_by_id, "texture", "Texture absent from the frozen seal closure")
        frozen_entry = frozen_by_id[texture_id]
        require(frozen_entry["texture"]["descriptor"]["sha256"] == checksum,
                "texture", "Delivered texture differs from the frozen source art")
        verified.append({"texture_id": texture_id, "name": texture.get("name"),
                         "revision": _integer(texture.get("revision"), 1, 2**63-2, "texture.revision"),
                         "width": _integer(texture.get("width"), 1, MAX_SIDE, "texture.width"),
                         "height": _integer(texture.get("height"), 1, MAX_SIDE, "texture.height"),
                         "address": _text(texture.get("address"), "texture.address"),
                         "sha256": checksum})
    return {"textures": verified, "texture_filter": texture_filter,
            "filter_tradeoff": FILTER_TRADEOFF[texture_filter]}


def evaluate(raw):
    members = read_members(raw)
    plate = _json(members["plate.json"], "plate.json")
    seal = _json(members["seal.json"], "seal.json")
    payload = _object(plate.get("payload"), "plate.payload")
    frozen = _object(seal.get("payload"), "seal.payload")
    plate_format = payload.get("format")
    seal_format = frozen.get("format")
    require(plate_format in SUPPORTED_PLATE and seal_format in SUPPORTED_SEAL,
            "format", "Unsupported plate or seal format")
    require(plate.get("state") == "ready" and seal.get("kind") == "seal"
            and seal.get("state") == "frozen", "metadata_state", "Unexpected embedded plate or seal state")
    plate_id = _text(plate.get("id"), "plate.id")
    exposure_id = _text(plate.get("exposure_id"), "plate.exposure_id")
    seal_id = _text(seal.get("id"), "seal.id")
    require(payload.get("seal_id") == seal_id, "seal_id", "Plate refers to another seal")
    content_hash = _checksum(frozen.get("content_hash"), "content_hash")
    require(_digest({key: value for key, value in frozen.items() if key != "content_hash"}) == content_hash,
            "seal_hash", "Frozen canonical content checksum mismatch")
    require(_checksum(payload.get("seal_hash"), "seal_hash") == content_hash,
            "seal_hash", "Plate checksum does not name its frozen content")
    lens = _object(frozen.get("lens"), "lens")
    require(_checksum(payload.get("lens_hash"), "lens_hash") == _digest(lens),
            "lens_hash", "Plate lens checksum mismatch")
    width = _integer(payload.get("width"), 16, MAX_SIDE, "width")
    height = _integer(payload.get("height"), 16, MAX_SIDE, "height")
    request = _object(payload.get("request"), "request")
    require(type(request.get("width")) is int and request["width"] == width
            and type(request.get("height")) is int and request["height"] == height,
            "extent", "Request dimensions disagree with the plate")
    samples = _integer(request.get("samples"), 1, 2, "samples")
    _text(request.get("renderer"), "request.renderer")
    objects = _objects(frozen, payload.get("objects"))
    descriptors = _object(payload.get("passes"), "passes")
    require(set(descriptors) == CHANNELS, "channels", "Expected exactly the six version-one channels")
    for name in sorted(CHANNELS):
        descriptor = _object(descriptors[name], "descriptor")
        require(set(descriptor) == {"sha256", "size"}, "descriptor", "Unexpected descriptor fields")
        data = members["passes/" + name]
        size = _integer(descriptor.get("size"), 0, 64*1024*1024, "descriptor.size")
        require(size == len(data), "channel_size", "Channel size mismatch: " + name)
        require(_checksum(descriptor.get("sha256"), "descriptor.sha256") == hashlib.sha256(data).hexdigest(),
                "channel_hash", "Channel checksum mismatch: " + name)
        if not name.endswith(".png"):
            stride = 3 if name == "color.rgb" else 4
            require(size == width * height * stride, "extent", "Raw channel extent mismatch: " + name)
    require(_checksum(payload.get("visual_hash"), "visual_hash") == _digest(descriptors),
            "visual_hash", "Channel manifest checksum mismatch")
    texture_report = _textures(payload, frozen, members, plate_format, request)
    decoded = {name: decode_rgb(members["passes/" + name], width, height)
               for name in ("color.png", "depth.png", "object.png")}
    require(decoded["color.png"] == members["passes/color.rgb"],
            "color_pixels", "PNG pixels disagree with the raw color channel")
    depths = struct.iter_unpack("<f", members["passes/depth.f32"])
    indices = struct.iter_unpack("<I", members["passes/object.u32"])
    covered = 0
    background = 0
    minimum = math.inf
    maximum = 0.
    picks = []
    sample_offsets = {0, width-1, (height//2)*width+width//2, width*height-1}
    first_foreground = False
    first_background = False
    object_preview = decoded["object.png"]
    for offset, ((depth,), (index,)) in enumerate(zip(depths, indices)):
        if index == 0:
            require(depth == math.inf, "depth_background", "Background must have positive infinite depth")
            background += 1
            take = not first_background
            first_background = True
        else:
            require(str(index) in objects, "object_index", "Pixel refers to an unknown object")
            require(math.isfinite(depth) and depth > 0,
                    "depth_value", "Foreground depth must be finite and positive")
            covered += 1
            minimum = min(minimum, depth)
            maximum = max(maximum, depth)
            take = not first_foreground
            first_foreground = True
        expected_color = bytes(((index * factor) % 255) if index else 0 for factor in (73, 151, 199))
        require(object_preview[offset*3:offset*3+3] == expected_color,
                "object_preview", "Object preview disagrees with the identifier channel")
        if take or offset in sample_offsets:
            picks.append({"x": offset % width, "y": offset // width, "object_index": index,
                          "object": objects.get(str(index)), "depth": depth if index else None,
                          "seal_id": seal_id})
    statistics = _object(payload.get("statistics"), "statistics")
    require(statistics.get("byte_order") == "little"
            and statistics.get("background_depth") == "positive_infinity"
            and statistics.get("depth_units") == "camera_forward_distance",
            "channel_semantics", "Unexpected raw-channel interpretation")
    require(type(statistics.get("covered_pixels")) is int and statistics["covered_pixels"] == covered,
            "coverage", "Coverage count disagrees with the identifier channel")
    if plate_format == "facet-plate-2":
        require(statistics.get("texture_filter") == texture_report["texture_filter"],
                "texture_filter", "Statistics disagree with the recorded display choice")
        taps = statistics.get("texture_taps")
        require(type(taps) is int and taps >= 0, "texture_taps", "Texture tap accounting is invalid")
    report = {
        "valid": True, "format": plate_format,
        "summary": {"width": width, "height": height, "samples": samples,
                    "covered_pixels": covered, "background_pixels": background,
                    "objects": len(objects), "finite_depth_range": [minimum, maximum] if covered else None,
                    "depth_units": "camera_forward_distance", "object_id_scope": "local_to_this_plate",
                    "texture_filter": texture_report["texture_filter"],
                    "textures": len(texture_report["textures"])},
        "provenance": {"plate_id": plate_id, "exposure_id": exposure_id, "seal_id": seal_id,
                       "seal_hash": content_hash, "lens_hash": payload["lens_hash"],
                       "visual_hash": payload["visual_hash"], "renderer": request["renderer"],
                       "texture_display": texture_report,
                       "assembly_id": _text(frozen.get("assembly_id"), "assembly_id"),
                       "assembly_revision": _integer(frozen.get("assembly_revision"), 1, 2**63-2, "assembly_revision"),
                       "objects": objects,
                       "frozen_resources": [
                           {"key": item["placement"]["key"],
                            "mesh_id": item["placement"]["mesh_id"], "mesh_revision": item["mesh_revision"],
                            "pigment_id": item["placement"]["pigment_id"], "pigment_revision": item["pigment_revision"]}
                           for item in sorted(frozen["closure"], key=lambda item: item["placement"]["key"])]},
        "channels": descriptors, "picks": picks, "trust": dict(TRUST),
    }
    return report, members
