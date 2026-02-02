import bpy
import os
import re
from collections import defaultdict
from bpy_extras.io_utils import ImportHelper

# =============================================================================
# Progress helper (fast, safe UI feedback)
# =============================================================================

class SWGI_Progress:
    def __init__(self, context, enabled=True):
        self.context = context
        self.enabled = enabled
        self.wm = context.window_manager
        self.active = False

    def begin(self, total=100, text="Working..."):
        if not self.enabled:
            return
        try:
            self.wm.progress_begin(0, total)
            self.context.workspace.status_text_set(text=text)
            self.active = True
        except Exception:
            self.active = False

    def update(self, value, text=None):
        if not (self.enabled and self.active):
            return
        try:
            self.wm.progress_update(value)
            if text is not None:
                self.context.workspace.status_text_set(text=text)
        except Exception:
            pass

    def end(self):
        if not (self.enabled and self.active):
            return
        try:
            self.wm.progress_end()
            self.context.workspace.status_text_set(text=None)
        except Exception:
            pass
        self.active = False


# =============================================================================
# Naming helpers
# =============================================================================

_DUP_SUFFIX_RE = re.compile(r"^(.*)\.(\d{3})$")

def strip_dup_suffix(name: str) -> str:
    m = _DUP_SUFFIX_RE.match(name)
    return m.group(1) if m else name

def family_name(obj_name: str) -> str:
    base = strip_dup_suffix(obj_name)
    if "-" in base:
        return base.rsplit("-", 1)[0]
    return base

def letter_index_to_code(i: int) -> str:
    out = ""
    i = int(i)
    while True:
        out = chr(ord("A") + (i % 26)) + out
        i = i // 26 - 1
        if i < 0:
            break
    return out

def canonical_master_name(family: str, letter: str) -> str:
    if re.match(r".*-\d+$", family):
        return f"{family}_{letter}"
    return f"{family}-{letter}"

def ensure_unique_name_in_idblock(idblock, desired: str) -> str:
    if desired not in idblock:
        return desired
    base = desired
    idx = 1
    while True:
        cand = f"{base}.{idx:03d}"
        if cand not in idblock:
            return cand
        idx += 1


# =============================================================================
# Safe collection / object utilities
# =============================================================================

def safe_unlink_from_all_collections(obj: bpy.types.Object):
    for c in list(obj.users_collection):
        try:
            c.objects.unlink(obj)
        except Exception:
            pass

def ensure_collection(parent: bpy.types.Collection, name: str) -> bpy.types.Collection:
    if name in bpy.data.collections:
        col = bpy.data.collections[name]
        if col.name not in parent.children:
            try:
                parent.children.link(col)
            except Exception:
                pass
        return col
    col = bpy.data.collections.new(name)
    parent.children.link(col)
    return col

def _unlink_collection_from_all_parents(col: bpy.types.Collection):
    col_name = col.name
    for parent in bpy.data.collections:
        try:
            if col_name in parent.children:
                parent.children.unlink(col)
        except Exception:
            pass
    scene_root = bpy.context.scene.collection
    try:
        if col_name in scene_root.children:
            scene_root.children.unlink(col)
    except Exception:
        pass

def purge_collections_if_empty(cols, verbose=False):
    removed = 0
    for col in cols:
        if not col or col.name not in bpy.data.collections:
            continue
        if len(col.all_objects) == 0:
            _unlink_collection_from_all_parents(col)
            try:
                bpy.data.collections.remove(col)
                removed += 1
            except Exception:
                pass
    if verbose and removed:
        print(f"[SWGI] Purged {removed} empty collections.")


# =============================================================================
# Mesh signature (fast: uses mesh bound_box)
# =============================================================================

def _mesh_bbox_dims_fast(mesh: bpy.types.Mesh):
    if not mesh or not hasattr(mesh, "bound_box") or not mesh.vertices:
        return (0.0, 0.0, 0.0)
    bb = mesh.bound_box
    minx = min(v[0] for v in bb); maxx = max(v[0] for v in bb)
    miny = min(v[1] for v in bb); maxy = max(v[1] for v in bb)
    minz = min(v[2] for v in bb); maxz = max(v[2] for v in bb)
    return (abs(maxx - minx), abs(maxy - miny), abs(maxz - minz))

