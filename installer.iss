#define MyAppName "iWorks Novel Toolkit"
#define MyAppVersion "1.4.1"
#define MyAppPublisher "iWorks"
#define MyAppExeName "iWorks.exe"
#define MyAppURL "https://github.com/3421013896/Qbook"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\iWorks
DefaultGroupName=iWorks
OutputDir=D:\Dbook\tomato-toolkit-release
OutputBaseFilename=iWorks-Setup-{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
LicenseFile=

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
chinesesimplified.FullInstall=一键安装
chinesesimplified.Installing=正在安装，请稍候...
chinesesimplified.InstallFinish=安装完成！
chinesesimplified.RunNow=立即运行 iWorks
chinesesimplified.CreateDesktop=创建桌面快捷方式

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"

[Files]
Source: "iWorks.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\iWorks"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 iWorks"; Filename: "{uninstallexe}"
Name: "{autodesktop}\iWorks"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 iWorks"; Flags: nowait postinstall
