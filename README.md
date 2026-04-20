# CS1 → CS2 Asset Converter

A desktop tool that converts **Cities Skylines 1** building and prop assets into **Cities Skylines 2** ready format, including automatic mesh conversion (via Blender), PBR texture remapping, and CS2-compliant file naming.

> **Current status:** Buildings and props working. Vehicles, trees, and networks are partially supported — see [Limitations](#known-issues-and-limitations).

---

## What it does

CS1 and CS2 use completely different asset pipelines. This tool bridges the gap:

| Step | What happens |
|------|-------------|
| **Scan** | Reads a folder of ModTools-dumped CS1 files (.obj mesh + PNG textures) |
| **Rename** | Derives a clean CS2-compatible asset name from the CS1 filename |
| **Mesh** | Converts .obj → .fbx via Blender with the correct CS2 axis/scale settings |
| **Textures** | Remaps CS1 specular workflow → CS2 PBR workflow with correct CS2 naming |
| **Prefab** | Generates a `.Prefab` JSON reference file for the CS2 Asset Editor |

### Texture mapping (CS1 → CS2)

| CS1 file | CS2 file | Notes |
|----------|----------|-------|
| `_d.png` diffuse | `_BaseColor.png` | Mild desaturation applied |
| `_n.png` normal | `_Normal.png` | Direct copy (same OpenGL format) |
| `_s.png` specular | `_MaskMap.png` | R=metallic, G=coat, B=unused, A=gloss |
| *(generated)* | `_ControlMask.png` | Black RGB = no colour variation |
| `_i.png` illumination | *not converted* | CS1 illumination maps cause HDR bloom in CS2 — add emissive manually in the CS2 Asset Editor if needed |

---

## Requirements

| Software | Version | Purpose | Download |
|----------|---------|---------|----------|
| **Python** | 3.10 or newer | Runs the tool | [python.org](https://www.python.org/downloads/) |
| **Blender** | 3.6 or newer | Converts .obj mesh to .fbx | [blender.org](https://www.blender.org/download/) |
| **Cities Skylines 1** | any | Source game | Steam |
| **ModTools** (CS1 mod) | latest | Exports mesh + textures from CS1 | [Steam Workshop](https://steamcommunity.com/sharedfiles/filedetails/?id=2434651215) |
| **Cities Skylines 2** | any | Target game | Steam / Paradox |
| **Pillow** | 10.0+ | Python image processing | installed via pip |
| **numpy** | 1.24+ | Python array operations | installed via pip |

---

## Installation

### Step 1 — Install Python

Download Python 3.10 or newer from [python.org/downloads](https://www.python.org/downloads/).

> **Windows:** During installation, tick **"Add Python to PATH"** before clicking Install.

### Step 2 — Install Blender

Download Blender from [blender.org/download](https://www.blender.org/download/) and install normally. The tool finds it automatically — no configuration needed.

### Step 3 — Download this tool

Click the green **Code** button on this page and choose **Download ZIP**, then extract it to a folder on your computer (e.g. your Desktop).

### Step 4 — Install Python dependencies

Open a terminal in the tool folder:

- **Windows:** Click the address bar in File Explorer, type `cmd`, press Enter
- **macOS:** Right-click the folder → New Terminal at Folder
- **Linux:** Right-click → Open Terminal Here. Also run: `sudo apt install python3-tk`

Then run:

```
pip install -r requirements.txt
```

### Step 5 — Launch the tool

```
python gui.py
```

---

## Usage

### Part 1 — Export your asset from CS1

You need to export the mesh and textures from CS1 using the **ModTools** mod before you can convert anything.

1. Subscribe to **ModTools** on the CS1 Steam Workshop and launch CS1
2. Load a city that contains the building or prop you want to convert
3. Press **Ctrl+E** to open Scene Explorer
4. Search for your asset by name (e.g. `HibbingHighSchool`)
5. Inside the asset, find **`m_mesh`** → click the arrow → **Dump mesh to OBJ**
6. Find **`m_material`** → dump each texture (diffuse, normal, specular, illumination)
7. All files save to:
   ```
   C:\Users\<you>\AppData\Local\Colossal Order\Cities_Skylines\Addons\Import\
   ```

### Part 2 — Convert with this tool

1. Launch the tool (`python gui.py`)
2. Check the **Blender Path** field shows a green ✓ — if not, click Browse and find `blender.exe`
3. Click **+ Add folder** and select the folder containing your dumped files
4. Set your **Output Directory**
5. Click **▶ Convert Assets**

A successful conversion produces:

```
cs2_output/
└── Import/
    ├── HibbingHighSchool.fbx
    ├── HibbingHighSchool_LOD1.fbx
    ├── HibbingHighSchool_BaseColor.png
    ├── HibbingHighSchool_ControlMask.png
    ├── HibbingHighSchool_MaskMap.png
    ├── HibbingHighSchool_Normal.png
    ├── HibbingHighSchool.Prefab
    └── conversion_report.json
```

### Part 3 — Import into CS2

1. Open **Cities Skylines 2**
2. From the main menu go to **Editor → Asset Editor**
3. Click the **Asset Importer** button (right panel)
4. Set **Project Root** = your `cs2_output` folder
5. Set **Assets Folder** = the subfolder containing your converted files (e.g. `Import`)
6. Choose a **Prefab Preset** (Building, Static Object, etc.)
7. Click **Import**
8. Place the asset in the scene, adjust settings, then **Save** and **Submit**

> **If you see a glowing white building:** Delete the CS2 import cache at
> `C:\Users\<you>\AppData\LocalLow\Colossal Order\Cities Skylines II\ImportedData\`
> then restart CS2 and reimport. Stale cache files are the most common cause.

---

## Known issues and limitations

| Issue | Cause | Workaround |
|-------|-------|-----------|
| Building glows white after placing | Stale CS2 import cache | Delete `ImportedData` folder, restart CS2 |
| Textures not loading | Files named incorrectly | Check all output files share the same stem name |
| Wrong scale | Blender not found | Ensure Blender is installed, check Blender Path field |
| Night glow / illumination missing | CS1 `_i` maps cause HDR bloom in CS2 | Add emissive manually in CS2 Asset Editor |
| Packaging fails on Paradox Mods | Paradox account / server issue | Ensure you are logged in to your Paradox account in CS2 |
| Animated assets (vehicles, citizens) | Animation clips not converted | Manual work required in Blender |
| Network assets (roads, tracks) | Complex lane data has no CS2 equivalent | Not currently supported |

---

## How it works (technical)

**Asset name derivation**
ModTools exports files with a Steam Workshop ID prefix e.g. `3705851170.Hibbing High School.obj`. The tool strips the ID, converts spaces to PascalCase, and uses the result as the stem for all output files so CS2 can automatically match textures to the mesh.

**Texture pipeline**
CS1 uses a specular/gloss workflow. CS2 uses metalness/PBR. The converter remaps:
- Specular R (intensity) → MaskMap R (metallic), clamped to max 0.3
- Specular G (gloss) → MaskMap A (glossiness), clamped to max 0.6
- ControlMask is generated as black RGB / white alpha (no colour variation, normal snow)

**Mesh conversion**
Blender is invoked headlessly using `bpy.ops.export_scene.fbx()` with `axis_forward="-Z"`, `axis_up="Y"`, and `apply_unit_scale=True` — the settings required for correct scale and orientation in CS2's Unity-based engine.

---

## Repository structure

```
cs1_to_cs2_converter/
├── converter.py        Core conversion logic
├── gui.py              Desktop GUI (tkinter)
├── requirements.txt    Python dependencies
└── README.md           This file
```

---

## Contributing

Pull requests welcome. Areas that would benefit most:

- [ ] Vehicle and citizen animated mesh support
- [ ] Automatic LOD mesh decimation via Blender Python
- [ ] Better emissive handling (selective window glow)
- [ ] Network / road asset support
- [ ] Batch conversion progress bar

---

## License

MIT License — free to use, modify, and distribute. Credit appreciated but not required.

---

## Credits

Built with:
- [Blender](https://www.blender.org/) — mesh conversion
- [Pillow](https://python-pillow.org/) — image processing
- [ModTools](https://steamcommunity.com/sharedfiles/filedetails/?id=2434651215) by Gameranx — CS1 asset extraction

Reference documentation:
- [CS2 Asset Creation Guide](https://cs2.paradoxwikis.com/Asset_Creation_Guide)
- [CS2 Texture Pipeline](https://cslmodding.info/cs2/textures/)
