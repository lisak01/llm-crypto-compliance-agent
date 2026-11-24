"""
File: export_traces.py

Purpose:
    Exports the saved n8n executions and converts them into anonymized trace
    JSON for the visualization. Output: traces/traces.json

How it works:
    1. Load all executions from the n8n public API (or from a saved file).
    2. For each execution, read "runData" (one entry per executed node).
    3. Turn every node execution into one "step" with run_id, step_id, agent,
       predecessor, start time, duration, status, error, input and output.
    4. Remove personal-looking data (names, accounts, HTTP headers).
    5. Write all runs with their steps into one JSON file.

Usage:
    export N8N_API_KEY=...            # n8n: Settings -> n8n API -> Create API key
    python scripts/export_traces.py
    python scripts/export_traces.py --from-file raw_executions.json   # offline
"""

import argparse  # reads command line options
import json  # reads and writes JSON
import os  # reads environment variables (API key, URL)
import re  # regular expressions, used to build clean step IDs
import urllib.request  # sends HTTP requests to the n8n API
from datetime import datetime, timezone  # converts timestamps to ISO format
from pathlib import Path  # builds file paths independent of the OS

ROOT = Path(__file__).resolve().parent.parent  # project root folder

# Own instrumentation: n8n has no "agent" or "role" field, so we map node names.
# Nodes not listed here get the role "agent".
ROLE_BY_NODE = {
    "Webhook": "trigger",  # starts the run
    "Ownership Check Needed?": "router",  # IF node, only decides the path
    "Approved?": "router",  # IF node
    "Rejected?": "router",  # IF node
}
LLM_NODES = {"Gemini Chat Model", "Groq Chat Model"}  # sub-nodes that call the LLM; they get the role "llm"
REDACT_KEYS = {"originator_name", "beneficiary_name", "originator_account"}  # fields to hide in the export


def slug(s):
    """Turns a node name into a short ID, e.g. 'Intake Agent' -> 'intake_agent'."""
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")  # replace non-letters with "_"


