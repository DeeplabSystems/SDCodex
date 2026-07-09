from .sdcodex_gallery_node import SDCodexGalleryNode

NODE_CLASS_MAPPINGS = {
    "SDCodexGallery": SDCodexGalleryNode
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SDCodexGallery": "SDCodex Gallery Selector"
}

WEB_DIRECTORY = "./js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
