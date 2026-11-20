"""Drive `dex serve` with a list of requests and print each reply (manual smoke tool).

  uv run python tests/drive_server.py '{"op":"hello"}' '{"op":"search","args":{"q":"acme"}}'
"""

import json
import subprocess
import sys
import time

REPLY_TIMEOUT_SECONDS = 60


def main() -> int:
    requests = [json.loads(argument) for argument in sys.argv[1:]]
    process = subprocess.Popen(["dex", "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    pending = set()
    for number, request in enumerate(requests, 1):
        request["id"] = number
        pending.add(number)
        process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()
    started = time.perf_counter()
    while pending and time.perf_counter() - started < REPLY_TIMEOUT_SECONDS:
        line = process.stdout.readline()
        if not line:
            break
        reply = json.loads(line)
        elapsed = (time.perf_counter() - started) * 1000
        if "event" in reply:
            print(f"{elapsed:7.0f}ms EVENT {reply['phase']}: {reply['message'][:90]}")
            continue
        pending.discard(reply["id"])
        body = reply.get("result") if reply["ok"] else reply.get("error")
        print(f"{elapsed:7.0f}ms #{reply['id']} {'ok' if reply['ok'] else 'ERR'} {json.dumps(body, default=str)[:260]}")
    process.kill()
    return 0 if not pending else 1


if __name__ == "__main__":
    sys.exit(main())
