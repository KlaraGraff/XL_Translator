#ifndef AppName
  #define AppName "Translator"
#endif
#define AppPublisher "KlaraGraff"

#ifndef AppExeName
  #define AppExeName "Translator.exe"
#endif

#ifndef WindowsPackageName
  #define WindowsPackageName "Translator_Windows"
#endif

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#ifndef OutputBaseName
  #define OutputBaseName "Translator_Windows_" + AppVersion + "_Setup"
#endif

[Setup]
AppId={{7D2B0122-4A35-45E7-9C22-FAB9EE33F4D5}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename={#OutputBaseName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=assets\app-icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "..\..\dist\{#WindowsPackageName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
