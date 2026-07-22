' Launch the Vibedentify desktop shell with NO console window flashing up.
' Double-click this, or pin it to the taskbar / Start menu.
Dim sh, fso, scriptDir
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = scriptDir
' 0 = hidden window, False = don't wait. pythonw.exe = Python with no console.
sh.Run "pythonw.exe """ & scriptDir & "\genre_app.pyw""", 0, False
