; ── Install: close any running instance before overwriting files ──────────────
!macro customInstall
  ; Kill any running Electron instance and its server child process
  ExecWait 'taskkill /F /IM "Shikigami Protocol.exe" /T' $0
  ExecWait 'taskkill /F /IM "server.exe" /T' $0
  Sleep 800
!macroend

; ── Uninstall: kill processes first so files aren't locked ────────────────────
!macro customUnInstall
  ; Kill the Electron renderer/main process (closes the window and tray)
  ExecWait 'taskkill /F /IM "Shikigami Protocol.exe" /T' $0
  ; Kill the Python backend server in case it's still running
  ExecWait 'taskkill /F /IM "server.exe" /T' $0
  Sleep 800
  ; Now remove the installation directory (including runtime-generated files)
  RMDir /r "$INSTDIR"
!macroend
