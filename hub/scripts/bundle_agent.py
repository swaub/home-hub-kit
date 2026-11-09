import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "homehub" / "pc_agent"
SCRIPTS = ("install_agent.ps1", "uninstall_agent.ps1")
HOW_TO = """Home Hub PC agent

This folder installs the small helper that lets the home hub check this computer,
look at its screen when asked, and apply a short list of fixes after the owner says yes.
It does not give anyone remote control of the computer.

1. Right-click PowerShell and choose "Run as administrator".
2. Run:  powershell -ExecutionPolicy Bypass -File "<this folder>\\install_agent.ps1"
   (add -NoFixes to allow checks only; the port is 7781 unless you pass -Port).
3. It downloads a pinned copy of Python from python.org (about 11 MB), registers a task that
   starts at logon, opens the port on private networks only, and prints the lines to add to the
   hub's config. The same lines are saved in C:\\ProgramData\\HomeHub\\install-notes.txt.

To remove it: run uninstall_agent.ps1 the same way.
"""


def members():
    files = [(ROOT / "scripts" / name, f"homehub-agent/{name}") for name in SCRIPTS]
    files += [(path, f"homehub-agent/pc_agent/{path.name}") for path in sorted(AGENT.iterdir()) if path.suffix in (".py", ".ps1")]
    return files


def build(out):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    names = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("homehub-agent/HOW TO INSTALL.txt", HOW_TO)
        names.append("homehub-agent/HOW TO INSTALL.txt")
        for source, name in members():
            archive.write(source, name)
            names.append(name)
    return names


def main(argv=None):
    parser = argparse.ArgumentParser(description="Zip the PC agent and its installer for a family computer")
    parser.add_argument("--out", default=str(ROOT / "dist" / "homehub-agent.zip"))
    args = parser.parse_args(argv)
    names = build(args.out)
    print(f"{args.out}: {len(names)} files")
    for name in names:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
