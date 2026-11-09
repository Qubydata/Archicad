# Model Import spike: does the generated GDL become a working Archicad object?

Everything in Model Import is tested without Archicad, except the one step that
needs it: Archicad's own `LP_XMLConverter` turning our XML into a `.gsm` library
part, and that part behaving in Archicad. This is a 20-minute manual check on a
Windows PC with Archicad 27, 28 or 29 (macOS works the same way, paths below).

You need this repository with `uv` (see "For developers" in the README). Steps 4
and 5 also need the Tapir add-on (installed with Tevavid Tools).

## 1. Write the test object's XML (no Archicad needed)

In a Command Prompt, in the repository folder:

```
uv run python -m archicad_mcp.modelimport --demo --xml-only "%USERPROFILE%\Desktop\spike"
```

This writes `Desktop\spike\Demo Table.xml` and `Desktop\spike\images\Demo Table.png`
(the preview picture). The demo is a 1.2 x 0.8 m table: oak top, four steel legs,
two tiny screws and a smooth oak vase, about 5,500 polygons.

## 2. Convert it with LP_XMLConverter (the tiny command)

Change `28` to your Archicad version. Copy the whole line:

```
"C:\Program Files\Graphisoft\Archicad 28\LP_XMLConverter.exe" xml2libpart -img "%USERPROFILE%\Desktop\spike\images" "%USERPROFILE%\Desktop\spike\Demo Table.xml" "%USERPROFILE%\Documents\Tevavid Library\Spike\Demo Table.gsm"
echo %ERRORLEVEL%
```

On macOS (Terminal, change both `28`s):

```
"/Applications/GRAPHISOFT/Archicad 28/Archicad 28.app/Contents/MacOS/LP_XMLConverter.app/Contents/MacOS/LP_XMLConverter" xml2libpart -img ~/Desktop/spike/images ~/Desktop/spike/"Demo Table.xml" ~/Documents/"Tevavid Library"/Spike/"Demo Table.gsm"
echo $?
```

Create the `Documents\Tevavid Library\Spike` folder first if the converter
complains that it is missing.

Expected: exit code `0` and a `Demo Table.gsm` of some tens of KB.
If it fails, copy everything it printed.

## 3. Look at the object in Archicad

1. Open any project. File > Libraries and Objects > Library Manager > Add >
   pick `Documents\Tevavid Library` > Reload & Apply.
2. Object tool > Settings: find **Demo Table** (search for "Demo"). Place it.
3. Tick what works:

- [ ] The settings dialog shows the preview picture (the table seen from the front left).
- [ ] Parameters: Width 1.2, Depth 0.8, Height 0.99, Keep Proportions on.
- [ ] 3D window: table and vase in brown and grey. No triangle lines across the flat
      table top. The vase looks smooth (shaded, no wireframe of its triangles).
- [ ] Floor plan: the outline of the top, plus the legs and the vase; hotspots at the
      corners, edge midpoints and the centre.
- [ ] Change Width to 2.4: Depth and Height follow (2x). Turn Keep Proportions off and
      change Width again: only the width changes. Stretch it in plan with the pet palette.
- [ ] Surfaces: "Oak" and "Steel" are listed. Pick a surface for Oak: the table top
      changes and "Use the Model's Own Colours" switches off. Switch it on again: brown again.
- [ ] "Plan Symbol from the 3D Model" on: the plan shows the projected table.
- [ ] A section through the table: the top and legs are cut with a fill.
- [ ] File > Libraries and Objects > Open Object > Demo Table > 3D script > Check Script:
      no errors. Same for the 2D and Parameter scripts.

## 4. The whole path with Archicad running

With a project open in Archicad and Tapir installed:

```
uv run python -m archicad_mcp.modelimport --demo --name "Spike Table" --folder Spike --archicad --place
```

Expected: `"mode": "library"`, `Documents\Tevavid Library` added to the project's
libraries (Library Manager), and "Spike Table" placed at the project origin.
Then the project's embedded library:

```
uv run python -m archicad_mcp.modelimport --demo --name "Spike Embedded" --folder Spike --target embedded --archicad --place
```

Expected: Library Manager > Embedded Library > Spike > Spike Embedded, placed at the origin.

And a real download (glTF/GLB, OBJ, a ZIP from D5 Works; FBX needs the Python 3.12
build, check with `uv run python --version`):

```
uv run python -m archicad_mcp.modelimport "%USERPROFILE%\Downloads\<the model>.zip" --remove-small 0.01 --max-polygons 20000 --folder Spike --archicad --place
```

Check that it stands on the floor, faces the front (-Y, the bottom of the plan),
has the right size and colours.

## 5. The fallback without LP_XMLConverter

```
uv run python -m archicad_mcp.modelimport --demo --name "Spike Morphs" --archicad --morphs
```

Expected: `"mode": "morphs"`, nine Morph elements at the origin with surfaces
"Spike Morphs - Oak" and "Spike Morphs - Steel". On Archicad 29 their triangle edges
are hidden; on 28 and older they show (a known Archicad API limit).

## What to send back

- The ticked list, with screenshots of anything that looks wrong.
- Anything LP_XMLConverter printed in step 2.
- If the conversion fails, also this, which shows the XML format your converter writes
  (the `Version` in its first lines is what `FORMAT_VERSION` in `gdl.py` may need):

  ```
  "C:\Program Files\Graphisoft\Archicad 28\LP_XMLConverter.exe" libpart2xml "%USERPROFILE%\Documents\Tevavid Library\Spike\Demo Table.gsm" "%USERPROFILE%\Desktop\spike\roundtrip.xml"
  ```

  (or any `.gsm` from the Archicad Library instead of Demo Table, if ours did not convert).

## What the generated part relies on (where to look if something is off)

| Item | In `gdl.py` | Risk |
| --- | --- | --- |
| XML format version 38, Model Element subtype | `FORMAT_VERSION`, `MODEL_ELEMENT` | Low: newer converters read older versions |
| Preview: `<Picture ... path="Demo Table.png">` with `-img` | `library_part_xml` | Medium: forum reports of `-img` pictures missing |
| Materials: `DEFINE MATERIAL` type 0, a Material parameter per material (default index 1) | `material_definitions` | Low |
| Hidden and smooth edges: `EDGE` status 1 and 2, `PGON` status 2, `BODY -1`/`BODY 4` | `body_lines` | Low |
| Keep Proportions via `GLOB_MODPAR_NAME` in the parameter script | `script_parameters` | Low |
| `STBit_FixSize` with scripted hotspots | `library_part_xml` | Low: stretching might need a tweak |
