import sys

from mc_translate.__main__ import main, windows_service_command


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "service":
        windows_service_command(sys.argv[2:])
    else:
        main()
