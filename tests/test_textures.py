"""Self-contained verification of the textured-material feature.

Runs without a network and only against a throwaway data root. It exercises:

* perspective-correct UV mapping across a grazing angle and the frame edge;
* per-corner UV islands so a seam keeps a different pattern on each side;
* the nearest / bilinear / trilinear display choices and their sampling cost;
* the freeze guarantee when a draft texture is swapped;
* offline bundle provenance verified by the service-independent facetreader.

Run:  python3 tests/test_textures.py
Exit status is zero only when every assertion holds.
"""
import io
import math
import shutil
import tempfile
import zipfile

from facetproof.application import Application
from facetproof.routing import route_request, PREFIX
from facetreader import validate_bundle


def _texture(size=64):
    pixels = []
    for y in range(size):
        for x in range(size):
            pixels.append([0 if x % 2 == 0 else 255,
                           0 if (x // 8 + y // 8) % 2 == 0 else 255,
                           20 if (12 <= x <= 26 and 12 <= y <= 26) else 235])
    return pixels


def _cube():
    vertices, uvs, faces, uv_faces = [], [], [], []
    index = {}

    def vertex(p):
        if p not in index:
            index[p] = len(vertices)
            vertices.append(list(p))
        return index[p]

    def uv(p):
        uvs.append(list(p))
        return len(uvs) - 1

    def quad(corners, rect):
        ids = [vertex(p) for p in corners]
        u0, v0, u1, v1 = rect
        local = [uv((u0, v0)), uv((u1, v0)), uv((u1, v1)), uv((u0, v1))]
        faces.append([ids[0], ids[1], ids[2]])
        faces.append([ids[0], ids[2], ids[3]])
        uv_faces.append([local[0], local[1], local[2]])
        uv_faces.append([local[0], local[2], local[3]])

    quad([(-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)], (0, 0, 1, 1))
    quad([(1, -1, -1), (-1, -1, -1), (-1, 1, -1), (1, 1, -1)], (0, 0, 3, 3))
    quad([(1, -1, 1), (1, -1, -1), (1, 1, -1), (1, 1, 1)], (0, 0, 1, 1))
    quad([(-1, -1, -1), (-1, -1, 1), (-1, 1, 1), (-1, 1, -1)], (0, 0, 1, 1))
    quad([(-1, 1, 1), (1, 1, 1), (1, 1, -1), (-1, 1, -1)], (0, 0, 1, 1))
    quad([(-1, -1, -1), (1, -1, -1), (1, -1, 1), (-1, -1, 1)], (0, 0, 1, 1))
    return {"name": "cube", "vertices": vertices, "faces": faces,
            "uvs": uvs, "uv_faces": uv_faces}


def _edge_quad():
    return {"name": "edge", "vertices": [[-6, -1, -.4], [6, -1, -.4], [6, 1, -.4], [-6, 1, -.4]],
            "faces": [[0, 1, 2], [0, 2, 3]],
            "uvs": [[0, 0], [8, 0], [8, 1], [0, 1]],
            "uv_faces": [[0, 1, 2], [0, 2, 3]]}


def main():
    root = tempfile.mkdtemp(prefix="facet-test-")
    try:
        app = Application(root)

        def call(method, route, data=None):
            return route_request(app, method, PREFIX + route, data)

        texture = call("POST", "textures", {"name": "art", "width": 64, "height": 64,
                                            "pixels": _texture(), "address": "repeat"})
        cube = call("POST", "meshes", _cube())
        edge = call("POST", "meshes", _edge_quad())
        pigment = call("POST", "pigments", {"name": "glaze", "color": [.95, .95, .95],
                                            "two_sided": True, "texture_id": texture["id"]})

        # A pigment cannot bind a texture to geometry that lacks UVs.
        plain = call("POST", "meshes", {"name": "plain",
                                        "vertices": [[-1, -1, 0], [1, -1, 0], [1, 1, 0]],
                                        "faces": [[0, 1, 2]]})
        assembly_bad = call("POST", "assemblies", {"name": "bad"})
        try:
            call("PUT", "assemblies/" + assembly_bad["id"],
                 {"revision": 1, "placements": [{"key": "p", "mesh_id": plain["id"],
                                                 "pigment_id": pigment["id"]}]})
            raise AssertionError("textured pigment on UV-less geometry was accepted")
        except Exception as exc:
            assert getattr(exc, "code", None) == "uv", exc

        assembly = call("POST", "assemblies", {"name": "scene",
                                               "lens": {"eye": [3.2, 2.1, 4.0]}})
        call("PUT", "assemblies/" + assembly["id"], {"revision": 1, "placements": [
            {"key": "cube", "mesh_id": cube["id"], "pigment_id": pigment["id"]},
            {"key": "edge-strip", "mesh_id": edge["id"], "pigment_id": pigment["id"]}]})
        seal = call("POST", "assemblies/" + assembly["id"] + "/seal", {"revision": 2})
        assert seal["payload"]["format"] == "facet-seal-2"
        frozen_checksum = seal["payload"]["closure"][0]["texture"]["descriptor"]["sha256"]

        plates, colors, taps = {}, {}, {}
        for texture_filter in ("nearest", "bilinear", "trilinear"):
            call("POST", "seals/" + seal["id"] + "/exposures",
                 {"key": "k-" + texture_filter, "width": 96, "height": 96, "samples": 2,
                  "texture_filter": texture_filter})
            rendered = call("POST", "worker/step")
            assert rendered["state"] == "succeeded", rendered
            plate = rendered["plate"]
            plates[texture_filter] = plate
            colors[texture_filter] = call("GET", "plates/" + plate["id"] + "/passes/color.rgb")[0]
            taps[texture_filter] = plate["payload"]["statistics"]["texture_taps"]

        # Sampling cost is an explicit, ordered tradeoff.
        assert taps["nearest"] < taps["bilinear"] < taps["trilinear"], taps
        assert colors["bilinear"] != colors["trilinear"], "mip LOD never engaged"
        assert colors["nearest"] != colors["bilinear"], "filter choice had no visual effect"

        # Freeze: swapping the draft texture leaves the seal hash and the
        # already delivered pixels untouched.
        before = seal["payload"]["content_hash"]
        call("PUT", "textures/" + texture["id"],
             {"revision": 1, "name": "swapped", "width": 64, "height": 64,
              "pixels": [[255, 0, 0] for _ in range(4096)], "address": "repeat"})
        assert call("GET", "seals/" + seal["id"])["payload"]["content_hash"] == before
        assert call("GET", "plates/" + plates["bilinear"]["id"] + "/passes/color.rgb")[0] == colors["bilinear"]

        # Offline provenance without the service, including tamper rejection.
        bundle = call("GET", "plates/" + plates["trilinear"]["id"] + "/bundle")[0]
        report = validate_bundle(bundle)
        assert report["valid"], report
        display = report["provenance"]["texture_display"]
        assert display["texture_filter"] == "trilinear"
        assert display["textures"][0]["sha256"] == frozen_checksum
        assert not validate_bundle(bundle[:-40])["valid"]

        source = zipfile.ZipFile(io.BytesIO(bundle))
        tex_member = next(n for n in source.namelist() if n.startswith("textures/"))
        tampered = io.BytesIO()
        with zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as out:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == tex_member:
                    data = data[:80] + bytes([data[80] ^ 255]) + data[81:]
                out.writestr(item, data)
        assert not validate_bundle(tampered.getvalue())["valid"]

        print("texture feature checks passed:")
        print("  tap cost nearest < bilinear < trilinear =",
              taps["nearest"], taps["bilinear"], taps["trilinear"])
        print("  frozen texture sha256 =", frozen_checksum)
        print("  offline report format =", report["format"])
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
