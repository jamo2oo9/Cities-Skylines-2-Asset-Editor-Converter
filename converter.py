"""
Cities Skylines 1 -> Cities Skylines 2 Asset Converter  v3.0
=============================================================
Converts ModTools-dumped CS1 assets (.obj mesh + PNG textures)
into CS2-ready format following the official asset pipeline.

CS2 output files:
    <Name>.fbx                 mesh (via Blender)
    <Name>_LOD1.fbx            LOD mesh
    <Name>_BaseColor.png       diffuse colour
    <Name>_ControlMask.png     colour variation mask (black = none)
    <Name>_MaskMap.png         PBR mask (metallic/coat/gloss)
    <Name>_Normal.png          normal map

Reference:
    https://cs2.paradoxwikis.com/Asset_Creation_Guide
    https://cslmodding.info/cs2/textures/

Dependencies:
    pip install Pillow numpy
"""

import os
import re
import sys
import json
import shutil
import logging
import argparse
import glob as glob_mod
import subprocess
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

try:
    from PIL import Image
    import numpy as np
    PILLOW_OK = True
except ImportError:
    PILLOW_OK = False

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("cs_converter")


# ── Data models ───────────────────────────────────────────────────────────────

class AssetType(str, Enum):
    BUILDING = "building"
    PROP     = "prop"
    VEHICLE  = "vehicle"
    TREE     = "tree"
    UNKNOWN  = "unknown"


@dataclass
class CS1Textures:
    diffuse:      Optional[Path] = None
    normal:       Optional[Path] = None
    specular:     Optional[Path] = None
    alpha:        Optional[Path] = None
    illumination: Optional[Path] = None
    # Combined maps from ModTools "Dump All"
    aci:          Optional[Path] = None   # RGB = Alpha, Colour, Illumination
    xys:          Optional[Path] = None   # RGB = Normal X, Normal Y, Specular


@dataclass
class CS2Textures:
    base_color:   Optional[Path] = None
    control_mask: Optional[Path] = None
    mask_map:     Optional[Path] = None
    normal:       Optional[Path] = None
    # Emissive intentionally omitted: CS1 _i maps cause HDR bloom in CS2.
    # Add manually in CS2 Asset Editor if needed.


@dataclass
class ConversionResult:
    success:      bool      = False
    asset_name:   str       = ""
    asset_type:   AssetType = AssetType.UNKNOWN
    output_dir:   Optional[Path] = None
    mesh_fbx:     Optional[Path] = None
    lod_fbx:      Optional[Path] = None
    cs1_textures: CS1Textures = field(default_factory=CS1Textures)
    cs2_textures: CS2Textures = field(default_factory=CS2Textures)
    prefab_file:  Optional[Path] = None
    warnings:     list = field(default_factory=list)
    errors:       list = field(default_factory=list)


# ── Asset folder scanner ──────────────────────────────────────────────────────

