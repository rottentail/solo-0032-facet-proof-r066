"""Versioned raster texture resources.

A texture is an 8-bit sRGB image whose PNG bytes live in the content-addressed
blob store; the payload only keeps the descriptor, dimensions and the decoded
linear texels used for rendering. The frozen seal copies this whole payload, so
revising or replacing a texture used by a draft can never alter an already
frozen or delivered exposure.
"""
from .errors import require
from .png import encode_rgb
from .pigments import srgb_to_linear
from .values import identity, integer, text, object_value, choice

MIN_SIDE = 8
MAX_SIDE = 256
ADDRESS_MODES = {"repeat", "clamp"}
TEXTURE_FORMAT = "facet-texture-1"


def _power_of_two(value):
    return value >= MIN_SIDE and value <= MAX_SIDE and (value & (value - 1)) == 0


def validate_texture(data):
    object_value(data)
    name = text(data.get("name"), "name")
    width = integer(data.get("width"), "width", MIN_SIDE, MAX_SIDE)
    height = integer(data.get("height"), "height", MIN_SIDE, MAX_SIDE)
    require(_power_of_two(width), "width", "Texture width must be a power of two between 8 and 256")
    require(_power_of_two(height), "height", "Texture height must be a power of two between 8 and 256")
    pixels = data.get("pixels")
    require(isinstance(pixels, list) and len(pixels) == width * height,
            "pixels", "pixels must be one RGB triple per texel in row order")
    srgb = bytearray(width * height * 3)
    linear = []
    for index, triple in enumerate(pixels):
        require(isinstance(triple, list) and len(triple) == 3, "pixels", "Each texel needs three channels")
        for channel, sample in enumerate(triple):
            value = integer(sample, "texel", 0, 255)
            srgb[index * 3 + channel] = value
            linear.append(srgb_to_linear(value / 255))
    address = choice(data.get("address", "repeat"), "address", ADDRESS_MODES)
    return {"name": name, "format": TEXTURE_FORMAT, "width": width, "height": height,
            "address": address,
            "pixels": [list(srgb[i:i+3]) for i in range(0, len(srgb), 3)],
            "linear": linear, "png_bytes": encode_rgb(width, height, bytes(srgb))}


class Textures:
    def __init__(self, db, blobs):
        self.db, self.blobs = db, blobs

    def _store(self, payload):
        png = payload.pop("png_bytes")
        payload["descriptor"] = self.blobs.put(png)
        return payload

    def create(self, data):
        payload = self._store(validate_texture(data))
        with self.db.unit_of_work() as conn:
            return self.db.insert(conn, identity("texture"), "texture", "usable", payload)

    def revise(self, texture_id, data):
        payload = self._store(validate_texture(data))
        require("revision" in data, "revision", "Expected revision is required")
        with self.db.unit_of_work() as conn:
            obj = self.db.get(conn, texture_id, "texture")
            return self.db.update(conn, obj, payload, expected=data.get("revision"))

    def retire(self, texture_id, revision):
        with self.db.unit_of_work() as conn:
            obj = self.db.get(conn, texture_id, "texture")
            require(obj["state"] == "usable", "state", "Texture already retired", 409)
            return self.db.update(conn, obj, obj["payload"], "retired", revision)

    def get(self, texture_id):
        with self.db.unit_of_work() as conn:
            return self.db.get(conn, texture_id, "texture")
