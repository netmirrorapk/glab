"""Reference Library dialog — manage @character / @location reference photos
and the global defaults used for image character/location consistency.

Opened from the main window. All persistence goes through db_manager
(reference_library table + app_settings default_character/default_location).
"""

import json
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QComboBox, QPushButton, QListWidget, QListWidgetItem, QFileDialog,
    QMessageBox, QFrame, QWidget,
)

from src.db import db_manager

_IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"


def parse_label_file(path):
    """Parse a G-Labs label file (exported from Prompt Studio) into a list of
    (name, category) tuples. Accepts JSON ({"references":[...]} or a bare list of
    strings/objects) or a plain text file with one @handle / name per line."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read().strip()
    items = []
    data = None
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if data is not None:
        seq = data.get("references") if isinstance(data, dict) else data
        if isinstance(seq, list):
            for it in seq:
                if isinstance(it, str):
                    items.append((it, "character"))
                elif isinstance(it, dict):
                    items.append((it.get("name") or it.get("handle") or "",
                                  it.get("category") or "character"))
    else:
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                # allow "name | category" or just a name/@handle
                if "|" in s:
                    nm, cat = s.split("|", 1)
                    items.append((nm.strip(), cat.strip()))
                else:
                    items.append((s, "character"))
    out = []
    for nm, cat in items:
        nm = str(nm or "").lstrip("@").strip()
        cat = str(cat or "character").strip().lower()
        if cat not in ("character", "location"):
            cat = "character"
        if nm:
            out.append((nm, cat))
    return out


class ReferenceLibraryDialog(QDialog):
    """Add/remove named character & location references, and pick the global
    defaults that apply to prompt lines without an explicit @tag."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Reference Library — Characters & Locations")
        self.setMinimumWidth(560)
        self._picked_photo_path = ""

        root = QVBoxLayout(self)
        root.setSpacing(12)

        intro = QLabel(
            "Add a character or location photo and give it a short tag name.\n"
            "Use it in prompts like:  @fox @cafe | the fox drinking coffee\n"
            "Import labels from Prompt Studio, then just add a photo to each. "
            "Click a label below to load its name, then choose a photo."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        root.addWidget(self._make_add_section())
        root.addWidget(self._hline())
        root.addWidget(QLabel("Saved references"))
        self.list_widget = QListWidget()
        self.list_widget.setMinimumHeight(160)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        root.addWidget(self.list_widget)

        del_row = QHBoxLayout()
        self.btn_import = QPushButton("Import labels…")
        self.btn_import.clicked.connect(self._on_import)
        del_row.addWidget(self.btn_import)
        del_row.addStretch(1)
        self.btn_delete = QPushButton("Delete selected")
        self.btn_delete.clicked.connect(self._on_delete)
        del_row.addWidget(self.btn_delete)
        self.btn_delete_all = QPushButton("Delete all")
        self.btn_delete_all.clicked.connect(self._on_delete_all)
        del_row.addWidget(self.btn_delete_all)
        root.addLayout(del_row)

        root.addWidget(self._hline())
        root.addWidget(self._make_defaults_section())

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        close_row.addWidget(btn_close)
        root.addLayout(close_row)

        self._reload()

    # ── UI builders ────────────────────────────────────────────────

    def _hline(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def _make_add_section(self):
        box = QWidget()
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)

        grid.addWidget(QLabel("Tag name"), 0, 0)
        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("e.g. fox  (used as @fox)")
        grid.addWidget(self.edit_name, 0, 1)

        grid.addWidget(QLabel("Category"), 0, 2)
        self.combo_category = QComboBox()
        self.combo_category.addItems(["character", "location"])
        grid.addWidget(self.combo_category, 0, 3)

        grid.addWidget(QLabel("Photo"), 1, 0)
        self.lbl_photo = QLabel("No file selected")
        self.lbl_photo.setStyleSheet("color: gray;")
        grid.addWidget(self.lbl_photo, 1, 1)
        btn_pick = QPushButton("Choose photo…")
        btn_pick.clicked.connect(self._on_pick_photo)
        grid.addWidget(btn_pick, 1, 2)
        self.btn_add = QPushButton("Add / Update")
        self.btn_add.clicked.connect(self._on_add)
        grid.addWidget(self.btn_add, 1, 3)

        return box

    def _make_defaults_section(self):
        box = QWidget()
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)

        grid.addWidget(QLabel("Global defaults (used when a line has no tag)"), 0, 0, 1, 4)

        grid.addWidget(QLabel("Default character"), 1, 0)
        self.combo_def_char = QComboBox()
        grid.addWidget(self.combo_def_char, 1, 1)

        grid.addWidget(QLabel("Default location"), 1, 2)
        self.combo_def_loc = QComboBox()
        grid.addWidget(self.combo_def_loc, 1, 3)

        btn_save = QPushButton("Save defaults")
        btn_save.clicked.connect(self._on_save_defaults)
        grid.addWidget(btn_save, 2, 3)

        return box

    # ── Data ───────────────────────────────────────────────────────

    def _reload(self):
        """Refresh the list and the default combo boxes from the DB."""
        try:
            refs = db_manager.get_references()
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Could not load references:\n{exc}")
            return

        self.list_widget.clear()
        for r in refs:
            path = r.get("photo_path") or ""
            has_photo = bool(path) and os.path.exists(path)
            fname = os.path.basename(path) if path else ""
            if not has_photo:
                fname = "⚠ needs photo — click to add"
            item = QListWidgetItem(f"@{r['name']}   [{r['category']}]   —   {fname}")
            item.setData(Qt.UserRole, r["name"])
            if not has_photo:
                item.setForeground(Qt.yellow)
            self.list_widget.addItem(item)

        chars = [r["name"] for r in refs if r["category"] == "character"]
        locs = [r["name"] for r in refs if r["category"] == "location"]
        self._fill_default_combo(self.combo_def_char, chars, "default_character")
        self._fill_default_combo(self.combo_def_loc, locs, "default_location")

    def _fill_default_combo(self, combo, names, setting_key):
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("(none)", "")
        for n in names:
            combo.addItem("@" + n, n)
        current = str(db_manager.get_setting(setting_key, "") or "").strip()
        idx = combo.findData(current) if current else 0
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    # ── Handlers ───────────────────────────────────────────────────

    def _on_pick_photo(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose reference photo", "", _IMAGE_FILTER
        )
        if path:
            self._picked_photo_path = path
            self.lbl_photo.setText(os.path.basename(path))
            self.lbl_photo.setStyleSheet("")

    def _on_add(self):
        name = self.edit_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing name", "Enter a tag name.")
            return
        if not self._picked_photo_path or not os.path.exists(self._picked_photo_path):
            QMessageBox.warning(self, "Missing photo", "Choose a photo file.")
            return
        try:
            db_manager.add_reference(
                name, self.combo_category.currentText(), self._picked_photo_path
            )
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Could not save reference:\n{exc}")
            return

        self.edit_name.clear()
        self._picked_photo_path = ""
        self.lbl_photo.setText("No file selected")
        self.lbl_photo.setStyleSheet("color: gray;")
        self._reload()

    def _on_item_clicked(self, item):
        """Load a saved label into the add form so the user can attach a photo."""
        name = item.data(Qt.UserRole)
        ref = db_manager.get_reference_by_name(name)
        if not ref:
            return
        self.edit_name.setText(ref["name"])
        idx = self.combo_category.findText(ref["category"])
        if idx >= 0:
            self.combo_category.setCurrentIndex(idx)

    def _on_import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import reference labels",
            "", "Label files (*.json *.txt);;All files (*)",
        )
        if not path:
            return
        try:
            items = parse_label_file(path)
        except Exception as exc:
            QMessageBox.critical(self, "Import failed", f"Could not read labels:\n{exc}")
            return
        if not items:
            QMessageBox.warning(self, "Nothing to import", "No labels found in that file.")
            return

        added, kept = 0, 0
        try:
            for name, category in items:
                # Never overwrite an existing reference's photo — only create
                # missing labels (photo-less) for the user to fill in.
                if db_manager.get_reference_by_name(name):
                    kept += 1
                    continue
                db_manager.add_reference(name, category, "")
                added += 1
        except Exception as exc:
            QMessageBox.critical(self, "Import error", f"Could not import labels:\n{exc}")
            return

        self._reload()
        QMessageBox.information(
            self, "Labels imported",
            f"Added {added} new label(s)"
            + (f", kept {kept} existing" if kept else "")
            + ".\nNow click each ⚠ label and add its photo.",
        )

    def _on_delete_all(self):
        try:
            n = len(db_manager.get_references())
        except Exception:
            n = 0
        if n == 0:
            QMessageBox.information(self, "Empty", "There are no references to delete.")
            return
        if QMessageBox.question(
            self, "Delete all references",
            f"Delete ALL {n} references? This cannot be undone.",
        ) != QMessageBox.Yes:
            return
        try:
            db_manager.delete_all_references()
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Could not delete all:\n{exc}")
            return
        self._reload()

    def _on_delete(self):
        item = self.list_widget.currentItem()
        if not item:
            return
        name = item.data(Qt.UserRole)
        if QMessageBox.question(
            self, "Delete reference", f"Delete @{name}?"
        ) != QMessageBox.Yes:
            return
        try:
            db_manager.delete_reference(name)
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Could not delete:\n{exc}")
            return
        self._reload()

    def _on_save_defaults(self):
        try:
            db_manager.set_setting(
                "default_character", self.combo_def_char.currentData() or ""
            )
            db_manager.set_setting(
                "default_location", self.combo_def_loc.currentData() or ""
            )
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Could not save defaults:\n{exc}")
            return
        QMessageBox.information(self, "Saved", "Global defaults saved.")
