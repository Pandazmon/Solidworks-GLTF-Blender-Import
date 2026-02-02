bl_info = {
    "name": "Solidworks GLTF Importer",
    "author": "ChatGPT",
    "version": (2, 4, 0),
    "blender": (5, 0, 0),
    "location": "View3D > N Panel > Solidworks Importer",
    "description": "Robust SolidWorks GLTF/GLB import, linking, organisation, and material pipeline (with optional updater).",
    "category": "Import-Export",
}

from . import swgi_main
from . import updater

def register():
    updater.register_updater()
    swgi_main.register()

def unregister():
    swgi_main.unregister()
    updater.unregister_updater()