class AssetFolder:
    """
    Scans a ModTools-dumped folder, identifies meshes and textures,
    and derives a clean CS2-compatible asset name.

    Handles ModTools naming conventions:
        <SteamID>.<AssetName>.obj
        <SteamID>.<AssetName>_d.png   (diffuse)
        <SteamID>.<AssetName>_n.png   (normal)
        <SteamID>.<AssetName>_s.png   (specular)
        <SteamID>.<AssetName>_a.png   (alpha)
        <SteamID>.<AssetName>_i.png   (illumination)
        <SteamID>.<AssetName>_lod.obj (LOD mesh)
        <SteamID>.<AssetName>_lod_*.png (LOD textures — ignored)
    """

    # Texture suffix → slot mapping (checked against lowercased stem)
    DIFFUSE_KEYS  = ("_d", "_diffuse",  "_maintex",  "_albedo",  "_color",  "_basecolor")
    NORMAL_KEYS   = ("_n", "_normal",   "_bumpmap",  "_nrm",     "_normalmap")
    SPECULAR_KEYS = ("_s", "_specular", "_spec",     "_specmap", "_smoothness")
    ALPHA_KEYS    = ("_a", "_alpha",    "_opacity")
    ILLUM_KEYS    = ("_i", "_illumination", "_illum", "_illummap")
    # Combined texture map names produced by ModTools "Dump All"
    # ACI = Alpha (R), Color/Diffuse (G), Illumination (B) — gamma lifted
    # XYS = Normal X (R), Normal Y (G), Specular (B) — gamma lifted
    COMBINED_ACI  = ("_aci",)
    COMBINED_XYS  = ("_xys",)

    MESH_EXTS    = {".obj", ".fbx"}
    TEXTURE_EXTS = {".png", ".jpg", ".jpeg", ".tga", ".dds", ".bmp"}

    # Keywords for asset type detection from folder/mesh name
    BUILDING_KEYS = ("school","house","shop","office","station","building","hospital",
                     "hotel","hall","church","high","factory","warehouse","park",
                     "fire","police","library","museum","airport","stadium")
    VEHICLE_KEYS  = ("car","bus","train","tram","plane","truck","vehicle","van","ship","boat")
    TREE_KEYS     = ("tree","pine","oak","bush","plant","hedge","forest","shrub")
    PROP_KEYS     = ("prop","bench","sign","lamp","fence","barrier","pillar","bollard")

    def __init__(self, folder: Path):
        self.folder = folder
        self.name   = folder.name

    @staticmethod
    def clean_name(raw: str) -> str:
        """
        Derive a clean CS2-compatible asset name from a raw file stem.
        Examples:
            "3705851170.Hibbing High School"   -> "HibbingHighSchool"
            "255710.My_Cool_Building_lod"      -> "MyCoolBuilding"
            "PropFence01"                      -> "PropFence01"
        """
        # Strip Steam ID prefix (digits + dot)
        name = re.sub(r"^\d+\.", "", raw).strip()
        # Strip LOD suffix
        name = re.sub(r"_lod\d*$", "", name, flags=re.IGNORECASE).strip()
        # Strip CS1 texture suffixes
        for sfx in ("_d","_n","_s","_a","_i","_c","_diffuse","_normal",
                    "_specular","_alpha","_illumination","_color"):
            if name.lower().endswith(sfx):
                name = name[:-len(sfx)]
                break
        # PascalCase: split on spaces, hyphens, underscores
        words = re.split(r"[\s\-_]+", name)
        result = []
        for w in words:
            if not w:
                continue
            # Preserve existing internal capitalisation; just ensure first char is upper
            result.append(w[0].upper() + w[1:])
        name = "".join(result)
        # Strip non-alphanumeric
        name = re.sub(r"[^a-zA-Z0-9]", "", name)
        return name or "ConvertedAsset"

    @staticmethod
    def _is_lod(stem: str) -> bool:
        sl = stem.lower()
        return sl.endswith("_lod") or "_lod_" in sl or sl.endswith("_lod1")

    def scan(self) -> ConversionResult:
        files = sorted(
            [f for f in self.folder.iterdir() if f.is_file()],
            key=lambda f: f.name
        )

        # ── Meshes ──
        main_mesh = lod_mesh = None
        for f in files:
            if f.suffix.lower() not in self.MESH_EXTS:
                continue
            if self._is_lod(f.stem):
                lod_mesh  = f
            elif main_mesh is None:
                main_mesh = f

        # ── Asset name from mesh filename ──
        source = main_mesh.stem if main_mesh else self.name
        asset_name = self.clean_name(source)
        log.info(f"  Asset name: '{asset_name}'  (from: '{source}')")

        result            = ConversionResult(asset_name=asset_name)
        result.mesh_fbx   = main_mesh
        result.lod_fbx    = lod_mesh

        # ── Textures (LOD variants skipped) ──
        ts        = CS1Textures()
        unmatched = []
        for f in files:
            if f.suffix.lower() not in self.TEXTURE_EXTS:
                continue
            if self._is_lod(f.stem):
                continue  # skip LOD textures
            sl = f.stem.lower()
            if   any(sl.endswith(k) for k in self.COMBINED_ACI):  ts.aci          = f
            elif any(sl.endswith(k) for k in self.COMBINED_XYS):  ts.xys          = f
            elif any(sl.endswith(k) for k in self.DIFFUSE_KEYS):  ts.diffuse      = f
            elif any(sl.endswith(k) for k in self.NORMAL_KEYS):   ts.normal       = f
            elif any(sl.endswith(k) for k in self.SPECULAR_KEYS): ts.specular     = f
            elif any(sl.endswith(k) for k in self.ALPHA_KEYS):    ts.alpha        = f
            elif any(sl.endswith(k) for k in self.ILLUM_KEYS):    ts.illumination = f
            else:
                unmatched.append(f)
        # Positional fallback for unrecognised texture names
        for i, f in enumerate(unmatched):
            if   i == 0 and not ts.diffuse:  ts.diffuse  = f
            elif i == 1 and not ts.normal:   ts.normal   = f
            elif i == 2 and not ts.specular: ts.specular = f
        # Discard uniform placeholder textures (all-white or all-black).
        # ModTools "Dump All" produces these when a slot has no real data.
        if PILLOW_OK:
            for attr in ("alpha", "illumination", "specular"):
                path = getattr(ts, attr)
                if path:
                    try:
                        arr = np.array(Image.open(str(path)).convert("L"),
                                       dtype=np.float32) / 255.0
                        mean_val = float(arr.mean())
                        if mean_val > 0.98 or mean_val < 0.02:
                            log.info(f"  Discarding {path.name} "
                                     f"(uniform {mean_val:.2f} — placeholder)")
                            setattr(ts, attr, None)
                    except Exception:
                        pass

        result.cs1_textures = ts

        # ── Log scan results ──
        log.info(f"  Files in folder: {len(files)}")
        log.info(f"    mesh:    {main_mesh.name if main_mesh else 'NONE'}")
        log.info(f"    lod:     {lod_mesh.name  if lod_mesh  else 'NONE'}")
        log.info(f"    diffuse: {ts.diffuse.name      if ts.diffuse      else 'NONE'}")
        log.info(f"    normal:  {ts.normal.name        if ts.normal       else 'NONE'}")
        log.info(f"    specular:{ts.specular.name      if ts.specular     else 'NONE'}")
        log.info(f"    alpha:   {ts.alpha.name         if ts.alpha        else 'NONE'}")
        log.info(f"    illumin: {ts.illumination.name  if ts.illumination else 'NONE'}")
        log.info(f"    aci:     {ts.aci.name           if ts.aci          else 'NONE'}")
        log.info(f"    xys:     {ts.xys.name           if ts.xys          else 'NONE'}")
        if ts.aci:
            log.info("  NOTE: ACI combined texture detected — will split R=alpha G=diffuse B=illumination")
        if ts.xys:
            log.info("  NOTE: XYS combined texture detected — will split R=normalX G=normalY B=specular")

        # ── Asset type ──
        nl = (asset_name + " " + self.name).lower()
        if   any(k in nl for k in self.BUILDING_KEYS): result.asset_type = AssetType.BUILDING
        elif any(k in nl for k in self.VEHICLE_KEYS):  result.asset_type = AssetType.VEHICLE
        elif any(k in nl for k in self.TREE_KEYS):     result.asset_type = AssetType.TREE
        elif any(k in nl for k in self.PROP_KEYS):     result.asset_type = AssetType.PROP
        else:                                          result.asset_type = AssetType.BUILDING

        # ── Validate ──
        if not main_mesh and not ts.diffuse:
            result.errors.append(
                "No mesh (.obj/.fbx) or diffuse texture found. "
                "Make sure you exported this asset from CS1 using ModTools."
            )
        else:
            result.success = True
            if not main_mesh:   result.warnings.append("No mesh found — textures only.")
            if not ts.diffuse:  result.warnings.append("No diffuse texture found.")

        return result


