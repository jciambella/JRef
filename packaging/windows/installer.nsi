; JRef per-user installer (no administrator rights needed).
; Built by build_windows.py:  makensis -DVERSION=x.y.z -DSTAGE=<dir> -DOUTFILE=<exe> -DWIZARD_BMP=<bmp> installer.nsi
Target amd64-unicode  ; 64-bit like the bundled Python (also lets it run under a 64-bit-only Wine)
!include "MUI2.nsh"
!include "LogicLib.nsh"

!define APPNAME "JRef"
!define APPSUBTITLE "Journal Reference Manager"
!define AUTHOR "JC"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\JRef"
; Earlier product names, each with its own folder, shortcuts and uninstall entry: 0.5.x "JBibManager", <= 0.4 "BibManager".
!define LEGACY_UNINSTKEY_1 "Software\Microsoft\Windows\CurrentVersion\Uninstall\JBibManager"
!define LEGACY_UNINSTKEY_2 "Software\Microsoft\Windows\CurrentVersion\Uninstall\BibManager"

Name "${APPNAME} ${VERSION}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\JRef"
InstallDirRegKey HKCU "Software\JRef" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 64
BrandingText "${APPNAME} ${VERSION}"

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "FileDescription" "${APPNAME} - ${APPSUBTITLE} Setup"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "${AUTHOR}"

!define MUI_ICON "${STAGE}\JRef.ico"
!define MUI_UNICON "${STAGE}\JRef.ico"
!ifdef WIZARD_BMP
  !define MUI_WELCOMEFINISHPAGE_BITMAP "${WIZARD_BMP}"
!endif
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "This installs ${APPNAME} ${VERSION} (${APPSUBTITLE}) for your Windows account only (no administrator rights needed).$\r$\n$\r$\nYour library (PDFs, .bib files, database) is never stored in the program folder, and uninstalling never touches it.$\r$\n$\r$\nAn existing JBibManager or BibManager installation is replaced; its settings and library are carried over."
!define MUI_FINISHPAGE_RUN "$INSTDIR\JRef.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Start ${APPNAME}"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

!macro RemoveLegacy KEY LABEL
  ReadRegStr $0 HKCU "${KEY}" "InstallLocation"
  ${If} $0 != ""
  ${AndIf} ${FileExists} "$0\Uninstall.exe"
    DetailPrint "Removing the previous ${LABEL} installation in $0"
    ExecWait '"$0\Uninstall.exe" /S _?=$0'
    Delete "$0\Uninstall.exe"
    RMDir "$0"
  ${EndIf}
!macroend

Section "${APPNAME}" SecMain
  SectionIn RO
  ; Window layout and theme of JBibManager 0.5 live in the registry key its uninstaller deletes: keep a copy
  ; under the new name first (only if JRef has none yet).
  EnumRegKey $1 HKCU "Software\JRef\JRef" 0
  ${If} $1 == ""
    nsExec::Exec 'reg copy "HKCU\Software\JBibManager\JBibManager" "HKCU\Software\JRef\JRef" /s /f'
    Pop $1
  ${EndIf}
  ; Replace an installation under an earlier name. Its uninstaller removes only the program files, the
  ; shortcuts and its registry entries -- never the library or the settings file (migrated at first start).
  !insertmacro RemoveLegacy "${LEGACY_UNINSTKEY_1}" "JBibManager"
  !insertmacro RemoveLegacy "${LEGACY_UNINSTKEY_2}" "BibManager"
  ; Upgrades: replace the runtime and the application, never anything else in the folder.
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\app"
  SetOutPath "$INSTDIR"
  File /r "${STAGE}\*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortCut "$SMPROGRAMS\${APPNAME}.lnk" "$INSTDIR\JRef.exe" "" "$INSTDIR\JRef.ico" 0 SW_SHOWNORMAL "" "${APPSUBTITLE}"
  WriteRegStr HKCU "Software\JRef" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME} - ${APPSUBTITLE}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "${AUTHOR}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\JRef.ico"
  WriteRegStr HKCU "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTKEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "EstimatedSize" ${SIZE_KB}
SectionEnd

Section "Desktop shortcut" SecDesktop
  CreateShortCut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\JRef.exe" "" "$INSTDIR\JRef.ico" 0 SW_SHOWNORMAL "" "${APPSUBTITLE}"
SectionEnd

LangString DESC_SecMain ${LANG_ENGLISH} "The application and its private Python runtime."
LangString DESC_SecDesktop ${LANG_ENGLISH} "Put a JRef shortcut on the desktop."
!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} $(DESC_SecMain)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} $(DESC_SecDesktop)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
  Delete "$SMPROGRAMS\${APPNAME}.lnk"
  Delete "$DESKTOP\${APPNAME}.lnk"
  ; Only what the installer created: the library and %APPDATA%\JRef (settings) are kept.
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\app"
  Delete "$INSTDIR\JRef.exe"
  Delete "$INSTDIR\JRef.ico"
  Delete "$INSTDIR\LICENSES.txt"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTKEY}"
  DeleteRegValue HKCU "Software\JRef" "InstallDir"  ; the window settings below this key are kept
SectionEnd
