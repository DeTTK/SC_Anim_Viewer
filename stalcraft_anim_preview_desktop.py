#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import struct
import subprocess
import time
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, QUrl
from PySide6.QtGui import QAction, QDrag
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from blender_link_module import BlenderLinkModule


def parse_mcal_clips(path: Path) -> list[str]:
    data = path.read_bytes()
    o = 0
    if data[o:o + 4] != b"MCAL":
        raise ValueError("Not an MCAL file")
    o += 4
    o += 4
    bone_count = struct.unpack_from("<I", data, o)[0]
    o += 4
    o += 1
    clip_count = struct.unpack_from("<i", data, o)[0]
    o += 4

    names: list[str] = []
    keyframe_size = bone_count * 14
    for _ in range(clip_count):
        name_len = struct.unpack_from("<H", data, o)[0]
        o += 2
        name = data[o:o + name_len].decode("ascii", errors="ignore")
        o += name_len
        frames = struct.unpack_from("<i", data, o)[0]
        o += 4
        o += 4
        o += frames * keyframe_size
        names.append(name)
    return names


def sanitize(value: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return s or "item"


def clip_display_name(name: str) -> str:
    n = name.strip()
    if n.lower().startswith("main:"):
        return n.split(":", 1)[1].strip()
    return n


def run_cmd(args: list[str], cwd: Path | None = None) -> None:
    def _run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True)

    def _install_pkg(spec: str) -> None:
        install_cmd = ["py", "-m", "pip", "install", spec]
        install_proc = subprocess.run(install_cmd, capture_output=True, text=True)
        if install_proc.returncode != 0:
            raise RuntimeError(
                "Failed to install missing Python dependency automatically.\n"
                f"Command: {' '.join(install_cmd)}\n"
                f"{install_proc.stdout}\n{install_proc.stderr}"
            )

    proc = _run()
    if proc.returncode == 0:
        return

    output = f"{proc.stdout}\n{proc.stderr}"
    is_scfile_cli = len(args) >= 3 and args[0] == "py" and args[1] == "-m" and args[2] == "scfile"
    missing = re.search(r"No module named '([^']+)'", output)
    if is_scfile_cli and missing:
        pkg_map = {
            "click": "click>=8.2,<9",
            "rich": "rich>=14.1,<15",
            "lz4": "lz4>=4.4,<5",
            "zstandard": "zstandard>=0.25,<1",
            "numpy": "numpy>=2.3,<3",
        }
        missing_mod = missing.group(1).split(".", 1)[0]
        _install_pkg(pkg_map.get(missing_mod, missing_mod))
        proc = _run()
        if proc.returncode == 0:
            return

    raise RuntimeError(f"Command failed: {' '.join(args)}\n{proc.stdout}\n{proc.stderr}")


