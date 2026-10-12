# collect.ps1 - scan qoder CLI transcripts and emit JSON usage stats (incremental)
# Qoder's local usage fields are all 0, so we estimate tokens from character counts.
# Each assistant request is assumed to resend the whole preceding context (capped at $CAP chars).
#
# Incremental: per-file stats are cached in files-state.json keyed by path+mtime+size,
# so a rescan only re-parses new/changed transcripts (usually a few seconds).
param([switch]$Full, [int]$CustomWindowK = 300)
$ErrorActionPreference = 'SilentlyContinue'
# ensure UTF-8 output when piped/captured (PS 5.1 defaults to OEM codepage)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

$conv = 0.32      # chars -> tokens (mixed cn/en/code)
$CAP  = [long](200000 / $conv)             # qoder built-in models: 200K token window -> ~625K chars
$CAPC = [long]($CustomWindowK * 1000 / $conv)  # BYOK (qoder-custom-*) models: default 300K tokens
$MaxCap = [Math]::Max($CAP, $CAPC)
$StateVersion = 6   # bump when estimation logic changes (invalidates old cache)
$StatePath = Join-Path $PSScriptRoot 'files-state.json'

# Must stay behaviourally identical to normModel() in index.html and norm_model() in
# collect-ccswitch.py. Changing it means bumping $StateVersion (full rescan).
function NormModel([string]$s) {
  $t = "$s".Trim().ToLower()                          # "$s" turns $null into ''
  if ($t -eq '' -or $t -eq '<synthetic>') { return 'unknown' }
  $i = $t.LastIndexOf('/')
  if ($i -ge 0) { $t = $t.Substring($i + 1) }
  $t = $t -replace '(-[a-z]{1,4})?-[0-9]{4,8}$', ''   # single-quoted: $ must not interpolate
  $t = $t -replace '[-_.]+$', ''
  if ($t -eq '' -or $t -eq '<synthetic>') { return 'unknown' }
  return $t
}

# Accumulate into a per-model bucket. Never assign a cached hashtable by reference here:
# $acc[$k] = $cached[$k] aliases it, and aggregation runs before the state file is saved,
# so the all-files sum would be written back into per-file state and inflate every later run.
function Add-Model([hashtable]$m, [string]$model, [int]$req, [long]$inC, [long]$outC) {
  if ($null -eq $m[$model]) { $m[$model] = @{ req = 0; inC = [long]0; outC = [long]0 } }
  $m[$model].req  += $req
  $m[$model].inC  += $inC
  $m[$model].outC += $outC
}

$roots = @(
  @{ key = 'intl'; label = 'Qoder CLI 国际版 (.qoder)';   path = "$env:USERPROFILE\.qoder\projects";   real = $false },
  @{ key = 'cn';   label = 'Qoder CLI 国内版 (.qoder-cn)'; path = "$env:USERPROFILE\.qoder-cn\projects"; real = $false },
  # Claude Code transcripts record REAL usage tokens -> direct sum, no estimation/cap
  @{ key = 'cc';   label = 'Claude Code (.claude)';        path = "$env:USERPROFILE\.claude\projects"; real = $true },
  # CodeBuddy: real usage (input_tokens/output_tokens), epoch-ms timestamps
  @{ key = 'codebuddy'; label = 'CodeBuddy (.codebuddy)';  path = "$env:USERPROFILE\.codebuddy\projects"; real = $true },
  # Cline CLI: per-session *.messages.json with metrics.inputTokens/outputTokens + epoch-ms ts
  @{ key = 'cline'; label = 'Cline CLI (.cline)'; path = "$env:USERPROFILE\.cline\data\sessions"; real = $true; filter = '*.messages.json' },
  # Cline VSCode extension (older): per-task api_conversation_history.json with metrics.tokens
  @{ key = 'cline-vsc'; label = 'Cline (VSCode 插件)'; path = "$env:APPDATA\Code\User\globalStorage\saoudrizwan.claude-dev\tasks"; real = $true; filter = 'api_conversation_history.json' },
  # Reasonix: per-request stats jsonl (prompt/completion/cache, ISO+tz timestamps)
  @{ key = 'reasonix'; label = 'Reasonix'; path = "$env:APPDATA\reasonix\stats"; real = $true }
)

