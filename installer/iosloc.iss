; iOS 虚拟定位轨迹工具 —— Windows 安装包脚本（Inno Setup 6）
;
; 构建：
;   1) .venv\Scripts\python.exe -m PyInstaller --clean --noconfirm Location.spec
;   2) "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\iosloc.iss
;
; 产物：dist\installer\iOSLocationTrack-1.1.0_win-setup.exe
;
; 说明：
; - 安装包**不包含** Apple 的驱动。程序首次启动时会自动检测 Apple Mobile Device
;   Support，缺失时自动下载安装（因此 exe 以 requireAdministrator 清单启动）。
; - 安装目录默认 %LOCALAPPDATA%\Programs\iOSLocationTrack，避免 UAC 写 Program Files
;   带来的虚拟化问题；用户数据在 %LOCALAPPDATA%\iOSLocationTrack（不随卸载删除）。

#define MyAppName "iOS 虚拟定位轨迹工具"
#define MyAppNameEn "iOSLocationTrack"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "本地工具"
#define MyAppExeName "iOSLocationTrack.exe"
#define DistDir "..\dist\iOSLocationTrack"

[Setup]
AppId={{8F3A5C21-6B4E-4D7A-9E15-2C7B0A4F9D31}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppNameEn}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 每用户安装，不需要管理员；但程序自身启动时会请求提权（装 Apple 驱动用）
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
; 产物名带 _win 后缀，与 macOS 的 _mac 对应，Release 页面上一眼能分清平台。
OutputBaseFilename={#MyAppNameEn}-{#MyAppVersion}_win-setup
SetupIconFile=..\resources\AppIcon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; 程序需要 Windows 10 1809+ / Windows 11
DisableWelcomePage=no
LicenseFile=..\README.md

[Languages]
; Inno Setup 6.7 起官方安装包不再自带简体中文，因此把翻译文件随仓库一起带着
; （installer\languages\ChineseSimplified.isl），保证任何机器上都能构建出中文安装向导。
; 文件缺失时自动退回英文，不会让构建失败。
#define ChineseIsl "languages\ChineseSimplified.isl"
#if FileExists(AddBackslash(SourcePath) + ChineseIsl)
Name: "chinese"; MessagesFile: "compiler:Default.isl,{#ChineseIsl}"
#endif
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: checkedonce
Name: "quicklaunchicon"; Description: "创建快速启动栏快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 注意：不要在这里放 Apple 的驱动/安装包。程序会按需自动下载，避免分发第三方二进制。

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\检查 Apple 驱动"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--check-driver"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: quicklaunchicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只删安装目录内的临时文件；用户数据（%LOCALAPPDATA%\iOSLocationTrack）保留，
; 里面有 config.json 和瓦片缓存，卸载后重装还能用。
Type: filesandordirs; Name: "{app}\_internal\__pycache__"

[Code]
// 安装完成后提示用户：首次启动会自动安装 Apple 驱动（可能需要几分钟）
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // 无需额外动作；提示由 [Run] 段的 postinstall 复选框承担
  end;
end;
