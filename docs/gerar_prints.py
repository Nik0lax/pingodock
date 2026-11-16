"""Gera os prints do README (docs/print-escuro.png e docs/print-claro.png).

Usa o código real de desenho do dock, com apps genéricos (nada da sua configuração)
sobre um papel de parede abstrato nas cores do Pingo. O desfoque do Windows não
aparece em renderização fora da tela, então o fundo atrás do painel é desfocado aqui.

Uso:  .venv\\Scripts\\python.exe docs\\gerar_prints.py
"""
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dock  # noqa: E402
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (QColor, QImage, QLinearGradient, QPainter, QPainterPath,  # noqa: E402
                           QRadialGradient)
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

WIDTH, HEIGHT, SCALE = 1280, 380, 2  # tamanho lógico e escala (2 = nítido em telas retina)


def demo_config() -> dict:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    home = Path.home()
    apps = [
        ("Explorador de Arquivos", rf"{windir}\explorer.exe"),
        ("Microsoft Edge", rf"{pf86}\Microsoft\Edge\Application\msedge.exe"),
        ("Google Chrome", rf"{pf}\Google\Chrome\Application\chrome.exe"),
        ("Visual Studio Code", rf"{local}\Programs\Microsoft VS Code\Code.exe"),
        ("Bloco de Notas", rf"{windir}\System32\notepad.exe"),
        ("PowerShell", rf"{windir}\System32\WindowsPowerShell\v1.0\powershell.exe"),
    ]
    items = [{"name": n, "path": p} for n, p in apps if os.path.exists(p)]
    items.append({"type": "separator"})
    items += [{"name": "Fotos", "path": str(home / "Pictures")},
              {"name": "Música", "path": str(home / "Music")}]
    items += [{"type": "separator"}, {"type": "trash"}]
    cfg = dict(dock.DEFAULT_CONFIG)
    cfg.update(icon_size=56, apps=items, trash_added=True, blur=True)
    return cfg


def wallpaper(light: bool) -> QImage:
    img = QImage(WIDTH * SCALE, HEIGHT * SCALE, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(SCALE, SCALE)
    base = QLinearGradient(0, 0, WIDTH, HEIGHT)
    if light:
        base.setColorAt(0, QColor(226, 236, 255))
        base.setColorAt(1, QColor(246, 230, 250))
        blobs = [(180, 120, 260, QColor(120, 205, 255, 150)), (640, 60, 300, QColor(255, 170, 205, 130)),
                 (1080, 160, 280, QColor(255, 196, 140, 140)), (420, 330, 240, QColor(175, 150, 255, 120))]
    else:
        base.setColorAt(0, QColor(10, 16, 44))
        base.setColorAt(1, QColor(30, 16, 64))
        blobs = [(180, 120, 260, QColor(40, 150, 255, 170)), (640, 60, 300, QColor(255, 90, 160, 120)),
                 (1080, 160, 280, QColor(255, 150, 70, 120)), (420, 330, 240, QColor(140, 100, 255, 150))]
    p.fillRect(QRectF(0, 0, WIDTH, HEIGHT), base)
    for x, y, r, color in blobs:
        g = QRadialGradient(QPointF(x, y), r)
        g.setColorAt(0, color)
        g.setColorAt(1, QColor(color.red(), color.green(), color.blue(), 0))
        p.fillRect(QRectF(0, 0, WIDTH, HEIGHT), g)
    p.end()
    return img


def blurred(img: QImage) -> QImage:
    """Desfoque suave (reduz e amplia duas vezes), parecido com o do Windows."""
    out = img
    for factor in (14, 6):
        small = out.scaled(out.width() // factor, out.height() // factor,
                           Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        out = small.scaled(img.width(), img.height(), Qt.AspectRatioMode.IgnoreAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
    return out


def render(d: dock.Dock, light: bool, out: Path) -> None:
    d.theme_name = "light" if light else "dark"
    bg = wallpaper(light)
    canvas = QImage(bg)
    p = QPainter(canvas)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    p.scale(SCALE, SCALE)
    offset = QPoint((WIDTH - d.width()) // 2, HEIGHT - d.height())
    panel = d.layout()[0].translated(offset)
    # fundo desfocado atrás do painel (o que o Windows faz na tela)
    clip = QPainterPath()
    clip.addRoundedRect(panel, dock.PANEL_RADIUS, dock.PANEL_RADIUS)
    p.save()
    p.setClipPath(clip)
    p.drawImage(QRectF(0, 0, WIDTH, HEIGHT), blurred(bg))
    p.restore()
    d.render(p, offset, renderFlags=QWidget.RenderFlag.DrawChildren)
    p.end()
    canvas.save(str(out))
    print(f"gerado: {out}")


def main() -> None:
    app = QApplication(sys.argv[:1])
    cfg = demo_config()
    dock.load_config = lambda: dict(cfg)
    dock.Dock.save = lambda self: None  # nunca grava configuração
    d = dock.Dock()
    d.slow_timer.stop(); d.cover_timer.stop(); d.anim_timer.stop()

    # ícones na resolução da imagem final (mais nítidos que os da tela)
    px = math.ceil(d.S * d.M * SCALE)
    for it in d.items:
        if not it.is_sep:
            it.reload_icon(d.provider, px)
    # apps "abertos" (bolinha) e o mouse sobre o VS Code (ampliação + nome)
    for it in d.items:
        if not it.is_sep and it.name in ("Explorador de Arquivos", "Google Chrome", "Visual Studio Code"):
            it.running = True
    d.reveal, d.hover = 1.0, 0.0
    rects = d.layout()[1]
    target = next((i for i, it in enumerate(d.items) if getattr(it, "name", "") == "Visual Studio Code"), 3)
    pointer = QPoint(int(rects[target].center().x()), int(rects[target].center().y()))
    d.mouse_x, d.hover = float(pointer.x()), 1.0
    dock.QCursor = type("Cursor", (), {"pos": staticmethod(lambda: d.mapToGlobal(pointer))})

    docs = ROOT / "docs"
    render(d, False, docs / "print-escuro.png")
    render(d, True, docs / "print-claro.png")
    app.quit()


if __name__ == "__main__":
    main()