# ---- load state: path -> @{ mtime; size; days = { day -> @{req;inC;outC;m{model}} } } ----
$state = @{}
if (-not $Full -and (Test-Path $StatePath)) {
  try {
    $raw = Get-Content $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($raw.version -eq $StateVersion) {
      foreach ($p in $raw.files.PSObject.Properties) {
        $e = $p.Value
        $days = @{}
        foreach ($d in $e.days.PSObject.Properties) {
          # $null test is mandatory: SilentlyContinue swallows the error from indexing $null,
          # which would drop model data silently while every source total still looked right.
          $mm = @{}
          if ($null -ne $d.Value.m) {
            foreach ($mp in $d.Value.m.PSObject.Properties) {
              $mm[$mp.Name] = @{ req = [int]$mp.Value.req; inC = [long]$mp.Value.inC; outC = [long]$mp.Value.outC }
            }
          }
          $days[$d.Name] = @{ req = [int]$d.Value.requests; inC = [long]$d.Value.inC; outC = [long]$d.Value.outC; m = $mm }
        }
        $state[$p.Name] = @{ mtime = $e.mtime; size = [long]$e.size; root = $e.root; days = $days }
      }
    }
  } catch { $state = @{} }
}

$seen = @{}
$reused = 0; $reparsed = 0
foreach ($r in $roots) {
  $flt = '*.jsonl'
  if ($r.filter) { $flt = $r.filter }
  $files = Get-ChildItem $r.path -Recurse -File -Filter $flt -ErrorAction SilentlyContinue
  foreach ($f in $files) {
    $seen[$f.FullName] = $true
    $mtime = $f.LastWriteTimeUtc.ToString('o')
    $old = $state[$f.FullName]
    if ($old -and $old.mtime -eq $mtime -and $old.size -eq $f.Length) {
      $reused++
      continue  # unchanged, keep cached per-file stats
    }
    # ---- parse one transcript ----
    $ctx = [long]0
    $days = @{}
    # Cline (CLI + VSCode): whole-file JSON array; CLI uses metrics.{inputTokens,outputTokens},
    # VSCode extension uses metrics.tokens.{prompt,completion}; both carry epoch-ms ts
    if ($r.key -eq 'cline' -or $r.key -eq 'cline-vsc') {
      try {
        $obj = Get-Content $f.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
        # CLI: {version, messages:[...]};  VSCode extension: top-level array
        $arr = $obj
        if ($obj -is [array]) { $arr = $obj } elseif ($obj.messages) { $arr = $obj.messages }
        foreach ($e in $arr) {
          if ($e.role -ne 'assistant') { continue }
          if (-not $e.metrics) { continue }
          $inT = $null; $outT = $null
          if ($e.metrics.inputTokens -ne $null) {
            $inT = [long]$e.metrics.inputTokens
            $outT = [long]$e.metrics.outputTokens
          } elseif ($e.metrics.tokens) {
            $inT = [long]$e.metrics.tokens.prompt
            $outT = [long]$e.metrics.tokens.completion
          }
          if ($null -eq $inT) { continue }
          $d = 'unknown'
          if ($e.ts) {
            try { $d = [DateTimeOffset]::FromUnixTimeMilliseconds([long]$e.ts).UtcDateTime.ToString('yyyy-MM-dd') } catch {}
          }
          if (-not $days[$d]) { $days[$d] = @{ req = 0; inC = [long]0; outC = [long]0; m = @{} } }
          $days[$d].req++
          $days[$d].inC += $inT
          $days[$d].outC += $outT
          Add-Model $days[$d].m (NormModel $e.modelInfo.id) 1 $inT $outT
        }
      } catch {}
      $state[$f.FullName] = @{ mtime = $mtime; size = $f.Length; root = $r.key; days = $days }
      $reparsed++
      continue
    }
    foreach ($line in [System.IO.File]::ReadLines($f.FullName)) {
      if ($line.Length -lt 30) { continue }
      # Reasonix: per-request JSON lines, prompt/completion, ISO timestamp with tz
      if ($r.key -eq 'reasonix') {
        try { $o = $line | ConvertFrom-Json } catch { continue }
        if ($null -eq $o -or $null -eq $o.prompt) { continue }
        $d = 'unknown'
        if ($o.ts) {
          try { $d = ([DateTimeOffset]::Parse([string]$o.ts, [Globalization.CultureInfo]::InvariantCulture)).UtcDateTime.ToString('yyyy-MM-dd') } catch {}
        }
        if (-not $days[$d]) { $days[$d] = @{ req = 0; inC = [long]0; outC = [long]0; m = @{} } }
        $rq = 1
        if ($o.requests) { $rq = [int]$o.requests }
        $rp = [long]$o.prompt
        $rc = [long]$o.completion
        $days[$d].req += $rq
        $days[$d].inC += $rp
        $days[$d].outC += $rc
        Add-Model $days[$d].m (NormModel $o.model) $rq $rp $rc
        continue
      }
      # CodeBuddy: type:"message" role:"assistant", epoch-ms timestamp, real usage
      if ($r.key -eq 'codebuddy') {
        if ($line -like '*"role":"assistant"*' -and $line -match '"input_tokens":(\d+)[^}]*?"output_tokens":(\d+)') {
          $inT = [long]$matches[1]; $outT = [long]$matches[2]
          $d = 'unknown'
          if ($line -match '"timestamp":(\d{13})') {
            $d = [DateTimeOffset]::FromUnixTimeMilliseconds([long]$matches[1]).UtcDateTime.ToString('yyyy-MM-dd')
          }
          $rawM = ''
          if ($line -match '"providerData":\{[^}]*"model":"([^"]*)"') { $rawM = $matches[1] }
          if (-not $days[$d]) { $days[$d] = @{ req = 0; inC = [long]0; outC = [long]0; m = @{} } }
          $days[$d].req++
          $days[$d].inC += $inT
          $days[$d].outC += $outT
          Add-Model $days[$d].m (NormModel $rawM) 1 $inT $outT
        }
        continue
      }
      if ($line -like '*"type":"assistant"*') {
        $d = 'unknown'
        if ($line -match '"timestamp":"(\d{4}-\d{2}-\d{2})') { $d = $matches[1] }
        # "model":" occurs exactly once per assistant line in qoder + claude transcripts
        $rawM = ''
        if ($line -match '"model":"([^"]+)"') { $rawM = $matches[1] }
        $model = NormModel $rawM
        if (-not $days[$d]) { $days[$d] = @{ req = 0; inC = [long]0; outC = [long]0; m = @{} } }
        if ($r.real) {
          # Claude Code: sum REAL usage tokens (input + cache read/creation as input side)
          if ($line -match '"usage":\{[^}]*"input_tokens":(\d+)[^}]*"cache_creation_input_tokens":(\d+)[^}]*"cache_read_input_tokens":(\d+)[^}]*"output_tokens":(\d+)') {
            $ui = [long]$matches[1] + [long]$matches[2] + [long]$matches[3]
            $uo = [long]$matches[4]
            $days[$d].req++
            $days[$d].inC += $ui
            $days[$d].outC += $uo
            Add-Model $days[$d].m $model 1 $ui $uo
          }
          continue
        }
        # Qoder (estimated): per-model window, BYOK custom -> 300K, built-ins -> 200K
        $cap = $CAP
        if ($rawM -match '^qoder-custom-') { $cap = $CAPC }
        $inEst = [Math]::Min($ctx, $cap)
        $days[$d].req++
        $days[$d].inC += $inEst
        $outL = [long]0
        foreach ($tx in [regex]::Matches($line, '"text":"((?:[^"\\]|\\.)*)"')) { $outL += $tx.Groups[1].Value.Length }
        $outEst = [Math]::Min($outL, $cap)
        $days[$d].outC += $outEst
        Add-Model $days[$d].m $model 1 $inEst $outEst
      }
      if (-not $r.real) {
        $ctx += $line.Length
        if ($ctx -gt $MaxCap) { $ctx = $MaxCap }
      }
    }
    $state[$f.FullName] = @{ mtime = $mtime; size = $f.Length; root = $r.key; days = $days }
    $reparsed++
  }
}
# drop entries for deleted files
foreach ($k in @($state.Keys)) { if (-not $seen.ContainsKey($k)) { $state.Remove($k) } }

