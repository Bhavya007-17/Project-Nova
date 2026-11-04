"""App catalog (SPEC §8): Start-menu apps + aliases, spoken-name resolution, window -> app identity."""

import pytest

from nova.actions.catalog import Catalog, apps_from_rows, parse_aliases

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
    {"Name": "Uninstall Spotify Helper", "AppID": "{abc}\\unins000.exe", "Exe": r"C:\Program Files\Helper\unins000.exe", "Args": ""},
    {"Name": "Settings", "AppID": "windows.immersivecontrolpanel_cw5n1h2txyewy!microsoft.windows.immersivecontrolpanel", "Exe": "", "Args": ""},
    {"Name": "", "AppID": "nameless", "Exe": "", "Args": ""},
]
ALIASES = {
    "canvas": "https://canvas.vt.edu", "fusion": "Autodesk Fusion", "chrome": "Google Chrome", "claude": "Claude",
    "chatgpt": "ChatGPT", "docs": "https://docs.google.com", "vs code": "Visual Studio Code",
    "photoshop": "Adobe Photoshop",  # not installed: ignored
}
CATALOG = Catalog(apps_from_rows(ROWS), ALIASES)


def best(phrase, strict=False):
    found = CATALOG.resolve(phrase, strict=strict).best
    return found.target.name if found else None


def test_rows_become_apps_without_uninstallers_or_nameless_entries():
    names = [app.name for app in CATALOG.apps]
    assert "Uninstall Spotify Helper" not in names and "" not in names
    assert CATALOG.app("google chrome").aumid == "Chrome"  # lookups ignore case


@pytest.mark.parametrize(
    ("phrase", "name"),
    [
        ("chrome", "Google Chrome"),  # the alias, not "Web Server for Chrome"
        ("google chrome", "Google Chrome"),
        ("Google Chrome", "Google Chrome"),
        ("fusion", "Autodesk Fusion"),
        ("claude", "Claude"),
        ("chat gpt", "ChatGPT"),  # spaces don't matter
        ("canvas", "Canvas"),
        ("notepad", "Notepad"),
        ("teams", "Microsoft Teams"),  # vendor word dropped
        ("edge", "Microsoft Edge"),
        ("vs code", "Visual Studio Code"),
        ("calculater", "Calculator"),  # close spelling
        ("web server for chrome", "Web Server for Chrome"),
    ],
)
def test_spoken_names_resolve(phrase, name):
    assert best(phrase) == name


def test_sites_resolve_to_their_address():
    found = CATALOG.resolve("canvas").best
    assert found.target.kind == "site" and found.target.url == "https://canvas.vt.edu"


def test_strict_resolution_takes_only_whole_names_and_aliases():
    assert best("chrome", strict=True) == "Google Chrome"
    assert best("notepad", strict=True) == "Notepad"
    assert best("teams", strict=True) is None  # no alias: always-listening won't guess
    assert best("store", strict=True) is None  # "go to the store" is chatter
    assert best("calculater", strict=True) is None


def test_partial_names_are_offered_not_taken():
    resolution = CATALOG.resolve("code")
    assert resolution.best is None
    assert [m.target.name for m in resolution.matches][:1] == ["Visual Studio Code"]


@pytest.mark.parametrize(("heard", "meant"), [("foam", "Google Chrome"), ("home", "Google Chrome"), ("claire", "Claude")])
def test_sound_alikes_are_offered_for_mishearings(heard, meant):
    resolution = CATALOG.resolve(heard)
    assert resolution.best is None  # never acted on without asking
    assert meant in [m.target.name for m in resolution.matches[:3]]


def test_unrelated_words_offer_nothing():
    assert CATALOG.resolve("door").matches == ()
    assert CATALOG.resolve("the").matches == ()
    assert CATALOG.resolve("").matches == ()


def test_window_identity():
    assert CATALOG.app_for(r"C:\Program Files\Google\Chrome\Application\chrome.exe", ["Chrome"]).name == "Google Chrome"
    assert CATALOG.app_for(r"c:\program files\google\chrome\application\chrome.exe", []).name == "Google Chrome"
    assert CATALOG.app_for(r"C:\Program Files\WindowsApps\Claude\claude.exe", ["Claude_pzs8sxrjxfjjc!Claude"]).name == "Claude"
    assert CATALOG.app_for(FUSION_DIR + r"\Fusion360.exe", []).name == "Autodesk Fusion"  # same folder as its shortcut
    assert CATALOG.app_for(r"C:\Windows\explorer.exe", []).name == "File Explorer"
    assert CATALOG.app_for(r"C:\Windows\System32\notepad.exe", []).name == "Notepad"  # file name = app name
    assert CATALOG.app_for(r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE", []) is None  # Word's and
    # Excel's folder: no guess
    assert CATALOG.app_for(r"C:\Tools\mystery.exe", []) is None


def test_launcher_shortcuts_never_claim_windows():
    sdk = {"Name": "Windows Software Development Kit", "AppID": "{sdk}", "Exe": r"C:\Windows\explorer.exe",
           "Args": r"C:\Program Files (x86)\Windows Kits"}
    catalog = Catalog(apps_from_rows([*ROWS, sdk]), ALIASES)
    assert catalog.app_for(r"C:\Windows\explorer.exe", []).name == "File Explorer"
    assert catalog.app_for(r"C:\Windows\System32\cmd.exe", []) is None


def test_aliases_file():
    text = "# comment\ncanvas: https://canvas.vt.edu\nVS Code: Visual Studio Code\n"
    assert parse_aliases(text) == {"canvas": "https://canvas.vt.edu", "vs code": "Visual Studio Code"}
    with pytest.raises(ValueError):
        parse_aliases("a: x\na: y\n")  # duplicate
    with pytest.raises(ValueError):
        parse_aliases("- just a list\n")
    assert parse_aliases("") == {}


def test_running_apps_are_familiar_for_sound_alikes():
    assert CATALOG.resolve("not bad").matches == ()  # not an alias, not running: no guess
    offered = [m.target.name for m in CATALOG.resolve("not bad", familiar=frozenset({"Notepad"})).matches]
    assert offered[:1] == ["Notepad"]