def _quantise(v: float, step: float) -> float:
    if step <= 0:
        return v
    return round(v / step) * step

def mesh_signature(obj: bpy.types.Object, quant_step: float, cache: dict):
    me = obj.data
    if me in cache:
        return cache[me]
    dx, dy, dz = _mesh_bbox_dims_fast(me)
    sig = (
        len(me.vertices),
        len(me.edges),
        len(me.polygons),
        _quantise(dx, quant_step),
        _quantise(dy, quant_step),
        _quantise(dz, quant_step),
    )
    cache[me] = sig
    return sig


# =============================================================================
# Material utilities (dedupe + remap)
# =============================================================================

def _get_principled(mat: bpy.types.Material):
    if not mat or not mat.use_nodes or not mat.node_tree:
        return None
    for n in mat.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED":
            return n
    return None

def _image_id_from_socket(socket):
    if not socket or not getattr(socket, "is_linked", False):
        return ""
    for link in socket.links:
        node = link.from_node
        if node and node.type == "TEX_IMAGE":
            img = getattr(node, "image", None)
            if img:
                return img.filepath or img.name
    return ""

def material_signature(mat: bpy.types.Material, cache: dict):
    if mat in cache:
        return cache[mat]
    if not mat:
        sig = ("<None>",)
        cache[mat] = sig
        return sig
    if not mat.use_nodes or not mat.node_tree:
        sig = ("NONODE", tuple(round(v, 6) for v in mat.diffuse_color[:]), getattr(mat, "blend_method", ""))
        cache[mat] = sig
        return sig
    bsdf = _get_principled(mat)
    if not bsdf:
        sig = ("NOBSDF", mat.name)
        cache[mat] = sig
        return sig
    bc = tuple(round(v, 6) for v in bsdf.inputs["Base Color"].default_value[:])
    m  = round(float(bsdf.inputs["Metallic"].default_value), 6)
    r  = round(float(bsdf.inputs["Roughness"].default_value), 6)
    bc_img = _image_id_from_socket(bsdf.inputs["Base Color"])
    m_img  = _image_id_from_socket(bsdf.inputs["Metallic"])
    r_img  = _image_id_from_socket(bsdf.inputs["Roughness"])
    n_img  = _image_id_from_socket(bsdf.inputs["Normal"])
    em_img = _image_id_from_socket(bsdf.inputs["Emission"]) if "Emission" in bsdf.inputs else ""
    alpha = getattr(mat, "blend_method", "")
    sig = ("PBR", bc, m, r, bc_img, m_img, r_img, n_img, em_img, alpha)
    cache[mat] = sig
    return sig

def collect_materials_from_objects(objs):
    mats = set()
    for o in objs:
        if not o or o.type != "MESH":
            continue
        for slot in o.material_slots:
            if slot and slot.material:
                mats.add(slot.material)
    return mats

def dedupe_materials_on_objects(objs, verbose=False, respect_names=True):
    mat_cache = {}
    sig_to_master = {}

    def base_name(n: str):
        return strip_dup_suffix(n).strip()

    mats = list(collect_materials_from_objects(objs))
    for mat in mats:
        sig = material_signature(mat, mat_cache)
        key = (sig, base_name(mat.name)) if respect_names else (sig, None)
        if key not in sig_to_master:
            sig_to_master[key] = mat

    replaced = 0
    for o in objs:
        if not o or o.type != "MESH":
            continue
        for slot in o.material_slots:
            mat = slot.material
            if not mat:
                continue
            sig = material_signature(mat, mat_cache)
            key = (sig, base_name(mat.name)) if respect_names else (sig, None)
            master = sig_to_master.get(key)
            if master and master != mat:
                slot.material = master
                replaced += 1

    if verbose:
        print(f"[SWGI] Material dedupe: replaced_slots={replaced}, unique={len(sig_to_master)} (respect_names={respect_names})")
    return replaced

def parse_remap_rules(text: str):
    rules = []
    if not text:
        return rules
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "->" not in line:
            continue
        k, v = line.split("->", 1)
        key = k.strip().lower()
        target = v.strip()
        if key and target:
            rules.append((key, target))
    rules.sort(key=lambda kv: len(kv[0]), reverse=True)
    return rules

