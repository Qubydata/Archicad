# Third-party notices

## Tapir command reference

The Tevavid Archicad add-on (`addon/`) is built on the Archicad add-on of the Tapir Archicad
Automation project, <https://github.com/ENZYME-APD/tapir-archicad-automation>, and
`src/archicad_mcp/data/tapir_catalog.json` is generated from its command reference. For Archicad
versions without a Tevavid add-on build, the setup downloads the Tapir add-on from that project's
releases. Its license:

MIT License

Copyright (c) 2024 Enzyme APD

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Lucide icons

The panel and the setup use icons from Lucide, <https://lucide.dev>.

ISC License

Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2022 as part of Feather (MIT).
All other copyright (c) for Lucide are held by Lucide Contributors 2022.

Permission to use, copy, modify, and/or distribute this software for any purpose with or
without fee is hereby granted, provided that the above copyright notice and this permission
notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS
SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE
AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT,
NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.

## Site from Map data and libraries

- Elevation: Terrain Tiles on AWS by Mapzen/Tilezen, combining open data including SRTM (NASA),
  3DEP (USGS), ETOPO1 (NOAA) and other national sources. See
  <https://github.com/tilezen/joerd/blob/master/docs/attribution.md>.
- Buildings: (c) OpenStreetMap contributors, available under the Open Database License (ODbL),
  <https://www.openstreetmap.org/copyright>.
- ezdxf (MIT license), to read DXF survey drawings.
- Pillow (HPND license, MIT-CMU), to read the elevation tiles and draw the icons.

## Model Import libraries

- trimesh (MIT license), to read glTF/GLB, OBJ, STL, PLY, OFF and Collada models.
- pycollada (BSD license), for Collada (DAE) through trimesh.
- assimp-py (MIT license), with the Open Asset Import Library (BSD license), to read FBX, 3DS and 3MF.
- fast-simplification (MIT license), to reduce the polygon count.