# ── OBJ → FBX via Blender ─────────────────────────────────────────────────────

class OBJtoFBX:
    """
    Converts .obj to .fbx using Blender in headless mode.
    Falls back to generating a ready-to-run Blender script if not found.

    Blender export settings for CS2:
        axis_forward="-Z", axis_up="Y"   (Unity/CS2 left-handed Y-up)
        apply_unit_scale=True             (bakes metres into vertex data)
        bake_space_transform=True         (applies axis conversion)
        global_scale=1.0
    """

    BLENDER_SCRIPT = "\n".join([
        "import bpy, sys",
        "obj_path = sys.argv[sys.argv.index('--') + 1]",
        "fbx_path = sys.argv[sys.argv.index('--') + 2]",
        "bpy.ops.wm.read_factory_settings(use_empty=True)",
        "bpy.ops.wm.obj_import(filepath=obj_path)",
        "bpy.ops.object.select_all(action='SELECT')",
        "if len(bpy.context.selected_objects) > 1:",
        "    bpy.context.view_layer.objects.active = bpy.context.selected_objects[0]",
        "    bpy.ops.object.join()",
        "bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)",
        "bpy.ops.export_scene.fbx(",
        "    filepath=fbx_path,",
        "    use_selection=True,",
        "    global_scale=1.0,",
        "    apply_unit_scale=True,",
        "    apply_scale_options='FBX_SCALE_NONE',",
        "    bake_space_transform=True,",
        "    axis_forward='-Z',",
        "    axis_up='Y',",
        "    mesh_smooth_type='FACE',",
        "    use_mesh_modifiers=True,",
        "    add_leaf_bones=False,",
        "    path_mode='COPY',",
        "    embed_textures=False,",
        ")",
        "print('FBX exported: ' + fbx_path)",
    ])

    BLENDER_SEARCH_PATHS = [
        # Windows — glob for any version
        r"C:\Program Files\Blender Foundation\Blender *\blender.exe",
        r"C:\Program Files (x86)\Blender Foundation\Blender *\blender.exe",
        r"C:\Users\*\AppData\Local\Programs\Blender Foundation\Blender *\blender.exe",
        # macOS
        "/Applications/Blender.app/Contents/MacOS/Blender",
        # Linux
        "/usr/bin/blender",
        "/usr/local/bin/blender",
        "/snap/bin/blender",
    ]

    def __init__(self, obj_path: Path):
        self.obj_path = obj_path

    def convert(self, out_path: Path) -> bool:
        blender = self._find_blender()
        if blender:
            return self._run(blender, out_path)
        self._write_script(out_path)
        return False

    def _find_blender(self) -> Optional[str]:
        # 1. PATH
        import shutil as sh
        found = sh.which("blender")
        if found:
            log.info(f"  Blender on PATH: {found}")
            return found
        # 2. Env var override
        env = os.environ.get("BLENDER_PATH")
        if env and Path(env).exists():
            log.info(f"  Blender via BLENDER_PATH: {env}")
            return env
        # 3. Glob search
        for pattern in self.BLENDER_SEARCH_PATHS:
            matches = sorted(glob_mod.glob(pattern))
            if matches:
                found = matches[-1]  # latest version
                log.info(f"  Blender found: {found}")
                return found
        # 4. Windows registry
        try:
            import winreg
            for hive in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
                for subkey in [r"SOFTWARE\BlenderFoundation",
                               r"SOFTWARE\WOW6432Node\BlenderFoundation"]:
                    try:
                        key = winreg.OpenKey(hive, subkey)
                        d, _ = winreg.QueryValueEx(key, "Install_Dir")
                        candidate = Path(d) / "blender.exe"
                        if candidate.exists():
                            log.info(f"  Blender via registry: {candidate}")
                            return str(candidate)
                    except Exception:
                        pass
        except ImportError:
            pass
        log.warning("  Blender not found. Set BLENDER_PATH env var or use Browse button.")
        return None

    def _run(self, exe: str, out_path: Path) -> bool:
        script = Path(tempfile.mktemp(suffix=".py"))
        try:
            script.write_text(self.BLENDER_SCRIPT)
            cmd = [exe, "--background", "--python", str(script),
                   "--", str(self.obj_path.resolve()), str(out_path.resolve())]
            log.info("  Running Blender headless...")
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if r.returncode == 0 and out_path.exists():
                log.info(f"  FBX: {out_path.name}")
                return True
            log.warning(f"  Blender exit code {r.returncode}")
            if r.stderr:
                log.warning(f"  {r.stderr[-400:]}")
            return False
        except subprocess.TimeoutExpired:
            log.warning("  Blender timed out (180s)")
            return False
        except Exception as e:
            log.warning(f"  Blender error: {e}")
            return False
        finally:
            try: script.unlink()
            except Exception: pass

    def _write_script(self, out_path: Path):
        """Write embedded-path script + instructions for manual conversion."""
        script = self.BLENDER_SCRIPT \
            .replace("sys.argv[sys.argv.index('--') + 1]",
                     repr(str(self.obj_path.resolve()))) \
            .replace("sys.argv[sys.argv.index('--') + 2]",
                     repr(str(out_path.resolve())))

        sp = out_path.parent / "convert_to_fbx.py"
        ip = out_path.parent / "BLENDER_STEP_NEEDED.txt"
        sp.write_text(script)
        ip.write_text(
            "BLENDER CONVERSION NEEDED\n"
            "=========================\n\n"
            "Blender (free) is required to produce the .fbx mesh.\n\n"
            "OPTION A — Re-run the converter after installing Blender:\n"
            "  1. Install Blender from https://www.blender.org/download/\n"
            "  2. Re-run this converter — it finds Blender automatically\n\n"
            "OPTION B — Run the script manually in Blender:\n"
            "  1. Open Blender\n"
            "  2. Go to the Scripting tab\n"
            f"  3. Open: {sp}\n"
            "  4. Click Run Script\n\n"
            "OPTION C — Command line:\n"
            f'  blender --background --python "{sp}"\n'
        )
        log.info(f"  Script: {sp.name}")
        log.warning("  Blender not found — see BLENDER_STEP_NEEDED.txt")


