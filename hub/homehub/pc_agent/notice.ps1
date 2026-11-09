param([string]$Text, [int]$Ms = 2500)
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = 'None'
$form.TopMost = $true
$form.ShowInTaskbar = $false
$form.StartPosition = 'Manual'
$form.BackColor = [System.Drawing.ColorTranslator]::FromHtml('#202020')
$form.AutoSize = $true
$form.AutoSizeMode = 'GrowAndShrink'
$label = New-Object System.Windows.Forms.Label
$label.Text = $Text
$label.Font = New-Object System.Drawing.Font('Segoe UI', 14)
$label.ForeColor = [System.Drawing.Color]::White
$label.AutoSize = $true
$label.Padding = New-Object System.Windows.Forms.Padding(24, 12, 24, 12)
$form.Controls.Add($label)
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $Ms
$timer.Add_Tick({ $form.Close() })
$form.Add_Shown({ $area = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea; $form.Location = New-Object System.Drawing.Point([int](($area.Width - $form.Width) / 2), 40); $timer.Start() })
[System.Windows.Forms.Application]::Run($form)
'shown'
