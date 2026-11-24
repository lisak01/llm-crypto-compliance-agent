"""
File: send_requests.py

Purpose:
    Sends every synthetic request from data/requests.json to the n8n webhook.
    Each HTTP call starts one workflow run (= one execution / one trace).

Usage:
    python scripts/send_requests.py
    python scripts/send_requests.py --url http://localhost:5678/webhook/crypto-withdrawal
"""

import argparse  # reads command line options (--url, --file, --pause)
import json  # converts requests to JSON text
import time  # used to wait between two requests
import urllib.request  # sends HTTP requests without extra libraries
from pathlib import Path  # builds file paths independent of the OS

ROOT = Path(__file__).resolve().parent.parent  # project root folder (one level above scripts/)


def main():
    """Reads the request file and posts each request to the webhook, one after another."""
    p = argparse.ArgumentParser()  # create the command line parser
    p.add_argument("--url", default="http://localhost:5678/webhook/crypto-withdrawal")  # webhook address
    p.add_argument("--file", default=str(ROOT / "data" / "requests.json"))  # input file
    p.add_argument("--pause", type=float, default=2.0, help="seconds between requests")  # wait time
    args = p.parse_args()  # read the options given by the user

    requests = json.loads(Path(args.file).read_text())  # load all requests from the JSON file
    for r in requests:  # go through every request
        body = json.dumps(r).encode()  # turn the request into JSON bytes for the HTTP body
        req = urllib.request.Request(args.url, data=body,  # build a POST request (data = POST)
                                     headers={"Content-Type": "application/json"})  # tell n8n it is JSON
        with urllib.request.urlopen(req) as resp:  # send the request and get the response
            print(f"{r['request_id']:8} {r['scenario']:26} -> HTTP {resp.status}")  # show result
        time.sleep(args.pause)  # wait so runs do not overlap too much
    print("Done. Wait until all runs have finished in n8n before exporting.")  # final hint


if __name__ == "__main__":  # only runs when the file is started directly
    main()  # start the script