def remap_materials_by_rules(objs, rules_text: str, verbose=False):
    rules = parse_remap_rules(rules_text)
    if not rules:
        return 0
    replaced = 0
    for o in objs:
        if not o or o.type != "MESH":
            continue
        for slot in o.material_slots:
            mat = slot.material
            if not mat:
                continue
            name_l = mat.name.lower()
            for key, target_name in rules:
                if key in name_l:
                    target = bpy.data.materials.get(target_name)
                    if target and target != mat:
                        slot.material = target
                        replaced += 1
                    break
    if verbose:
        print(f"[SWGI] Material remap: replaced_slots={replaced}, rules={len(rules)}")
    return replaced


# =============================================================================
# Display State detection: only collections created during import
# =============================================================================

def detect_display_state_collections_from_new(new_collection_names, imported_obj_names):
    imported_set = set(imported_obj_names)
    cols = []
    for cname in new_collection_names:
        col = bpy.data.collections.get(cname)
        if not col:
            continue
        if "display state" not in cname.lower():
            continue
        found = False
        for obj in col.all_objects:
            if obj.name in imported_set:
                found = True
                break
        if found:
            cols.append(col)
    cols.sort(key=lambda c: c.name)
    return cols

def keep_only_display_state(imported_obj_names, chosen_collection_name, verbose=False):
    chosen = bpy.data.collections.get(chosen_collection_name)
    if not chosen:
        return 0
    imported_set = set(imported_obj_names)
    keep = set(o.name for o in chosen.all_objects if o.name in imported_set)
    delete_names = [n for n in imported_obj_names if n not in keep]
    deleted = 0
    for n in delete_names:
        obj = bpy.data.objects.get(n)
        if not obj:
            continue
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            deleted += 1
        except Exception:
            pass
    if verbose:
        print(f"[SWGI] DisplayState keep '{chosen_collection_name}': deleted {deleted}")
    return deleted


# =============================================================================
# Linking meshes
# =============================================================================

def link_meshes_by_family(objects, quant_step: float, prefer_existing: bool, verbose=False, progress: SWGI_Progress | None = None):
    objs = [o for o in objects if o and o.name in bpy.data.objects and o.type == "MESH" and o.data]
    if not objs:
        return {}, (0, 0, 0)

    sig_cache = {}
    fam_map = defaultdict(list)
    for o in objs:
        fam_map[family_name(o.name)].append(o)

    processing_names = set(o.name for o in objs)

    def find_existing_master(fam, sig):
        for o in bpy.data.objects:
            if o.name in processing_names:
                continue
            if o.type != "MESH" or not o.data:
                continue
            if family_name(o.name) != fam:
                continue
            if mesh_signature(o, quant_step, sig_cache) == sig:
                return o
        return None

    linked = 0
    renamed = 0
    families = 0
    masters_by_obj = {}

    fam_keys = sorted(fam_map.keys())
    total_fams = len(fam_keys)

    for i, fam in enumerate(fam_keys):
        families += 1
        if progress:
            progress.update(int((i / max(total_fams, 1)) * 60), text=f"Linking meshes… ({i+1}/{total_fams}) {fam}")

        group = fam_map[fam]
        clusters = defaultdict(list)
        for o in group:
            sig = mesh_signature(o, quant_step, sig_cache)
            clusters[sig].append(o)

        cluster_items = sorted(clusters.items(), key=lambda kv: kv[0])

        for ci, (sig, cluster_objs) in enumerate(cluster_items):
            letter = letter_index_to_code(ci)
            desired = canonical_master_name(fam, letter)

            master = None
            if prefer_existing:
                master = find_existing_master(fam, sig)

            if master is None:
                for o in cluster_objs:
                    if o.name == desired:
                        master = o
                        break
            if master is None:
                master = cluster_objs[0]

            master_data = master.data

            if master.name != desired:
                new_name = ensure_unique_name_in_idblock(bpy.data.objects, desired)
                try:
                    master.name = new_name
                    renamed += 1
                except Exception:
                    pass

            try:
                master_data.name = f"{master.name}_Mesh"
            except Exception:
                pass

            for o in cluster_objs:
                if o == master:
                    masters_by_obj[o.name] = master.name
                    continue
                if o.data != master_data:
                    o.data = master_data
                    linked += 1
                masters_by_obj[o.name] = master.name

    if verbose:
        print(f"[SWGI] Mesh link summary: linked={linked}, renamed_masters={renamed}, families={families}")
    return masters_by_obj, (linked, renamed, families)


