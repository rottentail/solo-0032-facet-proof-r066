from .algebra import point, face_normal, unit, sub, dot
from .camera import frustum_planes, project, view_point
from .clipping import clip_triangle
from .errors import require
from .shading import illuminate
from .values import integer, digest
from .pigments import srgb_to_linear
from .sampling import build_sampler, FILTERS

RENDERER_REVISION = "facet-raster-2"


def validate_request(data):
    width = integer(data.get("width", 256), "width", 16, 512)
    height = integer(data.get("height", 256), "height", 16, 512)
    samples = integer(data.get("samples", 1), "samples", 1, 2)
    texture_filter = data.get("texture_filter", "bilinear")
    require(isinstance(texture_filter, str) and texture_filter in FILTERS,
            "texture_filter", "texture_filter must be one of " + ", ".join(FILTERS))
    return {"width": width, "height": height, "samples": samples,
            "texture_filter": texture_filter, "renderer": RENDERER_REVISION}


def _texture_sampler(seal, texture_id, texture_filter):
    for item in seal["closure"]:
        texture = item.get("texture")
        if texture is not None and texture.get("id") == texture_id:
            return build_sampler(texture, texture_filter)
    require(False, "texture", "Frozen closure is missing a referenced texture", 409)


def build_plan(seal, request):
    width, height = request["width"] * request["samples"], request["height"] * request["samples"]
    texture_filter = request["texture_filter"]
    lens = seal["lens"]
    planes = frustum_planes(lens, width, height)
    triangles = []
    objects = {}
    samplers = {}
    total_faces = sum(len(item["mesh"]["faces"]) for item in seal["closure"])
    require(total_faces <= 40000, "capacity", "Exposure exceeds 40000 source faces", 422)
    sorted_items = sorted(seal["closure"], key=lambda item: item["placement"]["key"])
    for object_id, item in enumerate(sorted_items, 1):
        placement, mesh, pigment = item["placement"], item["mesh"], item["pigment"]
        objects[str(object_id)] = {"key": placement["key"], "mesh_id": placement["mesh_id"],
                                   "pigment_id": placement["pigment_id"]}
        texture = item.get("texture")
        texture_id = pigment.get("texture_id")
        sampler = None
        if texture_id:
            require(texture is not None and texture.get("id") == texture_id,
                    "texture", "Pigment references a texture absent from the frozen closure", 409)
            require("uvs" in mesh and "uv_faces" in mesh,
                    "uv", "A textured pigment requires per-corner mesh UVs", 422)
            sampler = _texture_sampler(seal, texture_id, texture_filter)
            samplers[texture_id] = sampler
        vertices = [point(placement["matrix"], v) for v in mesh["vertices"]]
        for face_number, face in enumerate(mesh["faces"]):
            world = [vertices[i] for i in face]
            normal = face_normal(world)
            center = [sum(v[a] for v in world) / 3 for a in range(3)]
            direction = sub(lens["eye"], center)
            if dot(direction, direction) < 1e-12:
                continue
            view_direction = unit(direction) if lens["projection"] == "perspective" else [-v for v in lens["forward"]]
            if not pigment["two_sided"] and dot(normal, view_direction) <= 0:
                continue
            color = illuminate(normal, pigment, seal["illumination"], view_direction)
            face_uv = mesh["uv_faces"][face_number] if sampler is not None else None
            entries = []
            for corner, vertex_index in enumerate(face):
                camera_vertex = view_point(lens, world[corner])
                if sampler is None:
                    entries.append([camera_vertex])
                else:
                    entries.append([camera_vertex, mesh["uvs"][face_uv[corner]]])
            for clipped in clip_triangle(entries, planes):
                projected = []
                uvs = []
                for entry in clipped:
                    projected.append(project(lens, entry[0], width, height))
                    if sampler is not None:
                        uvs.append(entry[1])
                record = {"vertices": projected, "color": color,
                          "object_id": object_id, "face": face_number}
                if sampler is not None:
                    record["uvs"] = uvs
                    record["texture_id"] = texture_id
                triangles.append(record)
    return {"width": width, "height": height, "triangles": triangles, "objects": objects,
            "samplers": samplers, "texture_filter": texture_filter,
            "projection": lens["projection"],
            "background": [srgb_to_linear(c) for c in seal["illumination"]["background"]],
            "lens_hash": digest(lens), "source_faces": total_faces}
