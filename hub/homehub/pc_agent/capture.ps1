param([string]$Out, [int]$MaxSide = 1568)
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
$screen = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$graphics = [System.Drawing.Graphics]::FromImage($screen)
$graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$graphics.Dispose()
$scale = [Math]::Min(1.0, $MaxSide / [Math]::Max($screen.Width, $screen.Height))
$small = New-Object System.Drawing.Bitmap $screen, ([int][Math]::Round($screen.Width * $scale)), ([int][Math]::Round($screen.Height * $scale))
$small.Save($Out, [System.Drawing.Imaging.ImageFormat]::Jpeg)
@{ width = $screen.Width; height = $screen.Height; scale = $scale } | ConvertTo-Json -Compress
