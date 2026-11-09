var shell = new ActiveXObject("WScript.Shell");
var files = new ActiveXObject("Scripting.FileSystemObject");
var runner = files.BuildPath(files.GetParentFolderName(WScript.ScriptFullName), "start-monitor.ps1");
var powershell = shell.ExpandEnvironmentStrings(
    "%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
);
var command = '"' + powershell + '" -NoProfile -NonInteractive -WindowStyle Hidden ' +
    '-ExecutionPolicy Bypass -File "' + runner + '" -Background';

// Keep the scheduled task active for the lifetime of the monitor.
WScript.Quit(shell.Run(command, 0, true));
