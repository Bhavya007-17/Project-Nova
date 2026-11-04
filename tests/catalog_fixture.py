"""A catalog like this laptop's (shared by the catalog, Tier 0 and gate tests)."""

from nova.actions.catalog import DEFAULT_ALIASES, Catalog, apps_from_rows, parse_aliases

FUSION_DIR = r"C:\Users\b\AppData\Local\Autodesk\webdeploy\production\6a0c"
ROWS = [
    {"Name": "Google Chrome", "AppID": "Chrome", "Exe": r"C:\Program Files\Google\Chrome\Application\chrome.exe", "Args": "--profile"},
    {"Name": "Web Server for Chrome", "AppID": "Chrome._crx_ofhbbk",
     "Exe": r"C:\Program Files\Google\Chrome\Application\chrome_proxy.exe", "Args": "--app-id=ofhbbk"},
    {"Name": "Autodesk Fusion", "AppID": "AutodeskInc.Fusion360", "Exe": FUSION_DIR + r"\FusionLauncher.exe", "Args": ""},
    {"Name": "Claude", "AppID": "Claude_pzs8sxrjxfjjc!Claude", "Exe": "", "Args": ""},
    {"Name": "ChatGPT", "AppID": "OpenAI.Codex_2p2nqsd0c76g0!App", "Exe": "", "Args": ""},
    {"Name": "Notepad", "AppID": "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App", "Exe": "", "Args": ""},
    {"Name": "Calculator", "AppID": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", "Exe": "", "Args": ""},
    {"Name": "Microsoft Teams", "AppID": "MSTeams_8wekyb3d8bbwe!MSTeams", "Exe": "", "Args": ""},
    {"Name": "Microsoft Edge", "AppID": "MSEdge", "Exe": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "Args": ""},
    {"Name": "Microsoft Store", "AppID": "Microsoft.WindowsStore_8wekyb3d8bbwe!App", "Exe": "", "Args": ""},
    {"Name": "Visual Studio Code", "AppID": "Microsoft.VisualStudioCode", "Exe": r"C:\Users\b\AppData\Local\Programs\Microsoft VS Code\Code.exe", "Args": ""},
    {"Name": "Word", "AppID": "Microsoft.Office.WINWORD.EXE.15", "Exe": r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE", "Args": ""},
    {"Name": "Excel", "AppID": "Microsoft.Office.EXCEL.EXE.15", "Exe": r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE", "Args": ""},
    {"Name": "File Explorer", "AppID": "Microsoft.Windows.Explorer", "Exe": "::{52205FD8-5DFB-447D-801A-D0B52F2E83E1}", "Args": ""},
    {"Name": "Terminal", "AppID": "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App", "Exe": "", "Args": ""},
    {"Name": "Spotify", "AppID": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", "Exe": "", "Args": ""},
    {"Name": "Uninstall Spotify Helper", "AppID": r"{abc}\unins000.exe", "Exe": r"C:\Program Files\Helper\unins000.exe", "Args": ""},
    {"Name": "Settings", "AppID": "windows.immersivecontrolpanel_cw5n1h2txyewy!microsoft.windows.immersivecontrolpanel", "Exe": "", "Args": ""},
    {"Name": "Camera", "AppID": "Microsoft.WindowsCamera_8wekyb3d8bbwe!App", "Exe": "", "Args": ""},
    {"Name": "Outlook (classic)", "AppID": "Microsoft.Office.OUTLOOK.EXE.15", "Exe": r"C:\Program Files\Microsoft Office\root\Office16\OUTLOOK.EXE", "Args": ""},
    {"Name": "", "AppID": "nameless", "Exe": "", "Args": ""},
]
CATALOG = Catalog(apps_from_rows(ROWS), parse_aliases(DEFAULT_ALIASES))
