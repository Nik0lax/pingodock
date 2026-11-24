param(
    [Parameter(Mandatory)]
    [ValidateSet('Windows', 'PingoComExplorer', 'PingoShell', 'PingoShellComArquivos')]
    [string]$Cenario,
    [ValidateRange(3, 120)]
    [int]$Amostras = 10
)

$ErrorActionPreference = 'Stop'
if (-not ('PingoShellProbe' -as [type])) {
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class PingoShellProbe {
    [DllImport("user32.dll")] public static extern IntPtr GetShellWindow();
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern IntPtr FindWindow(string windowClass, string title);
    public static IntPtr ByTitle(string title) { return FindWindow(null, title); }
    public static IntPtr ByClass(string windowClass) { return FindWindow(windowClass, null); }
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
}
'@
}

function Get-WindowPid([string]$title) {
    $handle = [PingoShellProbe]::ByTitle($title)
    [uint32]$owner = 0
    if ($handle -ne [IntPtr]::Zero) {
        [void][PingoShellProbe]::GetWindowThreadProcessId($handle, [ref]$owner)
    }
    $owner
}

function Get-Median($values) {
    $ordered = @($values | Sort-Object)
    $middle = [math]::Floor($ordered.Count / 2)
    if ($ordered.Count % 2) { return $ordered[$middle] }
    return ($ordered[$middle - 1] + $ordered[$middle]) / 2
}

$session = (Get-Process -Id $PID).SessionId
$collected = for ($i = 0; $i -lt $Amostras; $i++) {
    $native = [PingoShellProbe]::ByClass('Shell_TrayWnd')
    $explorerShell = [PingoShellProbe]::GetShellWindow() -ne [IntPtr]::Zero -or $native -ne [IntPtr]::Zero
    $prototype = Get-WindowPid 'Pingo Dock Shell'
    $dock = Get-WindowPid 'Pingo Dock'
    $dev = Get-WindowPid 'Pingo Dock Dev'
    $explorerIds = @(Get-Process explorer -ErrorAction SilentlyContinue |
        Where-Object SessionId -eq $session | Select-Object -ExpandProperty Id)
    $valid = switch ($Cenario) {
        'Windows' { $explorerShell -and [PingoShellProbe]::IsWindowVisible($native) -and -not ($prototype -or $dock -or $dev) }
        'PingoComExplorer' { $explorerShell -and ($dock -or $dev) -and -not $prototype }
        'PingoShell' { -not $explorerShell -and $prototype -and -not ($dock -or $dev -or $explorerIds.Count) }
        'PingoShellComArquivos' { -not $explorerShell -and $prototype -and -not ($dock -or $dev) -and $explorerIds.Count -gt 0 }
    }
    if (-not $valid) { throw "O estado da sessão não corresponde ao cenário $Cenario." }

    $ids = @($prototype, $dock, $dev) + @(Get-Process PingoDockTaskbar -ErrorAction SilentlyContinue |
        Where-Object SessionId -eq $session | Select-Object -ExpandProperty Id)
    if ($dev) {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$dev"
        while ($parent.ParentProcessId) {
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($parent.ParentProcessId)"
            if (-not $parent -or $parent.Name -ne 'pythonw.exe' -or $parent.CommandLine -notmatch '\bdev\.py\b') { break }
            $ids += $parent.ProcessId
        }
    }
    $memory = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
    $processes = Get-CimInstance Win32_PerfFormattedData_PerfProc_Process
    [pscustomobject]@{
        DisponivelMiB = $memory.AvailableBytes / 1MB
        ComprometidaMiB = $memory.CommittedBytes / 1MB
        PingoPrivadaResidenteMiB = ($processes | Where-Object IDProcess -in ($ids | Where-Object { $_ -gt 0 }) |
            Measure-Object WorkingSetPrivate -Sum).Sum / 1MB
        ExplorerPrivadaResidenteMiB = ($processes | Where-Object IDProcess -in $explorerIds |
            Measure-Object WorkingSetPrivate -Sum).Sum / 1MB
    }
    if ($i -lt $Amostras - 1) { Start-Sleep -Seconds 1 }
}

[pscustomobject]@{
    Cenario = $Cenario
    Windows = (Get-CimInstance Win32_OperatingSystem).Caption
    Amostras = $Amostras
    DisponivelMiB = [math]::Round((Get-Median $collected.DisponivelMiB), 1)
    ComprometidaMiB = [math]::Round((Get-Median $collected.ComprometidaMiB), 1)
    PingoPrivadaResidenteMiB = [math]::Round((Get-Median $collected.PingoPrivadaResidenteMiB), 1)
    ExplorerPrivadaResidenteMiB = [math]::Round((Get-Median $collected.ExplorerPrivadaResidenteMiB), 1)
} | ConvertTo-Json
