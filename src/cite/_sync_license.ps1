param(
    [Parameter(Mandatory = $true)]
    [string]$LicenseManagerExe,

    [Parameter(Mandatory = $true)]
    [string]$HaspId,

    [int]$TimeoutSeconds = 180,

    # Extra seconds to wait after the UI is ready before clicking Synchronize.
    # The License Manager needs a moment to establish its internet connection
    # after startup; clicking too quickly causes a "no Internet connection"
    # result even when connectivity is available.
    [int]$StartupSettleSeconds = 3
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient
Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class CiteNativeWindow {
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool SetWindowPos(
        IntPtr hWnd,
        IntPtr hWndInsertAfter,
        int X,
        int Y,
        int cx,
        int cy,
        uint uFlags
    );

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool PostMessage(
        IntPtr hWnd,
        uint Msg,
        IntPtr wParam,
        IntPtr lParam
    );
}
"@

function Write-Result {
    param(
        [bool]$Success,
        [string]$Status,
        [string]$Message,
        [int]$ProcessId = 0,
        [int]$ExitCode = 0
    )

    [pscustomobject]@{
        Success = $Success
        Status = $Status
        Message = $Message
        ProcessId = if ($ProcessId) { $ProcessId } else { $null }
    } | ConvertTo-Json -Compress
    exit $ExitCode
}

function Invoke-Control {
    param([System.Windows.Automation.AutomationElement]$Element)

    # This License Manager exposes its controls as native HWND-backed panes,
    # with no reliable UI Automation patterns. Post BM_CLICK asynchronously so
    # modal confirmation/result dialogs do not block this automation thread.
    $handle = $Element.Current.NativeWindowHandle
    if ($handle -ne 0) {
        $posted = [CiteNativeWindow]::PostMessage(
            [IntPtr]$handle,
            0x00F5,
            [IntPtr]::Zero,
            [IntPtr]::Zero
        )
        if ($posted) { return }
    }

    $pattern = $null
    if (-not $Element.TryGetCurrentPattern(
        [System.Windows.Automation.InvokePattern]::Pattern,
        [ref]$pattern
    )) {
        throw "Control '$($Element.Current.Name)' cannot be invoked."
    }
    $pattern.Invoke()
}

$launcher = $null
$app = $null

function Stop-OwnedProcesses {
    $owned = @(
        Get-Process -Name "licmgr", "licmgr_s" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Id -notin $existingIds -and
                $_.StartTime -ge $adapterStartedAt.AddSeconds(-1)
            }
    )
    foreach ($process in $owned) {
        try {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        } catch {
            # Best-effort cleanup for an unexpected adapter exception.
        }
    }
}

# Record existing instances so this run can identify and own only its new child.
$existingIds = @(
    Get-Process -Name "licmgr", "licmgr_s" -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty Id
)
$adapterStartedAt = Get-Date