# ── Texture converter ─────────────────────────────────────────────────────────

class TextureConverter:
    """
    Converts CS1 textures to CS2 PBR format with correct naming.

    CS1 → CS2 mapping:
        _d  diffuse    → _BaseColor   RGB=colour, A=opacity
        _n  normal     → _Normal      direct copy (same OpenGL tangent-space)
        _s  specular   → _MaskMap     R=metallic, G=coat(0), B=unused(0), A=gloss
              (generated) → _ControlMask  black RGB = no colour variation, white A = snow

    NOTE: _Emissive is intentionally not generated.
    CS1 _i illumination maps have very high average brightness which causes
    HDR bloom/glow in CS2's renderer. Add emissive manually in the CS2
    Asset Editor if needed for windows/lights.
    """

    def __init__(self, asset_name: str, cs1: CS1Textures, out_dir: Path):
        if not PILLOW_OK:
            raise ImportError("Run:  pip install Pillow numpy")
        self.name = asset_name
        self.cs1  = cs1
        self.out  = out_dir
        self.out.mkdir(parents=True, exist_ok=True)

    def convert(self) -> CS2Textures:
        # Split combined textures first so individual slots are populated
        self._split_combined()

        cs2 = CS2Textures()
        if self.cs1.diffuse:
            cs2.base_color   = self._base_color()
        cs2.control_mask     = self._control_mask()
        cs2.mask_map         = self._mask_map()
        if self.cs1.normal:
            cs2.normal       = self._normal()
        return cs2

    def _split_combined(self):
        """
        Split ModTools "Dump All" combined textures into individual maps.

        ACI texture (Alpha/Colour/Illumination combined):
            R channel = Alpha mask
            G channel = Colour/Diffuse
            B channel = Illumination
            These are gamma lifted (+0.45 correction needed before use)

        XYS texture (Normal/Specular combined):
            R channel = Normal X
            G channel = Normal Y
            B channel = Specular
            These are also gamma lifted

        Only splits if the individual maps aren't already present.
        """
        if self.cs1.aci and not self.cs1.diffuse:
            log.info("  Splitting ACI combined texture...")
            img = self._resize(self._open(self.cs1.aci, "RGB"))
            arr = np.array(img, dtype=np.float32) / 255.0

            # Apply gamma correction (ACI textures are gamma lifted at 0.45)
            arr = np.power(arr, 0.45)

            # G channel = Colour/Diffuse
            diffuse_rgb = np.stack([arr[:,:,1], arr[:,:,1], arr[:,:,1]], axis=2)
            # Actually use full RGB from G channel reconstruction isn't right —
            # ACI G channel is a greyscale colour mask, not full RGB.
            # The actual colour comes from the G channel applied as multiply.
            # For CS2 we just use G as a grey diffuse since we have no full colour.
            diffuse_arr = np.clip(arr[:,:,1:2] * np.ones((1,1,3)), 0, 1)
            diffuse_full = np.concatenate([
                (diffuse_arr * 255).astype(np.uint8),
                np.full((*diffuse_arr.shape[:2], 1), 255, dtype=np.uint8)
            ], axis=2)
            p_d = self.out / f"{self.name}_aci_diffuse.png"
            Image.fromarray(diffuse_full, "RGBA").save(str(p_d))
            self.cs1.diffuse = p_d
            log.info(f"  ACI -> diffuse: {p_d.name}")

            # R channel = Alpha
            alpha_arr = (np.clip(arr[:,:,0], 0, 1) * 255).astype(np.uint8)
            p_a = self.out / f"{self.name}_aci_alpha.png"
            Image.fromarray(alpha_arr, "L").save(str(p_a))
            self.cs1.alpha = p_a
            log.info(f"  ACI -> alpha:   {p_a.name}")

        elif self.cs1.aci and self.cs1.diffuse:
            # Have both ACI and diffuse — use ACI alpha channel only
            log.info("  ACI present alongside diffuse — extracting alpha channel only")
            img = self._resize(self._open(self.cs1.aci, "RGB"))
            arr = np.array(img, dtype=np.float32) / 255.0
            arr = np.power(arr, 0.45)
            alpha_arr = (np.clip(arr[:,:,0], 0, 1) * 255).astype(np.uint8)
            p_a = self.out / f"{self.name}_aci_alpha.png"
            Image.fromarray(alpha_arr, "L").save(str(p_a))
            if not self.cs1.alpha:
                self.cs1.alpha = p_a

        if self.cs1.xys and not self.cs1.normal:
            log.info("  Splitting XYS combined texture...")
            img = self._resize(self._open(self.cs1.xys, "RGB"))
            arr = np.array(img, dtype=np.float32) / 255.0

            # Apply gamma correction
            arr = np.power(arr, 0.45)

            # RG channels = Normal X and Y — reconstruct Z and build normal map
            nx = arr[:,:,0] * 2.0 - 1.0
            ny = arr[:,:,1] * 2.0 - 1.0
            nz = np.sqrt(np.clip(1.0 - nx**2 - ny**2, 0, 1))
            normal_rgb = np.stack([
                np.clip((nx + 1.0) / 2.0, 0, 1),
                np.clip((ny + 1.0) / 2.0, 0, 1),
                np.clip((nz + 1.0) / 2.0, 0, 1),
            ], axis=2)
            p_n = self.out / f"{self.name}_xys_normal.png"
            Image.fromarray((normal_rgb * 255).astype(np.uint8), "RGB").save(str(p_n))
            self.cs1.normal = p_n
            log.info(f"  XYS -> normal:  {p_n.name}")

            # B channel = Specular
            spec_arr = (np.clip(arr[:,:,2], 0, 1) * 255).astype(np.uint8)
            p_s = self.out / f"{self.name}_xys_specular.png"
            Image.fromarray(spec_arr, "L").save(str(p_s))
            if not self.cs1.specular:
                self.cs1.specular = p_s
            log.info(f"  XYS -> specular:{p_s.name}")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _p(self, suffix: str) -> Path:
        return self.out / f"{self.name}_{suffix}.png"

    def _open(self, path: Path, mode: str = "RGBA") -> "Image.Image":
        return Image.open(str(path)).convert(mode)

    def _target_size(self) -> tuple:
        """
        Return output texture size as a square power-of-2.

        CS2 strictly requires square textures. Non-square textures (e.g. 2048x1024)
        cause rendering artefacts including the reflective/mirror appearance.

        We take the LARGER dimension and make both axes equal to it,
        then pad the shorter axis with black when saving rather than
        stretching — this preserves UV mapping correctly.
        """
        ref = self.cs1.diffuse or self.cs1.normal or self.cs1.specular
        if ref:
            w, h = Image.open(str(ref)).size
            side = max(w, h)
        else:
            side = 512
        p2 = 1
        while p2 < side:
            p2 <<= 1
        size = max(512, min(4096, p2))
        return (size, size)

    def _pad_to_square(self, img: "Image.Image", mode: str = "RGBA") -> "Image.Image":
        """
        Pad an image to square with black (transparent for RGBA).
        Places original content at top-left, padding at bottom/right.
        This correctly handles non-square CS1 textures (e.g. 2048x1024)
        without stretching UVs — the UV coords still map to the original
        content area in the top-left of the padded square.
        """
        tw, th = self._target_size()
        img = img.convert(mode)
        if img.size == (tw, th):
            return img
        out = Image.new(mode, (tw, th), 0)  # black / transparent padding
        out.paste(img, (0, 0))
        if img.width != tw or img.height != th:
            log.info(f"  Padded {img.size} -> ({tw},{th}) to satisfy CS2 square requirement")
        return out

    def _resize(self, img: "Image.Image") -> "Image.Image":
        """Resize/pad image to target square size for CS2 compatibility."""
        tw, th = self._target_size()
        iw, ih = img.size
        if (iw, ih) == (tw, th):
            return img
        # If image is already square and just needs resizing, use LANCZOS
        if iw == ih:
            return img.resize((tw, th), Image.LANCZOS)
        # Non-square: scale to fit within target keeping aspect ratio, then pad
        scale = min(tw / iw, th / ih)
        new_w = int(iw * scale)
        new_h = int(ih * scale)
        scaled = img.resize((new_w, new_h), Image.LANCZOS)
        out = Image.new(img.mode, (tw, th), 0)
        out.paste(scaled, (0, 0))
        return out

    # ── texture generation ────────────────────────────────────────────────────

    def _base_color(self) -> Path:
        """
        CS2 BaseColor = diffuse RGB + alpha channel.

        Alpha handling:
          - If a dedicated _a.png alpha map exists, use it (fences, railings etc.)
          - If the diffuse has an alpha channel, check whether it is genuinely
            transparent (e.g. a fence texture) or just CS1 artefact data.
            Decision: if more than 5% of pixels are below 200 opacity AND a
            dedicated alpha map exists, use the diffuse alpha. Otherwise force
            full opaque (255) — buildings should never be transparent.
          - Default for buildings: full opaque alpha.

        Also applies mild desaturation to match CS2's clean visual style.
        """
        img = self._resize(self._open(self.cs1.diffuse))
        arr = np.array(img, dtype=np.float32) / 255.0
        rgb = arr[:, :, :3]

        # Mild desaturation (CS1 textures tend to be more saturated)
        lum = (rgb * np.array([0.2126, 0.7152, 0.0722])).sum(axis=2, keepdims=True)
        rgb = np.clip(lum + (rgb - lum) * 0.9, 0, 1)

        # Alpha channel decision
        if self.cs1.alpha:
            # Dedicated alpha map present — use it (fences, railings, transparent elements)
            alpha = np.array(
                self._resize(self._open(self.cs1.alpha, "L")),
                dtype=np.float32
            )[:, :, np.newaxis] / 255.0
            avg_alpha = float(alpha.mean())
            log.info(f"  BaseColor alpha: using _a map (avg={avg_alpha:.2f})")
        else:
            # No dedicated alpha map — force fully opaque.
            # CS1 diffuse textures often carry alpha channel data that was used
            # for CS1-specific rendering (e.g. colour variation, dirt overlays).
            # In CS2 that data makes the building transparent/streaky.
            alpha = np.ones((*rgb.shape[:2], 1), dtype=np.float32)
            log.info(f"  BaseColor alpha: forced opaque (no _a map)")

        out = (np.concatenate([rgb, alpha], axis=2) * 255).astype(np.uint8)
        p   = self._p("BaseColor")
        Image.fromarray(out, "RGBA").save(str(p), optimize=True)
        log.info(f"  BaseColor    -> {p.name}")
        return p

    def _control_mask(self) -> Path:
        """
        CS2 ControlMask:
          RGB = colour variation masks  (black = no variation = preserves diffuse)
          A   = SnowRemove              (white = normal snow accumulation)
        Black RGB is the safe default for converted assets.
        """
        size = self._target_size()
        arr  = np.zeros((*size[::-1], 4), dtype=np.uint8)
        arr[:, :, 3] = 255  # alpha white = normal snow
        p    = self._p("ControlMask")
        Image.fromarray(arr, "RGBA").save(str(p), optimize=True)
        log.info(f"  ControlMask  -> {p.name}")
        return p

    def _mask_map(self) -> Path:
        """
        CS2 MaskMap:
          R = Metallic   (0 — buildings are not metallic)
          G = Coat       (0 — no coat layer)
          B = Unused     (0 — always black)
          A = Glossiness (derived from CS1 specular using binary threshold)

        CS1 specular maps often have a bimodal distribution — near-zero for
        walls and near-1.0 for glass/windows. Passing high CS1 specular values
        directly into CS2 PBR makes buildings look like chrome mirrors.

        Binary threshold treatment:
          - Pixels with CS1 spec > 0.5 (glass/windows) → CS2 gloss = 0.4
          - Pixels with CS1 spec <= 0.5 (walls/roof)   → CS2 gloss = 0.05
        This gives glass buildings realistic-looking tinted glass in CS2
        without the mirror effect.

        If no specular map: fully matte (all zero).
        """
        size = self._target_size()
        h, w = size[1], size[0]

        if self.cs1.specular:
            spec_img = self._resize(self._open(self.cs1.specular, "L"))
            spec_arr = np.array(spec_img, dtype=np.float32) / 255.0

            # Check if specular is all-white (useless placeholder from Dump All)
            if spec_arr.mean() > 0.98:
                log.info(f"  MaskMap: specular is all-white placeholder — treating as matte")
                gloss = np.zeros((h, w), dtype=np.float32)
            else:
                # Binary threshold: glass vs wall
                window_mask = spec_arr > 0.5
                gloss = np.where(window_mask, 0.4, 0.05).astype(np.float32)
                pct_glass = window_mask.mean() * 100
                log.info(f"  MaskMap: {pct_glass:.0f}% glass (gloss=0.4), "
                         f"{100-pct_glass:.0f}% wall (gloss=0.05)")
        else:
            gloss = np.zeros((h, w), dtype=np.float32)
            log.info(f"  MaskMap: no specular — fully matte")

        mask = np.stack([
            np.zeros((h, w), dtype=np.float32),  # R metallic
            np.zeros((h, w), dtype=np.float32),  # G coat
            np.zeros((h, w), dtype=np.float32),  # B unused
            gloss,                                 # A glossiness
        ], axis=2)
        out = (np.clip(mask, 0, 1) * 255).astype(np.uint8)
        p   = self._p("MaskMap")
        Image.fromarray(out, "RGBA").save(str(p), optimize=True)
        log.info(f"  MaskMap      -> {p.name}")
        return p

    def _normal(self) -> Path:
        """
        CS1 and CS2 both use OpenGL tangent-space normal maps — direct copy.
        Resize to match other textures.
        """
        img = self._resize(self._open(self.cs1.normal, "RGB"))
        p   = self._p("Normal")
        img.save(str(p), optimize=True)
        log.info(f"  Normal       -> {p.name}")
        return p


