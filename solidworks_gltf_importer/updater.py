import bpy
import os
import json
import tempfile
import zipfile
import urllib.request
from bpy.types import Operator, AddonPreferences
from bpy.props import StringProperty, BoolProperty
from pathlib import Path

ADDON_PACKAGE = __package__  # "solidworks_gltf_importer"
ADDON_MODULE_NAME = ADDON_PACKAGE.split(".")[0] if ADDON_PACKAGE else "solidworks_gltf_importer"

def _current_version_tuple():
    try:
        import importlib
        mod = importlib.import_module(ADDON_MODULE_NAME)
        return tuple(mod.bl_info.get("version", (0, 0, 0)))
    except Exception:
        return (0, 0, 0)

def _addons_dir():
    # User scripts/addons path
    # bpy.utils.user_resource('SCRIPTS', "addons") is best for installs.
    p = bpy.utils.user_resource('SCRIPTS', path="addons")
    return Path(p) if p else None

def _addon_install_dir():
    ad = _addons_dir()
    if not ad:
        return None
    return ad / ADDON_MODULE_NAME

def _fetch_json(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers={"User-Agent": "Blender-Addon-Updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read().decode("utf-8", errors="replace")
    return json.loads(data)

def _download_file(url: str, dst_path: Path, timeout: float = 30.0):
    req = urllib.request.Request(url, headers={"User-Agent": "Blender-Addon-Updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dst_path, "wb") as f:
        f.write(resp.read())

def _safe_extract_zip(zip_path: Path, target_dir: Path):
    # Extract into a temporary folder first, then swap into place.
    tmp_dir = target_dir.parent / (target_dir.name + "_NEW")
    if tmp_dir.exists():
        try:
            import shutil
            shutil.rmtree(tmp_dir)
        except Exception:
            pass
    tmp_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(tmp_dir)

    # The zip should contain a single top-level folder named ADDON_MODULE_NAME
    # If it doesn't, we try to detect and move contents.
    expected = tmp_dir / ADDON_MODULE_NAME
    if expected.exists() and expected.is_dir():
        extracted_root = expected
    else:
        # Try to find the first folder with __init__.py
        extracted_root = None
        for child in tmp_dir.iterdir():
            if child.is_dir() and (child / "__init__.py").exists():
                extracted_root = child
                break
        if extracted_root is None:
            raise RuntimeError("ZIP does not contain a valid addon folder with __init__.py")

    # Swap: move current to backup, then move new into place
    import shutil
    backup_dir = target_dir.parent / (target_dir.name + "_OLD")
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
    if target_dir.exists():
        shutil.move(str(target_dir), str(backup_dir))
    shutil.move(str(extracted_root), str(target_dir))

    # Cleanup leftover temp container dir
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass

# -----------------------------------------------------------------------------
# Preferences (user sets manifest URL + optional auto-check)
# -----------------------------------------------------------------------------

class SWGI_AddonPrefs(AddonPreferences):
    bl_idname = ADDON_MODULE_NAME

    update_manifest_url: StringProperty(
        name="Update Manifest URL",
        description="URL to JSON manifest containing latest version and download_url",
        default="",
    )
    auto_check_on_startup: BoolProperty(
        name="Auto-check on Startup",
        description="Check for updates when Blender starts (once per session)",
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.label(text="Updater (optional)")
        col.prop(self, "update_manifest_url")
        col.prop(self, "auto_check_on_startup")
        row = col.row(align=True)
        row.operator("swgi.check_updates", icon="FILE_REFRESH")
        row.operator("swgi.install_update", icon="IMPORT")
        col.label(text="Tip: host a JSON manifest on GitHub raw or any HTTPS URL.")

# -----------------------------------------------------------------------------
# Operators
# -----------------------------------------------------------------------------

def _set_global_update_state(available: bool, version_text: str = "", download_url: str = "", notes: str = ""):
    wm = bpy.context.window_manager
    wm.swgi_update_available = available
    wm.swgi_update_version = version_text
    wm.swgi_update_url = download_url
    wm.swgi_update_notes = notes

class SWGI_OT_check_updates(Operator):
    bl_idname = "swgi.check_updates"
    bl_label = "Check Updates"
    bl_description = "Check the update manifest URL for a newer version"

    def execute(self, context):
        prefs = context.preferences.addons[ADDON_MODULE_NAME].preferences
        url = prefs.update_manifest_url.strip()
        if not url:
            self.report({'WARNING'}, "No Update Manifest URL set in Add-on Preferences.")
            _set_global_update_state(False)
            return {'CANCELLED'}

        try:
            manifest = _fetch_json(url)
            latest = tuple(manifest.get("version", (0, 0, 0)))
            dl = manifest.get("download_url", "")
            notes = manifest.get("notes", "")

            current = _current_version_tuple()

            if latest > current and dl:
                _set_global_update_state(True, ".".join(map(str, latest)), dl, notes)
                self.report({'INFO'}, f"Update available: {latest} (current {current}).")
            else:
                _set_global_update_state(False)
                self.report({'INFO'}, f"No update found (current {current}).")
        except Exception as e:
            _set_global_update_state(False)
            self.report({'ERROR'}, f"Update check failed: {e}")

        return {'FINISHED'}

class SWGI_OT_install_update(Operator):
    bl_idname = "swgi.install_update"
    bl_label = "Install Update"
    bl_description = "Download and install the available update ZIP (restart Blender after)"

    def execute(self, context):
        wm = context.window_manager
        if not getattr(wm, "swgi_update_available", False):
            self.report({'WARNING'}, "No update available. Click 'Check Updates' first.")
            return {'CANCELLED'}

        dl_url = getattr(wm, "swgi_update_url", "")
        if not dl_url:
            self.report({'ERROR'}, "Missing download URL from manifest.")
            return {'CANCELLED'}

        addons_dir = _addons_dir()
        target_dir = _addon_install_dir()
        if not addons_dir or not target_dir:
            self.report({'ERROR'}, "Could not resolve add-ons directory.")
            return {'CANCELLED'}

        try:
            with tempfile.TemporaryDirectory() as td:
                zip_path = Path(td) / "update.zip"
                _download_file(dl_url, zip_path)
                _safe_extract_zip(zip_path, target_dir)

            self.report({'INFO'}, "Update installed. Please restart Blender.")
        except Exception as e:
            self.report({'ERROR'}, f"Install failed: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}

# -----------------------------------------------------------------------------
# Auto-check hook (optional)
# -----------------------------------------------------------------------------

def _startup_check():
    try:
        prefs = bpy.context.preferences.addons[ADDON_MODULE_NAME].preferences
    except Exception:
        return None

    if not prefs.auto_check_on_startup:
        return None
    if getattr(prefs, "_session_checked", False):
        return None

    prefs._session_checked = True
    try:
        bpy.ops.swgi.check_updates('INVOKE_DEFAULT')
    except Exception:
        pass
    return None

def register_updater():
    bpy.utils.register_class(SWGI_AddonPrefs)
    bpy.utils.register_class(SWGI_OT_check_updates)
    bpy.utils.register_class(SWGI_OT_install_update)

    bpy.types.WindowManager.swgi_update_available = BoolProperty(default=False)
    bpy.types.WindowManager.swgi_update_version = StringProperty(default="")
    bpy.types.WindowManager.swgi_update_url = StringProperty(default="")
    bpy.types.WindowManager.swgi_update_notes = StringProperty(default="")

    # One-shot timer after startup
    bpy.app.timers.register(_startup_check, first_interval=2.0)

def unregister_updater():
    # Clean up properties
    try:
        del bpy.types.WindowManager.swgi_update_available
        del bpy.types.WindowManager.swgi_update_version
        del bpy.types.WindowManager.swgi_update_url
        del bpy.types.WindowManager.swgi_update_notes
    except Exception:
        pass

    bpy.utils.unregister_class(SWGI_OT_install_update)
    bpy.utils.unregister_class(SWGI_OT_check_updates)
    bpy.utils.unregister_class(SWGI_AddonPrefs)
