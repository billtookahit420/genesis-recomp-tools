#!/usr/bin/env python3
"""GenesisRecomp Tools - GUI.

A profile-based converter, like the N64 tool: segagenesisrecomp
needs a per-game integration profile (RAM layout + disassembly
discovery data) on top of the ROM, so the queue flags every ROM that
no bundled profile covers as needs-review with the reason, instead
of pretending a conversion happened. The bundled profile is the
framework's flagship: Sonic the Hedgehog (JUE REV00).
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

import engine

SETTINGS_PATH = engine.APP_DIR / "settings.json"

HELP_TEXT = """GenesisRecomp Tools converts Sega Genesis / Mega Drive ROMs from
your own collection into native PC recompilations, using the
segagenesisrecomp framework by Matthew Stanley (mstan) /
RetroPortingToolkit.

HOW GENESIS RECOMPS DIFFER FROM THE OTHER SYSTEMS
A Genesis recomp is not produced from a ROM alone. The framework's
shared runner executes a per-game integration - a game profile
carrying the game's RAM layout plus discovery data derived from a
disassembly of that exact game (seed functions, subroutine and
jump-table lists, annotations). A Sonic profile does not describe
another 68000 game. So, like the N64 tool, this converter is
profile-based: it converts the games a profile exists for and flags
everything else as needs-review with the honest reason. Nothing is
faked and nothing is silently skipped.

BUNDLED PROFILE
  Sonic the Hedgehog - JUE REV00 only (CRC32 F9394E97, 512 KiB).
  Other revisions (REV01 and friends) are different code and are NOT
  qualified targets yet; the probe says so when it sees one.

PROBING
The probe parses the Genesis header (system string, domestic and
overseas titles, serial, checksum, ROM range, region codes), verifies
the header checksum, and identifies the image by CRC32/SHA-1 over the
plain image. Interleaved .smd copier images are detected (512-byte
header, 0xAA 0xBB magic) and deinterleaved before hashing, so an .smd
dump of a qualified game matches its profile exactly.

NETPLAY
The framework itself ships rollback netplay (it is wired for the
two-player titles upstream), but Sonic the Hedgehog is a
single-player game - its profile declares one player and there is
nothing to connect. This tool therefore offers no netplay toggle for
the bundled profile rather than a button that does nothing. If a
two-player profile is added later, the framework wiring exists.

RETROACHIEVEMENTS
Fresh wiring in the family style: an rcheevos module compiled into
the game (RC_CONSOLE_MEGA_DRIVE = 1; the hash is rcheevos' own
whole-image hash of the ROM the launcher picked; memory reads go to
the runner's work-RAM shadow). Put retroachievements.toml (your RA
username + Connect API token) next to the game executable - the
Settings tab can stamp your saved copy into every package. Login and
achievement unlocks pop up as toasts through the launcher's runtime
UI; the engine moves the template's recomp-ui pin to the upstream
revision that provides the toast API and verifies it is present.
Note: RetroAchievements counts unlocks from recomp clients as
Casual (softcore), because the service only honours hardcore from
approved clients. The tool never spoofs another client's identity.

