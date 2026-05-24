"""
Run this on Windows to find the SolidWorks Composer COM ProgID.

Usage:
    python -m composer.discover_com

Output: the ProgID to set as COMPOSER_PROGID in your .env file,
or to paste into the prog_ids list in composer/server.py.
"""

import winreg


def find_composer_progids() -> list[str]:
    found = []
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "") as root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                    lower = name.lower()
                    if any(kw in lower for kw in ("composer", "swcomposer", "dassault")):
                        # Verify it has a CLSID subkey (i.e., it's a real COM object)
                        try:
                            with winreg.OpenKey(root, f"{name}\\CLSID"):
                                found.append(name)
                        except FileNotFoundError:
                            pass
                    i += 1
                except OSError:
                    break
    except Exception as e:
        print(f"Registry read error: {e}")
    return found


def main():
    print("Searching Windows Registry for Composer COM registrations...\n")
    ids = find_composer_progids()

    if ids:
        print("Found:")
        for pid in ids:
            print(f"  {pid}")
        print(f"\nAdd to your .env:")
        print(f"  COMPOSER_PROGID={ids[0]}")
        print(f"\nThen restart the bridge server.")
    else:
        print("No Composer COM registration found.")
        print("Options:")
        print("  1. Use BRIDGE_MODE=folder — export PNGs from Composer manually")
        print("  2. Verify Composer is installed (check Add/Remove Programs)")
        print("  3. Try running this script as Administrator")


if __name__ == "__main__":
    main()
