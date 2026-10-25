import sys

from mc_translate.__main__ import main, windows_service_command


if __name__ == "__main__":
    if sys.argv[1:] == ["--service-run"]:
        from mc_translate.winservice import run_service
        run_service()
    elif len(sys.argv) >= 2 and sys.argv[1] == "service":
        windows_service_command(sys.argv[2:])
    else:
        main()
