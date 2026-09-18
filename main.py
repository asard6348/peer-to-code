import argparse
import os

from app import App

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Peer to Code")
    parser.add_argument("--config", metavar="PATH", help="Use a specific config.json instead of the default location")
    parser.add_argument("path", metavar="PATH", nargs="?",
                         help="A file or folder to open - autofills the Path field on the Open tab "
                              "once the window is up, same as browsing to it or dropping it in by hand")
    args = parser.parse_args()
    initial_path = os.path.expanduser(args.path) if args.path else None
    App(config_path=args.config, initial_path=initial_path).run()