# =============================================================================
# Organisation
# =============================================================================

def create_empty(name: str, style: str, size: float):
    e = bpy.data.objects.new(name, None)
    try:
        e.empty_display_type = style
    except Exception:
        e.empty_display_type = "PLAIN_AXES"
    e.empty_display_size = size
    return e

def set_parent_keep_world(child: bpy.types.Object, parent: bpy.types.Object):
    if child.parent == parent:
        return
    mw = child.matrix_world.copy()
    child.parent = parent
    child.matrix_parent_inverse = parent.matrix_world.inverted()
    child.matrix_world = mw

def delete_imported_by_type(imported_names, obj_type: str, verbose=False):
    deleted = 0
    for n in list(imported_names):
        obj = bpy.data.objects.get(n)
        if obj and obj.type == obj_type:
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
                deleted += 1
            except Exception:
                pass
    if verbose and deleted:
        print(f"[SWGI] Deleted {deleted} objects of type {obj_type}")
    return deleted

def organise_per_group(imported_names, label, masters_by_objname, empty_style, empty_size, delete_empties, delete_cameras, verbose=False, progress=None):
    scene_root = bpy.context.scene.collection
    top_name = ensure_unique_name_in_idblock(bpy.data.collections, label)
    top = ensure_collection(scene_root, top_name)

    groups = defaultdict(list)
    for obj_name, master_name in masters_by_objname.items():
        obj = bpy.data.objects.get(obj_name)
        if obj and obj.type == "MESH":
            groups[master_name].append(obj_name)

    master_keys = sorted(groups.keys())
    total = len(master_keys)

    for i, master_name in enumerate(master_keys):
        if progress:
            progress.update(60 + int((i / max(total, 1)) * 25), text=f"Organising groups… ({i+1}/{total}) {master_name}")

        sub_name = ensure_unique_name_in_idblock(bpy.data.collections, master_name)
        sub = ensure_collection(top, sub_name)

        empty_name = ensure_unique_name_in_idblock(bpy.data.objects, f"{master_name}_EMPTY")
        empty = create_empty(empty_name, empty_style, empty_size)
        sub.objects.link(empty)

        for oname in groups[master_name]:
            obj = bpy.data.objects.get(oname)
            if not obj:
                continue
            safe_unlink_from_all_collections(obj)
            sub.objects.link(obj)
            set_parent_keep_world(obj, empty)

    if delete_cameras:
        delete_imported_by_type(imported_names, "CAMERA", verbose=verbose)
    if delete_empties:
        delete_imported_by_type(imported_names, "EMPTY", verbose=verbose)

    return top

def organise_original(label, chosen_display_state_collection: str | None):
    scene_root = bpy.context.scene.collection
    top_name = ensure_unique_name_in_idblock(bpy.data.collections, label)
    top = ensure_collection(scene_root, top_name)

    if chosen_display_state_collection and chosen_display_state_collection in bpy.data.collections:
        chosen = bpy.data.collections[chosen_display_state_collection]
        if chosen.name not in top.children:
            try:
                top.children.link(chosen)
            except Exception:
                pass
        try:
            if chosen.name in scene_root.children:
                scene_root.children.unlink(chosen)
        except Exception:
            pass

    return top

def organise_none(imported_names, label, empty_style, empty_size, delete_empties, delete_cameras, verbose=False):
    scene_root = bpy.context.scene.collection
    top_name = ensure_unique_name_in_idblock(bpy.data.collections, label)
    top = ensure_collection(scene_root, top_name)

    empty_name = ensure_unique_name_in_idblock(bpy.data.objects, f"ROOT_{label}")
    root_empty = create_empty(empty_name, empty_style, empty_size)
    top.objects.link(root_empty)

    for n in imported_names:
        obj = bpy.data.objects.get(n)
        if not obj or obj.type != "MESH":
            continue
        safe_unlink_from_all_collections(obj)
        top.objects.link(obj)
        set_parent_keep_world(obj, root_empty)

    if delete_cameras:
        delete_imported_by_type(imported_names, "CAMERA", verbose=verbose)
    if delete_empties:
        delete_imported_by_type(imported_names, "EMPTY", verbose=verbose)

    return top


# =============================================================================
# Scene storage
# =============================================================================

