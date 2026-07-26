$ErrorActionPreference = "Stop"

function Write-ProtocolMessage([hashtable]$Message) {
    [Console]::Out.WriteLine(($Message | ConvertTo-Json -Depth 10 -Compress))
    [Console]::Out.Flush()
}

function Get-FreeTcpPort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try {
        return ([Net.IPEndPoint]$listener.LocalEndpoint).Port
    }
    finally {
        $listener.Stop()
    }
}

function Find-Edge([string]$ConfiguredPath) {
    $candidates = @(
        $ConfiguredPath,
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    throw "Microsoft Edge was not found; specify msedge.exe with --edge-path"
}

$edgeProcess = $null
$socket = $null
$profilePath = $null
$removeProfileOnExit = $false

try {
    $initLine = [Console]::In.ReadLine()
    if (-not $initLine) {
        throw "Missing initialization message"
    }
    $init = $initLine | ConvertFrom-Json
    $edgePath = Find-Edge ([string]$init.edge_path)
    $timeoutSeconds = [Math]::Max(1.0, [double]$init.timeout)
    $sourceUserData = Join-Path $env:LOCALAPPDATA "Microsoft\Edge\User Data"
    $sourceLocalState = Join-Path $sourceUserData "Local State"
    $profileName = "Default"
    if (Test-Path -LiteralPath $sourceLocalState -PathType Leaf) {
        try {
            $localState = Get-Content `
                -LiteralPath $sourceLocalState `
                -Raw | ConvertFrom-Json
            if ($localState.profile.last_used) {
                $profileName = [string]$localState.profile.last_used
            }
        }
        catch {
            $profileName = "Default"
        }
    }

    $runningEdge = @(Get-Process "msedge" -ErrorAction SilentlyContinue)
    if ($runningEdge.Count -gt 0) {
        throw (
            "Microsoft Edge is running. Fully exit every Edge process " +
            "and run the command again so the verified profile can be reused."
        )
    }
    $profilePath = Join-Path $env:TEMP (
        "skrbt-edge-" + [guid]::NewGuid().ToString("N")
    )
    $removeProfileOnExit = $true
    $profileMode = "cloned"
    New-Item -ItemType Directory -Path $profilePath -Force | Out-Null
    if (Test-Path -LiteralPath $sourceLocalState -PathType Leaf) {
        Copy-Item `
            -LiteralPath $sourceLocalState `
            -Destination (Join-Path $profilePath "Local State") `
            -Force
    }

    $sourceProfile = Join-Path $sourceUserData $profileName
    $targetProfile = Join-Path $profilePath $profileName
    New-Item -ItemType Directory -Path $targetProfile -Force | Out-Null
    foreach ($fileName in @("Preferences", "Secure Preferences")) {
        $sourceFile = Join-Path $sourceProfile $fileName
        if (Test-Path -LiteralPath $sourceFile -PathType Leaf) {
            Copy-Item `
                -LiteralPath $sourceFile `
                -Destination (Join-Path $targetProfile $fileName) `
                -Force
        }
    }
    $sourceNetwork = Join-Path $sourceProfile "Network"
    $targetNetwork = Join-Path $targetProfile "Network"
    New-Item -ItemType Directory -Path $targetNetwork -Force | Out-Null
    foreach ($fileName in @(
        "Cookies",
        "Cookies-journal",
        "Network Persistent State",
        "TransportSecurity"
    )) {
        $sourceFile = Join-Path $sourceNetwork $fileName
        if (Test-Path -LiteralPath $sourceFile -PathType Leaf) {
            Copy-Item `
                -LiteralPath $sourceFile `
                -Destination (Join-Path $targetNetwork $fileName) `
                -Force
        }
    }
    foreach ($directoryName in @(
        "IndexedDB",
        "Local Storage",
        "Session Storage",
        "Service Worker",
        "shared_proto_db"
    )) {
        $sourceDirectory = Join-Path $sourceProfile $directoryName
        if (Test-Path -LiteralPath $sourceDirectory -PathType Container) {
            Copy-Item `
                -LiteralPath $sourceDirectory `
                -Destination $targetProfile `
                -Recurse `
                -Force
        }
    }
    $port = Get-FreeTcpPort
    $arguments = @(
        "--headless=new",
        "--disable-gpu",
        "--disable-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--remote-allow-origins=*",
        "--remote-debugging-port=$port",
        "--user-data-dir=$profilePath",
        "--profile-directory=$profileName",
        "about:blank"
    )
    $edgeProcess = Start-Process `
        -FilePath $edgePath `
        -ArgumentList $arguments `
        -PassThru `
        -WindowStyle Hidden

    $version = $null
    $startupDeadline = [DateTime]::UtcNow.AddSeconds($timeoutSeconds)
    while ([DateTime]::UtcNow -lt $startupDeadline -and $null -eq $version) {
        try {
            $version = Invoke-RestMethod `
                "http://127.0.0.1:$port/json/version" `
                -TimeoutSec 1
        }
        catch {
            Start-Sleep -Milliseconds 100
        }
    }
    if ($null -eq $version) {
        throw "Timed out while starting the Edge debugging port"
    }

    $target = Invoke-RestMethod `
        -Method Put `
        "http://127.0.0.1:$port/json/new?about:blank"
    $socket = [Net.WebSockets.ClientWebSocket]::new()
    $socket.ConnectAsync(
        [uri]$target.webSocketDebuggerUrl,
        [Threading.CancellationToken]::None
    ).GetAwaiter().GetResult() | Out-Null

    $script:nextCommandId = 0
    function Invoke-Cdp([string]$Method, [hashtable]$Params = @{}) {
        $script:nextCommandId++
        $commandId = $script:nextCommandId
        $payload = @{
            id = $commandId
            method = $Method
            params = $Params
        } | ConvertTo-Json -Depth 20 -Compress
        $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
        $cancellation = [Threading.CancellationTokenSource]::new(
            [TimeSpan]::FromSeconds($timeoutSeconds)
        )
        try {
            $socket.SendAsync(
                [ArraySegment[byte]]::new($bytes),
                [Net.WebSockets.WebSocketMessageType]::Text,
                $true,
                $cancellation.Token
            ).GetAwaiter().GetResult() | Out-Null

            while ($true) {
                $stream = [IO.MemoryStream]::new()
                do {
                    $buffer = [byte[]]::new(65536)
                    $received = $socket.ReceiveAsync(
                        [ArraySegment[byte]]::new($buffer),
                        $cancellation.Token
                    ).GetAwaiter().GetResult()
                    if (
                        $received.MessageType -eq
                        [Net.WebSockets.WebSocketMessageType]::Close
                    ) {
                        throw "The Edge debugging connection was closed"
                    }
                    $stream.Write($buffer, 0, $received.Count)
                } while (-not $received.EndOfMessage)

                $message = (
                    [Text.Encoding]::UTF8.GetString($stream.ToArray()) |
                    ConvertFrom-Json
                )
                if ($message.id -eq $commandId) {
                    if ($null -ne $message.error) {
                        throw ($message.error | ConvertTo-Json -Compress)
                    }
                    return $message.result
                }
            }
        }
        catch [OperationCanceledException] {
            throw "Timed out waiting for an Edge debugging response"
        }
        finally {
            $cancellation.Dispose()
        }
    }

    Invoke-Cdp "Network.enable" | Out-Null
    Invoke-Cdp "Page.enable" | Out-Null

    Write-ProtocolMessage @{
        ok = $true
        ready = $true
        browser = [string]$version.Browser
        profile_mode = $profileMode
    }

    $hasOriginPage = $false
    while ($true) {
        $requestLine = [Console]::In.ReadLine()
        if ($null -eq $requestLine) {
            break
        }
        try {
            $request = $requestLine | ConvertFrom-Json
            if ($request.command -eq "close") {
                break
            }
            if ($request.command -ne "fetch") {
                throw "Unknown Edge helper command"
            }

            if (-not $hasOriginPage) {
                $navigation = Invoke-Cdp "Page.navigate" @{
                    url = [string]$request.url
                    referrer = [string]$request.referer
                }
                if ($navigation.errorText) {
                    throw "Edge navigation failed: $($navigation.errorText)"
                }

                $deadline = [DateTime]::UtcNow.AddSeconds($timeoutSeconds)
                $pageState = $null
                while ([DateTime]::UtcNow -lt $deadline) {
                    $stateResult = Invoke-Cdp "Runtime.evaluate" @{
                        expression = (
                            "JSON.stringify({" +
                            "ready:document.readyState," +
                            "url:location.href})"
                        )
                        returnByValue = $true
                    }
                    if ($stateResult.result.value) {
                        $pageState = (
                            $stateResult.result.value | ConvertFrom-Json
                        )
                        if ($pageState.ready -eq "complete") {
                            break
                        }
                    }
                    Start-Sleep -Milliseconds 100
                }
                if ($null -eq $pageState -or $pageState.ready -ne "complete") {
                    throw "Timed out while loading the Edge page: $($request.url)"
                }

                # A real browser can finish a managed challenge itself.
                $document = $null
                $challengeDeadline = [DateTime]::UtcNow.AddSeconds(
                    [Math]::Min(5.0, $timeoutSeconds)
                )
                do {
                    Start-Sleep -Milliseconds 500
                    $documentResult = Invoke-Cdp "Runtime.evaluate" @{
                        expression = (
                            "JSON.stringify({" +
                            "url:location.href," +
                            "html:document.documentElement.outerHTML})"
                        )
                        returnByValue = $true
                    }
                    $document = (
                        $documentResult.result.value | ConvertFrom-Json
                    )
                    $isChallenge = (
                        $document.html -match
                        "(?i)cf-chl-|challenge-platform|just a moment"
                    )
                } while (
                    $isChallenge -and
                    [DateTime]::UtcNow -lt $challengeDeadline
                )
                $hasOriginPage = $true
            }
            else {
                $urlJson = (
                    [string]$request.url | ConvertTo-Json -Compress
                )
                $fetchExpression = (
                    "(async()=>{" +
                    "const response=await fetch($urlJson,{" +
                    "credentials:'include',cache:'no-store'});" +
                    "const html=await response.text();" +
                    "return JSON.stringify({" +
                    "url:response.url,html:html,status:response.status});" +
                    "})()"
                )
                $fetchResult = Invoke-Cdp "Runtime.evaluate" @{
                    expression = $fetchExpression
                    awaitPromise = $true
                    returnByValue = $true
                }
                if ($fetchResult.exceptionDetails) {
                    throw "Edge fetch failed for $($request.url)"
                }
                $document = $fetchResult.result.value | ConvertFrom-Json
            }
            $htmlBytes = [Text.Encoding]::UTF8.GetBytes([string]$document.html)
            Write-ProtocolMessage @{
                ok = $true
                final_url = [string]$document.url
                document_b64 = [Convert]::ToBase64String($htmlBytes)
            }
        }
        catch {
            Write-ProtocolMessage @{
                ok = $false
                error = $_.Exception.Message
            }
        }
    }
}
catch {
    Write-ProtocolMessage @{
        ok = $false
        fatal = $true
        error = $_.Exception.Message
    }
    exit 1
}
finally {
    if ($null -ne $socket) {
        $socket.Dispose()
    }
    if ($null -ne $edgeProcess -and -not $edgeProcess.HasExited) {
        & taskkill.exe /PID $edgeProcess.Id /T /F 2>$null | Out-Null
    }
    Start-Sleep -Milliseconds 200
    if ($removeProfileOnExit -and $profilePath) {
        Remove-Item `
            -LiteralPath $profilePath `
            -Recurse `
            -Force `
            -ErrorAction SilentlyContinue
    }
}
