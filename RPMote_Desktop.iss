; RPMote installer export
; Build this script with Inno Setup 6. The generated installer is written to
; the Desktop as: RPMote_Setup.exe

#define MyAppName      "RPMote"
#ifndef MyAppVersion
  #define MyAppVersion "5.1.0"
#endif
#define MyAppPublisher "OnlyCyber"
#define MyAppExeName   "RPMote.exe"
#define DesktopDir     "C:\Users\grant\OneDrive\Desktop"
#define ProjectDir     RemoveBackslashUnlessRoot(SourcePath)
#define SourceDir      AddBackslash(ProjectDir) + "dist\RPMote"

#if !FileExists(SourceDir + "\_internal\vcruntime140.dll") || !FileExists(SourceDir + "\_internal\vcruntime140_1.dll") || !FileExists(SourceDir + "\_internal\msvcp140.dll")
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
OutputDir={#DesktopDir}
OutputBaseFilename=RPMote_Setup
SetupIconFile={#ProjectDir}\app\resources\icons\app_icon.ico
LicenseFile={#ProjectDir}\LICENSE.txt
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

; Remove the previous bundled runtime before copying the new one so stale
; PyInstaller files cannot survive an upgrade.
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

; PyInstaller --onedir output: the EXE and its _internal runtime are both
; required, so package the complete dist\RPMote directory.
[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ProjectDir}\LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
