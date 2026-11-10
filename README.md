# Silent Hunter DAT Importer / Exporter

This Blender add-on imports Silent Hunter `.dat` scenes and exports them as `.blend` or Wavefront `.obj` + `.mtl`.

## Import and export

- **File → Import → Silent Hunter DAT (.dat)** opens a DAT file picker.
- The 3D View **N** sidebar has the **Silent Hunter** tab, with import and export controls.
- **Flip V axis** converts the DAT UV convention to Blender's convention. It is enabled by default; turn it off if a DAT already uses Blender-style UVs.
- The importer reads per-corner UV indices and all stored texture channels, along with model geometry, names, node transforms, materials, and texture maps.
- Mesh object names and mesh datablock names follow the DAT labels. Material names follow the DAT texture filenames.
- Available textures are resolved from the sibling `Data/Textures` directory and packed into `.blend` exports. OBJ exports include an `.mtl` and copy available textures to the output folder.

Texture files absent from the game/mod data folder cannot be included.

## Origin and license

The DAT chunk structure and parser references are based on [skwasjer/SilentHunter](https://github.com/skwasjer/SilentHunter.git). That upstream repository is licensed under Apache-2.0; see its [license](https://github.com/skwasjer/SilentHunter/blob/master/LICENSE.md).

This add-on is distributed under **GNU GPL version 3 only**. See the included `LICENSE` file.
