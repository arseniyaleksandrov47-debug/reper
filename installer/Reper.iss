#define MyAppName "Reper"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Reper"
#define MyAppExeName "Reper.exe"

[Setup]
AppId={{A7E4C910-4D2B-4F88-8A2C-9E31B60D1102}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=Reper_Setup
SetupIconFile=..\assets\reper.ico
UninstallDisplayIcon={app}\reper.ico
WizardImageFile=..\assets\wizard_large.bmp
WizardSmallImageFile=..\assets\wizard_small.bmp
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
WizardImageStretch=no
CloseApplications=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal\frontend_dist"

[Files]
Source: "..\dist\Reper\*"; DestDir: "{app}"; Excludes: "data,data\*,.local-secrets,.local-secrets\*,.local-cache,.local-cache\*,*.log"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\assets\reper.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "installed.flag"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\reper.ico"
Name: "{autoprograms}\Удалить {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\reper.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