class SWGI_NameItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()

def display_state_enum_items(self, context):
    scn = context.scene
    imported_names = [it.name for it in scn.swgi_last_imported_names]
    new_col_names = [it.name for it in scn.swgi_last_imported_collections]
    cols = detect_display_state_collections_from_new(new_col_names, imported_names)

    if not cols:
        return [("__ALL__", "No Display States Found (Keep All)", "No Display State collections detected; keep everything.")]
    return [(c.name, c.name, "") for c in cols]


# =============================================================================
# UI Props
# =============================================================================

class SWGI_Props(bpy.types.PropertyGroup):
    quantise: bpy.props.FloatProperty(
        name="Dimension Quantise",
        description="Quantise mesh bbox dims to reduce float noise (Blender units).",
        default=0.0001,
        min=0.0,
        soft_max=0.01,
    )
    verbose: bpy.props.BoolProperty(name="Verbose", default=True)
    show_progress: bpy.props.BoolProperty(name="Show Progress", default=True)

    organisation_mode: bpy.props.EnumProperty(
        name="Organisation",
        items=[
            ("PER_GROUP", "Collections per Object Groups", "Subcollection per master group (Brick-A, Brick-B...)"),
            ("ORIGINAL", "Original Import Collections", "Keep original imported hierarchy (chosen Display State only)"),
            ("NONE", "No Collections", "Everything under one top collection"),
        ],
        default="PER_GROUP",
    )

    empty_display_type: bpy.props.EnumProperty(
        name="Empty Style",
        items=[
            ("PLAIN_AXES", "Axes", ""),
            ("ARROWS", "Arrows", ""),
            ("SINGLE_ARROW", "Single Arrow", ""),
            ("CIRCLE", "Circle", ""),
            ("CUBE", "Cube", ""),
            ("SPHERE", "Sphere", ""),
            ("CONE", "Cone", ""),
            ("IMAGE", "Image", ""),
        ],
        default="PLAIN_AXES",
    )
    empty_display_size: bpy.props.FloatProperty(name="Empty Size", default=0.5, min=0.001, soft_max=10.0)

    delete_imported_empties: bpy.props.BoolProperty(name="Delete Imported Empties", default=True)
    delete_imported_cameras: bpy.props.BoolProperty(name="Delete Imported Cameras", default=True)

    material_dedupe: bpy.props.BoolProperty(name="Deduplicate Materials", default=True)
    material_dedupe_respect_names: bpy.props.BoolProperty(name="Respect Names", default=True)
    material_remap: bpy.props.BoolProperty(name="Remap Materials by Rules", default=False)
    material_rules: bpy.props.StringProperty(
        name="Material Rules",
        description="One per line: keyword -> ExistingMaterialName",
        default=(
            "# Examples:\n"
            "# galvanized -> Galvanised_Generic\n"
            "# galv -> Galvanised_Generic\n"
            "# matte steel -> Steel_Matte_Generic\n"
            "# aluminium -> Aluminium_Generic\n"
            "# whitesolid -> White_Powdercoat\n"
        )
    )


# =============================================================================
# Operators
# =============================================================================

