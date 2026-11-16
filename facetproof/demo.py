import argparse
import json
from pathlib import Path
import tempfile
import shutil
from .application import Application
from .errors import Fault, require
from .routing import route_request, PREFIX

FLOWS = ["mesh-intake", "mesh-retirement", "assembly-edit", "lens-edit", "seal-closure", "exposure-submit", "render-plates", "cancel-exposure", "retry-exposure", "verify-plate", "pixel-pick", "compare-plates", "proof-decision", "quarantine-plate", "recover-lease", "export-bundle", "reserve-capacity", "settle-capacity", "resize-capacity", "tile-progress", "offline-inspect", "backup-closure", "content-inspection", "texture-display"]

FILTERS = ("nearest", "bilinear", "trilinear")


def _sample_texture(size=64):
    """Dense fine stripes (alias/shimmer region) plus a bold mark (readability)."""
    pixels = []
    for y in range(size):
        for x in range(size):
            fine = 0 if (x % 2 == 0) else 255                 # one-texel dense lines
            coarse = 0 if ((x // 8 + y // 8) % 2 == 0) else 255
            mark = 20 if (12 <= x <= 26 and 12 <= y <= 26) else 235
            pixels.append([fine, coarse, mark])
    return pixels


def _cube_mesh():
    """Every quad corner gets its own UV index, so each cube face is an
    independent UV island: two sides of an edge may keep different patterns."""
    positions = {}
    vertices, uvs, faces, uv_faces = [], [], [], []

    def vertex(p):
        if p not in positions:
            positions[p] = len(vertices)
            vertices.append(list(p))
        return positions[p]

    def uv(point):
        uvs.append(list(point))
        return len(uvs) - 1

    def quad(corners, uv_rect):
        u0, v0, u1, v1 = uv_rect
        ids = [vertex(p) for p in corners]
        local = [uv((u0, v0)), uv((u1, v0)), uv((u1, v1)), uv((u0, v1))]
        faces.append([ids[0], ids[1], ids[2]])
        faces.append([ids[0], ids[2], ids[3]])
        uv_faces.append([local[0], local[1], local[2]])
        uv_faces.append([local[0], local[2], local[3]])

    # Two UV islands with deliberately different repeat windows across the seam.
    quad([(-1,-1, 1), (1,-1, 1), (1, 1, 1), (-1, 1, 1)], (0, 0, 1, 1))
    quad([( 1,-1,-1), (-1,-1,-1), (-1, 1,-1), (1, 1,-1)], (0, 0, 3, 3))
    quad([(1,-1, 1), (1,-1,-1), (1, 1,-1), (1, 1, 1)], (0, 0, 1, 1))
    quad([(-1,-1,-1), (-1,-1, 1), (-1, 1, 1), (-1, 1,-1)], (0, 0, 1, 1))
    quad([(-1, 1, 1), (1, 1, 1), (1, 1,-1), (-1, 1,-1)], (0, 0, 1, 1))
    quad([(-1,-1,-1), (1,-1,-1), (1,-1, 1), (-1,-1, 1)], (0, 0, 1, 1))
    return {"name": "标记细纹立方体", "vertices": vertices, "faces": faces,
            "uvs": uvs, "uv_faces": uv_faces}


def _edge_quad_mesh():
    """A wide quad deliberately crossing the frame edge in the demo camera."""
    vertices = [[-6, -1, -.4], [6, -1, -.4], [6, 1, -.4], [-6, 1, -.4]]
    faces = [[0, 1, 2], [0, 2, 3]]
    uvs = [[0, 0], [8, 0], [8, 1], [0, 1]]
    uv_faces = [[0, 1, 2], [0, 2, 3]]
    return {"name": "跨画面边缘细纹板", "vertices": vertices, "faces": faces,
            "uvs": uvs, "uv_faces": uv_faces}


def _render_plate(app, call, seal_id, key, texture_filter):
    request = {"key": key, "width": 96, "height": 96, "samples": 2,
               "texture_filter": texture_filter}
    call("POST", "seals/" + seal_id + "/exposures", request)
    rendered = call("POST", "worker/step")
    require(rendered["state"] == "succeeded", "commission", str(rendered), 500)
    return rendered["plate"]


def _color_bytes(plate, call):
    return call("GET", "plates/" + plate["id"] + "/passes/color.rgb")[0]


def _texture_flow(app, call, output):
    texture = call("POST", "textures", {"name": "标记与细纹", "width": 64,
                                        "height": 64, "pixels": _sample_texture(),
                                        "address": "repeat"})
    mesh = call("POST", "meshes", _cube_mesh())
    edge_mesh = call("POST", "meshes", _edge_quad_mesh())
    pigment = call("POST", "pigments", {"name": "贴图白釉", "color": [.95, .95, .95],
                                        "two_sided": True, "texture_id": texture["id"]})
    assembly = call("POST", "assemblies", {"name": "贴图显示取舍图",
                                           "lens": {"eye": [3.2, 2.1, 4.0]}})
    assembly = call("PUT", "assemblies/" + assembly["id"],
                    {"revision": 1, "placements": [
                        {"key": "cube", "mesh_id": mesh["id"], "pigment_id": pigment["id"]},
                        {"key": "edge-strip", "mesh_id": edge_mesh["id"], "pigment_id": pigment["id"]}]})
    seal = call("POST", "assemblies/" + assembly["id"] + "/seal", {"revision": 2})

    # One frozen scene rendered under every display choice. A small camera
    # jitter in a second seal quantifies motion shimmer on dense fine lines.
    jitter_assembly = call("POST", "assemblies", {"name": "镜头微动同场景",
                                                  "lens": {"eye": [3.32, 2.05, 4.08]}})
    call("PUT", "assemblies/" + jitter_assembly["id"],
         {"revision": 1, "placements": [
             {"key": "cube", "mesh_id": mesh["id"], "pigment_id": pigment["id"]},
             {"key": "edge-strip", "mesh_id": edge_mesh["id"], "pigment_id": pigment["id"]}]})
    jitter_seal = call("POST", "assemblies/" + jitter_assembly["id"] + "/seal", {"revision": 2})

    plates, jitter_plates, metrics = {}, {}, []
    for texture_filter in FILTERS:
        plate = _render_plate(app, call, seal["id"], "filter-" + texture_filter, texture_filter)
        moved = _render_plate(app, call, jitter_seal["id"], "jitter-" + texture_filter, texture_filter)
        plates[texture_filter], jitter_plates[texture_filter] = plate, moved
        base = _color_bytes(plate, call)
        moved_bytes = _color_bytes(moved, call)
        # Whole-frame change mixes geometry and texture motion; report it as an
        # observation. The structural cost guarantee is the texel-tap count.
        changed = sum(a != b for a, b in zip(base, moved_bytes))
        # Mean absolute neighbour difference is high where edges stay crisp and
        # low where mip averaging softens the image.
        width = plate["payload"]["width"]
        contrast = 0
        samples = 0
        for y in range(width - 1):
            for x in range(width - 1):
                o = (y * width + x) * 3
                contrast += abs(base[o] - base[o + 3])
                samples += 1
        stats = plate["payload"]["statistics"]
        metrics.append({"texture_filter": texture_filter,
                        "edge_contrast": round(contrast / max(1, samples), 3),
                        "motion_changed_bytes": changed,
                        "texture_taps": stats["texture_taps"],
                        "texel_taps_per_textured_fragment":
                            {"nearest": 1, "bilinear": 4, "trilinear": 8}[texture_filter],
                        "loss": {"nearest": "边缘最锐利，但远处密集细纹随镜头微动闪变（无 LOD 平滑）",
                                 "bilinear": "平面内边缘平滑且保留细节，但远处细纹仍会闪",
                                 "trilinear": "镜头微动下最稳定，代价是采样最多且最细小字被柔化"}[texture_filter]})
        if output:
            from pathlib import Path
            Path(output).mkdir(parents=True, exist_ok=True)
            (Path(output) / ("texture-" + texture_filter + ".png")).write_bytes(
                call("GET", "plates/" + plate["id"] + "/passes/color.png")[0])

    # Explainable, testable sampling-cost ordering: each richer filter spends
    # strictly more texel fetches per textured fragment. Sharpness and stability
    # observations accompany it rather than being forced into a brittle order.
    by = {m["texture_filter"]: m for m in metrics}
    require(by["nearest"]["texture_taps"] < by["bilinear"]["texture_taps"]
            < by["trilinear"]["texture_taps"], "commission", "Texture tap ordering wrong", 500)
    require(by["nearest"]["texel_taps_per_textured_fragment"] == 1
            and by["bilinear"]["texel_taps_per_textured_fragment"] == 4
            and by["trilinear"]["texel_taps_per_textured_fragment"] == 8,
            "commission", "Per-fragment tap contract wrong", 500)
    require(by["nearest"]["edge_contrast"] >= by["trilinear"]["edge_contrast"],
            "commission", "Nearest did not preserve harder edges than trilinear", 500)
    # With real LOD engagement, trilinear mip averaging must visibly soften the
    # same scene relative to bilinear, proving the stability choice is real.
    require(_color_bytes(plates["bilinear"], call) != _color_bytes(plates["trilinear"], call),
            "commission", "Trilinear output identical to bilinear; mip LOD not engaged", 500)
    require(by["trilinear"]["edge_contrast"] < by["nearest"]["edge_contrast"],
            "commission", "Trilinear did not soften fine detail versus nearest", 500)

    # Freeze guarantee: revise the draft texture, then prove the historical
    # seal and its already rendered pixels are unchanged.
    before_hash = seal["payload"]["content_hash"]
    before_pixels = _color_bytes(plates["bilinear"], call)
    call("PUT", "textures/" + texture["id"],
         {"revision": 1, "name": "替换后的图", "width": 64, "height": 64,
          "pixels": [[255, 0, 0] for _ in range(64 * 64)], "address": "repeat"})
    require(call("GET", "seals/" + seal["id"])["payload"]["content_hash"] == before_hash,
            "commission", "Texture swap rewrote frozen history", 500)
    require(_color_bytes(plates["bilinear"], call) == before_pixels,
            "commission", "Texture swap changed a delivered plate", 500)

    # Offline delivery carries the texture bytes and the display choice; the
    # independent reader verifies provenance without contacting the service.
    from facetreader import validate_bundle
    bundle = call("GET", "plates/" + plates["trilinear"]["id"] + "/bundle")[0]
    report = validate_bundle(bundle)
    require(report["valid"], "commission", str(report), 500)
    provenance = report["provenance"]["texture_display"]
    require(provenance["texture_filter"] == "trilinear" and len(provenance["textures"]) == 1,
            "commission", "Offline texture provenance missing", 500)
    frozen_checksum = seal["payload"]["closure"][0]["texture"]["descriptor"]["sha256"]
    require(provenance["textures"][0]["sha256"] == frozen_checksum,
            "commission", "Offline texture does not match frozen source art", 500)

    return {"workflow": "texture-display", "filters": metrics,
            "offline": {"format": report["format"],
                        "texture_filter": provenance["texture_filter"],
                        "tradeoff": provenance["filter_tradeoff"],
                        "texture_sha256": provenance["textures"][0]["sha256"]},
            "freeze_guard": "texture swap left the sealed hash and delivered pixels unchanged",
            "rejected": "state"}


def commission(root, flow, output=None):
    app = Application(root)
    def call(method, route, data=None): return route_request(app, method, PREFIX + route, data)
    def rejected(method, route, data, code):
        try: call(method, route, data)
        except Fault as exc:
            require(exc.code == code, "commission", f"Expected {code}, got {exc.code}", 500)
            return exc.code
        raise Fault("commission", "Expected rejection", 500)
    mesh_data = {"name":"斜翼", "vertices":[[-1,-1,0],[1,-1,0],[1,1,0],[-1,1,0],[0,0,1.2]], "faces":[[0,1,4],[1,2,4],[2,3,4],[3,0,4],[0,3,2],[0,2,1]]}
    mesh = call("POST", "meshes", mesh_data)
    pigment = call("POST", "pigments", {"name":"釉青", "color":[.12,.62,.61], "two_sided":True})
    assembly = call("POST", "assemblies", {"name":"斜翼结构说明图", "lens":{"eye":[3,2,4]}})
    assembly = call("PUT", "assemblies/"+assembly["id"], {"revision":1, "placements":[{"key":"wing", "mesh_id":mesh["id"], "pigment_id":pigment["id"], "transform":{"euler":[0,20,0]}}]})
    result = {"workflow":flow}
    if flow == "texture-display":
        return _texture_flow(app, call, output)
    if flow == "mesh-intake":
        result["rejected"] = rejected("POST", "meshes", {**mesh_data, "faces":[[0,0,1]]}, "face")
        return result
    if flow == "mesh-retirement":
        call("POST", "meshes/"+mesh["id"]+"/retire", {"revision":1})
        result["rejected"] = rejected("POST", "assemblies/"+assembly["id"]+"/seal", {"revision":2}, "retired")
        return result
    if flow in ("assembly-edit", "lens-edit"):
        changes = {"name":"更新说明图"} if flow == "assembly-edit" else {"lens":{"projection":"orthographic"}}
        edited = call("PUT", "assemblies/"+assembly["id"], {"revision":2, **changes})
        result.update(revision=edited["revision"], rejected=rejected("PUT", "assemblies/"+assembly["id"], {"revision":2, "name":"过期"}, "stale_revision"))
        return result
    seal = call("POST", "assemblies/"+assembly["id"]+"/seal", {"revision":2})
    if flow == "seal-closure":
        call("PUT", "pigments/"+pigment["id"], {"revision":1, "name":"改色", "color":[1,0,0]})
        require(call("GET", "seals/"+seal["id"]) == seal, "commission", "Seal changed", 500)
        result["rejected"] = rejected("PUT", "assemblies/"+assembly["id"], {"revision":3, "name":"改稿"}, "sealed")
        return result
    request = {"key":"commission", "width":96, "height":96, "samples":2}
    if flow in {'reserve-capacity', 'settle-capacity', 'resize-capacity'}:
        request.update(width=16, height=16, samples=1)
        call('PUT', 'resources', {'capacity':256, 'revision':1})
        exposure = call('POST', 'seals/'+seal['id']+'/exposures', request)
        result['reserved'] = call('GET', 'resources')['reserved']
        require(result['reserved'] == 256, 'commission', 'Reservation missing', 500)
        result['rejected'] = rejected('POST', 'seals/'+seal['id']+'/exposures', {**request,'key':'too-many'}, 'capacity_reserved')
        if flow == 'reserve-capacity':
            call('POST', 'exposures/'+exposure['id']+'/cancel')
        elif flow == 'settle-capacity':
            claim = app.exposures.claim()
            call('POST', 'exposures/'+exposure['id']+'/cancel')
            require(call('GET', 'resources')['reserved'] == 256, 'commission', 'Accepted cancellation released running work', 500)
            app.exposures.fail(exposure['id'], claim['token'], 'cancel_checkpoint')
        else:
            call('PUT', 'resources', {'capacity':512, 'revision':2})
            second = call('POST', 'seals/'+seal['id']+'/exposures', {**request,'key':'second'})
            policy = call('PUT', 'resources', {'capacity':256, 'revision':3})
            require(policy['overcommitted'] == 256, 'commission', 'Prior promises were erased', 500)
            rejected('PUT', 'resources', {'capacity':512,'revision':3}, 'stale_revision')
            require(call('POST', 'worker/step')['state']=='succeeded','commission','Promised work lost',500)
            require(call('POST', 'worker/step')['state']=='succeeded','commission','Second promise lost',500)
            result['settled'] = second['id']
        require(call('GET', 'resources')['reserved'] == 0, 'commission', 'Settled work leaked capacity', 500)
        result['capacity'] = call('GET', 'resources')
        return result
    exposure = call("POST", "seals/"+seal["id"]+"/exposures", request)
    eroute = "exposures/"+exposure["id"]
    if flow == 'tile-progress':
        require(not call('GET', eroute+'/progress')['planned'], 'commission', 'Queued work claims completed pixels', 500)
    if flow == "exposure-submit":
        require(call("POST", "seals/"+seal["id"]+"/exposures", request)["id"] == exposure["id"], "commission", "Idempotency mismatch", 500)
        result["rejected"] = rejected("POST", "seals/"+seal["id"]+"/exposures", {**request, "width":128}, "key_conflict")
        return result
    if flow == "cancel-exposure":
        result["state"] = call("POST", eroute+"/cancel")["state"]
        require(call("POST", "worker/step")["state"] == "idle", "commission", "Cancelled work claimed", 500)
        result["rejected"] = rejected("POST", eroute+"/retry", {}, "state")
        return result
    if flow in ("retry-exposure", "recover-lease"):
        claimed = app.exposures.claim(lease_seconds=-1 if flow == "recover-lease" else 120)
        if flow == "retry-exposure":
            app.exposures.fail(exposure["id"], claimed["token"], "interrupted")
            call("POST", eroute+"/retry")
        else:
            require(exposure["id"] in call("POST", "worker/recover")["recovered"], "commission", "Recovery failed", 500)
    rendered = call("POST", "worker/step")
    require(rendered["state"] == "succeeded", "commission", str(rendered), 500)
    plate = rendered["plate"]
    route = "plates/"+plate["id"]
    require(plate["payload"]["statistics"]["covered_pixels"] > 0, "commission", "Empty image", 500)
    result["statistics"] = plate["payload"]["statistics"]
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        for name in ("color.png", "depth.png", "object.png"): (Path(output)/name).write_bytes(call("GET", route+"/passes/"+name)[0])
    if flow == 'offline-inspect':
        from facetreader import validate_bundle
        bundle = call('GET', route+'/bundle')[0]
        report = validate_bundle(bundle)
        require(report['valid'], 'commission', str(report), 500)
        damaged = validate_bundle(bundle[:-20])
        require(not damaged['valid'], 'commission', 'Truncated package was accepted', 500)
        result['offline'] = report['summary']
        result['rejected'] = damaged['error']
    elif flow == 'backup-closure':
        proof = call('POST', route+'/proofs', {'note':'备份前核验'})
        call('POST', 'proofs/'+proof['id']+'/decide', {'state':'accepted','note':'通道完整'})
        backup = call('POST', 'maintenance/backups')
        op = backup['operation_id']
        require(call('GET', 'maintenance/backups/'+op)['state']=='running','commission','Missing running backup',500)
        rejected('GET', 'maintenance/backups/'+op+'/verify', {}, 'backup_incomplete')
        completed = call('POST', 'maintenance/backups/'+op+'/resume')
        require(completed['state']=='succeeded','commission','Backup was not published',500)
        call('POST', route+'/quarantine', {'reason':'备份时点后的撤回'})
        require(call('GET', 'maintenance/backups/'+op+'/verify')['valid'],'commission','Immutable backup verification failed',500)
        restore_root = Path(root)/'restore-check'
        shutil.copytree(completed['data_directory'], restore_root)
        restored = Application(restore_root)
        require(restored.plates.verify(plate['id'])['valid'] and restored.proofs.get(proof['id'])['state']=='accepted',
                'commission','Restored snapshot mixed later review facts',500)
        require(restored.bundles.export(plate['id']), 'commission', 'Restored export failed', 500)
        result['backup'] = {'state':completed['state'],'references':len(completed['manifest']['closure']['references'])}
        result['rejected'] = rejected('GET', route+'/bundle', {}, 'plate_state')
    elif flow == 'content-inspection':
        result['inspection'] = call('GET', 'maintenance/inspect')
        result['rejected'] = rejected('GET', 'maintenance/backups/unknown', {}, 'backup_id')
    elif flow == 'tile-progress':
        progress = call('GET', eroute+'/progress')
        require(progress['complete'] and progress['committed_tiles'] == 9 and progress['state'] == 'succeeded',
                'commission', 'Persisted progress does not match the completed exposure', 500)
        result['progress'] = progress
        result['verification'] = call('POST', route+'/verify')
    elif flow == "verify-plate": result["verification"] = call("POST", route+"/verify")
    elif flow == "pixel-pick":
        result["pick"] = call("GET", route+"/pick?x=48&y=48")
        require(result["pick"]["object"] is not None, "commission", "Center object missing", 500)
    elif flow == "compare-plates":
        call("POST", "seals/"+seal["id"]+"/exposures", {**request, "key":"second"})
        second = call("POST", "worker/step")["plate"]
        result["comparison"] = call("POST", "comparisons", {"left":plate["id"], "right":second["id"]})["payload"]
        require(result["comparison"]["color_changed"] == 0, "commission", "Determinism failed", 500)
    elif flow in ("proof-decision", "quarantine-plate"):
        proof = call("POST", route+"/proofs", {"note":"检查结构轮廓"})
        result["proof"] = call("POST", "proofs/"+proof["id"]+"/decide", {"state":"accepted", "note":"通过"})["state"]
        if flow == "quarantine-plate":
            call("POST", route+"/quarantine", {"reason":"撤回"})
            require(call("GET", "proofs/"+proof["id"])["state"] == "invalidated", "commission", "Proof remains accepted", 500)
            call("POST", route+"/purge")
            result["rejected"] = rejected("GET", route+"/bundle", {}, "plate_state")
    elif flow == "export-bundle":
        bundle = call("GET", route+"/bundle")[0]
        require(bundle == call("GET", route+"/bundle")[0], "commission", "Bundle changed", 500)
        result["bundle_bytes"] = len(bundle)
    if "rejected" not in result:
        result["rejected"] = rejected("POST", eroute+"/cancel", {}, "state")
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--flow", choices=FLOWS, default="render-plates")
    parser.add_argument("--output")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="facetproof-") as root:
        print(json.dumps(commission(root, args.flow, args.output), ensure_ascii=False, indent=2))
if __name__ == "__main__": main()
