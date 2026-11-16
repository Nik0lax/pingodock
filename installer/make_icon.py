"""Gera installer/pingodock.ico — o Pingo (a gota do assistente) sobre um dock."""
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QGuiApplication, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QRadialGradient)

SIZES = [16, 24, 32, 48, 64, 128, 256]

NAVY_TOP = QColor(18, 30, 72)
NAVY_BOTTOM = QColor(8, 12, 34)
GLOW = QColor(38, 120, 255)
DROP = QColor(214, 246, 252)
DROP_SHADE = QColor(168, 226, 242)
EYES = QColor(14, 26, 52)
# cores de personalização do Pingo (laranja e lilás) nos iconezinhos do dock
SIDE_ICONS = [QColor(246, 163, 91), QColor(201, 167, 240)]


def drop_path(cx: float, cy: float, r: float) -> QPainterPath:
    """Gota com a ponta para cima: círculo embaixo + ponta suave."""
    tip = cy - r * 1.55
    path = QPainterPath()
    path.moveTo(cx, tip)
    path.cubicTo(cx + r * 0.5, tip + r * 0.55, cx + r, cy - r * 0.45, cx + r, cy)
    path.arcTo(QRectF(cx - r, cy - r, 2 * r, 2 * r), 0, -180)
    path.cubicTo(cx - r, cy - r * 0.45, cx - r * 0.5, tip + r * 0.55, cx, tip)
    path.closeSubpath()
    return path


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 256, size / 256)  # desenha em coordenadas 256×256
    small = size <= 32

    # fundo: quadrado arredondado azul-marinho
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(8, 8, 240, 240), 56, 56)
    grad = QLinearGradient(0, 8, 0, 248)
    grad.setColorAt(0, NAVY_TOP)
    grad.setColorAt(1, NAVY_BOTTOM)
    p.fillPath(bg, grad)

    cx, cy, r = 128, (124 if small else 116), (60 if small else 50)

    # brilho azul atrás do Pingo
    p.save()
    p.setClipPath(bg)
    glow = QRadialGradient(QPointF(cx, cy - 6), 120)
    glow.setColorAt(0, QColor(GLOW.red(), GLOW.green(), GLOW.blue(), 200))
    glow.setColorAt(0.45, QColor(GLOW.red(), GLOW.green(), GLOW.blue(), 70))
    glow.setColorAt(1, QColor(GLOW.red(), GLOW.green(), GLOW.blue(), 0))
    p.fillRect(QRectF(0, 0, 256, 256), glow)
    p.restore()

    # barra do dock
    bar = QPainterPath()
    bar.addRoundedRect(QRectF(26, 176, 204, 50), 20, 20)
    p.fillPath(bar, QColor(255, 255, 255, 46))
    p.setPen(QPen(QColor(255, 255, 255, 70), 2))
    p.drawPath(bar)

    if not small:
        # iconezinhos ao lado (o Pingo é o ícone "ampliado" do meio)
        for x, color in zip((40, 188), SIDE_ICONS):
            icon = QPainterPath()
            icon.addRoundedRect(QRectF(x, 186, 28, 28), 8, 8)
            p.fillPath(icon, color)
        # bolinha de "aberto" sob o Pingo
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 230))
        p.drawEllipse(QPointF(cx, 218), 4.5, 4.5)

    # o Pingo
    body = drop_path(cx, cy, r)
    shade = QLinearGradient(cx - r, cy - r, cx + r, cy + r)
    shade.setColorAt(0, DROP)
    shade.setColorAt(1, DROP_SHADE)
    p.fillPath(body, shade)
    # olhos: dois ovais escuros
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(EYES)
    ew, eh, gap = r * 0.26, r * 0.5, r * 0.34
    for ex in (cx - gap, cx + gap):
        p.drawRoundedRect(QRectF(ex - ew / 2, cy - eh * 0.55, ew, eh), ew / 2, ew / 2)
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(data)


def main(out: Path) -> None:
    QGuiApplication(sys.argv[:1])
    images = [png_bytes(render(size)) for size in SIZES]
    # Formato ICO com imagens PNG (suportado desde o Windows Vista).
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, blob in zip(SIZES, images):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        blobs += blob
    out.write_bytes(header + entries + blobs)
    print(f"ícone gerado: {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("pingodock.ico"))