def iso(ms):
    """Converts a Unix timestamp in milliseconds into an ISO 8601 string (UTC)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")  # e.g. 2026-11-18T17:05:01.123+00:00


def redact(obj):
    """
    Anonymizes data recursively.

    - Removes HTTP headers completely.
    - Replaces names and account numbers with "[redacted]".
    Works on nested dicts and lists.
    """
    if isinstance(obj, dict):  # object: check every key
        out = {}  # new cleaned object
        for k, v in obj.items():  # go through all key/value pairs
            if k == "headers":  # HTTP headers can contain IPs etc.
                continue  # skip them completely
            out[k] = "[redacted]" if k in REDACT_KEYS else redact(v)  # hide or clean further
        return out  # return the cleaned object
    if isinstance(obj, list):  # list: clean every element
        return [redact(v) for v in obj]  # return the cleaned list
    return obj  # simple values (numbers, text) stay as they are


def first_json(task):
    """
    Returns the output data of one node execution (first item), anonymized.

    - Normal nodes: output is in data["main"].
    - LLM sub-nodes (Gemini/Groq Chat Model): output is in data["ai_languageModel"];
      we keep only the generated text.
    - Webhook: only the posted body is kept (no headers, no query).
    Returns None if the node produced no output (e.g. because it failed).
    """
    data = task.get("data") or {}  # all outputs of this node execution
    if "ai_languageModel" in data:  # LLM sub-node
        try:
            gen = data["ai_languageModel"][0][0]["json"]["response"]["generations"][0][0]  # first answer of the LLM
            return {"text": gen.get("text")}  # keep only the answer text
        except (KeyError, IndexError, TypeError):  # unexpected format or failed call
            return None  # nothing to return
    item = None  # output item (stays None if the node produced nothing)
    for output in data.get("main") or []:  # IF nodes have 2 outputs (true, false); only one is filled
        if output:  # this output has items
            item = output[0]["json"]  # n8n format: data -> main -> output -> item 0 -> json
            break  # take the first filled output
    if item is None:  # node has no output (e.g. it failed)
        return None  # nothing to return
    if "body" in item and "headers" in item:  # this is the webhook output
        item = item["body"]  # keep only the request itself
    return redact(item)  # anonymize before returning


def llm_input(task):
    """Returns the prompt that was sent to the LLM (only for LLM sub-nodes), anonymized."""
    try:
        return redact(task["inputOverride"]["ai_languageModel"][0][0]["json"])  # messages sent to the model
    except (KeyError, IndexError, TypeError):  # not an LLM node or no prompt saved
        return None  # nothing to return


def llm_info(task):
    """Returns model name and token usage of an LLM call (from n8n tracing metadata)."""
    tr = (task.get("metadata") or {}).get("tracing")  # tracing data written by n8n
    if not tr:  # not an LLM node
        return None  # nothing to return
    return {
        "provider": tr.get("gen_ai.provider.name"),  # e.g. "google" or "groq"
        "model": tr.get("gen_ai.request.model"),  # e.g. "gemini-3.8-flash"
        "input_tokens": tr.get("gen_ai.usage.input_tokens"),  # prompt length in tokens
        "output_tokens": tr.get("gen_ai.usage.output_tokens"),  # answer length in tokens
    }


def fetch_executions(base_url, api_key, workflow_id=None):
    """
    Loads all executions (with full data) from the n8n public API.

    The API returns max. 100 executions per page, so we follow "nextCursor"
    until there are no more pages.
    """
    executions, cursor = [], None  # collected executions, current page cursor
    while True:  # repeat until the last page
        url = f"{base_url}/api/v1/executions?includeData=true&limit=100"  # includeData = node details
        if workflow_id:  # optional: only one workflow
            url += f"&workflowId={workflow_id}"  # add the filter
        if cursor:  # not the first page
            url += f"&cursor={cursor}"  # ask for the next page
        req = urllib.request.Request(url, headers={"X-N8N-API-KEY": api_key})  # API key goes in the header
        with urllib.request.urlopen(req) as resp:  # send the request
            page = json.loads(resp.read())  # parse the JSON answer
        executions += page.get("data", [])  # add this page's executions
        cursor = page.get("nextCursor")  # cursor for the next page (None if last page)
        if not cursor:  # no more pages
            return executions  # return everything


def convert(executions):
    """
    Converts raw n8n executions into our trace format.

    Returns:
        list of runs; each run has summary fields and a list of steps.
    """
    executions = [e for e in executions if e.get("data")]  # skip executions without saved data
    executions.sort(key=lambda e: e.get("startedAt") or "")  # oldest run first
    runs = []  # collects the converted runs
    for idx, ex in enumerate(executions, start=1):  # number runs from 1
        run_id = f"run_{idx:02d}"  # own run ID, e.g. run_07
        run_data = ex["data"]["resultData"]["runData"]  # node name -> list of executions of that node

        # Flatten: one entry per node execution (a node can run more than once).
        raw = []  # list of (node name, run index of the node, task data)
        for node, tasks in run_data.items():  # every node that ran
            for t_idx, task in enumerate(tasks):  # every time this node ran
                raw.append((node, t_idx, task))  # store it
        raw.sort(key=lambda x: x[2].get("startTime", 0))  # order by start time = execution order

        step_ids = {}  # (node, run index) -> step_id, needed to find predecessors
        for node, t_idx, _ in raw:  # go through all node executions
            step_ids[(node, t_idx)] = f"{run_id}_{slug(node)}" + (f"_{t_idx}" if t_idx else "")  # e.g. run_07_intake_agent

        steps, outputs = [], {}  # converted steps, step_id -> output data
        for seq, (node, t_idx, task) in enumerate(raw, start=1):  # seq = position in the run
            src = (task.get("source") or [None])[0] or {}  # info about the previous node
            prev_node = src.get("previousNode")  # name of the previous node (None for the webhook)
            predecessor = step_ids.get((prev_node, src.get("previousNodeRun", 0))) if prev_node else None  # its step_id
            err = task.get("error")  # error object if the node failed
            status = task.get("executionStatus") or ("error" if err else "success")  # status from n8n
            start = task.get("startTime")  # start time in ms
            dur = task.get("executionTime", 0)  # duration in ms
            out = first_json(task)  # output data (anonymized)
            is_llm = node in LLM_NODES  # True for the LLM sub-node
            outputs[step_ids[(node, t_idx)]] = out  # remember it as input for the next step
            steps.append({  # one trace step
                "run_id": run_id,  # which run
                "step_id": step_ids[(node, t_idx)],  # unique step ID
                "seq": seq,  # position in the run (1, 2, 3, ...)
                "agent": node,  # agent = node name
                "role": "llm" if is_llm else ROLE_BY_NODE.get(node, "agent"),  # trigger, router, agent or llm
                "predecessor": predecessor,  # step_id of the previous step
                "branch": src.get("previousNodeOutput") if prev_node else None,  # which IF output (0 = true, 1 = false)
                "start_time": iso(start) if start else None,  # start as ISO time
                "end_time": iso(start + dur) if start else None,  # end = start + duration
                "duration_ms": dur,  # duration in ms
                "status": status,  # success or error
                "error_message": (err or {}).get("message"),  # error text if failed
                "input": llm_input(task) if is_llm else (outputs.get(predecessor) if predecessor else None),  # LLM: prompt; others: output of the predecessor
                "output": out,  # output of this step
                "llm": llm_info(task),  # model and tokens (only for LLM calls, else None)
            })

        body = next((s["output"] for s in steps if s["role"] == "trigger"), {}) or {}  # original request
        decided = next((s["output"] for s in steps if s["output"] and "decision" in s["output"]), {})  # output of the Decision Agent
        # The run failed if its last step failed (n8n stops there). Earlier errors that a
        # retry fixed (e.g. LLM rate limit) do not make the run fail; they stay visible per step.
        last_step = max(steps, key=lambda s: (s["end_time"] or "", s["seq"])) if steps else None  # step that ended last
        failed = last_step if last_step and last_step["status"] == "error" else None  # failed step, if the run failed
        if failed and failed["role"] == "llm":  # an LLM call fails inside its agent
            failed = next((s for s in steps if s["step_id"] == failed["predecessor"]), failed)  # report the agent
        starts = [s["start_time"] for s in steps if s["start_time"]]  # all start times
        ends = [s["end_time"] for s in steps if s["end_time"]]  # all end times
        total = None  # total run duration (unknown until computed)
        if starts and ends:  # only if we have times
            total = int((datetime.fromisoformat(max(ends)) -  # last end
                         datetime.fromisoformat(min(starts))).total_seconds() * 1000)  # minus first start, in ms
        runs.append({  # one run with summary
            "run_id": run_id,  # own run ID
            "n8n_execution_id": ex.get("id"),  # original ID in n8n (for checking)
            "request_id": body.get("request_id"),  # request ID from the data generator
            "scenario": body.get("scenario"),  # test scenario label
            "status": "error" if failed else "success",  # overall status (final outcome)
            "decision": decided.get("decision"),  # approve / review / reject (None if failed before the decision)
            "failed_step": failed["step_id"] if failed else None,  # where it failed
            "recovered_errors": 0 if failed else sum(1 for s in steps if s["status"] == "error"),  # errors fixed by a retry
            "started_at": min(starts) if starts else None,  # run start time
            "total_duration_ms": total,  # total duration
            "steps": steps,  # all steps of this run
        })
    return runs  # return all converted runs


def main():
    """Reads options, loads the executions, converts them and writes traces/traces.json."""
    p = argparse.ArgumentParser()  # create the command line parser
    p.add_argument("--url", default=os.getenv("N8N_URL", "http://localhost:5678"))  # n8n address
    p.add_argument("--workflow-id", default=os.getenv("N8N_WORKFLOW_ID"))  # optional workflow filter
    p.add_argument("--from-file", help="use a saved raw API response instead of calling n8n")  # offline mode
    p.add_argument("--save-raw", help="also save the raw API response (do not commit it)")  # debug copy
    p.add_argument("--since", default=os.getenv("N8N_SINCE"),  # e.g. 2026-11-19T02:02:00
                   help="only export runs that started at or after this UTC time (ISO format)")
    p.add_argument("--out", default=str(ROOT / "traces" / "traces.json"))  # output file
    args = p.parse_args()  # read the options

    if args.from_file:  # offline mode: read from a file
        raw = json.loads(Path(args.from_file).read_text())  # load the saved data
        executions = raw.get("data", raw) if isinstance(raw, dict) else raw  # accept API page or plain list
    else:  # normal mode: ask n8n
        key = os.getenv("N8N_API_KEY")  # API key from the environment
        if not key:  # no key set
            raise SystemExit("Set N8N_API_KEY first.")  # stop with a clear message
        executions = fetch_executions(args.url, key, args.workflow_id)  # load from the API
        if args.save_raw:  # user wants a raw copy
            Path(args.save_raw).write_text(json.dumps(executions, indent=2))  # save it (not anonymized!)

    if args.since:  # skip older runs, e.g. test runs before a fix
        executions = [x for x in executions if (x.get("startedAt") or "") >= args.since]  # ISO strings compare correctly
    runs = convert(executions)  # convert into trace format
    doc = {  # final JSON document
        "schema_version": 1,  # version of our trace format
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),  # export time
        "source": "n8n executions (runData)",  # where the data comes from
        "derived_fields": {  # fields that are NOT from n8n directly (required by the task)
            "agent": "n8n node name",
            "role": "own mapping in export_traces.py (trigger/router/agent)",
            "run_id": "own numbering by start time",
            "input": "output of the predecessor step; for LLM steps the prompt sent to the model",
            "predecessor (llm steps)": "the AI agent that called the model (parent), not a sequential step",
            "total_duration_ms": "computed from first start to last end",
        },
        "runs": runs,  # all runs
    }
    Path(args.out).parent.mkdir(exist_ok=True)  # create the traces folder if needed
    Path(args.out).write_text(json.dumps(doc, indent=2))  # write the file
    print(f"Wrote {len(runs)} runs to {args.out}")  # confirmation
    for r in runs:  # short overview of every run
        print(f"  {r['run_id']} {r['scenario'] or '-':26} {r['status']:8} "  # run, scenario, status
              f"{r['decision'] or '-':8} {r['total_duration_ms']} ms")  # decision, duration


if __name__ == "__main__":  # only runs when the file is started directly
    main()  # start the script