# Always use a fresh instance. Reusing an existing instance can capture a
# user's visible window, leave stale dialogs behind, and report the prior run's
# result. This process is owned by the adapter and can be cleaned up reliably.
$launcher = Start-Process `
    -FilePath $LicenseManagerExe `
    -WindowStyle Hidden `
    -PassThru

$app = $null
$startDeadline = (Get-Date).AddSeconds(30)
do {
    $app = Get-Process -Name "licmgr" -ErrorAction SilentlyContinue |
        Where-Object { $_.Id -notin $existingIds } |
        Sort-Object StartTime -Descending |
        Select-Object -First 1
    if (-not $app) {
        Start-Sleep -Milliseconds 50
    }
} until ($app -or (Get-Date) -ge $startDeadline)

if (-not $app) {
    if ($launcher -and -not $launcher.HasExited) {
        Stop-Process -Id $launcher.Id -Force -ErrorAction SilentlyContinue
    }
    Write-Result $false "StartFailed" "License Manager did not start." 0 2
}

function Get-AppElements {
    # The manager can split the main dialog, wizard, and result modal across
    # separate licmgr processes. Return the combined UI tree for every process
    # created by this adapter run so the result message cannot be missed.
    $owned = @(
        Get-Process -Name "licmgr", "licmgr_s" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Id -notin $existingIds -and
                $_.StartTime -ge $adapterStartedAt.AddSeconds(-1)
            }
    )
    foreach ($process in $owned) {
        try {
            $condition = [System.Windows.Automation.PropertyCondition]::new(
                [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
                $process.Id
            )
            $found =
                [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
                    [System.Windows.Automation.TreeScope]::Descendants,
                    $condition
                )
            foreach ($element in $found) { $element }
        } catch {
            # A transient child process can exit while its tree is inspected.
        }
    }
}

function Update-AppProcess {
    # The self-extracting manager can replace its inner licmgr.exe while the
    # synchronization wizard advances. Follow the newest owned process that
    # currently exposes UI elements.
    $candidates = @(
        Get-Process -Name "licmgr" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Id -notin $existingIds -and
                $_.StartTime -ge $adapterStartedAt.AddSeconds(-1)
            } |
            Sort-Object StartTime -Descending
    )
    foreach ($candidate in $candidates) {
        try {
            $condition = [System.Windows.Automation.PropertyCondition]::new(
                [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
                $candidate.Id
            )
            $candidateElements =
                [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
                    [System.Windows.Automation.TreeScope]::Descendants,
                    $condition
                )
            if ($candidateElements.Count -gt 0) {
                $script:app = $candidate
                return
            }
        } catch {
            # A transient process may disappear while it is inspected.
        }
    }
}

function Hide-AppWindows {
    if (-not $app -or $app.HasExited) { return }

    # SW_HIDE removes this application's descendants from the UI Automation
    # tree. Move windows far off-screen instead: they remain automatable but
    # are not visible to the interactive user. Modals centered on their parent
    # also inherit the off-screen location.
    $offscreenX = -32000
    $offscreenY = -32000
    $flags = 0x0001 -bor 0x0004 -bor 0x0010  # NOSIZE|NOZORDER|NOACTIVATE

    try {
        $app.Refresh()
        if ($app.MainWindowHandle -ne 0) {
            $null = [CiteNativeWindow]::SetWindowPos(
                $app.MainWindowHandle,
                [IntPtr]::Zero,
                $offscreenX,
                $offscreenY,
                0,
                0,
                $flags
            )
        }
    } catch {
        # The process can refresh while a modal dialog is being created.
    }

    try {
        $windows = @(Get-AppElements) |
            Where-Object {
                $_.Current.ControlType.ProgrammaticName -eq "ControlType.Window"
            }
        foreach ($window in $windows) {
            $handle = $window.Current.NativeWindowHandle
            if ($handle -ne 0) {
                $null = [CiteNativeWindow]::SetWindowPos(
                    [IntPtr]$handle,
                    [IntPtr]::Zero,
                    $offscreenX,
                    $offscreenY,
                    0,
                    0,
                    $flags
                )
            }
        }
    } catch {
        # A disappearing result dialog is harmless.
    }
}

function Close-Manager {
    # Result dialogs close asynchronously. Retry the main Close action until it
    # takes effect, then force-stop every process created by this adapter run.
    for ($attempt = 0; $attempt -lt 12; $attempt++) {
        if ($app.HasExited) { break }
        Hide-AppWindows
        try {
            $elements = @(Get-AppElements)
            $close = $elements |
                Where-Object {
                    $_.Current.AutomationId -eq "2" -and
                    $_.Current.ControlType.ProgrammaticName -eq
                        "ControlType.Button"
                } |
                Select-Object -First 1
            if ($close -and $close.Current.IsEnabled) {
                Invoke-Control $close
            }
        } catch {
            # The synchronization result has already been captured. Do not
            # replace it with a cleanup error.
        }
        Start-Sleep -Milliseconds 250
        try {
            $app.Refresh()
        } catch {
            break
        }
    }

    Stop-OwnedProcesses
}

# Convert any unexpected UI Automation exception into structured status and
# guarantee that this run cannot leave its private License Manager processes.
trap {
    $adapterError = $_.Exception.Message
    $failedProcessId = if ($app) { $app.Id } else { 0 }
    Stop-OwnedProcesses
    Write-Result $false "AdapterError" $adapterError $failedProcessId 2
}

# Wait for the main dialog and its Synchronize control (automation ID 1008).
$sync = $null
$controlDeadline = (Get-Date).AddSeconds(30)
do {
    Update-AppProcess
    Hide-AppWindows
    $elements = @(Get-AppElements)
    $sync = $elements |
        Where-Object { $_.Current.AutomationId -eq "1008" } |
        Select-Object -First 1
    if (-not $sync) {
        Start-Sleep -Milliseconds 250
    }
} until ($sync -or (Get-Date) -ge $controlDeadline)

if (-not $sync) {
    Close-Manager
    Write-Result $false "ControlNotFound" `
        "Synchronize control 1008 was not found." $app.Id 2
}

