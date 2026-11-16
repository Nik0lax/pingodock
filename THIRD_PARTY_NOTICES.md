# Componentes de terceiros

O código do Pingo Dock está sob a licença do arquivo [LICENSE](LICENSE) (PolyForm Noncommercial 1.0.0).
O instalador do Pingo Dock também inclui os componentes abaixo, cada um com a sua própria licença,
que **não** é alterada pela licença do Pingo Dock.

## Qt 6 e Qt for Python (PySide6 / Shiboken6) — versão 6.12

- Copyright © The Qt Company Ltd. e outros colaboradores.
- Licença: **GNU Lesser General Public License v3.0** (LGPL-3.0), que complementa a GNU General
  Public License v3.0. Textos completos em [licenses/LGPL-3.0.txt](licenses/LGPL-3.0.txt) e
  [licenses/GPL-3.0.txt](licenses/GPL-3.0.txt).
- Código-fonte: <https://code.qt.io/> e <https://code.qt.io/cgit/pyside/pyside-setup.git/>.
- As bibliotecas do Qt são distribuídas sem modificações, como arquivos separados
  (`_internal\PySide6\*.dll` na pasta de instalação). Você pode substituí-las por outra versão
  compatível do Qt/PySide6, conforme permite a LGPL.

## Python 3.12

- Copyright © Python Software Foundation.
- Licença: **PSF License Agreement**. Texto completo em [licenses/Python-PSF.txt](licenses/Python-PSF.txt).
- <https://www.python.org/>

## Empacotamento

O instalador é gerado com [PyInstaller](https://pyinstaller.org/) (GPL-2.0 com exceção que permite
distribuir programas empacotados sob outras licenças) e [Inno Setup](https://jrsoftware.org/isinfo.php).
Nenhum código desses dois programas é redistribuído além do carregador do PyInstaller, coberto
por essa exceção.
