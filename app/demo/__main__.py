"""Start with ``python -m app.demo --port 9911``; Ctrl+C exits."""

import argparse

from app.demo.server import build_server


def main():
    parser = argparse.ArgumentParser(description="Local synthetic replay demo (no cloud calls)")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback IP literal only")
    parser.add_argument("--port", default=9911, type=int)
    args = parser.parse_args()
    try:
        server = build_server(host=args.host, port=args.port)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    host, port = server.server_address[:2]
    address = f"[{host}]" if ":" in host else host
    print(
        f"Synthetic replay only; semantic accuracy is not measured. http://{address}:{port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nReplay demo stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
