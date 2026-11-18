; ==========================================================================
; RPMote — Inno Setup Installer Script
; ==========================================================================
; Build the PyInstaller --onedir output first:
;   .venv\Scripts\python.exe build.py --installer
;
; Then compile this script with Inno Setup 6:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
;
; Output: Output\RPMote_Setup.exe
; ==========================================================================

#define MyAppName      "RPMote"
; build.py passes /DMyAppVersion=<app/version.py's APP_VERSION>, so the
; installer and Add/Remove Programs always match the app. Compiled by hand
; without it, this fallback is used.
#ifndef MyAppVersion
  #define MyAppVersion "5.0.8"
#endif
#define MyAppPublisher "Grant"
#define MyAppExeName   "RPMote.exe"
#define MyAppURL       ""

; Fail even a manual Inno compilation if the bootstrap runtime is incomplete.
#if !FileExists("dist\RPMote\_internal\vcruntime140.dll") || !FileExists("dist\RPMote\_internal\vcruntime140_1.dll") || !FileExists("dist\RPMote\_internal\msvcp140.dll")
  #error "Incomplete Python / Visual C++ runtime. Run .venv\Scripts\python.exe build.py before compiling the installer."
#endif

[Setup]
AppId={{CED27D19-29F9-48F7-9840-2EA59F510D0B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=RPMote_Setup
SetupIconFile=app\resources\icons\app_icon.ico
LicenseFile=LICENSE.txt
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

; Inno only adds and overwrites -- it never removes files that dropped out of
; a later build. A stale orphan under _internal is not inert: that folder is on
; the frozen app's sys.path, so a leftover extension module with no package left
; around it (_internal\yaml\_yaml.pyd from an older build) still imports, and
; then crashes startup. Every upgrade starts from a clean _internal.
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Files]
; Include the entire PyInstaller --onedir output folder
Source: "dist\RPMote\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}";         Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";    Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
