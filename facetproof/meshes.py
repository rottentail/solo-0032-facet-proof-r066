import math
from .algebra import bounds, cross, norm, sub
from .errors import require
from .values import identity, integer, text, vector, object_value


def _uv(value, field):
    require(isinstance(value, (list, tuple)) and len(value) == 2, "uv", field + " needs two components")
    clean = []
    for component in value:
        require(isinstance(component, (int, float)) and not isinstance(component, bool), "uv", field + " must be numeric")
        require(math.isfinite(component) and -10000 <= component <= 10000, "uv", field + " is out of range")
        clean.append(float(component))
    return clean


def validate_mesh(data):
    object_value(data)
    name = text(data.get("name"), "name")
    vertices = data.get("vertices")
    faces = data.get("faces")
    require(isinstance(vertices, list) and 3 <= len(vertices) <= 10000,
            "vertices", "Mesh needs 3..10000 vertices")
    vertices = [vector(p, "vertex", low=-10000, high=10000) for p in vertices]
    require(isinstance(faces, list) and 1 <= len(faces) <= 20000,
            "faces", "Mesh needs 1..20000 triangular faces")
    raw_uvs = data.get("uvs")
    uv_faces = data.get("uv_faces")
    has_uv = raw_uvs is not None or uv_faces is not None
    uvs = []
    if has_uv:
        require(isinstance(raw_uvs, list) and 1 <= len(raw_uvs) <= 30000, "uvs", "Mesh needs 1..30000 UV points")
        uvs = [_uv(point, "uv") for point in raw_uvs]
        require(isinstance(uv_faces, list) and len(uv_faces) == len(faces),
                "uv_faces", "uv_faces must give one UV index triple per face")
    clean_faces = []
    clean_uv_faces = []
    for face_number, face in enumerate(faces):
        require(isinstance(face, list) and len(face) == 3,
                "face", "Faces must be index triples")
        indices = [integer(i, "index", 0, len(vertices)-1) for i in face]
        require(len(set(indices)) == 3, "face", "Repeated face vertex")
        a, b, c = [vertices[i] for i in indices]
        require(norm(cross(sub(b, a), sub(c, a))) > 1e-9,
                "degenerate", "Mesh contains a degenerate triangle")
        clean_faces.append(indices)
        if has_uv:
            uv_face = uv_faces[face_number]
            require(isinstance(uv_face, list) and len(uv_face) == 3,
                    "uv_face", "UV faces must be index triples")
            uv_indices = [integer(i, "uv_index", 0, len(uvs)-1) for i in uv_face]
            # Per-corner UV indices intentionally allow sharing and seams:
            # two geometrically shared corners may point at different UVs, so
            # each side of a seam can carry a different part of the texture.
            clean_uv_faces.append(uv_indices)
    payload = {"name": name, "vertices": vertices, "faces": clean_faces,
               "bounds": bounds(vertices)}
    if has_uv:
        payload["uvs"] = uvs
        payload["uv_faces"] = clean_uv_faces
    return payload


class Meshes:
    def __init__(self, db):
        self.db = db

    def create(self, data):
        payload = validate_mesh(data)
        with self.db.unit_of_work() as conn:
            return self.db.insert(conn, identity("mesh"), "mesh", "usable", payload)

    def retire(self, mesh_id, revision):
        with self.db.unit_of_work() as conn:
            mesh = self.db.get(conn, mesh_id, "mesh")
            require(mesh["state"] == "usable", "state", "Mesh already retired", 409)
            return self.db.update(conn, mesh, mesh["payload"], "retired", revision)

    def get(self, mesh_id):
        with self.db.unit_of_work() as conn:
            return self.db.get(conn, mesh_id, "mesh")
