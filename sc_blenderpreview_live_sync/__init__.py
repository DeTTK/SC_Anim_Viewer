bl_info = {
    "name": "SC BlenderPreview Live Sync",
    "author": "Codex",
    "version": (1, 0, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > SC Live",
    "description": "Live export edited rig/mesh to BlenderPreview bundle",
    "category": "Import-Export",
}

import json
import time
from datetime import datetime
from pathlib import Path

import bpy


class _LiveState:
    running = False
    dirty = False
    last_export_at = 0.0
    last_change_at = 0.0
    last_bundle = ""
    last_error = ""


STATE = _LiveState()
HANDLER_REF = None


def _set_status(scene: bpy.types.Scene, text: str) -> None:
    scene.sc_live_status = text


def _preview_root(scene: bpy.types.Scene) -> Path:
    return Path(bpy.path.abspath(scene.sc_live_preview_root)).expanduser().resolve()


def _active_bundle_info(scene: bpy.types.Scene) -> tuple[Path, Path, str]:
    root = _preview_root(scene)
    active_path = root / "active_bundle.json"
    if not active_path.exists():
        raise FileNotFoundError(f"active_bundle.json not found: {active_path}")
    data = json.loads(active_path.read_text(encoding="utf-8"))
    bundle_path = Path(str(data.get("bundle_path", ""))).expanduser().resolve()
    if not bundle_path.exists():
        raise FileNotFoundError(f"Active bundle not found: {bundle_path}")
    live_link_path = bundle_path / "live_link.json"
    model_name = str(data.get("model_live_name", "model_live.glb")).strip() or "model_live.glb"
    return bundle_path, live_link_path, model_name


def _next_revision(live_link_path: Path) -> int:
    if not live_link_path.exists():
        return 1
    try:
        data = json.loads(live_link_path.read_text(encoding="utf-8"))
        rev = int(data.get("revision", 0))
        return max(0, rev) + 1
    except Exception:
        return 1


def _write_live_link(live_link_path: Path, revision: int, model_name: str) -> None:
    payload = {
        "enabled": True,
        "revision": int(revision),
        "model": model_name,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    tmp = live_link_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    tmp.replace(live_link_path)


def _snapshot_ui_state() -> dict:
    ctx = bpy.context
    view_layer = ctx.view_layer
    active = view_layer.objects.active
    selected = [obj.name for obj in ctx.selected_objects]
    return {
        "active_name": active.name if active else "",
        "selected_names": selected,
        "mode": str(ctx.mode or "OBJECT"),
    }


def _mode_set_name(mode: str) -> str | None:
    mapping = {
        "OBJECT": "OBJECT",
        "EDIT_MESH": "EDIT",
        "EDIT_ARMATURE": "EDIT",
        "EDIT_CURVE": "EDIT",
        "POSE": "POSE",
        "PAINT_WEIGHT": "WEIGHT_PAINT",
        "PAINT_VERTEX": "VERTEX_PAINT",
        "PAINT_TEXTURE": "TEXTURE_PAINT",
        "SCULPT": "SCULPT",
    }
    return mapping.get(mode)


def _restore_ui_state(snapshot: dict) -> None:
    view_layer = bpy.context.view_layer
    active_name = str(snapshot.get("active_name") or "")
    selected_names = [str(x) for x in snapshot.get("selected_names", [])]
    mode_name = _mode_set_name(str(snapshot.get("mode") or "OBJECT"))

    active_obj = bpy.data.objects.get(active_name) if active_name else None
    if active_obj is not None:
        view_layer.objects.active = active_obj

    for obj in bpy.context.selected_objects:
        try:
            obj.select_set(False)
        except Exception:
            pass
    for name in selected_names:
        obj = bpy.data.objects.get(name)
        if obj is not None:
            try:
                obj.select_set(True)
            except Exception:
                pass

    if active_obj is not None:
        view_layer.objects.active = active_obj
    if mode_name and active_obj is not None:
        try:
            bpy.ops.object.mode_set(mode=mode_name)
        except Exception:
            pass


def _export_live_model(scene: bpy.types.Scene) -> str:
    bundle_path, live_link_path, model_name = _active_bundle_info(scene)
    out_path = bundle_path / model_name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = _snapshot_ui_state()
    try:
        result = bpy.ops.export_scene.gltf(
            filepath=str(out_path),
            check_existing=False,
            export_format="GLB",
            use_selection=bool(scene.sc_live_use_selection),
        )
        if "FINISHED" not in result:
            raise RuntimeError(f"GLTF export failed: {result}")
    finally:
        _restore_ui_state(snapshot)

    revision = _next_revision(live_link_path)
    _write_live_link(live_link_path, revision, model_name)
    STATE.last_bundle = str(bundle_path)
    STATE.last_export_at = time.monotonic()
    return f"Exported rev {revision} -> {out_path.name}"


def _depsgraph_is_relevant(upd_id) -> bool:
    return isinstance(
        upd_id,
        (
            bpy.types.Object,
            bpy.types.Mesh,
            bpy.types.Armature,
            bpy.types.Curve,
            bpy.types.Collection,
        ),
    )


def _depsgraph_handler(scene: bpy.types.Scene, depsgraph: bpy.types.Depsgraph) -> None:
    if not STATE.running:
        return
    for update in depsgraph.updates:
        if _depsgraph_is_relevant(update.id):
            STATE.dirty = True
            STATE.last_change_at = time.monotonic()
            break


def _timer_tick():
    if not STATE.running:
        return None
    scene = bpy.context.scene
    if scene is None:
        return 0.3

    interval = max(0.1, float(scene.sc_live_interval))
    idle_delay = max(0.0, float(scene.sc_live_idle_delay))
    now = time.monotonic()
    changed_ago = now - STATE.last_change_at
    if STATE.dirty and changed_ago >= idle_delay and (now - STATE.last_export_at) >= interval:
        try:
            msg = _export_live_model(scene)
            _set_status(scene, msg)
            STATE.last_error = ""
            STATE.dirty = False
        except Exception as exc:
            STATE.last_error = str(exc)
            _set_status(scene, f"Error: {exc}")
            STATE.last_export_at = now
    return max(0.08, interval * 0.5)


def _start_sync(scene: bpy.types.Scene) -> None:
    global HANDLER_REF
    STATE.running = True
    STATE.dirty = True
    STATE.last_error = ""
    STATE.last_export_at = 0.0
    STATE.last_change_at = time.monotonic()
    HANDLER_REF = _depsgraph_handler
    if HANDLER_REF not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(HANDLER_REF)
    if not bpy.app.timers.is_registered(_timer_tick):
        bpy.app.timers.register(_timer_tick, first_interval=0.1, persistent=True)
    _set_status(scene, "Live sync started")


def _stop_sync(scene: bpy.types.Scene) -> None:
    global HANDLER_REF
    STATE.running = False
    STATE.dirty = False
    if HANDLER_REF and HANDLER_REF in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(HANDLER_REF)
    HANDLER_REF = None
    _set_status(scene, "Live sync stopped")


class SC_LIVE_OT_start(bpy.types.Operator):
    bl_idname = "sc_live.start"
    bl_label = "Start Live Sync"
    bl_description = "Start automatic live export to BlenderPreview"

    def execute(self, context: bpy.types.Context):
        try:
            _active_bundle_info(context.scene)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            _set_status(context.scene, f"Error: {exc}")
            return {"CANCELLED"}
        _start_sync(context.scene)
        return {"FINISHED"}


class SC_LIVE_OT_stop(bpy.types.Operator):
    bl_idname = "sc_live.stop"
    bl_label = "Stop Live Sync"
    bl_description = "Stop automatic live export"

    def execute(self, context: bpy.types.Context):
        _stop_sync(context.scene)
        return {"FINISHED"}


class SC_LIVE_OT_push_now(bpy.types.Operator):
    bl_idname = "sc_live.push_now"
    bl_label = "Push Now"
    bl_description = "Export model immediately and notify BlenderPreview"

    def execute(self, context: bpy.types.Context):
        try:
            msg = _export_live_model(context.scene)
            _set_status(context.scene, msg)
            self.report({"INFO"}, msg)
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            _set_status(context.scene, f"Error: {exc}")
            return {"CANCELLED"}


class SC_LIVE_PT_panel(bpy.types.Panel):
    bl_idname = "SC_LIVE_PT_panel"
    bl_label = "SC Live Preview"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SC Live"

    def draw(self, context: bpy.types.Context):
        scene = context.scene
        layout = self.layout

        col = layout.column(align=True)
        col.prop(scene, "sc_live_preview_root")
        col.prop(scene, "sc_live_interval")
        col.prop(scene, "sc_live_idle_delay")
        col.prop(scene, "sc_live_use_selection")

        row = layout.row(align=True)
        row.operator("sc_live.start", icon="PLAY")
        row.operator("sc_live.stop", icon="PAUSE")
        layout.operator("sc_live.push_now", icon="EXPORT")

        layout.separator()
        layout.label(text=f"Running: {'YES' if STATE.running else 'NO'}")
        if STATE.last_bundle:
            layout.label(text=f"Bundle: {Path(STATE.last_bundle).name}")
        layout.label(text=f"Status: {scene.sc_live_status}")


CLASSES = (
    SC_LIVE_OT_start,
    SC_LIVE_OT_stop,
    SC_LIVE_OT_push_now,
    SC_LIVE_PT_panel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sc_live_preview_root = bpy.props.StringProperty(
        name="Preview Root",
        description="Folder with BlenderPreview desktop app",
        subtype="DIR_PATH",
        default="//",
    )
    bpy.types.Scene.sc_live_interval = bpy.props.FloatProperty(
        name="Sync Interval (s)",
        description="Minimum delay between auto-exports",
        default=0.35,
        min=0.1,
        max=10.0,
    )
    bpy.types.Scene.sc_live_idle_delay = bpy.props.FloatProperty(
        name="Idle Delay (s)",
        description="Export only after no edits for this delay",
        default=1.2,
        min=0.0,
        max=30.0,
    )
    bpy.types.Scene.sc_live_use_selection = bpy.props.BoolProperty(
        name="Only Selected",
        description="Export only selected objects",
        default=False,
    )
    bpy.types.Scene.sc_live_status = bpy.props.StringProperty(
        name="Status",
        default="Idle",
    )


def unregister():
    scene = bpy.context.scene
    if scene is not None:
        _stop_sync(scene)
    if bpy.app.timers.is_registered(_timer_tick):
        bpy.app.timers.unregister(_timer_tick)
    del bpy.types.Scene.sc_live_status
    del bpy.types.Scene.sc_live_use_selection
    del bpy.types.Scene.sc_live_interval
    del bpy.types.Scene.sc_live_idle_delay
    del bpy.types.Scene.sc_live_preview_root
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