# Select the requested key when the combo exposes list items. If the manager
# only sees one key, its default selection is retained.
$combo = $elements |
    Where-Object {
        $_.Current.AutomationId -eq "1021" -and
        $_.Current.ControlType.ProgrammaticName -eq "ControlType.ComboBox"
    } |
    Select-Object -First 1

if ($combo) {
    $expand = $null
    if ($combo.TryGetCurrentPattern(
        [System.Windows.Automation.ExpandCollapsePattern]::Pattern,
        [ref]$expand
    )) {
        try {
            $expand.Expand()
            Start-Sleep -Milliseconds 300
            $elements = @(Get-AppElements)
            $items = @(
                $elements |
                    Where-Object {
                        $_.Current.ControlType.ProgrammaticName -eq
                            "ControlType.ListItem"
                    }
            )

            try {
                $haspHex = [Convert]::ToString(
                    [uint64]$HaspId, 16
                ).ToUpperInvariant().PadLeft(8, "0")
            } catch {
                $haspHex = $HaspId
            }

            $match = $items |
                Where-Object {
                    $_.Current.Name -like "*$HaspId*" -or
                    $_.Current.Name -like "*$haspHex*"
                } |
                Select-Object -First 1

            if ($match) {
                $selection = $null
                if ($match.TryGetCurrentPattern(
                    [System.Windows.Automation.SelectionItemPattern]::Pattern,
                    [ref]$selection
                )) {
                    $selection.Select()
                }
            } elseif ($items.Count -gt 1) {
                Close-Manager
                Write-Result $false "HaspNotFound" `
                    "HASP key $HaspId was not present in License Manager." `
                    $app.Id 2
            }
        } finally {
            try {
                $expand.Collapse()
            } catch {
                # The combo may disappear if License Manager refreshes.
            }
        }
    }
}

$elements = @(Get-AppElements)
$sync = $elements |
    Where-Object { $_.Current.AutomationId -eq "1008" } |
    Select-Object -First 1

if (-not $sync -or -not $sync.Current.IsEnabled) {
    $detail = $elements |
        Where-Object {
            $_.Current.ControlType.ProgrammaticName -in
                "ControlType.Text", "ControlType.Edit"
        } |
        ForEach-Object { $_.Current.Name } |
        Where-Object { $_ } |
        Select-Object -Unique
    Close-Manager
    Write-Result $false "SynchronizeDisabled" ($detail -join " | ") $app.Id 2
}

# Give the fresh instance time to establish its internet connection while
# continuously suppressing any window it creates.
if ($StartupSettleSeconds -gt 0) {
    $settleDeadline = (Get-Date).AddSeconds($StartupSettleSeconds)
    while ((Get-Date) -lt $settleDeadline) {
        Update-AppProcess
        Hide-AppWindows
        Start-Sleep -Milliseconds 100
    }
}

# Moving and refreshing native windows invalidates some UI Automation element
# objects. Always reacquire Synchronize immediately before invoking it.
Update-AppProcess
$elements = @(Get-AppElements)
$sync = $elements |
    Where-Object { $_.Current.AutomationId -eq "1008" } |
    Select-Object -First 1