class SWGI_OT_choose_display_state(bpy.types.Operator):
    bl_idname = "swgi.choose_display_state"
    bl_label = "Choose Display State"

    display_state: bpy.props.EnumProperty(items=display_state_enum_items)

    def invoke(self, context, event):
        if not self.display_state:
            items = display_state_enum_items(self, context)
            self.display_state = items[0][0]
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Select which Display State to keep:")
        layout.prop(self, "display_state", text="")

    def execute(self, context):
        scn = context.scene
        p = scn.swgi_props
        prog = SWGI_Progress(context, enabled=p.show_progress)

        imported_names = [it.name for it in scn.swgi_last_imported_names]
        new_col_names = [it.name for it in scn.swgi_last_imported_collections]
        label = scn.swgi_last_import_label
        prefer_existing = scn.swgi_prefer_existing

        display_state_cols = detect_display_state_collections_from_new(new_col_names, imported_names)

        chosen_col_name = None
        deleted_other = 0

        prog.begin(100, text="Preparing…")

        if self.display_state and self.display_state != "__ALL__":
            chosen_col_name = self.display_state
            prog.update(5, text="Keeping chosen Display State…")
            deleted_other = keep_only_display_state(imported_names, chosen_col_name, verbose=p.verbose)

        prog.update(10, text="Gathering imported objects…")
        imported_objs_live = [bpy.data.objects[n] for n in imported_names if n in bpy.data.objects]

        prog.update(15, text="Linking meshes…")
        masters_by_objname, mesh_stats = link_meshes_by_family(
            imported_objs_live,
            quant_step=p.quantise,
            prefer_existing=prefer_existing,
            verbose=p.verbose,
            progress=prog,
        )
        linked_count, renamed_masters, families_count = mesh_stats

        prog.update(65, text="Organising collections…")
        mode = p.organisation_mode

        if mode == "PER_GROUP":
            top = organise_per_group(
                imported_names=imported_names,
                label=label,
                masters_by_objname=masters_by_objname,
                empty_style=p.empty_display_type,
                empty_size=p.empty_display_size,
                delete_empties=p.delete_imported_empties,
                delete_cameras=p.delete_imported_cameras,
                verbose=p.verbose,
                progress=prog,
            )
            purge_collections_if_empty(display_state_cols, verbose=p.verbose)

        elif mode == "ORIGINAL":
            top = organise_original(label=label, chosen_display_state_collection=chosen_col_name)
            for c in list(display_state_cols):
                if chosen_col_name and c.name == chosen_col_name:
                    continue
                if len(c.all_objects) == 0:
                    _unlink_collection_from_all_parents(c)
                    try:
                        bpy.data.collections.remove(c)
                    except Exception:
                        pass

        else:  # NONE
            top = organise_none(
                imported_names=imported_names,
                label=label,
                empty_style=p.empty_display_type,
                empty_size=p.empty_display_size,
                delete_empties=p.delete_imported_empties,
                delete_cameras=p.delete_imported_cameras,
                verbose=p.verbose,
            )
            purge_collections_if_empty(display_state_cols, verbose=p.verbose)

        prog.update(90, text="Processing materials…")
        affected_meshes = [bpy.data.objects[n] for n in imported_names if n in bpy.data.objects and bpy.data.objects[n].type == "MESH"]

        mat_dedupe_count = 0
        mat_remap_count = 0
        if p.material_dedupe:
            mat_dedupe_count = dedupe_materials_on_objects(affected_meshes, verbose=p.verbose, respect_names=p.material_dedupe_respect_names)
        if p.material_remap:
            mat_remap_count = remap_materials_by_rules(affected_meshes, p.material_rules, verbose=p.verbose)

        prog.update(100, text="Done")
        prog.end()

        msg = (
            f"Kept: {self.display_state if self.display_state != '__ALL__' else 'ALL'} "
            f"(deleted {deleted_other}). Mesh linked {linked_count}, renamed {renamed_masters} masters, "
            f"{families_count} families. Mode: {mode}. Top: {top.name}. "
            f"Mat dedupe: {mat_dedupe_count}, remap: {mat_remap_count}."
        )
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class SWGI_OT_import_and_link(bpy.types.Operator, ImportHelper):
    bl_idname = "swgi.import_and_link"
    bl_label = "Import & Link"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ""
    filter_glob: bpy.props.StringProperty(default="*.glb;*.gltf", options={"HIDDEN"})

    def execute(self, context):
        scn = context.scene

        before_objs = set(bpy.data.objects.keys())
        before_cols = set(bpy.data.collections.keys())

        try:
            bpy.ops.import_scene.gltf(filepath=self.filepath)
        except Exception as e:
            self.report({"ERROR"}, f"Import failed: {e}")
            return {"CANCELLED"}

        after_objs = set(bpy.data.objects.keys())
        after_cols = set(bpy.data.collections.keys())

        new_obj_names = sorted(list(after_objs - before_objs))
        new_col_names = sorted(list(after_cols - before_cols))

        scn.swgi_last_imported_names.clear()
        for n in new_obj_names:
            it = scn.swgi_last_imported_names.add()
            it.name = n

        scn.swgi_last_imported_collections.clear()
        for n in new_col_names:
            it = scn.swgi_last_imported_collections.add()
            it.name = n

        scn.swgi_last_import_label = strip_dup_suffix(os.path.splitext(os.path.basename(self.filepath))[0])
        scn.swgi_prefer_existing = True

        bpy.ops.swgi.choose_display_state("INVOKE_DEFAULT")
        return {"FINISHED"}


