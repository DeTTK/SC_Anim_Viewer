from __future__ import annotations

import argparse
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from .mcal_inspect import inspect_mcal_file


class McalInspectGui:
    def __init__(self, root: tk.Tk, initial_path: Path | None = None) -> None:
        self.root = root
        self.root.title("MCAL Inspect GUI")
        self.root.geometry("1200x760")
        self.root.minsize(980, 620)

        self.root_dir = tk.StringVar(value=str(initial_path or Path.cwd()))
        self.status_var = tk.StringVar(value="Select a .mcal file to inspect.")
        self.path_var = tk.StringVar(value="-")
        self.kind_var = tk.StringVar(value="-")
        self.magic_var = tk.StringVar(value="-")
        self.version_var = tk.StringVar(value="-")
        self.size_var = tk.StringVar(value="-")

        self._tree_path_map: dict[str, Path] = {}

        self._build_ui()
        self.load_root()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(outer)
        top.pack(fill=tk.X)
        ttk.Label(top, text="Root:").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.root_dir).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        ttk.Button(top, text="Browse", command=self.choose_root).pack(side=tk.LEFT)
        ttk.Button(top, text="Load", command=self.load_root).pack(side=tk.LEFT, padx=(8, 0))

        panes = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        left = ttk.Labelframe(panes, text="Folders and .mcal/.mcvd files", padding=8)
        right = ttk.Labelframe(panes, text="Inspector", padding=8)
        panes.add(left, weight=2)
        panes.add(right, weight=3)

        self.tree = ttk.Treeview(left, columns=("kind",), show="tree headings")
        self.tree.heading("#0", text="Name")
        self.tree.heading("kind", text="Type")
        self.tree.column("#0", width=360, stretch=True)
        self.tree.column("kind", width=90, stretch=False, anchor=tk.W)

        ybar = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ybar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ybar.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<<TreeviewOpen>>", self.on_open_node)
        self.tree.bind("<<TreeviewSelect>>", self.on_select_node)

        meta = ttk.Frame(right)
        meta.pack(fill=tk.X)
        self._meta_row(meta, 0, "Path", self.path_var)
        self._meta_row(meta, 1, "Type", self.kind_var)
        self._meta_row(meta, 2, "Magic", self.magic_var)
        self._meta_row(meta, 3, "Version", self.version_var)
        self._meta_row(meta, 4, "Size", self.size_var)

        list_wrap = ttk.Frame(right)
        list_wrap.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        ttk.Label(list_wrap, text="Clips / strings").pack(anchor=tk.W)

        self.clip_list = tk.Listbox(list_wrap)
        clip_ybar = ttk.Scrollbar(list_wrap, orient=tk.VERTICAL, command=self.clip_list.yview)
        self.clip_list.configure(yscrollcommand=clip_ybar.set)
        self.clip_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        clip_ybar.pack(side=tk.RIGHT, fill=tk.Y)

        footer = ttk.Frame(outer, padding=(0, 8, 0, 0))
        footer.pack(fill=tk.X)
        ttk.Label(footer, textvariable=self.status_var).pack(anchor=tk.W)

    def _meta_row(self, parent: ttk.Frame, row: int, label: str, value_var: tk.StringVar) -> None:
        ttk.Label(parent, text=f"{label}:").grid(row=row, column=0, sticky=tk.W, pady=2)
        ttk.Label(parent, textvariable=value_var).grid(row=row, column=1, sticky=tk.W, padx=(8, 0), pady=2)
        parent.columnconfigure(1, weight=1)

    def choose_root(self) -> None:
        path = filedialog.askdirectory(title="Select root directory")
        if path:
            self.root_dir.set(path)
            self.load_root()

    def load_root(self) -> None:
        root_path = Path(self.root_dir.get().strip()).expanduser()
        if not root_path.exists():
            self.status_var.set("Root path does not exist.")
            return
        self.tree.delete(*self.tree.get_children())
        self._tree_path_map.clear()
        root_id = self.tree.insert("", tk.END, text=root_path.name or str(root_path), values=("dir",), open=True)
        self._tree_path_map[root_id] = root_path
        self._add_dummy(root_id)
        self._populate_node(root_id)
        self.status_var.set(f"Loaded: {root_path.as_posix()}")

    def _add_dummy(self, node_id: str) -> None:
        self.tree.insert(node_id, tk.END, text="...", values=(""), tags=("dummy",))

    def _populate_node(self, node_id: str) -> None:
        path = self._tree_path_map.get(node_id)
        if not path or not path.is_dir():
            return

        for child in self.tree.get_children(node_id):
            self._drop_tree_mapping_recursive(child)
            self.tree.delete(child)

        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as err:
            self.status_var.set(f"Cannot read '{path}': {err}")
            return

        for entry in entries:
            if entry.is_dir():
                child_id = self.tree.insert(node_id, tk.END, text=entry.name, values=("dir",))
                self._tree_path_map[child_id] = entry
                self._add_dummy(child_id)
            elif entry.suffix.lower() in {".mcal", ".mcvd"}:
                child_id = self.tree.insert(node_id, tk.END, text=entry.name, values=(entry.suffix.lower().lstrip("."),))
                self._tree_path_map[child_id] = entry

    def _drop_tree_mapping_recursive(self, node_id: str) -> None:
        for child in self.tree.get_children(node_id):
            self._drop_tree_mapping_recursive(child)
        self._tree_path_map.pop(node_id, None)

    def on_open_node(self, _event: tk.Event) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        self._populate_node(selected[0])

    def on_select_node(self, _event: tk.Event) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        node_id = selected[0]
        path = self._tree_path_map.get(node_id)
        if not path:
            return
        if path.is_dir():
            self._populate_node(node_id)
            self.status_var.set(f"Directory: {path.as_posix()}")
            return

        if path.suffix.lower() not in {".mcal", ".mcvd"}:
            return

        try:
            report = inspect_mcal_file(path, max_strings=400)
        except Exception as err:
            self.status_var.set(f"Failed to parse file: {err}")
            return

        self.path_var.set(report.path)
        self.kind_var.set(report.kind)
        self.magic_var.set(report.magic)
        self.version_var.set(str(report.version))
        self.size_var.set(f"{report.size} bytes")
        self.clip_list.delete(0, tk.END)
        for row in report.strings:
            suffix_parts: list[str] = []
            if row.frame_count is not None:
                suffix_parts.append(f"frames={row.frame_count}")
            if row.sample_step is not None:
                suffix_parts.append(f"step={row.sample_step:.6f}")
            suffix = f"  ({', '.join(suffix_parts)})" if suffix_parts else ""
            self.clip_list.insert(tk.END, f"@{row.offset:08d}  {row.value}{suffix}")
        self.status_var.set(f"Parsed: {path.name}. Strings: {len(report.strings)}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mcal-inspect-gui", description="GUI inspector for MCAL/MCVD files.")
    parser.add_argument("path", nargs="?", default=".", help="Initial root directory.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = tk.Tk()
    app = McalInspectGui(root, initial_path=Path(args.path))
    _ = app
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
