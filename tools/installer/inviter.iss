; Inno Setup 6.7.3+. Compile using python -m tools.build_windows.
#ifndef SourceRoot
  #define SourceRoot "..\.."
#endif
#ifndef AppVersion
  #define AppVersion "1.2.6"
#endif
#ifndef InstallerAppId
  #define InstallerAppId "InviterStudio.6F767BF5-6BE1-46F7-BDF2-B040E81F3691"
#endif
#ifndef OutputName
  #define OutputName "InviterStudioSetup"
#endif
#ifndef SourcePackage
  #define SourcePackage SourceRoot + "\dist\InviterStudio"
#endif
#define BaseLibraryHash GetSHA256OfFile(SourcePackage + "\_internal\base_library.zip")
#ifndef DefaultInstallDir
  #define DefaultInstallDir "{localappdata}\Programs\InviterStudio"
#endif
#ifndef DesktopRoot
  #define DesktopRoot "{autodesktop}"
#endif
#ifndef StartMenuRoot
  #define StartMenuRoot "{autoprograms}"
#endif

[Setup]
AppId={#InstallerAppId}
AppName=Inviter Studio
AppVersion={#AppVersion}
AppPublisher=Inviter Studio
AppCopyright=Copyright (c) 2026 Inviter Studio
AppVerName=Inviter Studio {#AppVersion}
DefaultDirName={#DefaultInstallDir}
DefaultGroupName=Inviter Studio
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=yes
DisableReadyPage=no
DisableFinishedPage=no
UsePreviousAppDir=yes
UsePreviousTasks=yes
WizardStyle=modern dynamic
WizardSizePercent=110
WizardImageFile={#SourceRoot}\build\installer-assets\wizard.bmp
WizardImageFileDynamicDark={#SourceRoot}\build\installer-assets\wizard.bmp
WizardSmallImageFile={#SourceRoot}\build\installer-assets\small.bmp
WizardSmallImageFileDynamicDark={#SourceRoot}\build\installer-assets\small.bmp
SetupIconFile={#SourceRoot}\build\installer-assets\app.ico
UninstallDisplayIcon={app}\InviterStudio.exe
UninstallDisplayName=Inviter Studio
Uninstallable=yes
UninstallLogMode=append
AppMutex=Local\InviterStudio.Running
SetupMutex=InviterStudio.Setup
CloseApplications=yes
RestartApplications=no
AllowRootDirectory=no
AllowUNCPath=no
Compression=lzma2
SolidCompression=yes
OutputDir={#SourceRoot}\dist
OutputBaseFilename={#OutputName}
VersionInfoVersion={#AppVersion}.0
VersionInfoProductName=Inviter Studio
VersionInfoDescription=Inviter Studio Setup

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
russian.WelcomeLabel1=Вас приветствует мастер установки Inviter Studio
russian.WelcomeLabel2=Мастер установит Inviter Studio на ваш компьютер.%n%nВы сможете выбрать папку программы и ярлыки. Python и необходимые библиотеки включены: отдельно скачивать их не нужно.%n%nДля работы нужен интернет и ваш Telegram-аккаунт. Где получить API ID и API Hash, объясняется в форме добавления аккаунта.%n%nДанные хранятся отдельно в профиле Windows и сохраняются при обновлении и удалении программы. Перед обновлением закройте Inviter Studio.
russian.FinishedLabel=Inviter Studio установлена.%n%nОткройте программу и добавьте свой Telegram-аккаунт. Настройки и сохранённые результаты доступны при следующем запуске.

[CustomMessages]
russian.DesktopTask=Создать ярлык на рабочем столе
russian.StartMenuTask=Добавить ярлыки в меню «Пуск»
russian.ShortcutsGroup=Ярлыки:
russian.LaunchApp=Запустить Inviter Studio
russian.InvalidFolder=Выберите отдельную папку программы, например D:\Programs\InviterStudio. Нельзя устанавливать программу в корень диска, системную папку или папку её пользовательских данных.
russian.ProfileNote=Данные и Telegram-сессии хранятся отдельно в профиле Windows и сохраняются после удаления программы.
english.DesktopTask=Create a desktop shortcut
english.StartMenuTask=Add shortcuts to the Start menu
english.ShortcutsGroup=Shortcuts:
english.LaunchApp=Launch Inviter Studio
english.InvalidFolder=Choose a dedicated application folder, for example D:\Programs\InviterStudio. Do not select a drive root, system folder, or the application's user data folder.
english.ProfileNote=Data and Telegram sessions are stored separately in your Windows profile and kept after uninstalling the program.

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopTask}"; GroupDescription: "{cm:ShortcutsGroup}"
Name: "startmenuicon"; Description: "{cm:StartMenuTask}"; GroupDescription: "{cm:ShortcutsGroup}"

[Files]
Source: "{#SourcePackage}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "_internal\base_library.zip,data\*,sessions\*,*.session,*.session-journal,*.session-wal,*.session-shm,*.db,*.db-wal,*.db-shm,.env"
Source: "{#SourcePackage}\_internal\base_library.zip"; DestDir: "{app}\_internal"; Flags: ignoreversion; Check: NeedsBaseLibrary

[UninstallDelete]
; An identical archive can be retained when upgrading an unpacked legacy build.
Type: files; Name: "{app}\_internal\base_library.zip"

[Icons]
Name: "{#DesktopRoot}\Inviter Studio"; Filename: "{app}\InviterStudio.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{#StartMenuRoot}\Inviter Studio\Inviter Studio"; Filename: "{app}\InviterStudio.exe"; WorkingDir: "{app}"; Tasks: startmenuicon
Name: "{#StartMenuRoot}\Inviter Studio\Удалить Inviter Studio"; Filename: "{uninstallexe}"; Tasks: startmenuicon

[InstallDelete]
Type: files; Name: "{#DesktopRoot}\Inviter Studio.lnk"; Tasks: not desktopicon
Type: files; Name: "{#StartMenuRoot}\Inviter Studio\Inviter Studio.lnk"; Tasks: not startmenuicon
Type: files; Name: "{#StartMenuRoot}\Inviter Studio\Удалить Inviter Studio.lnk"; Tasks: not startmenuicon

[Run]
Filename: "{app}\InviterStudio.exe"; Description: "{cm:LaunchApp}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[Code]
function NeedsBaseLibrary: Boolean;
var
  Target: String;
begin
  Result := True;
  Target := ExpandConstant('{app}\_internal\base_library.zip');
  if FileExists(Target) then begin
    try
      Result := CompareText(GetSHA256OfFile(Target), '{#BaseLibraryHash}') <> 0;
      if not Result then
        Log('Стандартная библиотека Python совпадает с установщиком; замена не требуется.');
    except
      Log('Не удалось сравнить стандартную библиотеку Python; используется обычная проверка установки.');
    end;
  end;
end;

function SameOrInside(const Candidate, Parent: String): Boolean;
begin
  Result := Pos(Lowercase(AddBackslash(ExpandFileName(Parent))), Lowercase(AddBackslash(ExpandFileName(Candidate)))) = 1;
end;

function ValidAppFolder: Boolean;
var
  Target, Profile, EnvProfile: String;
begin
  Target := RemoveBackslashUnlessRoot(ExpandFileName(WizardDirValue));
  Profile := ExpandConstant('{localappdata}\InviterStudio');
  EnvProfile := GetEnv('INVITER_HOME');
  Result := (Length(Target) > 3) and
    (CompareText(Target, GetEnv('USERPROFILE')) <> 0) and
    (CompareText(Target, ExpandConstant('{localappdata}')) <> 0) and
    (CompareText(Target, ExpandConstant('{userappdata}')) <> 0) and
    (CompareText(Target, ExpandConstant('{autodesktop}')) <> 0) and
    (CompareText(Target, ExpandConstant('{userdocs}')) <> 0) and
    not SameOrInside(Target, ExpandConstant('{win}')) and
    not SameOrInside(Target, Profile);
  if (EnvProfile <> '') and SameOrInside(Target, EnvProfile) then
    Result := False;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpSelectDir) and not ValidAppFolder then begin
    SuppressibleMsgBox(CustomMessage('InvalidFolder'), mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

procedure InitializeWizard;
var
  ProfileLabel: TNewStaticText;
begin
  ProfileLabel := TNewStaticText.Create(WizardForm);
  ProfileLabel.Parent := WizardForm.SelectDirPage;
  ProfileLabel.Left := WizardForm.DirEdit.Left;
  ProfileLabel.Top := WizardForm.DirEdit.Top + WizardForm.DirEdit.Height + ScaleY(20);
  ProfileLabel.Width := WizardForm.SelectDirLabel.Width;
  ProfileLabel.Height := ScaleY(64);
  ProfileLabel.AutoSize := False;
  ProfileLabel.WordWrap := True;
  ProfileLabel.Caption := CustomMessage('ProfileNote');
end;