class SWGI_OT_import_and_link_self(bpy.types.Operator, ImportHelper):
    bl_idname = "swgi.import_and_link_self"
    bl_label = "Import & Link (Self)"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ""
    filter_glob: bpy.props.StringProperty(default="*.glb;*.gltf", options={"HIDDEN"})

    def execute(self, context):
        scn = context.scene

        before_objs = set(bpy.data.objects.keys())
        before_cols = set(bpy.data.collections.keys())

        try:
            bpy.ops.import_scene.gltf(filepath=self.filepath)
        except Exception as e:
            self.report({"ERROR"}, f"Import failed: {e}")
            return {"CANCELLED"}

        after_objs = set(bpy.data.objects.keys())
        after_cols = set(bpy.data.collections.keys())

        new_obj_names = sorted(list(after_objs - before_objs))
        new_col_names = sorted(list(after_cols - before_cols))

        scn.swgi_last_imported_names.clear()
        for n in new_obj_names:
            it = scn.swgi_last_imported_names.add()
            it.name = n

        scn.swgi_last_imported_collections.clear()
        for n in new_col_names:
            it = scn.swgi_last_imported_collections.add()
            it.name = n

        scn.swgi_last_import_label = strip_dup_suffix(os.path.splitext(os.path.basename(self.filepath))[0])
        scn.swgi_prefer_existing = False

        bpy.ops.swgi.choose_display_state("INVOKE_DEFAULT")
        return {"FINISHED"}


# =============================================================================
# Panel
# =============================================================================

class SWGI_PT_panel(bpy.types.Panel):
    bl_label = "Solidworks Importer"
    bl_idname = "SWGI_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Solidworks Importer"

    def draw(self, context):
        layout = self.layout
        p = context.scene.swgi_props

        layout.label(text="Import")
        col = layout.column(align=True)
        col.operator("swgi.import_and_link", icon="IMPORT")
        col.operator("swgi.import_and_link_self", icon="IMPORT")

        layout.separator()
        layout.label(text="Organisation")
        layout.prop(p, "organisation_mode", text="")

        layout.separator()
        layout.label(text="Performance")
        row = layout.row(align=True)
        row.prop(p, "quantise")
        row = layout.row(align=True)
        row.prop(p, "show_progress")
        row.prop(p, "verbose")

        layout.separator()
        layout.label(text="Empty Display")
        row = layout.row(align=True)
        row.prop(p, "empty_display_type", text="")
        row.prop(p, "empty_display_size", text="Size")

        layout.separator()
        layout.label(text="Cleanup")
        col = layout.column(align=True)
        col.prop(p, "delete_imported_empties")
        col.prop(p, "delete_imported_cameras")

        layout.separator()
        layout.label(text="Materials")
        col = layout.column(align=True)
        col.prop(p, "material_dedupe")
        if p.material_dedupe:
            col.prop(p, "material_dedupe_respect_names")
        col.prop(p, "material_remap")
        if p.material_remap:
            col.prop(p, "material_rules", text="")


# =============================================================================
# Register
# =============================================================================

classes = (
    SWGI_NameItem,
    SWGI_Props,
    SWGI_OT_choose_display_state,
    SWGI_OT_import_and_link,
    SWGI_OT_import_and_link_self,
    SWGI_PT_panel,
)

def register():
    for c in classes:
        bpy.utils.register_class(c)

    bpy.types.Scene.swgi_props = bpy.props.PointerProperty(type=SWGI_Props)
    bpy.types.Scene.swgi_last_imported_names = bpy.props.CollectionProperty(type=SWGI_NameItem)
    bpy.types.Scene.swgi_last_imported_collections = bpy.props.CollectionProperty(type=SWGI_NameItem)
    bpy.types.Scene.swgi_last_import_label = bpy.props.StringProperty(default="")
    bpy.types.Scene.swgi_prefer_existing = bpy.props.BoolProperty(default=True)

def unregister():
    del bpy.types.Scene.swgi_prefer_existing
    del bpy.types.Scene.swgi_last_import_label
    del bpy.types.Scene.swgi_last_imported_collections
    del bpy.types.Scene.swgi_last_imported_names
    del bpy.types.Scene.swgi_props

    for c in reversed(classes):
        bpy.utils.unregister_class(c)
