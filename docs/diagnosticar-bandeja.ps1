param([ValidateSet('Pingo','Nativa')][string]$Modo = 'Pingo')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class PingoTrayProbe {
    public delegate bool Visit(IntPtr h, IntPtr data);
    [DllImport("user32.dll")] public static extern bool EnumWindows(Visit callback, IntPtr data);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint process);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder name, int count);
}
"@
$os = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
Write-Output "Modo: $Modo | Windows: $($os.DisplayVersion) $($os.CurrentBuild).$($os.UBR)"
Write-Host 'Clique na seta da bandeja e deixe o painel ou a mensagem de erro aberto. Captura em 8 segundos.'
Start-Sleep -Seconds 8
$explorerIds = @(Get-Process explorer -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
$windows = [Collections.Generic.List[object]]::new()
$callback = [PingoTrayProbe+Visit]{
    param($h,$unused)
    [uint32]$owner = 0
    [void][PingoTrayProbe]::GetWindowThreadProcessId($h,[ref]$owner)
    if ($owner -in $explorerIds) {
        $class = [Text.StringBuilder]::new(256)
        [void][PingoTrayProbe]::GetClassName($h,$class,$class.Capacity)
        $visible = [PingoTrayProbe]::IsWindowVisible($h)
        if ($visible -or $class.ToString() -match 'Tray|Overflow') {
            $windows.Add([pscustomobject]@{Handle=$h; Class=$class.ToString(); Visible=$visible})
        }
    }
    return $true
}
[void][PingoTrayProbe]::EnumWindows($callback,[IntPtr]::Zero)
$windows | ForEach-Object { Write-Output "Explorer: $($_.Class) | visivel=$($_.Visible)" }
$bars = @($windows | Where-Object Class -in 'Shell_TrayWnd','Shell_SecondaryTrayWnd')
foreach ($bar in $bars) {
    try {
        $root = [Windows.Automation.AutomationElement]::FromHandle($bar.Handle)
        $nodes = $root.FindAll([Windows.Automation.TreeScope]::Descendants,[Windows.Automation.Condition]::TrueCondition)
        $matched = 0
        foreach ($node in $nodes) {
            try {
                $info = $node.Current
                if ($info.AutomationId -notmatch 'Chevron|HiddenIcons|Overflow' -and
                    $info.ClassName -notmatch 'Chevron|Overflow' -and
                    $info.Name -notmatch 'hidden icon|notification chevron|ícones ocultos|divisa de notificação') { continue }
                $matched++
                $patterns = ($node.GetSupportedPatterns() | ForEach-Object ProgrammaticName) -join ', '
                Write-Output "UIA: $($bar.Class) | ID=$($info.AutomationId) | Classe=$($info.ClassName) | foraDaTela=$($info.IsOffscreen) | padroes=$patterns"
            } catch { continue }
        }
        Write-Output "UIA: $($bar.Class) | elementos=$($nodes.Count) | candidatos=$matched"
    } catch {
        Write-Output "UIA: $($bar.Class) | erro=$($_.Exception.GetType().Name)"
    }
}
