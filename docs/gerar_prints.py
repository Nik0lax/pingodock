"""Captura o Pingo Dock em execução, com um perfil temporário para documentação.

Uso: .venv\\Scripts\\python.exe docs\\gerar_prints.py
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["PINGODOCK_DEV"] = "1"

import dock
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap, QRadialGradient
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget


class DesktopBackground(QWidget):
    def __init__(self, geometry):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setGeometry(geometry)

    def paintEvent(self, _event):
        painter = QPainter(self)
        width, height = self.width(), self.height()
        base = QLinearGradient(0, 0, width, height)
        base.setColorAt(0, QColor("#142640"))
        base.setColorAt(1, QColor("#24213e"))
        painter.fillRect(self.rect(), base)
        for x, y, radius, ink in ((0.03, 0.86, 0.56, "#165b82"), (0.87, 0.26, 0.53, "#474082")):
            gradient = QRadialGradient(width * x, height * y, width * radius)
            gradient.setColorAt(0, QColor(ink))
            gradient.setColorAt(1, Qt.transparent)
            painter.fillRect(self.rect(), gradient)


def wait_for(condition, milliseconds=6000):
    until = time.monotonic() + milliseconds / 1000
    while time.monotonic() < until:
        if result := condition():
            return result
        QTest.qWait(60)
    raise RuntimeError("Um elemento do Pingo Dock não ficou pronto para a captura.")


def capture(screen, area, filename):
    QTest.qWait(240)
    pixmap = screen.grabWindow(0, area.x(), area.y(), area.width(), area.height())
    if pixmap.isNull() or not pixmap.save(str(ROOT / "docs" / filename)):
        raise RuntimeError(f"Não foi possível salvar {filename}")
    print(filename)


def capture_start(group, screen):
    leader = group.leader
    launcher = leader.start_launcher = dock.StartLauncher(leader)
    launcher.account_name = "Usuário"
    launcher.account_photo = QPixmap()
    shortcuts = launcher.start_shortcuts()
    folders = [entry for entry in shortcuts if entry[0] and "acess" in entry[0][0].casefold()]
    apps = [((), entry["name"], entry["path"]) for entry in dock.default_apps()]
    launcher.entries = folders + apps
    if folders:
        launcher.expanded_folders.add(folders[0][0][:1])
    launcher.catalog_state = "ready"
    launcher.catalog_loaded_at = time.monotonic()
    leader.toggle_start_launcher()
    QTest.qWait(150)
    first_section = launcher.apps.topLevelItem(0)
    first_folder = first_section.child(0) if first_section is not None else None
    if first_folder is not None and first_folder.childCount():
        first_folder.setExpanded(True)
    wait_for(lambda: any(icon.cacheKey() != launcher.generic_icon.cacheKey()
                         for icon in launcher.icons.values()))
    capture(screen, launcher.geometry().adjusted(-14, -14, 14, 14), "print-iniciar-14.png")
    launcher.close()


def capture_previews(group, screen, temp):
    widget = group.leader
    item = next(entry for entry in group.items if isinstance(entry, dock.DockItem)
                and "notepad.exe" in entry.match_names)
    files = {
        "Planejamento.txt": "Pingo Dock\n\nPlanejamento da versão\n\n"
                             "• Revisar o guia de boas-vindas\n• Conferir os atalhos\n• Testar a barra superior",
        "Notas.txt": "Notas da versão 1.4\n\nMenu Iniciar com busca\n"
                     "Prévia das janelas abertas\nControles na barra superior",
    }
    processes = []

    def find_windows():
        pids = {process.pid for process in processes}
        windows = [hwnd for hwnd, pid in dock.app_windows()
                   if pid in pids and any(Path(name).stem in dock.window_title(hwnd) for name in files)]
        return windows if len(windows) == len(files) else None

    try:
        for name, contents in files.items():
            path = temp / name
            path.write_text(contents, encoding="utf-8-sig")
            processes.append(subprocess.Popen([item.target, str(path)]))
        windows = wait_for(find_windows)
        for hwnd in windows:
            dock.user32.SetWindowPos(hwnd, None, 0, 0, 680, 620,
                                     dock.SWP_NOMOVE | dock.SWP_NOACTIVATE | 0x0004)
        QTest.qWait(180)
        item.running = True
        index = group.items.index(item)
        thumb = widget.layout()[1][index]
        anchor = widget.mapToGlobal(QPoint(round(thumb.center().x()), round(thumb.top())))
        previews = dock.WindowPreviews(widget, item, tuple(windows), anchor)
        try:
            QTest.qWait(180)
            previews.show_peek(windows[0])
            wait_for(lambda: previews.peek is not None and previews.peek.isVisible())
            region = previews.geometry().united(previews.peek.geometry())
            capture(screen, region.adjusted(-14, -14, 14, 14), "print-janelas-14.png")
        finally:
            previews.clear_thumbnails()
            previews.close()
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def main():
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    dock.configure_ui_font(app)
    screen = app.primaryScreen()
    desktop = DesktopBackground(screen.geometry())
    desktop.show()
    with tempfile.TemporaryDirectory() as temporary:
        temp = Path(temporary)
        dock.CONFIG_PATH = temp / "config.json"
        dock.save_config({**dock.DEFAULT_CONFIG, "apps": dock.default_apps(), "theme": "dark",
                          "top_bar_style": "glass", "welcome_seen": True})
        group = dock.DockGroup()
        group.build()
        for widget in group.docks:
            for timer in (widget.anim_timer, widget.slow_timer, widget.cover_timer):
                timer.stop()
        bar = group.top_bar = dock.TopBar(group)
        bar.show()
        try:
            group.show_welcome()
            group.welcome.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            group.welcome.show()
            group.welcome.raise_()
            wait_for(group.welcome.isVisible)
            capture(screen, screen.geometry(), "print-desktop-14.png")
            guide = group.welcome
            capture(screen, guide.geometry().adjusted(-14, -14, 14, 14), "print-boas-vindas-14.png")
            guide.close()
            capture_start(group, screen)
            capture_previews(group, screen, temp)

            bar.set_surface("white")
            bar.volume.toggle_popup()
            popup = bar.volume.popup
            origin = bar.geometry().topLeft()
            area = QRect(origin.x(), origin.y(), 227, popup.geometry().bottom() - origin.y() + 15)
            capture(screen, area, "print-volume-14.png")
            popup.close()

            bar.open_tray()
            wait_for(bar.background_tray.isVisible)
            QTest.qWait(300)
            panel = bar.background_tray.geometry()
            panel.setTop(bar.y())
            panel.setRight(bar.geometry().right())
            panel.adjust(-13, 0, 0, 13)
            capture(screen, panel, "print-bandeja-14.png")
        finally:
            bar.reset_tray_session()
            bar.close()
            group.close_docks()
            desktop.close()


if __name__ == "__main__":
    main()