VERIFICATION STATUS (honest labels)
  Framework + recompiler: built from source and exercised here.
  Probe: validated against synthetic header fixtures, including an
  interleaved .smd round-trip.
  Whole pipeline: run end-to-end here with a synthetic header
  fixture standing in for the ROM - scaffold, recompile, full build,
  package, and the packaged executable booting to the real launcher
  with box art on screen; pointing it at the fixture gives the
  designed refusal (ROM identity mismatch - the launcher CRC-verifies
  the player's dump before starting).
  RetroAchievements module: compile-verified inside the game target.
  Hash / login / toast proof, a real-game conversion and the ROM
  census are PENDING a ROM set from your own collection (this tool
  only converts your own dumps, and the collection had not arrived
  when this build was made).
  Linux packages are the primary target. Windows cross-builds use
  MinGW with the host-built recompiler generating the code and are
  compile-verified (full build + package produced); Windows runtime
  is untested.

BIOS
None. Genesis cartridges need no BIOS, the runtime asks for none,
and nothing is shipped.

CREDITS
segagenesisrecomp, GenesisRecomp and the SonicTheHedgehogRecomp
project by Matthew Stanley (mstan), RetroPortingToolkit -
PolyForm Noncommercial 1.0.0. recomp-ui by the same team (MIT).
Third-party components in the framework: ymfm (BSD-3-Clause),
z80-recomp-core, m68k-recomp-core, recomp-net, rbengine, SDL2 (zlib).
RetroAchievements support uses rcheevos (MIT). Unofficial fan tool;
not affiliated with SEGA.
"""


class ConvertWorker(QThread):
    line = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, job):
        super().__init__()
        self.job = job

    def run(self):
        try:
            summary = engine.run_job(self.job, log=self.line.emit)
            self.done.emit(summary)
        except Exception:
            self.failed.emit(traceback.format_exc())


class ToolboxWorker(QThread):
    done = Signal(object)

    def run(self):
        try:
            self.done.emit(engine.toolbox_status())
        except Exception:
            self.done.emit([])


class CoverWorker(QThread):
    done = Signal(str, object)

    def __init__(self, title):
        super().__init__()
        self.title = title

    def run(self):
        try:
            png, _desc = engine.fetch_cover(self.title)
        except Exception:
            png = None
        self.done.emit(self.title, png)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GenesisRecomp Tools")
        self.resize(1080, 720)
        self.probes = []
        self.worker = None
        self.tool_worker = None
        # Cover workers are held until they finish: dropping the last
        # reference to a running QThread aborts the app (the GB/SNES
        # field crash). Keyed by title so stale results are discarded.
        self._cover_workers = set()
        self._cover_wanted = None
        self.settings = self._load_settings()
        self._build_ui()
        self.refresh_toolbox()
        QTimer.singleShot(100, self._check_first_run)

    # ------------------------------------------------------------ settings
    def _load_settings(self):
        try:
            return json.loads(SETTINGS_PATH.read_text())
        except Exception:
            return {}

    def _save_settings(self):
        data = {
            "output_dir": self.output_edit.text(),
            "ra_enabled": self.ra_check.isChecked(),
            "ra_popups": self.popups_check.isChecked(),
            "ra_toml": self.toml_edit.text(),
            "target_linux": self.linux_check.isChecked(),
            "target_windows": self.windows_check.isChecked(),
            "jobs": self.jobs_spin.value(),
            "force": self.force_check.isChecked(),
        }
        try:
            SETTINGS_PATH.write_text(json.dumps(data, indent=1))
        except OSError:
            pass

    def _check_first_run(self):
        if engine.find_template() is None:
            self.log.appendPlainText(
                "First run: the framework template (SonicTheHedgehogRecomp "
                "+ segagenesisrecomp + recomp-ui) is fetched "
                "automatically when you start a conversion.")

    # ------------------------------------------------------------ UI build
    def _build_ui(self):
        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        tabs.addTab(self._convert_tab(), "Convert")
        tabs.addTab(self._help_tab(), "Help")

    def _convert_tab(self):
        page = QWidget()
        root = QVBoxLayout(page)

        row = QHBoxLayout()
        add_btn = QPushButton("Add ROMs...")
        add_btn.clicked.connect(self.add_roms)
        add_folder_btn = QPushButton("Add folder...")
        add_folder_btn.clicked.connect(self.add_folder)
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self.remove_selected)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.clear_queue)
        row.addWidget(add_btn)
        row.addWidget(add_folder_btn)
        row.addWidget(remove_btn)
        row.addWidget(clear_btn)
        row.addStretch(1)
        root.addLayout(row)

        split = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Title", "Serial", "Profile", "ROM", "Regions", "Status"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.show_selected)
        split.addWidget(self.table)

        details = QWidget()
        dl = QHBoxLayout(details)
        self.cover_label = QLabel("No cover")
        self.cover_label.setFixedSize(150, 200)
        self.cover_label.setAlignment(Qt.AlignCenter)
        self.cover_label.setStyleSheet("border: 1px solid #888;")
        dl.addWidget(self.cover_label)
        self.details_label = QLabel("Select a ROM to see its probe details.")
        self.details_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.details_label.setWordWrap(True)
        dl.addWidget(self.details_label, 1)
        split.addWidget(details)
        split.setSizes([380, 220])
        root.addWidget(split, 1)

        settings_box = QWidget()
        sl = QHBoxLayout(settings_box)
        self.ra_check = QCheckBox("RetroAchievements")
        self.ra_check.setChecked(self.settings.get("ra_enabled", True))
        self.popups_check = QCheckBox("RA pop-ups")
        self.popups_check.setChecked(self.settings.get("ra_popups", True))
        self.linux_check = QCheckBox("Linux")
        self.linux_check.setChecked(self.settings.get("target_linux", True))
        self.windows_check = QCheckBox("Windows")
        self.windows_check.setChecked(self.settings.get("target_windows", True))
        self.force_check = QCheckBox("Force flagged")
        self.force_check.setChecked(self.settings.get("force", False))
        sl.addWidget(self.ra_check)
        sl.addWidget(self.popups_check)
        sl.addWidget(self.linux_check)
        sl.addWidget(self.windows_check)
        sl.addWidget(self.force_check)
        sl.addWidget(QLabel("Jobs:"))
        self.jobs_spin = QSpinBox()
        self.jobs_spin.setRange(1, 8)
        self.jobs_spin.setValue(int(self.settings.get("jobs", 2)))
        sl.addWidget(self.jobs_spin)
        sl.addStretch(1)
        root.addWidget(settings_box)

        orow = QHBoxLayout()
        orow.addWidget(QLabel("Output folder:"))
        self.output_edit = QLineEdit(
            self.settings.get("output_dir",
                              str(engine.APP_DIR / "output")))
        orow.addWidget(self.output_edit, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self.browse_output)
        orow.addWidget(browse)
        root.addLayout(orow)

        trow = QHBoxLayout()
        trow.addWidget(QLabel("RA toml to include:"))
        self.toml_edit = QLineEdit(self.settings.get("ra_toml", ""))
        trow.addWidget(self.toml_edit, 1)
        tbrowse = QPushButton("Browse...")
        tbrowse.clicked.connect(self.browse_toml)
        trow.addWidget(tbrowse)
        root.addLayout(trow)

        toolbox_row = QHBoxLayout()
        self.toolbox_label = QLabel("Toolbox: checking...")
        toolbox_row.addWidget(self.toolbox_label, 1)
        refresh_btn = QPushButton("Recheck")
        refresh_btn.clicked.connect(self.refresh_toolbox)
        toolbox_row.addWidget(refresh_btn)
        install_btn = QPushButton("Install build tools")
        install_btn.clicked.connect(self.install_tools)
        toolbox_row.addWidget(install_btn)
        root.addLayout(toolbox_row)

        crow = QHBoxLayout()
        self.convert_btn = QPushButton("Convert")
        self.convert_btn.clicked.connect(self.convert)
        crow.addWidget(self.convert_btn)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        crow.addWidget(self.progress, 1)
        root.addLayout(crow)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        root.addWidget(self.log, 1)
        return page

    def _help_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(HELP_TEXT)
        layout.addWidget(text)
        return page

    # ------------------------------------------------------------ queue
    def add_roms(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Genesis ROMs", "",
            "Genesis ROMs (*.bin *.md *.gen *.smd);;All files (*)")
        self._add_paths(paths)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select ROM folder")
        if not folder:
            return
        paths = []
        for p in sorted(Path(folder).rglob("*")):
            if p.is_file() and p.suffix.lower() in engine.ROM_EXTS:
                paths.append(str(p))
        self._add_paths(paths)

    def _add_paths(self, paths):
        have = {pr["path"] for pr in self.probes}
        for path in paths:
            if path in have:
                continue
            probe = engine.probe_rom(path)
            self.probes.append(probe)
            row = self.table.rowCount()
            self.table.insertRow(row)
            status = ("ready" if probe["profile"] and not probe["reasons"]
                      else "needs review" if probe["needs_review"]
                      else "error")
            values = [probe["title"] or probe["filename"],
                      probe["serial"],
                      probe["profile_title"] or "-",
                      f"{probe['size']} B", probe["regions"], status]
            for col, v in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(v)))
        self.table.resizeColumnsToContents()

    def remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self.table.removeRow(r)
            if 0 <= r < len(self.probes):
                del self.probes[r]

    def clear_queue(self):
        self.table.setRowCount(0)
        self.probes = []

    def show_selected(self):
        rows = self.table.selectedIndexes()
        if not rows:
            return
        r = rows[0].row()
        if not (0 <= r < len(self.probes)):
            return
        pr = self.probes[r]
        lines = [
            f"File: {pr['filename']}",
            f"Overseas title: {pr['header_title_overseas'] or '-'}",
            f"Domestic title: {pr['header_title_domestic'] or '-'}",
            f"System: {pr['system'] or '-'}",
            f"Serial: {pr['serial'] or '-'}",
            f"Regions: {pr['regions'] or '-'}   I/O: {pr['io_support'] or '-'}",
            f"ROM range: {pr['rom_range']}   SRAM: {pr['sram'] or 'none'}",
            f"Format: {pr['format']}   Size: {pr['size']} bytes "
            f"(file {pr['file_size']})",
            f"Header checksum: stored 0x{(pr['checksum_stored'] or 0):04X} "
            f"computed 0x{(pr['checksum_computed'] or 0):04X} "
            f"({'ok' if pr['checksum_ok'] else 'mismatch'})",
            f"CRC32: 0x{(pr['crc32'] or 0):08X}",
            f"SHA-1: {pr['sha1']}",
            f"Profile: {pr['profile_title'] or 'none'}",
        ]
        for note in pr["info"]:
            lines.append(f"Note: {note}")
        for reason in pr["reasons"]:
            lines.append(f"Flag: {reason}")
        self.details_label.setText("\n".join(lines))
        self._fetch_cover(pr["title"])

    def _fetch_cover(self, title):
        if not title:
            return
        self._cover_wanted = title
        if any(w.title == title for w in self._cover_workers):
            return
        worker = CoverWorker(title)
        self._cover_workers.add(worker)
        worker.done.connect(self._cover_done)
        worker.finished.connect(lambda w=worker: self._cover_workers.discard(w))
        worker.start()

    def _cover_done(self, title, png):
        if title != self._cover_wanted or not png:
            return
        pix = QPixmap()
        if pix.loadFromData(png):
            self.cover_label.setPixmap(
                pix.scaled(self.cover_label.size(),
                           Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # ------------------------------------------------------------ actions
    def browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Output folder")
        if folder:
            self.output_edit.setText(folder)

    def browse_toml(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "RetroAchievements toml", "",
            "TOML files (*.toml);;All files (*)")
        if path:
            self.toml_edit.setText(path)

    def refresh_toolbox(self):
        if self.tool_worker and self.tool_worker.isRunning():
            return
        self.tool_worker = ToolboxWorker()
        self.tool_worker.done.connect(self._toolbox_done)
        self.tool_worker.start()

    def _toolbox_done(self, checks):
        if not checks:
            self.toolbox_label.setText("Toolbox: check failed")
            return
        missing = [c for c in checks if not c["ok"]]
        if not missing:
            self.toolbox_label.setText(
                f"Toolbox: all {len(checks)} checks pass")
        else:
            names = ", ".join(c["label"] for c in missing[:3])
            self.toolbox_label.setText(
                f"Toolbox: {len(missing)} missing ({names})")

    def install_tools(self):
        pkgs = " ".join(engine.INSTALL_PACKAGES)
        msg = (f"This runs:\n\npkexec apt-get install -y {pkgs}\n\n"
               "in a terminal, asking for your password there.")
        if QMessageBox.question(self, "Install build tools", msg) != \
                QMessageBox.Yes:
            return
        import subprocess
        subprocess.Popen([
            "xterm", "-e",
            f"pkexec apt-get update && pkexec apt-get install -y {pkgs}; "
            "echo; echo Done - press Enter; read _"])

    def convert(self):
        if self.worker and self.worker.isRunning():
            return
        if not self.probes:
            QMessageBox.information(self, "Nothing queued",
                                    "Add at least one ROM first.")
            return
        self._save_settings()
        job = {
            "roms": [pr["path"] for pr in self.probes],
            "output_dir": self.output_edit.text(),
            "ra": {"enabled": self.ra_check.isChecked(),
                   "popups": self.popups_check.isChecked(),
                   "toml": self.toml_edit.text()},
            "targets": {"linux": self.linux_check.isChecked(),
                        "windows": self.windows_check.isChecked()},
            "jobs": self.jobs_spin.value(),
            "force": self.force_check.isChecked(),
        }
        self.convert_btn.setEnabled(False)
        self.progress.setRange(0, 0)
        self.worker = ConvertWorker(job)
        self.worker.line.connect(
            lambda s: self.log.appendPlainText(s))
        self.worker.done.connect(self._convert_done)
        self.worker.failed.connect(self._convert_failed)
        self.worker.start()

    def _convert_done(self, summary):
        self.convert_btn.setEnabled(True)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        for pkg in summary.get("packages", []):
            note = f" ({pkg['note']})" if pkg.get("note") else ""
            self.log.appendPlainText(
                f"PACKAGE [{pkg['target']}]: {pkg['zip']}{note}")
        for err in summary.get("errors", []):
            self.log.appendPlainText(f"ERROR: {err}")
        self.log.appendPlainText("Conversion finished.")

    def _convert_failed(self, tb):
        self.convert_btn.setEnabled(True)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.log.appendPlainText(tb)
        QMessageBox.critical(self, "Conversion failed",
                             tb.splitlines()[-1] if tb else "unknown error")


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
