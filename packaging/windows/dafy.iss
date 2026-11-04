#ifndef Payload
  #error Payload directory is required
#endif
#ifndef ProductVersion
  #define ProductVersion "1.0.0rc1"
#endif
[Setup]
AppId={{5C279313-55C9-494E-9A03-BA4E9B00DB84}
AppName=大肥鱼
AppVersion={#ProductVersion}
VersionInfoVersion=1.0.0.1
DefaultDirName={localappdata}\Programs\Dafeiyu
DefaultGroupName=大肥鱼
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
OutputBaseFilename=Dafeiyu-{#ProductVersion}-windows-x64-setup
OutputDir=..\..\dist
CloseApplications=yes
RestartApplications=no
DisableProgramGroupPage=yes
UninstallDisplayName=大肥鱼（保留记忆和设置）
[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
[Files]
Source: "{#Payload}\*"; DestDir: "{app}"; Excludes: "*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked
[Icons]
Name: "{group}\大肥鱼"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\launch.py"" --open"; WorkingDir: "{app}"
Name: "{userdesktop}\大肥鱼"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\launch.py"" --open"; WorkingDir: "{app}"; Tasks: desktopicon
[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\launch.py"" --open"; Description: "打开大肥鱼控制台"; Flags: nowait postinstall skipifsilent
