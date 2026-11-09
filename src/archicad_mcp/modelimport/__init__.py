"""Model Import: bring downloaded 3D models (glTF, OBJ, FBX, ...) into Archicad as library objects.

The engine reads a file into parts (one mesh per named node and material),
lets the user clean it up (delete parts, drop tiny parts, pick units and the
up axis, reduce the polygon count) and saves the result as a GDL library part
(.gsm) through Archicad's own LP_XMLConverter, or as Morph elements when the
converter cannot be found.

Modules:
    reader    files to a :class:`~.reader.Model` of parts and materials
    cleanup   :class:`~.cleanup.ImportSession`: the edits, undo, units, up axis, placement
    simplify  polygon reduction levels and the polygon budget bar
    preview   a small PNG picture of the model
    gdl       the library part XML (scripts, parameters, preview)
    library   library folders and LP_XMLConverter
    downloads new 3D files in the Downloads folder
    service   :class:`~.service.ModelImport`: everything above, plus Archicad
"""

from .errors import ModelImportError

__all__ = ["ModelImportError"]