# ---- aggregate ----
$outRoots = @()
foreach ($r in $roots) {
  $day = @{}
  $sess = 0
  foreach ($e in $state.Values) {
    if ($e.root -ne $r.key) { continue }
    $sess++
    foreach ($k in $e.days.Keys) {
      $v = $e.days[$k]
      if (-not $day[$k]) { $day[$k] = @{ req = 0; inC = [long]0; outC = [long]0; m = @{} } }
      $day[$k].req += $v.req; $day[$k].inC += $v.inC; $day[$k].outC += $v.outC
      if ($null -ne $v.m) {
        foreach ($mk in $v.m.Keys) {
          $mv = $v.m[$mk]
          # copy-in, never `$day[$k].m[$mk] = $mv` -- that aliases per-file state and the
          # sum would then be persisted back, permanently inflating later incremental runs
          if ($null -eq $day[$k].m[$mk]) { $day[$k].m[$mk] = @{ req = 0; inC = [long]0; outC = [long]0 } }
          $day[$k].m[$mk].req  += $mv.req
          $day[$k].m[$mk].inC  += $mv.inC
          $day[$k].m[$mk].outC += $mv.outC
        }
      }
    }
  }
  $days = @()
  $mult = if ($r.real) { 1 } else { $conv }   # real usage = already tokens; estimated needs conv
  foreach ($k in ($day.Keys | Sort-Object)) {
    $v = $day[$k]
    $bm = [ordered]@{}
    foreach ($mk in ($v.m.Keys | Sort-Object)) {
      $mv = $v.m[$mk]
      $bm[$mk] = [pscustomobject]@{
        requests = $mv.req
        inTokens  = [long][Math]::Round($mv.inC * $mult)
        outTokens = [long][Math]::Round($mv.outC * $mult)
      }
    }
    $days += [pscustomobject]@{
      day = $k; requests = $v.req
      inTokens  = [long][Math]::Round($v.inC * $mult)
      outTokens = [long][Math]::Round($v.outC * $mult)
      byModel = $bm
    }
  }
  [long]$inSum  = ($days | Measure-Object -Property inTokens -Sum).Sum
  [long]$outSum = ($days | Measure-Object -Property outTokens -Sum).Sum
  $reqSum = ($days | Measure-Object -Property requests -Sum).Sum
  $outRoots += [pscustomobject]@{
    key = $r.key; label = $r.label; sessions = $sess; real = [bool]$r.real
    requests = [int]$reqSum; inTokens = $inSum; outTokens = $outSum; days = $days
  }
}