# ── CS2 Prefab scaffold ───────────────────────────────────────────────────────

class PrefabGenerator:
    """Generates a .Prefab JSON reference file for the CS2 Asset Editor."""

    CATEGORIES = {
        AssetType.BUILDING: "Buildings",
        AssetType.PROP:     "Props",
        AssetType.VEHICLE:  "Vehicles",
        AssetType.TREE:     "Trees",
        AssetType.UNKNOWN:  "Props",
    }

    def __init__(self, result: ConversionResult):
        self.r = result

    def generate(self, out_dir: Path) -> Path:
        r   = self.r
        n   = r.asset_name
        cs2 = r.cs2_textures
        tex = {}
        if cs2.base_color:   tex["BaseColor"]   = cs2.base_color.name
        if cs2.control_mask: tex["ControlMask"] = cs2.control_mask.name
        if cs2.mask_map:     tex["MaskMap"]     = cs2.mask_map.name
        if cs2.normal:       tex["Normal"]      = cs2.normal.name

        prefab = {
            "__meta__": {
                "tool":    "CS1->CS2 Converter v3",
                "guide":   "https://cs2.paradoxwikis.com/Asset_Creation_Guide",
                "note":    (
                    "Place .fbx and textures in your CS2 UserMods/Assets folder. "
                    "Set Project Root = parent folder, Assets Folder = this folder. "
                    "NOTE: Add emissive manually in CS2 if windows/lights needed."
                ),
            },
            "name":        n,
            "displayName": n.replace("_", " "),
            "category":    self.CATEGORIES.get(r.asset_type, "Props"),
            "assetType":   r.asset_type.value,
            "mesh": {
                "file": f"{n}.fbx",
                "lod":  f"{n}_LOD1.fbx" if r.lod_fbx else "",
            },
            "textures": tex,
        }
        if r.asset_type == AssetType.BUILDING:
            prefab["building"] = {"footprint": {"width": 1, "length": 1},
                                  "height": 10.0, "zoneType": "Unzoned"}

        p = out_dir / f"{n}.Prefab"
        p.write_text(json.dumps(prefab, indent=2))
        log.info(f"  Prefab       -> {p.name}")
        return p


