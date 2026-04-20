"""
Cities Skylines 1 -> CS2 Asset Converter  —  GUI
Run:  python gui.py
"""
import sys
import threading
import queue
import logging
import subprocess
import platform
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
except ImportError:
    print("tkinter missing.  Ubuntu: sudo apt install python3-tk")
    sys.exit(1)

from converter import OBJPipeline, PILLOW_OK


# ── Log bridge ────────────────────────────────────────────────────────────────

class QueueHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q
    def emit(self, record):
        self.q.put(self.format(record))


# ── App ───────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    BG      = "#0f1923"
    PANEL   = "#17232f"
    BORDER  = "#1e3040"
    ACCENT  = "#00c8ff"
    ACCENT2 = "#00ffb3"
    WARN    = "#ffb347"
    ERR     = "#ff4f4f"
    TEXT    = "#d4e8f0"
    DIM     = "#5c8099"
    FH      = ("Courier New", 11, "bold")
    FB      = ("Courier New", 10)
    FS      = ("Courier New", 9)

    CS2_IMPORT_STEPS = [
        ("Step 1", "Find your CS2 Import folder",
         "Open File Explorer and paste this path into the address bar:\n"
         "%AppData%\\..\\LocalLow\\Colossal Order\\Cities Skylines II\\Mods\\UserMods\\Assets"),
        ("Step 2", "Copy the converted files there",
         "From the converter output folder, copy:\n"
         "  • The .fbx mesh file  (e.g. HibbingHighSchool.fbx)\n"
         "  • All textures from the textures_cs2 subfolder\n"
         "    (HibbingHighSchool_BaseColor.png, _MaskMap.png, _Normal.png etc.)\n"
         "Put them all in the same folder inside UserMods\\Assets."),
        ("Step 3", "Open the CS2 Asset Editor",
         "Launch Cities Skylines 2.\n"
         "From the main menu go to: Editor -> Asset Editor"),
        ("Step 4", "Import your mesh",
         "Click 'New Asset', choose Building (or your asset type).\n"
         "Click 'Import Mesh' and select your .fbx file.\n"
         "CS2 will automatically find the textures if they are in the same folder\n"
         "and named correctly (e.g. HibbingHighSchool_BaseColor.png)."),
        ("Step 5", "Adjust and save",
         "Set the lot size, zone type, and other properties.\n"
         "Click Save, then Publish to share on Paradox Mods."),
    ]

    GUIDE_STEPS = [
        ("Step 1", "Subscribe to ModTools",
         "In Steam, search the CS1 Workshop for 'ModTools' by Gameranx/klyte45 and subscribe.\n"
         "Then launch CS1 — ModTools loads automatically."),
        ("Step 2", "Load a city with your asset",
         "Open any saved city in CS1 that uses the building/prop you want to convert.\n"
         "Or start a new game and place the asset from the menu first."),
        ("Step 3", "Open ModTools Scene Explorer",
         "Press Ctrl+E to open Scene Explorer.\n"
         "In the search box at the top, type the asset's name (e.g. HibbingHighSchool)\n"
         "and press Enter to find it."),
        ("Step 4", "Dump the mesh",
         "In Scene Explorer, expand the asset and find 'm_mesh'.\n"
         "Click the small arrow next to it, then click 'Dump mesh to OBJ'.\n"
         "The .obj file saves to:  AppData\\Local\\Colossal Order\\Cities_Skylines\\Addons\\Import"),
        ("Step 5", "Dump the textures",
         "Still in Scene Explorer, find 'm_material' inside the asset.\n"
         "Click 'Dump diffuse', 'Dump normal', 'Dump specular', 'Dump illumination'.\n"
         "These also save to the same Import folder as the mesh."),
        ("Step 6", "Gather your files",
         "Go to the Import folder and collect the .obj and all texture .png files.\n"
         "Put them all into ONE folder together — name the folder after your asset.\n"
         "Example:  MyAssets\\HibbingHighSchool\\  containing  .obj + _d.png + _n.png etc."),
        ("Step 7", "Convert with this tool",
         "Come back here, click 'Add asset folder', select the folder you just made,\n"
         "set your output location, and click Convert.\n"
         "The CS2-ready files will appear in your output folder."),
    ]

    def __init__(self):
        super().__init__()
        self.title("CS1 → CS2 Asset Converter")
        self.geometry("860x680")
        self.minsize(700, 520)
        self.configure(bg=self.BG)
        self._log_q    = queue.Queue()
        self._running  = False
        self._folders  = []
        self._setup_logging()
        self._build_ui()
        self._check_deps()
        self._poll_logs()

    def _setup_logging(self):
        h = QueueHandler(self._log_q)
        h.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logging.getLogger().addHandler(h)
        logging.getLogger().setLevel(logging.INFO)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=self.BG, pady=12)
        hdr.pack(fill="x", padx=20)
        tk.Label(hdr, text="CS1  →  CS2", font=("Courier New", 20, "bold"),
                 fg=self.ACCENT, bg=self.BG).pack(side="left")
        tk.Label(hdr, text="  Asset Converter", font=("Courier New", 13),
                 fg=self.DIM, bg=self.BG).pack(side="left", pady=4)

        # Dep bar
        self._dep_lbl = tk.Label(self, text="Checking...", font=self.FS,
                                  fg=self.DIM, bg=self.PANEL, anchor="w", pady=4, padx=12)
        self._dep_lbl.pack(fill="x", padx=20, pady=(0, 4))

        # Notebook: Convert | Guide
        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("TNotebook",        background=self.BG,  borderwidth=0)
        style.configure("TNotebook.Tab",    background=self.PANEL, foreground=self.DIM,
                        padding=[14, 6], font=self.FS)
        style.map("TNotebook.Tab",
                  background=[("selected", self.BORDER)],
                  foreground=[("selected", self.ACCENT)])

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=20, pady=4)

        convert_tab = tk.Frame(nb, bg=self.BG)
        guide_tab   = tk.Frame(nb, bg=self.BG)
        nb.add(convert_tab, text="  Convert  ")
        nb.add(guide_tab,   text="  How to export from CS1  ")

        self._build_convert_tab(convert_tab)
        self._build_guide_tab(guide_tab)

    def _build_convert_tab(self, parent):
        # Folder list
        sel = self._lframe(parent, "Asset Folders  (each folder = one asset)")
        sel.pack(fill="x", pady=(6, 4))

        btn_row = tk.Frame(sel, bg=self.PANEL)
        btn_row.pack(fill="x")
        self._btn(btn_row, "+ Add folder",  self._add_folder).pack(side="left", padx=(0,6))
        self._btn(btn_row, "✕ Clear",       self._clear_folders, dim=True).pack(side="left")

        list_wrap = tk.Frame(sel, bg=self.BG, bd=1, relief="solid")
        list_wrap.pack(fill="x", pady=(8, 0))
        self._folder_list = tk.Listbox(
            list_wrap, bg=self.BG, fg=self.TEXT, selectbackground=self.BORDER,
            font=self.FS, height=5, bd=0, highlightthickness=0, activestyle="none")
        self._folder_list.pack(fill="both", padx=4, pady=4)

        self._folder_count = tk.Label(sel, text="No folders selected",
                                       font=self.FS, fg=self.DIM, bg=self.PANEL)
        self._folder_count.pack(anchor="w", pady=(4, 0))

        # Output dir
        out_f = self._lframe(parent, "Output Directory")
        out_f.pack(fill="x", pady=4)

        out_row = tk.Frame(out_f, bg=self.PANEL)
        out_row.pack(fill="x")
        self._out_var = tk.StringVar(value=str(Path.home() / "cs2_output"))
        tk.Entry(out_row, textvariable=self._out_var, bg=self.BG, fg=self.TEXT,
                 insertbackground=self.ACCENT, font=self.FS, bd=0,
                 highlightthickness=1, highlightcolor=self.ACCENT,
                 highlightbackground=self.BORDER
                 ).pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 8))
        self._btn(out_row, "Browse", self._browse_out).pack(side="right")

        # Blender path
        bl_f = self._lframe(parent, "Blender Path  (needed for mesh conversion)")
        bl_f.pack(fill="x", pady=4)

        bl_row = tk.Frame(bl_f, bg=self.PANEL)
        bl_row.pack(fill="x")
        self._blender_var = tk.StringVar(value=self._detect_blender())
        self._blender_entry = tk.Entry(
            bl_row, textvariable=self._blender_var, bg=self.BG, fg=self.TEXT,
            insertbackground=self.ACCENT, font=self.FS, bd=0,
            highlightthickness=1, highlightcolor=self.ACCENT,
            highlightbackground=self.BORDER
        )
        self._blender_entry.pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 8))
        self._btn(bl_row, "Browse", self._browse_blender).pack(side="right")

        bl_status = tk.Frame(bl_f, bg=self.PANEL)
        bl_status.pack(fill="x", pady=(4, 0))
        self._bl_status_lbl = tk.Label(
            bl_status, text="", font=self.FS, bg=self.PANEL, anchor="w"
        )
        self._bl_status_lbl.pack(side="left")
        self._update_blender_status()

        # Log
        log_f = self._lframe(parent, "Log")
        log_f.pack(fill="both", expand=True, pady=4)
        self._log_box = scrolledtext.ScrolledText(
            log_f, bg=self.BG, fg=self.TEXT, font=self.FS, bd=0,
            highlightthickness=0, state="disabled", wrap="word", height=8)
        self._log_box.pack(fill="both", expand=True)
        for tag, col in [("INFO", self.TEXT), ("WARNING", self.WARN),
                         ("ERROR", self.ERR), ("STEP", self.ACCENT2)]:
            self._log_box.tag_config(tag, foreground=col)

        # Progress + bottom
        self._progress = ttk.Progressbar(parent, mode="indeterminate",
                                          style="C.Horizontal.TProgressbar")
        self._progress.pack(fill="x", pady=(0, 4))
        s = ttk.Style(self)
        s.configure("C.Horizontal.TProgressbar",
                     troughcolor=self.PANEL, background=self.ACCENT,
                     bordercolor=self.BORDER, lightcolor=self.ACCENT,
                     darkcolor=self.ACCENT)

        bot = tk.Frame(parent, bg=self.BG)
        bot.pack(fill="x", pady=(0, 10))
        self._status = tk.StringVar(value="Ready")
        tk.Label(bot, textvariable=self._status, font=self.FS,
                 fg=self.DIM, bg=self.BG).pack(side="left")
        self._convert_btn = self._btn(bot, "▶  Convert Assets",
                                       self._start_conversion, accent=True)
        self._convert_btn.pack(side="right")

    def _build_guide_tab(self, parent):
        canvas = tk.Canvas(parent, bg=self.BG, highlightthickness=0)
        vsb    = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=self.BG)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<MouseWheel>",
                    lambda e: canvas.yview_scroll(-1*(e.delta//120), "units"))

        tk.Label(inner, text="How to export your CS1 asset using ModTools",
                 font=("Courier New", 12, "bold"), fg=self.ACCENT, bg=self.BG,
                 pady=12).pack(anchor="w", padx=20)

        for label, title, body in self.GUIDE_STEPS:
            card = tk.Frame(inner, bg=self.PANEL, bd=0)
            card.pack(fill="x", padx=20, pady=4)

            hdr_row = tk.Frame(card, bg=self.PANEL)
            hdr_row.pack(fill="x", padx=12, pady=(10, 2))
            tk.Label(hdr_row, text=label, font=("Courier New", 9, "bold"),
                     fg=self.ACCENT2, bg=self.PANEL, width=7, anchor="w"
                     ).pack(side="left")
            tk.Label(hdr_row, text=title, font=("Courier New", 10, "bold"),
                     fg=self.TEXT, bg=self.PANEL).pack(side="left")

            tk.Label(card, text=body, font=self.FS, fg=self.DIM, bg=self.PANEL,
                     justify="left", anchor="w", wraplength=700
                     ).pack(fill="x", padx=12, pady=(0, 10))

        # ModTools link helper
        note = tk.Frame(inner, bg=self.BORDER)
        note.pack(fill="x", padx=20, pady=(8, 16))
        tk.Label(note, text="  ModTools Workshop link:",
                 font=self.FS, fg=self.DIM, bg=self.BORDER).pack(side="left", pady=8)
        lnk = tk.Label(note, text="steamcommunity.com/sharedfiles/filedetails/?id=2434651215",
                        font=self.FS, fg=self.ACCENT, bg=self.BORDER, cursor="hand2")
        lnk.pack(side="left", padx=4)
        lnk.bind("<Button-1>", lambda e: self._open_url(
            "https://steamcommunity.com/sharedfiles/filedetails/?id=2434651215"))

        # Import folder shortcut
        imp = tk.Frame(inner, bg=self.BORDER)
        imp.pack(fill="x", padx=20, pady=(0, 20))
        tk.Label(imp, text="  Import folder (where ModTools saves files):",
                 font=self.FS, fg=self.DIM, bg=self.BORDER).pack(side="left", pady=8)
        import_path = Path.home() / "AppData" / "Local" / "Colossal Order" / \
                      "Cities_Skylines" / "Addons" / "Import"
        btn = tk.Label(imp, text="Open folder →", font=self.FS,
                       fg=self.ACCENT2, bg=self.BORDER, cursor="hand2", padx=8)
        btn.pack(side="left")
        btn.bind("<Button-1>", lambda e: self._open_folder(import_path))

    # ── helpers ───────────────────────────────────────────────────────────────

    def _lframe(self, parent, title):
        f = tk.LabelFrame(parent, text=f" {title} ", font=self.FS,
                          fg=self.DIM, bg=self.PANEL, bd=1, relief="flat",
                          padx=10, pady=8)
        return f

    def _btn(self, parent, text, cmd, accent=False, dim=False):
        fg = self.BG if accent else (self.DIM if dim else self.TEXT)
        bg = self.ACCENT if accent else self.BORDER
        return tk.Button(parent, text=text, command=cmd, font=self.FS,
                         fg=fg, bg=bg, activeforeground=fg,
                         activebackground=self.ACCENT2 if accent else "#263d50",
                         bd=0, padx=10, pady=5, cursor="hand2", relief="flat")

    def _check_deps(self):
        if PILLOW_OK:
            self._dep_lbl.config(text="✓ Pillow + numpy ready", fg=self.ACCENT2)
        else:
            self._dep_lbl.config(
                text="✗ Pillow/numpy missing — run: pip install Pillow numpy",
                fg=self.ERR)
            self._convert_btn.config(state="disabled")

    # ── folder actions ────────────────────────────────────────────────────────

    def _detect_blender(self) -> str:
        """Try to auto-detect Blender path at startup."""
        import shutil, glob
        found = shutil.which("blender")
        if found:
            return found
        patterns = [
            r"C:\Program Files\Blender Foundation\Blender *\blender.exe",
            r"C:\Program Files (x86)\Blender Foundation\Blender *\blender.exe",
        ]
        for pat in patterns:
            matches = glob.glob(pat)
            if matches:
                return sorted(matches)[-1]
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"SOFTWARE\BlenderFoundation")
            d, _ = winreg.QueryValueEx(key, "Install_Dir")
            candidate = str(Path(d) / "blender.exe")
            if Path(candidate).exists():
                return candidate
        except Exception:
            pass
        return ""

    def _browse_blender(self):
        path = filedialog.askopenfilename(
            title="Find blender.exe",
            filetypes=[("Blender", "blender.exe"), ("All", "*.*")]
        )
        if path:
            self._blender_var.set(path)
        self._update_blender_status()

    def _update_blender_status(self):
        path = self._blender_var.get().strip()
        if path and Path(path).exists():
            self._bl_status_lbl.config(
                text=f"✓ Found: {Path(path).name}", fg=self.ACCENT2)
        elif path:
            self._bl_status_lbl.config(
                text="✗ File not found at this path", fg=self.ERR)
        else:
            self._bl_status_lbl.config(
                text="⚠  Not found — browse for blender.exe  |  "
                     "Download free at blender.org",
                fg=self.WARN)

    def _add_folder(self):
        folder = filedialog.askdirectory(title="Select asset folder (containing .obj + textures)")
        if folder:
            p = Path(folder)
            if p not in self._folders:
                self._folders.append(p)
            self._refresh_list()

    def _clear_folders(self):
        self._folders.clear()
        self._refresh_list()

    def _refresh_list(self):
        self._folder_list.delete(0, "end")
        for p in self._folders:
            self._folder_list.insert("end", str(p))
        n = len(self._folders)
        self._folder_count.config(
            text=f"{n} folder{'s' if n!=1 else ''} selected" if n else "No folders selected")

    def _browse_out(self):
        d = filedialog.askdirectory(title="Select output directory")
        if d:
            self._out_var.set(d)

    # ── conversion ────────────────────────────────────────────────────────────

    def _start_conversion(self):
        if self._running:
            return
        if not self._folders:
            messagebox.showwarning("No folders", "Add at least one asset folder first.")
            return
        out = Path(self._out_var.get().strip())
        self._running = True
        self._convert_btn.config(state="disabled", text="Converting…")
        self._progress.start(12)
        self._log("─" * 50, "STEP")
        self._log(f"Starting conversion of {len(self._folders)} asset(s)…", "STEP")
        threading.Thread(target=self._run_conversion,
                         args=(list(self._folders), out), daemon=True).start()

    def _run_conversion(self, folders, out):
        # Pass the user-specified Blender path to the converter via env var
        blender_path = self._blender_var.get().strip()
        if blender_path and Path(blender_path).exists():
            import os
            os.environ["BLENDER_PATH"] = blender_path
    def _run_conversion(self, folders, out):
        results = []
        for folder in folders:
            try:
                results.append(OBJPipeline(folder, out).run())
            except Exception as e:
                logging.error(f"Fatal error on {folder.name}: {e}")
        self.after(0, self._conversion_done, results, out)

    def _conversion_done(self, results, out):
        self._progress.stop()
        self._running = False
        self._convert_btn.config(state="normal", text="▶  Convert Assets")
        ok = sum(1 for r in results if r.success)
        self._log("─" * 50, "STEP")
        self._log(f"Done: {ok}/{len(results)} converted", "STEP")
        self._log(f"Output: {out}", "INFO")

        blender_needed = False
        for r in results:
            for w in r.warnings:
                self._log(f"  ⚠  {r.asset_name}: {w}", "WARNING")
                if "lender" in w:
                    blender_needed = True
            for e in r.errors:
                self._log(f"  ✗  {r.asset_name}: {e}", "ERROR")

        self._status.set(f"Done — {ok}/{len(results)} converted")

        if blender_needed:
            self._log("─" * 50, "STEP")
            self._log("⚠  Blender needed for .fbx conversion — see below.", "WARNING")
            if messagebox.askyesno(
                "Blender required for mesh conversion",
                "The mesh .fbx needs Blender (free) to convert correctly.\n\n"
                "1. Install Blender from blender.org\n"
                "2. Re-run this converter — it will use Blender automatically\n\n"
                "A ready-to-run script (convert_to_fbx.py) has also been saved\n"
                "to your output folder if you want to run it manually.\n\n"
                "Open blender.org now?"
            ):
                self._open_url("https://www.blender.org/download/")

        if messagebox.askyesno("Done", f"{ok}/{len(results)} converted.\n\nOpen output folder?"):
            self._open_folder(out)

    # ── log ───────────────────────────────────────────────────────────────────

    def _log(self, msg, level="INFO"):
        self._log_box.config(state="normal")
        self._log_box.insert("end", msg + "\n", level)
        self._log_box.see("end")
        self._log_box.config(state="disabled")

    def _poll_logs(self):
        try:
            while True:
                msg = self._log_q.get_nowait()
                lvl = ("WARNING" if "WARNING" in msg else
                       "ERROR"   if "ERROR"   in msg else
                       "STEP"    if "─" in msg        else "INFO")
                self._log(msg, lvl)
        except queue.Empty:
            pass
        self.after(100, self._poll_logs)

    # ── utils ─────────────────────────────────────────────────────────────────

    def _open_folder(self, path: Path):
        try:
            if platform.system() == "Windows":
                subprocess.Popen(["explorer", str(path)])
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception:
            pass

    def _open_url(self, url: str):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass


if __name__ == "__main__":
    App().mainloop()