def choose_blender_exe() -> Path:
    env = os.environ.get("BLENDER_EXE")
    if env:
        p = Path(env).resolve()
        if p.exists():
            return p

    candidates = [
        Path(r"D:\SteamLibrary\steamapps\common\Blender\blender.exe"),
        Path(r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe"),
        Path(r"C:\Program Files\Blender Foundation\Blender 4.1\blender.exe"),
        Path(r"C:\Program Files\Blender Foundation\Blender 4.0\blender.exe"),
        Path(r"C:\Program Files\Blender Foundation\Blender\blender.exe"),
    ]
    for p in candidates:
        if p.exists():
            return p.resolve()
    raise FileNotFoundError("Blender executable not found. Set BLENDER_EXE or install Blender.")


def list_model_clips_via_workbench(awb_root: Path, scfile_root: Path, model_path: Path) -> list[str]:
    script = awb_root / "model_anim_extract.py"
    proc = subprocess.run(
        [
            "py",
            str(script),
            "list",
            "--model",
            str(model_path),
            "--scfile-root",
            str(scfile_root),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to list model clips:\n{proc.stdout}\n{proc.stderr}")

    names: list[str] = []
    for line in (proc.stdout or "").splitlines():
        m = re.match(r"^\s*\d+\s+(.+?)\s+frames=", line)
        if m:
            names.append(m.group(1).strip())
    return names


@dataclass
class ToolPaths:
    scfile_root: Path | None
    animation_workbench_root: Path | None


def detect_paths() -> ToolPaths:
    base = Path(__file__).resolve().parent
    sc_candidates = [
        base / "tools" / "sc-file-master",
        base / "sc-file-master",
    ]
    awb_candidates = [
        base / "tools" / "AnimationWorkbench",
        base / "AnimationWorkbench",
    ]
    sc = next((p for p in sc_candidates if p.exists() and (p / "scfile").exists()), None)
    awb = next((p for p in awb_candidates if p.exists() and (p / "mcal_to_blender_glb.py").exists()), None)
    return ToolPaths(
        scfile_root=sc.resolve() if sc else None,
        animation_workbench_root=awb.resolve() if awb else None,
    )


def detect_source_model_for_mcal(mcal_path: Path) -> Path | None:
    base = mcal_path.parent.parent
    for ext in (".mcsb", ".mcsa", ".mcvd"):
        p = base / f"origin{ext}"
        if p.exists():
            return p
    return None


def is_sc_model(path: Path | None) -> bool:
    if path is None:
        return False
    return path.suffix.lower() in {".mcsb", ".mcsa", ".mcvd"}


class ClipListWidget(QListWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)

    def startDrag(self, supportedActions) -> None:  # type: ignore[override]
        item = self.currentItem()
        if item is None:
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setText(item.text())
        drag.setMimeData(mime)
        drag.exec(supportedActions)


class SlotLineEdit(QLineEdit):
    def __init__(self, slot_index: int, on_drop, parent=None) -> None:
        super().__init__(parent)
        self.slot_index = slot_index
        self.on_drop = on_drop
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        text = event.mimeData().text().strip()
        if not text:
            event.ignore()
            return
        self.setText(text)
        if self.on_drop is not None:
            self.on_drop(self.slot_index, text)
        event.acceptProposedAction()


class MainWindow(QMainWindow):
    SLOT_COUNT = 8

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("STALCRAFT Model Viewer")
        self.resize(1560, 960)

        paths = detect_paths()
        self.scfile_root = paths.scfile_root
        self.awb_root = paths.animation_workbench_root
        self.project_root = Path(__file__).resolve().parent

        self.current_bundle: Path | None = None
        self.bundle_anim_dir: Path | None = None
        self.bundle_texture_dir: Path | None = None
        self.server_proc: subprocess.Popen | None = None

        self.target_model: Path | None = None
        self.available_refs: list[tuple[str, str, str]] = []
        self.pack_configs: dict[str, dict] = {
            "main": {
                "source_mode": "",
                "source_mcal": None,
                "source_model_for_remap": None,
                "source_model_anim": None,
                "available_names": [],
                "prepared_anim_map": {},
            },
        }

        self._build_ui()
        self.blender_link = BlenderLinkModule(
            project_root=self.project_root,
            show_status=lambda msg: self.statusBar().showMessage(msg),
            show_error=lambda title, msg: QMessageBox.warning(self, title, msg),
            run_js=self.run_js,
        )
        self._build_menu()
        missing: list[str] = []
        if self.scfile_root is None:
            missing.append("sc-file")
        if self.awb_root is None:
            missing.append("AnimationWorkbench")
        if missing:
            self.statusBar().showMessage(
                "Ready (GLB-only mode). Missing tools: " + ", ".join(missing) + "."
            )
        else:
            self.statusBar().showMessage("Ready")

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)

        cfg = QGroupBox("Inputs")
        cgl = QGridLayout(cfg)

        self.model_edit = QLineEdit()
        self.main_source_edit = QLineEdit()

        btn_model = QPushButton("Browse")
        btn_model.clicked.connect(self.pick_model)
        btn_main_source = QPushButton("Browse")
        btn_main_source.clicked.connect(self.pick_source)
        btn_build = QPushButton("Build + Load")
        btn_build.clicked.connect(self.build_and_load)

        cgl.addWidget(QLabel("Model (.mcsb/.mcsa/.mcvd/.glb)"), 0, 0)
        cgl.addWidget(self.model_edit, 0, 1)
        cgl.addWidget(btn_model, 0, 2)
        cgl.addWidget(QLabel("Main anim source (.mcal/.glb/folder)"), 1, 0)
        cgl.addWidget(self.main_source_edit, 1, 1)
        cgl.addWidget(btn_main_source, 1, 2)
        cgl.addWidget(btn_build, 0, 3, 2, 1)

        root.addWidget(cfg)

        # Viewport stays always visible on top.
        self.web = QWebEngineView()
        ws = self.web.settings()
        ws.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        ws.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        self.web.loadFinished.connect(self.on_web_loaded)
        self.web.page().profile().downloadRequested.connect(self.on_download_requested)
        root.addWidget(self.web, 5)

        # Controls are in bottom tabs (Sequence / Render / Textures).
        bottom_tabs = QTabWidget()
        root.addWidget(bottom_tabs, 3)

        seq_tab = QWidget()
        bottom_tabs.addTab(seq_tab, "Sequence")
        bl = QHBoxLayout(seq_tab)

        left = QGroupBox("Clip Names")
        ll = QVBoxLayout(left)
        self.clip_search = QLineEdit()
        self.clip_search.setPlaceholderText("Search clips...")
        self.clip_search.textChanged.connect(self.populate_available_list)
        ll.addWidget(self.clip_search)
        self.available_list = ClipListWidget()
        ll.addWidget(self.available_list)
        ll.addWidget(QLabel("Drag a clip name into slot field"))
        bl.addWidget(left, 2)

        right = QGroupBox("Sequence Slots")
        rl = QGridLayout(right)
        headers = ["Slot", "Clip name (drop here)", "Unload", "Weight", "Loop", "Mode"]
        for i, h in enumerate(headers):
            rl.addWidget(QLabel(h), 0, i)

        self.slot_name_edits: list[QLineEdit] = []
        self.slot_weight_sliders: list[QSlider] = []
        self.slot_weight_labels: list[QLabel] = []
        self.slot_loop_checks: list[QCheckBox] = []
        self.slot_mode_boxes: list[QComboBox] = []

        for i in range(self.SLOT_COUNT):
            rl.addWidget(QLabel(str(i)), i + 1, 0)

            name_edit = SlotLineEdit(i, self.on_slot_dropped)
            self.slot_name_edits.append(name_edit)
            rl.addWidget(name_edit, i + 1, 1)

            btn_unload = QPushButton("Unload")
            btn_unload.clicked.connect(lambda _=False, idx=i: self.unload_slot(idx))
            rl.addWidget(btn_unload, i + 1, 2)

            w_wrap = QWidget()
            w_l = QHBoxLayout(w_wrap)
            w_l.setContentsMargins(0, 0, 0, 0)
            wsld = QSlider(Qt.Horizontal)
            wsld.setRange(0, 100)
            wsld.setValue(100)
            wsld.valueChanged.connect(lambda _v, idx=i: self.apply_slot_weight(idx))
            wtxt = QLabel("1.00")
            wtxt.setMinimumWidth(36)
            self.slot_weight_sliders.append(wsld)
            self.slot_weight_labels.append(wtxt)
            w_l.addWidget(wsld)
            w_l.addWidget(wtxt)
            rl.addWidget(w_wrap, i + 1, 3)

            loop = QCheckBox()
            loop.setChecked(True)
            loop.toggled.connect(lambda _v, idx=i: self.apply_slot_loop(idx))
            self.slot_loop_checks.append(loop)
            rl.addWidget(loop, i + 1, 4)

            mode = QComboBox()
            mode.addItem("Normal", "normal")
            mode.addItem("Additive", "additive")
            mode.addItem("Override", "override")
            mode.currentIndexChanged.connect(lambda _v, idx=i: self.apply_slot_mode(idx))
            self.slot_mode_boxes.append(mode)
            rl.addWidget(mode, i + 1, 5)

        ctrl_row = self.SLOT_COUNT + 2
        self.global_speed = QSlider(Qt.Horizontal)
        self.global_speed.setRange(0, 200)
        self.global_speed.setValue(100)
        self.global_speed.valueChanged.connect(self.apply_global_speed)
        self.global_speed_label = QLabel("Global speed: 1.00")
        rl.addWidget(self.global_speed_label, ctrl_row, 0, 1, 2)
        rl.addWidget(self.global_speed, ctrl_row, 2, 1, 3)

        self.btn_export_mixed = QPushButton("Export Mixed GLB")
        self.btn_export_mixed.clicked.connect(self.export_mixed_glb)
        rl.addWidget(self.btn_export_mixed, ctrl_row, 5)

        bl.addWidget(right, 5)

        render_tab = QWidget()
        bottom_tabs.addTab(render_tab, "Render")
        rgl = QGridLayout(render_tab)

        self.grid_check = QCheckBox("Show grid")
        self.grid_check.setChecked(True)
        self.grid_check.toggled.connect(self.toggle_grid)
        rgl.addWidget(self.grid_check, 0, 0)

        self.uniform_light_check = QCheckBox("Uniform lighting")
        self.uniform_light_check.setChecked(True)
        self.uniform_light_check.toggled.connect(self.apply_uniform_mode)
        rgl.addWidget(self.uniform_light_check, 0, 1)

        self.bg_slider = QSlider(Qt.Horizontal)
        self.bg_slider.setRange(0, 255)
        self.bg_slider.setValue(63)
        self.bg_slider.valueChanged.connect(self.apply_bg)
        rgl.addWidget(QLabel("Background"), 1, 0)
        rgl.addWidget(self.bg_slider, 1, 1)

        self.sun_az_slider = QSlider(Qt.Horizontal)
        self.sun_az_slider.setRange(0, 360)
        self.sun_az_slider.setValue(45)
        self.sun_az_slider.valueChanged.connect(self.apply_sun_angles)
        rgl.addWidget(QLabel("Sun azimuth"), 2, 0)
        rgl.addWidget(self.sun_az_slider, 2, 1)

        self.sun_el_slider = QSlider(Qt.Horizontal)
        self.sun_el_slider.setRange(-10, 90)
        self.sun_el_slider.setValue(50)
        self.sun_el_slider.valueChanged.connect(self.apply_sun_angles)
        rgl.addWidget(QLabel("Sun elevation"), 3, 0)
        rgl.addWidget(self.sun_el_slider, 3, 1)

        self.sun_int_slider = QSlider(Qt.Horizontal)
        self.sun_int_slider.setRange(0, 300)
        self.sun_int_slider.setValue(0)
        self.sun_int_slider.valueChanged.connect(self.apply_light_intensity)
        rgl.addWidget(QLabel("Sun intensity"), 4, 0)
        rgl.addWidget(self.sun_int_slider, 4, 1)

        self.amb_int_slider = QSlider(Qt.Horizontal)
        self.amb_int_slider.setRange(0, 300)
        self.amb_int_slider.setValue(0)
        self.amb_int_slider.valueChanged.connect(self.apply_light_intensity)
        rgl.addWidget(QLabel("Ambient intensity"), 5, 0)
        rgl.addWidget(self.amb_int_slider, 5, 1)

        self.uni_int_slider = QSlider(Qt.Horizontal)
        self.uni_int_slider.setRange(0, 300)
        self.uni_int_slider.setValue(300)
        self.uni_int_slider.valueChanged.connect(self.apply_uniform_intensity)
        rgl.addWidget(QLabel("Uniform intensity"), 6, 0)
        rgl.addWidget(self.uni_int_slider, 6, 1)

        tex_tab = QWidget()
        bottom_tabs.addTab(tex_tab, "Textures")
        tgl = QGridLayout(tex_tab)

        self.diff_edit = QLineEdit()

        tgl.addWidget(QLabel("diff"), 0, 0)
        tgl.addWidget(self.diff_edit, 0, 1)
        bd = QPushButton("Browse")
        bd.clicked.connect(lambda: self.pick_texture("diff"))
        ad = QPushButton("Apply")
        ad.clicked.connect(lambda: self.apply_texture("diff"))
        cd = QPushButton("Clear")
        cd.clicked.connect(lambda: self.clear_texture("diff"))
        tgl.addWidget(bd, 0, 2)
        tgl.addWidget(ad, 0, 3)
        tgl.addWidget(cd, 0, 4)

        self.diff_brightness_slider = QSlider(Qt.Horizontal)
        self.diff_brightness_slider.setRange(0, 200)
        self.diff_brightness_slider.setValue(100)
        self.diff_brightness_slider.valueChanged.connect(self.apply_texture_tuning)
        tgl.addWidget(QLabel("diff brightness"), 1, 0)
        tgl.addWidget(self.diff_brightness_slider, 1, 1, 1, 4)

        self.tile_u_slider = QSlider(Qt.Horizontal)
        self.tile_u_slider.setRange(1, 800)
        self.tile_u_slider.setValue(100)
        self.tile_u_slider.valueChanged.connect(self.apply_texture_tuning)
        tgl.addWidget(QLabel("tile U"), 2, 0)
        tgl.addWidget(self.tile_u_slider, 2, 1, 1, 4)

        self.tile_v_slider = QSlider(Qt.Horizontal)
        self.tile_v_slider.setRange(1, 800)
        self.tile_v_slider.setValue(100)
        self.tile_v_slider.valueChanged.connect(self.apply_texture_tuning)
        tgl.addWidget(QLabel("tile V"), 3, 0)
        tgl.addWidget(self.tile_v_slider, 3, 1, 1, 4)

        self.setStatusBar(QStatusBar())

    def _build_menu(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu("File")
        view_menu = menu.addMenu("View")
        help_menu = menu.addMenu("Help")

        open_model = QAction("Open Model", self)
        open_model.triggered.connect(self.pick_model)
        open_main_source = QAction("Open Main Anim Source", self)
        open_main_source.triggered.connect(self.pick_source)
        build = QAction("Build + Load", self)
        build.triggered.connect(self.build_and_load)
        exit_act = QAction("Exit", self)
        exit_act.triggered.connect(self.close)

        file_menu.addAction(open_model)
        file_menu.addAction(open_main_source)
        file_menu.addSeparator()
        file_menu.addAction(build)
        file_menu.addSeparator()
        file_menu.addAction(exit_act)

        reset_cam = QAction("Reset Camera", self)
        reset_cam.triggered.connect(lambda: self.run_js("window.viewerApi && window.viewerApi.resetCamera();"))
        view_menu.addAction(reset_cam)

        about = QAction("About", self)
        about.triggered.connect(
            lambda: QMessageBox.information(
                self,
                "About",
                "STALCRAFT Model Viewer\nSlot-based lazy loading + render/texture controls",
            )
        )
        help_menu.addAction(about)
        self.blender_link.attach_alt_menu(menu, self.export_mixed_glb)

    def pick_model(self) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self,
            "Select model",
            str(Path.home()),
            "Model (*.mcsb *.mcsa *.mcvd *.glb);;All (*.*)",
        )
        if p:
            self.model_edit.setText(p)

    def pick_source(self) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self,
            "Select anim source",
            str(Path.home()),
            "Anim (*.mcal *.glb *.mcvd *.mcsa *.mcsb);;All (*.*)",
        )
        if p:
            self.main_source_edit.setText(p)
            return
        d = QFileDialog.getExistingDirectory(self, "Select folder with .glb", str(Path.home()))
        if d:
            self.main_source_edit.setText(d)

    def run_js(self, code: str, callback=None) -> None:
        if callback is None:
            self.web.page().runJavaScript(code)
        else:
            self.web.page().runJavaScript(code, 0, callback)

    def on_web_loaded(self, ok: bool) -> None:
        if not ok:
            self.statusBar().showMessage("Viewer failed to load")
            return
        self.statusBar().showMessage("Viewer loaded")
        self.blender_link.sync_viewer_state()
        self.apply_global_speed(self.global_speed.value())
        self.apply_bg(self.bg_slider.value())
        self.apply_sun_angles()
        self.apply_light_intensity()
        self.apply_uniform_mode(self.uniform_light_check.isChecked())
        self.apply_uniform_intensity(self.uni_int_slider.value())
        self.apply_texture_tuning()

    def build_and_load(self) -> None:
        try:
            model = Path(self.model_edit.text().strip())
            if not model.exists():
                raise FileNotFoundError("Model not found")

            main_source_text = self.main_source_edit.text().strip()
            if not main_source_text:
                raise FileNotFoundError("Set main anim source")

            main_source = Path(main_source_text) if main_source_text else None
            if main_source is not None and not main_source.exists():
                raise FileNotFoundError(f"Main source not found: {main_source}")

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_root = self.project_root / "build" / ts
            out_root.mkdir(parents=True, exist_ok=False)
            anim_out = out_root / "anims"
            anim_out.mkdir(parents=True, exist_ok=True)
            tex_out = out_root / "textures"
            tex_out.mkdir(parents=True, exist_ok=True)

            model_glb = self.prepare_model_glb(model, out_root)
            self.prepare_source_index("main", main_source, model, anim_out)
            self.build_available_refs()

            manifest = {"title": f"STALCRAFT Desktop Preview {ts}", "model": model_glb.name, "animations": []}
            (out_root / "manifest.js").write_text(
                "window.__MANIFEST = " + json.dumps(manifest, ensure_ascii=True, indent=2) + ";\n",
                encoding="utf-8",
            )

            web_dir = self.project_root / "web"
            shutil.copy2(web_dir / "desktop_viewer.html", out_root / "index.html")
            shutil.copy2(web_dir / "desktop_viewer.js", out_root / "desktop_viewer.js")
            shutil.copy2(web_dir / "desktop_viewer.css", out_root / "desktop_viewer.css")
            if (web_dir / "vendor").exists():
                shutil.copytree(web_dir / "vendor", out_root / "vendor", dirs_exist_ok=True)

            self.current_bundle = out_root
            self.bundle_texture_dir = tex_out
            self.blender_link.register_bundle(out_root)
            self.start_bundle_server(out_root)
            self.populate_available_list()
            self.statusBar().showMessage(f"Bundle ready: {out_root}")
        except Exception as e:
            QMessageBox.critical(self, "Build failed", str(e))
            self.statusBar().showMessage("Build failed")

    def prepare_model_glb(self, model: Path, out_root: Path) -> Path:
        if model.suffix.lower() == ".glb":
            dst = out_root / "model.glb"
            shutil.copy2(model, dst)
            return dst

        if model.suffix.lower() not in {".mcsb", ".mcsa", ".mcvd"}:
            raise ValueError("Model must be .glb/.mcsb/.mcsa/.mcvd")
        if self.scfile_root is None:
            raise RuntimeError("sc-file not found in project (expected ./tools/sc-file-master or ./sc-file-master)")

        conv = out_root / "_model_conv"
        conv.mkdir(parents=True, exist_ok=True)
        run_cmd(
            [
                "py",
                "-m",
                "scfile",
                "--skeleton",
                "--animation",
                "-F",
                "glb",
                "-O",
                str(conv),
                str(model),
            ],
            cwd=self.scfile_root,
        )
        glbs = sorted(conv.rglob("*.glb"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not glbs:
            raise RuntimeError("No GLB from model conversion")
        dst = out_root / "model.glb"
        shutil.copy2(glbs[0], dst)
        return dst

    def prepare_source_index(self, pack_id: str, source: Path | None, model: Path, anim_out: Path) -> None:
        cfg = self.pack_configs[pack_id]
        cfg["source_mode"] = ""
        cfg["source_mcal"] = None
        cfg["source_model_for_remap"] = None
        cfg["source_model_anim"] = None
        cfg["available_names"] = []
        cfg["prepared_anim_map"] = {}
        self.bundle_anim_dir = anim_out
        self.target_model = model

        if source is None:
            return

        if source.suffix.lower() == ".mcal":
            if self.awb_root is None or self.scfile_root is None:
                raise RuntimeError(
                    "MCAL source requires local tools: ./tools/AnimationWorkbench and ./tools/sc-file-master"
                )
            cfg["source_mode"] = "mcal"
            cfg["source_mcal"] = source
            cfg["source_model_for_remap"] = detect_source_model_for_mcal(source)
            cfg["available_names"] = parse_mcal_clips(source)
            return

        if source.suffix.lower() in {".mcvd", ".mcsa", ".mcsb"}:
            if self.awb_root is None or self.scfile_root is None:
                raise RuntimeError(
                    "Model-anim source requires local tools: ./tools/AnimationWorkbench and ./tools/sc-file-master"
                )
            cfg["source_mode"] = "model_anim"
            cfg["source_model_anim"] = source
            cfg["available_names"] = list_model_clips_via_workbench(
                awb_root=self.awb_root,
                scfile_root=self.scfile_root,
                model_path=source,
            )
            return

        if source.is_dir():
            cfg["source_mode"] = "glb_dir"
            glbs = sorted(source.rglob("*.glb"))
            for i, g in enumerate(glbs):
                name = g.stem
                out_name = f"{pack_id}_anim_{i:04d}_{sanitize(name)}.glb"
                shutil.copy2(g, anim_out / out_name)
                cfg["prepared_anim_map"][name.lower()] = f"anims/{out_name}"
                cfg["available_names"].append(name)
            return

        if source.suffix.lower() == ".glb":
            cfg["source_mode"] = "glb_file"
            out_name = f"{pack_id}_anim_0000_{sanitize(source.stem)}.glb"
            shutil.copy2(source, anim_out / out_name)
            cfg["prepared_anim_map"][source.stem.lower()] = f"anims/{out_name}"
            cfg["available_names"] = [source.stem]
            return

        raise ValueError(f"{pack_id}: anim source must be .mcal/.glb/.mcvd/.mcsa/.mcsb or folder")

    def build_available_refs(self) -> None:
        self.available_refs = []
        for name in self.pack_configs["main"]["available_names"]:
            self.available_refs.append((clip_display_name(name), "main", name))

    def populate_available_list(self) -> None:
        self.available_list.clear()
        q = self.clip_search.text().strip().lower()
        for display, _pack, _name in self.available_refs:
            if q and q not in display.lower():
                continue
            self.available_list.addItem(display)
        self.statusBar().showMessage(f"Available names: {self.available_list.count()} / {len(self.available_refs)}")

    def on_slot_dropped(self, idx: int, text: str) -> None:
        self.slot_name_edits[idx].setText(text)
        self.load_slot(idx)

    def resolve_clip_ref(self, clip_ref: str) -> tuple[str, str]:
        ref = clip_ref.strip()
        if not ref:
            raise ValueError("Empty clip name")

        for display, pack_id, raw_name in self.available_refs:
            if ref.lower() == display.lower():
                return pack_id, raw_name

        if ":" in ref:
            pack_part, name_part = ref.split(":", 1)
            p = pack_part.strip().lower()
            n = name_part.strip()
            if p in {"main", "m"}:
                return "main", n
            if p in {"weapon", "w"}:
                raise ValueError("Weapon pack is removed. Use main clip names only.")

        return "main", ref

    def ensure_clip_ready(self, clip_ref: str) -> str:
        pack_id, clip_name = self.resolve_clip_ref(clip_ref)
        cfg = self.pack_configs[pack_id]
        key = clip_name.strip().lower()
        if not key:
            raise ValueError("Empty clip name")

        rel = cfg["prepared_anim_map"].get(key)
        if rel:
            return rel

        if cfg["source_mode"] != "mcal":
            if cfg["source_mode"] == "model_anim":
                if self.awb_root is None or self.scfile_root is None:
                    raise RuntimeError(
                        "Model-anim conversion requires local tools: ./tools/AnimationWorkbench and ./tools/sc-file-master"
                    )
                source_model_anim = cfg["source_model_anim"]
                if source_model_anim is None or self.target_model is None or self.bundle_anim_dir is None:
                    raise RuntimeError(f"{pack_id}: model-anim mode not initialized")
                out_name = f"{pack_id}_lazy_{sanitize(clip_name)}.glb"
                out_path = self.bundle_anim_dir / out_name
                tmp_mcal = self.bundle_anim_dir / f"{pack_id}_tmp_{sanitize(clip_name)}.mcal"

                extract_script = self.awb_root / "model_anim_extract.py"
                run_cmd(
                    [
                        "py",
                        str(extract_script),
                        "extract-one",
                        "--model",
                        str(source_model_anim),
                        "--name",
                        clip_name,
                        "--output",
                        str(tmp_mcal),
                        "--scfile-root",
                        str(self.scfile_root),
                    ]
                )

                convert_script = self.awb_root / "mcal_to_blender_glb.py"
                model_for_convert = self.target_model if is_sc_model(self.target_model) else source_model_anim
                cmd = [
                    "py",
                    str(convert_script),
                    "--model",
                    str(model_for_convert),
                    "--mcal",
                    str(tmp_mcal),
                    "--clip-name",
                    clip_name,
                    "--out",
                    str(out_path),
                    "--scfile-root",
                    str(self.scfile_root),
                ]
                if is_sc_model(self.target_model) and source_model_anim != self.target_model:
                    cmd.extend(["--source-model", str(source_model_anim)])
                self.statusBar().showMessage(f"[{pack_id}] converting model clip: {clip_name}")
                QApplication.processEvents()
                run_cmd(cmd)
                try:
                    tmp_mcal.unlink(missing_ok=True)
                except Exception:
                    pass
                rel = f"anims/{out_name}"
                cfg["prepared_anim_map"][key] = rel
                self.statusBar().showMessage(f"[{pack_id}] ready: {clip_name}")
                return rel
            for k, v in cfg["prepared_anim_map"].items():
                if key == k or key in k:
                    return v
            raise ValueError(f"{pack_id}: clip not found: {clip_name}")

        source_mcal = cfg["source_mcal"]
        if source_mcal is None or self.target_model is None or self.bundle_anim_dir is None:
            raise RuntimeError(f"{pack_id}: MCAL mode not initialized")
        if self.awb_root is None or self.scfile_root is None:
            raise RuntimeError(
                "MCAL conversion requires local tools: ./tools/AnimationWorkbench and ./tools/sc-file-master"
            )

        out_name = f"{pack_id}_lazy_{sanitize(clip_name)}.glb"
        out_path = self.bundle_anim_dir / out_name
        script = self.awb_root / "mcal_to_blender_glb.py"
        model_for_convert = self.target_model
        if not is_sc_model(model_for_convert):
            model_for_convert = cfg["source_model_for_remap"]
            if not is_sc_model(model_for_convert):
                raise RuntimeError(
                    f"{pack_id}: target model is {self.target_model.suffix.lower()} and no source .mcsb/.mcsa/.mcvd "
                    f"was found for MCAL conversion"
                )
        cmd = [
            "py",
            str(script),
            "--model",
            str(model_for_convert),
            "--mcal",
            str(source_mcal),
            "--clip-name",
            clip_name,
            "--out",
            str(out_path),
            "--scfile-root",
            str(self.scfile_root),
        ]
        if is_sc_model(self.target_model) and cfg["source_model_for_remap"] is not None:
            cmd.extend(["--source-model", str(cfg["source_model_for_remap"])])

        self.statusBar().showMessage(f"[{pack_id}] converting: {clip_name}")
        QApplication.processEvents()
        run_cmd(cmd)
        rel = f"anims/{out_name}"
        cfg["prepared_anim_map"][key] = rel
        self.statusBar().showMessage(f"[{pack_id}] ready: {clip_name}")
        return rel

    def load_slot(self, idx: int) -> None:
        try:
            clip_name = self.slot_name_edits[idx].text().strip()
            if not clip_name:
                raise ValueError("Set clip name in slot first")
            rel = self.ensure_clip_ready(clip_name)
            label = clip_name.replace("'", "")
            self.run_js(f"window.viewerApi && window.viewerApi.setSlot({idx}, '{rel}', '{label}');")
            self.apply_slot_all(idx)
            self.statusBar().showMessage(f"Slot {idx} active: {clip_name}")
        except Exception as e:
            QMessageBox.warning(self, "Slot load failed", str(e))

    def unload_slot(self, idx: int) -> None:
        self.run_js(f"window.viewerApi && window.viewerApi.removeSlot({idx});")
        self.slot_name_edits[idx].clear()
        self.statusBar().showMessage(f"Slot {idx} unloaded")

    def apply_slot_all(self, idx: int) -> None:
        self.apply_slot_loop(idx)
        self.apply_slot_weight(idx)
        self.apply_slot_mode(idx)

    def apply_slot_loop(self, idx: int) -> None:
        v = self.slot_loop_checks[idx].isChecked()
        self.run_js(f"window.viewerApi && window.viewerApi.setSlotLoop({idx}, {str(v).lower()});")

    def apply_slot_weight(self, idx: int) -> None:
        v = float(self.slot_weight_sliders[idx].value()) / 100.0
        self.slot_weight_labels[idx].setText(f"{v:.2f}")
        self.run_js(f"window.viewerApi && window.viewerApi.setSlotWeight({idx}, {v});")

    def apply_slot_mode(self, idx: int) -> None:
        mode = str(self.slot_mode_boxes[idx].currentData() or "normal")
        self.run_js(f"window.viewerApi && window.viewerApi.setSlotMode({idx}, '{mode}');")

    def export_mixed_glb(self) -> None:
        try:
            self.blender_link.export_mixed_glb()
        except Exception as e:
            QMessageBox.warning(self, "Export mixed GLB", str(e))

    def on_download_requested(self, item) -> None:
        try:
            suggested = item.downloadFileName() or "mixed_loop.glb"
            if not suggested.lower().endswith(".glb"):
                suggested = "mixed_loop.glb"
            default_dir = Path.home() / "Desktop"
            out_path_str, _ = QFileDialog.getSaveFileName(
                self,
                "Save exported GLB",
                str((default_dir / suggested).resolve()),
                "GLB (*.glb)",
            )
            if not out_path_str:
                item.cancel()
                self.statusBar().showMessage("Export canceled")
                return
            out_path = Path(out_path_str).resolve()
            item.setDownloadDirectory(str(out_path.parent))
            item.setDownloadFileName(out_path.name)
            item.accept()
            self.statusBar().showMessage(f"Saving export: {out_path}")
        except Exception as e:
            item.cancel()
            QMessageBox.warning(self, "Export download failed", str(e))

    def apply_global_speed(self, value: int) -> None:
        v = float(value) / 100.0
        self.global_speed_label.setText(f"Global speed: {v:.2f}")
        self.run_js(f"window.viewerApi && window.viewerApi.setGlobalSpeed({v});")

    def toggle_grid(self, checked: bool) -> None:
        self.run_js(f"window.viewerApi && window.viewerApi.setGridVisible({str(bool(checked)).lower()});")

    def apply_bg(self, value: int) -> None:
        self.run_js(f"window.viewerApi && window.viewerApi.setBackgroundGray({int(value)});")

    def apply_sun_angles(self) -> None:
        az = int(self.sun_az_slider.value())
        el = int(self.sun_el_slider.value())
        self.run_js(f"window.viewerApi && window.viewerApi.setSunAngles({az}, {el});")

    def apply_light_intensity(self) -> None:
        sun = float(self.sun_int_slider.value()) / 100.0
        amb = float(self.amb_int_slider.value()) / 100.0
        self.run_js(f"window.viewerApi && window.viewerApi.setSunIntensity({sun});")
        self.run_js(f"window.viewerApi && window.viewerApi.setAmbientIntensity({amb});")

    def apply_uniform_mode(self, checked: bool) -> None:
        self.run_js(f"window.viewerApi && window.viewerApi.setUniformLighting({str(bool(checked)).lower()});")

    def apply_uniform_intensity(self, value: int) -> None:
        v = float(value) / 100.0
        self.run_js(f"window.viewerApi && window.viewerApi.setUniformIntensity({v});")

    def pick_texture(self, kind: str) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self,
            f"Select {kind} texture",
            str(Path.home()),
            "Texture (*.ol *.png *.jpg *.jpeg *.dds *.tga *.bmp *.webp);;All (*.*)",
        )
        if not p:
            return
        if kind != "diff":
            return
        self.diff_edit.setText(p)

    def _texture_edit(self, kind: str) -> QLineEdit:
        if kind != "diff":
            raise ValueError("Only diff texture is supported")
        return self.diff_edit

    def _prepare_texture_source(self, src: Path) -> Path:
        if src.suffix.lower() != ".ol":
            return src
        if self.bundle_texture_dir is None:
            raise RuntimeError("Build + Load first")
        if self.scfile_root is None:
            raise RuntimeError("OL texture decode requires local sc-file tool: ./tools/sc-file-master")

        conv_dir = self.bundle_texture_dir / "_ol_conv"
        conv_dir.mkdir(parents=True, exist_ok=True)
        run_cmd(
            [
                "py",
                "-m",
                "scfile",
                "-O",
                str(conv_dir),
                str(src),
            ],
            cwd=self.scfile_root,
        )
        candidates = sorted(
            [p for p in conv_dir.glob(f"{src.stem}.*") if p.suffix.lower() in {".dds", ".png", ".jpg", ".jpeg"}],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise RuntimeError(f"Failed to decode OL texture: {src}")
        return candidates[0]

    def apply_texture(self, kind: str) -> None:
        try:
            if self.bundle_texture_dir is None:
                raise RuntimeError("Build + Load first")
            src = Path(self._texture_edit(kind).text().strip())
            if not src.exists():
                raise FileNotFoundError(f"Texture not found: {src}")
            prepared_src = self._prepare_texture_source(src)
            dst_name = f"{kind}_{sanitize(src.stem)}_{int(time.time()*1000)}{prepared_src.suffix.lower()}"
            dst = self.bundle_texture_dir / dst_name
            shutil.copy2(prepared_src, dst)
            rel = f"textures/{dst_name}"
            self.run_js(f"window.viewerApi && window.viewerApi.setTextureMap('{kind}', '{rel}');")
            self.statusBar().showMessage(f"Applied {kind}: {src.name}")
            self.apply_texture_tuning()
        except Exception as e:
            QMessageBox.warning(self, "Texture apply failed", str(e))

    def clear_texture(self, kind: str) -> None:
        self.run_js(f"window.viewerApi && window.viewerApi.clearTextureMap('{kind}');")
        self.statusBar().showMessage(f"Cleared {kind}")

    def apply_texture_tuning(self) -> None:
        diff_b = float(self.diff_brightness_slider.value()) / 100.0
        tile_u = float(self.tile_u_slider.value()) / 100.0
        tile_v = float(self.tile_v_slider.value()) / 100.0
        self.run_js(f"window.viewerApi && window.viewerApi.setDiffBrightness({diff_b});")
        self.run_js(f"window.viewerApi && window.viewerApi.setTextureTiling({tile_u}, {tile_v});")

    def start_bundle_server(self, bundle_dir: Path) -> None:
        self.stop_bundle_server()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
        sock.close()
        self.server_proc = subprocess.Popen(
            ["py", "-m", "http.server", str(port), "--bind", "127.0.0.1"], cwd=str(bundle_dir)
        )
        self.web.setUrl(QUrl(f"http://127.0.0.1:{port}/index.html"))

    def stop_bundle_server(self) -> None:
        if self.server_proc is None:
            return
        try:
            self.server_proc.terminate()
            self.server_proc.wait(timeout=2)
        except Exception:
            try:
                self.server_proc.kill()
            except Exception:
                pass
        self.server_proc = None

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.stop_bundle_server()
        super().closeEvent(event)


def main() -> int:
    app = QApplication([])
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
