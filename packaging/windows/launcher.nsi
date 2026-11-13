; JRef.exe — tiny launcher: starts the bundled pythonw.exe with "-m bibmanager" and exits.
Target amd64-unicode  ; 64-bit like the bundled Python (also lets it run under a 64-bit-only Wine)
SilentInstall silent
RequestExecutionLevel user
!include "FileFunc.nsh"
!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!ifndef OUTFILE
  !define OUTFILE "JRef.exe"
!endif
Name "JRef"
OutFile "${OUTFILE}"
Icon "JRef.ico"
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "JRef"
VIAddVersionKey "FileDescription" "JRef - Journal Reference Manager"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "JC"

Section
  ${GetParameters} $0
  SetOutPath "$EXEDIR"
  Exec '"$EXEDIR\python\pythonw.exe" -m bibmanager $0'
SectionEnd
