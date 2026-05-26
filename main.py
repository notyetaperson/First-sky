#!/usr/bin/env python3
"""
FirstSky — Clean UI (PTK + FFV + ORL only) + Correct Imports
"""

import os
import sys
import time
from pathlib import Path
from typing import Callable, Dict

# ====================== CONFIG ======================
ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "output"
ASSETS_DIR = ROOT / "folders" / "assets"
MUSIC_DIR = ROOT / "folders" / "music"

for directory in (OUTPUT_DIR, ASSETS_DIR, MUSIC_DIR):
    directory.mkdir(parents=True, exist_ok=True)

# Add folders/ to path (critical for resyco and ffv)
folders_path = str(ROOT / "folders")
if folders_path not in sys.path:
    sys.path.insert(0, folders_path)

# ====================== COLORS ======================
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    CYAN = "\033[36m"
    BLUE = "\033[34m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    MAGENTA = "\033[35m"
    DIM = "\033[2m"

C = Colors()

# ====================== UI HELPERS ======================
def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def print_header():
    clear_screen()
    print(f"{C.BOLD}{C.CYAN}")
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 20 + "FIRSTSKY" + " " * 21 + "║")
    print("║" + " " * 12 + "Fully Local Reddit Video Generator" + " " * 12 + "║")
    print("╚" + "═" * 58 + "╝")
    print(f"{C.RESET}")

def print_section(title: str):
    print(f"\n{C.BOLD}{C.BLUE}◆ {title}{C.RESET}")
    print(f"{C.DIM}{'-' * 50}{C.RESET}")

# ====================== REAL TOOL LAUNCHERS ======================
def run_ptk():
    print(f"\n{C.GREEN}🚀 Launching PTK — Reddit Story Video Generator{C.RESET}")
    print(f"{C.DIM}Starting full interactive pipeline...{C.RESET}\n")
    try:
        from resyco.impl import main as ptk_main
        ptk_main()                    # This launches the full PTK interactive shell
    except ImportError as e:
        print(f"{C.RED}Import Error (PTK): {e}{C.RESET}")
        print(f"{C.YELLOW}Make sure folders/resyco/ exists and has __init__.py{C.RESET}")
    except Exception as e:
        print(f"{C.RED}PTK Runtime Error: {e}{C.RESET}")
    input(f"\n{C.DIM}Press Enter to return to menu...{C.RESET}")

def run_ffv():
    print(f"\n{C.MAGENTA}🎥 Launching FFV — Reaction & SFX Generator{C.RESET}")
    print(f"{C.DIM}Opening FFV interactive shell...{C.RESET}\n")
    try:
        from ffv.engine import main as ffv_main   # Try common entry
        ffv_main()
    except ImportError:
        try:
            # Fallback: run via resyco cli if ffv is integrated
            from resyco.impl import main as ptk_main
            # This might not be perfect but better than nothing
            print(f"{C.YELLOW}FFV direct import failed. Trying through main menu...{C.RESET}")
            ptk_main()
        except Exception as e:
            print(f"{C.RED}FFV Import Error: {e}{C.RESET}")
    except Exception as e:
        print(f"{C.RED}FFV Error: {e}{C.RESET}")
    input(f"\n{C.DIM}Press Enter to return to menu...{C.RESET}")

def run_orl():
    print(f"\n{C.CYAN}📖 Launching ORL — Wikipedia Science Slideshow{C.RESET}")
    print(f"{C.DIM}Starting ORL pipeline...{C.RESET}\n")
    try:
        from resyco.impl import main as ptk_main
        ptk_main()                    # ORL is inside the same interactive system
    except Exception as e:
        print(f"{C.RED}ORL Error: {e}{C.RESET}")
    input(f"\n{C.DIM}Press Enter to return to menu...{C.RESET}")

# ====================== MAIN MENU ======================
def main_menu():
    tools: Dict[str, Callable] = {
        "1": run_ptk,
        "ptk": run_ptk,
        "2": run_ffv,
        "ffv": run_ffv,
        "3": run_orl,
        "orl": run_orl,
    }

    while True:
        print_header()
        print_section("AVAILABLE TOOLS")

        print(f"   {C.BOLD}1{C.RESET} → PTK     {C.DIM}(Reddit Story → Vertical Video){C.RESET}")
        print(f"   {C.BOLD}2{C.RESET} → FFV     {C.DIM}(Reaction & SFX Videos){C.RESET}")
        print(f"   {C.BOLD}3{C.RESET} → ORL     {C.DIM}(Wikipedia Science Slides){C.RESET}")

        print(f"\n{C.BOLD}{C.BLUE}Commands:{C.RESET}")
        print(f"   {C.BOLD}q{C.RESET} → Quit")
        print(f"   {C.BOLD}h{C.RESET} → Refresh Menu")

        choice = input(f"\n{C.BOLD}{C.CYAN}Select tool: {C.RESET}").strip().lower()

        if choice in ("q", "quit", "exit"):
            print(f"\n{C.YELLOW}Goodbye! 👋{C.RESET}")
            break
        elif choice in ("h", "help"):
            continue
        elif choice in tools:
            try:
                tools[choice]()
            except KeyboardInterrupt:
                print(f"\n{C.RED}Interrupted by user.{C.RESET}")
        else:
            print(f"{C.RED}Invalid option. Try 1, 2, 3, ptk, ffv, orl.{C.RESET}")
            time.sleep(1.2)


if __name__ == "__main__":
    try:
        main_menu()
    except KeyboardInterrupt:
        print(f"\n\n{C.YELLOW}Exiting FirstSky...{C.RESET}")
        sys.exit(0)