# ── Conversion pipeline ───────────────────────────────────────────────────────

class OBJPipeline:
    """
    Full CS1 → CS2 conversion pipeline.
    Input:  folder containing .obj mesh + CS1 texture PNGs
    Output: .fbx mesh + CS2 PBR textures + .Prefab scaffold
    """

    def __init__(self, input_folder: Path, output_dir: Path):
        self.input_folder = input_folder
        self.output_dir   = output_dir / input_folder.name

    def run(self) -> ConversionResult:
        log.info(f"Converting: {self.input_folder.name}")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 1. Scan input
        result = AssetFolder(self.input_folder).scan()
        if not result.success:
            self._save_report(result)
            return result

        name = result.asset_name

        # 2. Mesh: OBJ → FBX via Blender
        if result.mesh_fbx:
            src  = result.mesh_fbx
            dest = self.output_dir / f"{name}.fbx"
            if src.suffix.lower() == ".obj":
                if not OBJtoFBX(src).convert(dest):
                    # Fallback: copy OBJ with warning
                    shutil.copy(str(src), str(self.output_dir / src.name))
                    result.mesh_fbx = self.output_dir / src.name
                    result.warnings.append(
                        "OBJ→FBX failed. Kept .obj — convert in Blender before CS2 import."
                    )
                else:
                    result.mesh_fbx = dest
            else:
                shutil.copy(str(src), str(dest))
                result.mesh_fbx = dest
                log.info(f"  Mesh:        -> {dest.name}")

        # 3. LOD mesh
        if result.lod_fbx:
            src  = result.lod_fbx
            dest = self.output_dir / f"{name}_LOD1.fbx"
            if src.suffix.lower() == ".obj":
                if not OBJtoFBX(src).convert(dest):
                    shutil.copy(str(src), str(self.output_dir / src.name))
                    result.lod_fbx = None
                else:
                    result.lod_fbx = dest
            else:
                shutil.copy(str(src), str(dest))
                result.lod_fbx = dest
            if result.lod_fbx:
                log.info(f"  LOD:         -> {result.lod_fbx.name}")

        # 4. Textures
        try:
            result.cs2_textures = TextureConverter(
                name, result.cs1_textures, self.output_dir
            ).convert()
        except Exception as e:
            log.error(f"  Texture conversion failed: {e}")
            result.warnings.append(f"Texture conversion failed: {e}")

        # 5. Prefab
        try:
            result.prefab_file = PrefabGenerator(result).generate(self.output_dir)
        except Exception as e:
            result.warnings.append(f"Prefab generation failed: {e}")

        result.output_dir = self.output_dir
        self._save_report(result)
        log.info(f"  Done -> {self.output_dir}")
        return result

    def _save_report(self, result: ConversionResult):
        cs2 = result.cs2_textures
        report = {
            "version":    "3.0",
            "asset":      result.asset_name,
            "success":    result.success,
            "type":       result.asset_type.value,
            "output":     str(result.output_dir) if result.output_dir else None,
            "files": {
                "mesh":         str(result.mesh_fbx)     if result.mesh_fbx     else None,
                "lod":          str(result.lod_fbx)      if result.lod_fbx      else None,
                "prefab":       str(result.prefab_file)  if result.prefab_file  else None,
                "BaseColor":    str(cs2.base_color)      if cs2.base_color      else None,
                "ControlMask":  str(cs2.control_mask)    if cs2.control_mask    else None,
                "MaskMap":      str(cs2.mask_map)        if cs2.mask_map        else None,
                "Normal":       str(cs2.normal)          if cs2.normal          else None,
            },
            "cs1_sources": {
                "diffuse":      str(result.cs1_textures.diffuse)      if result.cs1_textures.diffuse      else None,
                "normal":       str(result.cs1_textures.normal)       if result.cs1_textures.normal       else None,
                "specular":     str(result.cs1_textures.specular)     if result.cs1_textures.specular     else None,
                "alpha":        str(result.cs1_textures.alpha)        if result.cs1_textures.alpha        else None,
                "illumination": str(result.cs1_textures.illumination) if result.cs1_textures.illumination else None,
            },
            "warnings": result.warnings,
            "errors":   result.errors,
        }
        p = self.output_dir / "conversion_report.json"
        p.write_text(json.dumps(report, indent=2))


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="CS1→CS2 Asset Converter v3 — converts ModTools-dumped assets.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python converter.py ~/cs1dumps/MyBuilding/\n"
            "  python converter.py ~/cs1dumps/ -o ~/cs2_ready/\n"
        )
    )
    ap.add_argument("input",  type=Path,
                    help="Asset folder (with .obj + textures) or parent of multiple asset folders")
    ap.add_argument("-o", "--output", type=Path, default=Path("cs2_output"),
                    help="Output directory  (default: ./cs2_output)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Show debug output")
    args = ap.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    if not PILLOW_OK:
        print("ERROR: pip install Pillow numpy"); sys.exit(1)
    if not args.input.is_dir():
        print(f"ERROR: '{args.input}' is not a folder."); sys.exit(1)

    # Single asset folder or parent of many?
    has_mesh = any(f.suffix.lower() in {".obj", ".fbx"}
                   for f in args.input.iterdir() if f.is_file())
    folders  = [args.input] if has_mesh else sorted(
        f for f in args.input.iterdir() if f.is_dir())
    if not folders:
        print("No asset folders found."); sys.exit(1)

    print(f"\n{'='*54}")
    print(f"  CS1 → CS2 Converter  v3.0  |  {len(folders)} asset(s)")
    print(f"{'='*54}\n")

    results = [OBJPipeline(f, args.output).run() for f in folders]
    ok = sum(1 for r in results if r.success)

    print(f"\n{'='*54}")
    print(f"  {ok}/{len(results)} converted  →  {args.output.resolve()}")
    print(f"{'='*54}\n")
    for r in results:
        for w in r.warnings: print(f"  ⚠  {r.asset_name}: {w}")
        for e in r.errors:   print(f"  ✗  {r.asset_name}: {e}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
