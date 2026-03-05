from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction, QDesktopServices


class BlenderLinkModule:
    def __init__(
        self,
        project_root: Path,
        show_status: Callable[[str], None],
        show_error: Callable[[str, str], None],
        run_js: Callable[[str], None],
    ) -> None:
        self.project_root = project_root.resolve()
        self.show_status = show_status
        self.show_error = show_error
        self.run_js = run_js
        self.current_bundle: Path | None = None
        self.live_link_path: Path | None = None
        self.model_live_name = "model_live.glb"
        self.enabled = False
        self.enable_action: QAction | None = None

    def attach_alt_menu(self, menu_bar, export_callback: Callable[[], None]) -> None:
        alt_menu = menu_bar.addMenu("&ALT")

        self.enable_action = QAction("Enable BlenderLink", alt_menu)
        self.enable_action.setCheckable(True)
        self.enable_action.setChecked(self.enabled)
        self.enable_action.toggled.connect(self.set_enabled)
        alt_menu.addAction(self.enable_action)

        export_action = QAction("Export Mixed GLB", alt_menu)
        export_action.triggered.connect(export_callback)
        alt_menu.addAction(export_action)

        open_bundle = QAction("Open Active Bundle Folder", alt_menu)
        open_bundle.triggered.connect(self.open_active_bundle_folder)
        alt_menu.addAction(open_bundle)

    def register_bundle(self, bundle_dir: Path) -> None:
        self.current_bundle = bundle_dir.resolve()
        self.live_link_path = self.current_bundle / "live_link.json"

        self._write_live_link_payload(
            {
                "enabled": bool(self.enabled),
                "revision": 0,
                "model": self.model_live_name,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        self._write_active_bundle_info()
        self.sync_viewer_state()
        self.show_status("BlenderLink ready")

    def export_mixed_glb(self) -> None:
        self.show_status("Exporting mixed GLB from viewer (60 Hz bake)...")
        self.run_js("window.viewerApi && window.viewerApi.exportMixedLoopGlb(60);")

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if self.enable_action is not None and self.enable_action.isChecked() != self.enabled:
            self.enable_action.blockSignals(True)
            self.enable_action.setChecked(self.enabled)
            self.enable_action.blockSignals(False)

        payload = self._load_live_link_payload()
        payload["enabled"] = self.enabled
        payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._write_live_link_payload(payload)
        self.sync_viewer_state()
        state = "enabled" if self.enabled else "disabled"
        self.show_status(f"BlenderLink {state}")

    def sync_viewer_state(self) -> None:
        self.run_js(
            f"window.viewerApi && window.viewerApi.setLiveLinkEnabled({str(bool(self.enabled)).lower()});"
        )

    def open_active_bundle_folder(self) -> None:
        if self.current_bundle is None:
            self.show_error("BlenderLink", "Build + Load first")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.current_bundle)))

    def _load_live_link_payload(self) -> dict:
        if self.live_link_path is None or not self.live_link_path.exists():
            return {
                "enabled": bool(self.enabled),
                "revision": 0,
                "model": self.model_live_name,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        try:
            data = json.loads(self.live_link_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {
            "enabled": bool(self.enabled),
            "revision": 0,
            "model": self.model_live_name,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _write_live_link_payload(self, payload: dict) -> None:
        if self.live_link_path is None:
            return
        self.live_link_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(self.live_link_path, payload)

    def _write_active_bundle_info(self) -> None:
        if self.current_bundle is None or self.live_link_path is None:
            return
        self._write_json_atomic(
            self.project_root / "active_bundle.json",
            {
                "bundle_path": str(self.current_bundle),
                "live_link_path": str(self.live_link_path),
                "model_live_name": self.model_live_name,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
        )

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        tmp.replace(path)