# ---- save state ----
$save = [ordered]@{}
foreach ($k in ($state.Keys | Sort-Object)) {
  $e = $state[$k]
  $dobj = [ordered]@{}
  foreach ($dk in ($e.days.Keys | Sort-Object)) {
    $dv = $e.days[$dk]
    $mm = [ordered]@{}
    if ($null -ne $dv.m) {
      foreach ($mk in ($dv.m.Keys | Sort-Object)) {
        $mv = $dv.m[$mk]
        $mm[$mk] = [ordered]@{ req = $mv.req; inC = $mv.inC; outC = $mv.outC }
      }
    }
    $dobj[$dk] = [ordered]@{ requests = $dv.req; inC = $dv.inC; outC = $dv.outC; m = $mm }
  }
  $save[$k] = [ordered]@{ mtime = $e.mtime; size = $e.size; root = $e.root; days = $dobj }
}
$saveObj = [ordered]@{ version = $StateVersion; files = [pscustomobject]$save }
# Depth must stay well above the nesting: roots -> days -> day -> byModel -> model -> fields
# needs 6 exactly, so the old -Depth 6 had zero headroom and would have emitted the string
# "System.Collections.Hashtable", which the front end iterates as 28 bogus character models.
[System.IO.File]::WriteAllText($StatePath, ($saveObj | ConvertTo-Json -Depth 10 -Compress), (New-Object System.Text.UTF8Encoding($false)))

[pscustomobject]@{
  generatedAt = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
  conv        = $conv
  cap         = $CAP
  customWindowK = $CustomWindowK
  customCap   = $CAPC
  stateVersion = $StateVersion
  incremental = -not $Full
  reusedFiles = $reused
  reparsedFiles = $reparsed
  note        = 'qoder values are character-based estimates (built-in 200K / BYOK customWindowK windows); claude code values are REAL usage tokens from transcripts; byModel keys are normalized (provider prefix and date suffix stripped), cost only comes from cc-switch'
  roots       = $outRoots
} | ConvertTo-Json -Depth 10 -Compress