if (-not $sync -or -not $sync.Current.IsEnabled) {
    Close-Manager
    Write-Result $false "ControlNotFound" `
        "Synchronize control 1008 was unavailable after startup." $app.Id 2
}

Invoke-Control $sync

$confirmed = $false
$sawBusy = $false
$nextClicked = $false
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)

while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 100
    Update-AppProcess
    Hide-AppWindows
    $elements = @(Get-AppElements)

    $buttons = @(
        $elements |
            Where-Object {
                $_.Current.ControlType.ProgrammaticName -eq "ControlType.Button" -or
                $_.Current.NativeWindowHandle -ne 0
            }
    )

    $message = @(
        $elements |
            Where-Object {
                $_.Current.ControlType.ProgrammaticName -in
                    "ControlType.Text", "ControlType.Edit", "ControlType.Document",
                    "ControlType.Pane", "ControlType.Window"
            } |
            ForEach-Object { $_.Current.Name } |
            Where-Object { $_ } |
            Select-Object -Unique
    ) -join " | "

    # Synchronize opens a wizard. The button on its introductory page is Next,
    # so clicking control 1008 alone does not start the network operation.
    $next = $buttons |
        Where-Object {
            ($_.Current.Name -replace "&", "") -eq "Next" -and
            $_.Current.IsEnabled
        } |
        Select-Object -First 1

    if ($next -and -not $nextClicked) {
        Invoke-Control $next
        $nextClicked = $true
        $confirmed = $true
        $sawBusy = $true
        # Native button clicks are asynchronous. Give the wizard enough time
        # to replace the page before looking for another enabled Next button.
        Start-Sleep -Milliseconds 750
        continue
    }

    $yes = $buttons |
        Where-Object {
            ($_.Current.Name -replace "&", "") -eq "Yes" -and
            $_.Current.IsEnabled
        } |
        Select-Object -First 1

    if ($yes) {
        Invoke-Control $yes
        $confirmed = $true
        $sawBusy = $true
        continue
    }

    $sync = $elements |
        Where-Object { $_.Current.AutomationId -eq "1008" } |
        Select-Object -First 1
    if ($sync -and -not $sync.Current.IsEnabled) {
        $sawBusy = $true
    }

    $finish = $buttons |
        Where-Object {
            ($_.Current.Name -replace "&", "") -eq "Finish" -and
            $_.Current.IsEnabled
        } |
        Select-Object -First 1

    $operationMatches = [regex]::Matches(
        $message,
        "(?im)(?<step>[A-Za-z][^.\r\n|]*?)\.{2,}\s*(?<status>[A-Za-z]+)"
    )
    $reportedStatuses = @(
        $operationMatches |
            ForEach-Object { $_.Groups["status"].Value }
    )
    $nonSuccess = @(
        $reportedStatuses |
            Where-Object { $_ -notmatch "(?i)^Success$" }
    )
    $confirmationSucceeded = $message -match (
        "(?im)Sending confirmation\.{2,}\s*Success\b"
    )
    $operationSummary = @(
        $operationMatches |
            ForEach-Object { $_.Value.Trim() }
    ) -join " | "

    # This transcript is the vendor's definitive success result. It takes
    # precedence over the unrelated error-42 text/OK control that may remain in
    # the manager's main window while a valid synchronization completes.
    if (
        $reportedStatuses.Count -gt 0 -and
        $nonSuccess.Count -eq 0 -and
        $confirmationSucceeded
    ) {
        $processId = $app.Id
        if ($finish) { Invoke-Control $finish }
        Start-Sleep -Milliseconds 300
        Close-Manager
        Write-Result $true "Completed" $operationSummary $processId 0
    }

    if ($finish -and ($confirmed -or $sawBusy)) {
        $processId = $app.Id
        Invoke-Control $finish
        Start-Sleep -Milliseconds 300
        Close-Manager
        if ($reportedStatuses.Count -gt 0) {
            Write-Result $false "Failed" $operationSummary $processId 1
        }
        Write-Result $false "UnknownResult" `
            "Synchronization finished without an explicit success status: $message" `
            $processId 1
    }

    # Error dialogs use OK. Do not infer completion from a Close control: the
    # manager and wizard can expose multiple ordinary Close controls while the
    # operation is still running.
    $dismiss = $buttons |
        Where-Object {
            $name = $_.Current.Name -replace "&", ""
            $_.Current.IsEnabled -and $name -eq "OK"
        } |
        Select-Object -First 1

    if ($dismiss -and ($confirmed -or $sawBusy)) {
        # The main-window error-42 OK control can coexist with a valid operation.
        # Ignore it while the synchronization transcript is still progressing.
        if ($reportedStatuses.Count -gt 0 -and $nonSuccess.Count -eq 0) {
            continue
        }
        $failed = $message -match (
            "(?i)\b(error|failed|failure|cannot|invalid|not found|no license|" +
            "no internet|no connection|internet connection.*not|unable|" +
            "timed out|too old|needs to be updated|outdated)\b"
        )
        $succeeded = $message -match (
            "(?i)\b(success|successful|successfully|synchronized|" +
            "up[ -]to[ -]date|completed)\b"
        )
        $processId = $app.Id
        Invoke-Control $dismiss
        Start-Sleep -Milliseconds 300
        Close-Manager
        if ($failed) {
            Write-Result $false "Failed" $message $processId 1
        }
        if ($succeeded) {
            Write-Result $true "Completed" $message $processId 0
        }
        Write-Result $false "UnknownResult" `
            "Synchronization returned no explicit success status: $message" `
            $processId 1
    }
}

$processId = $app.Id
Stop-OwnedProcesses
Write-Result $false "TimedOut" `
    "No completion status was detected before the timeout." `
    $processId 3
