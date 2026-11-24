; Instalador do Pingo Dock (Inno Setup 6).
; Gere com:  .venv\Scripts\python.exe installer\build.py
; (o build.py empacota o PingoDock.exe antes e passa a versão em /DAppVersion)

#ifndef AppVersion
  #define AppVersion "1.2.0"
#endif
#define AppName  "PingoDock"
#define AppTitle "Pingo Dock"
#define AppExe   "PingoDock.exe"
; Nome antigo do produto: instalações dele são migradas para o Pingo Dock.
#define LegacyName "MacDock"

[Setup]
; Mesmo AppId do MacDock: instalar o Pingo Dock atualiza aquela instalação
; (em vez de deixar dois programas em "Aplicativos instalados").
AppId={{8C1E6F2B-4C1A-4E7E-9E1D-2B6B8E3A9F41}
AppName={#AppTitle}
AppVersion={#AppVersion}
AppVerName={#AppTitle} {#AppVersion}
VersionInfoVersion={#AppVersion}
AppPublisher=Pingo
; Instala só para o usuário atual: não precisa de administrador.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#AppName}
; Não reaproveita a pasta antiga (...\Programs\MacDock); ela é removida abaixo.
UsePreviousAppDir=no
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=..\dist
OutputBaseFilename=PingoDock-Setup-{#AppVersion}
SetupIconFile=pingodock.ico
; Mostra a licença (uso não comercial) para o usuário aceitar antes de instalar.
LicenseFile=..\LICENSE
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppTitle}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Fecha o dock se ele estiver aberto durante a instalação/atualização.
CloseApplications=force
RestartApplications=no

[Languages]
Name: "pt"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "startup"; Description: "Iniciar o Pingo Dock junto com o Windows"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Restos do MacDock (nome antigo). A configuração em %APPDATA%\MacDock fica:
; o Pingo Dock copia de lá na primeira vez que abre.
Type: filesandordirs; Name: "{localappdata}\Programs\{#LegacyName}"
Type: files; Name: "{autoprograms}\{#LegacyName}.lnk"
Type: files; Name: "{autodesktop}\{#LegacyName}.lnk"

[Files]
Source: "..\build\dist\PingoDock\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppTitle}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppTitle}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Mesmo valor que a opção "Iniciar com o Windows" do menu do dock usa.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue; Tasks: startup
; Na desinstalação, remove o valor mesmo que tenha sido ligado depois pelo menu do dock.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "{#AppName}"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppTitle}}"; Flags: nowait postinstall skipifsilent
; Atualização disparada pelo próprio dock (/UPDATE, modo silencioso): reabre o dock no fim.
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: IsUpdate

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im {#AppExe}"; Flags: runhidden; RunOnceId: "StopPingoDock"

[UninstallDelete]
; As configurações (%APPDATA%\PingoDock\config.json) ficam, para uma reinstalação
; manter seus apps; só o registro de diagnóstico é apagado.
Type: files; Name: "{userappdata}\{#AppName}\dock.log"

[Code]
function IsUpdate: Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/UPDATE') = 0 then
      Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  { Garante que nenhuma cópia do dock (nem do MacDock antigo) esteja segurando arquivos. }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im {#AppExe}', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im {#LegacyName}.exe', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Sleep(500); { Permite que o processo auxiliar restaure a barra e encerre. }
  Result := '';
end;
