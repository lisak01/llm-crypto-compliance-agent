"""
File: build_workflow.py

Purpose:
    Builds the n8n workflow file n8n/crypto_withdrawal_workflow.json.
    This file can be imported into n8n (Workflows -> Import from file).

Why a Python builder?
    Writing the workflow JSON by hand is error-prone. Here, the JavaScript code
    of each agent is kept as readable text, and the JSON is generated from it.

Workflow (one run = one withdrawal request):
    Webhook -> Intake -> Wallet Screening -> Travel Rule -> [Ownership Check?]
    -> Risk Scoring (LLM) -> Decision -> [Approved?] -> Broadcast
                                         [Rejected?] -> Rejection Notice (LLM) / Manual Review (LLM)

Agent types:
    - LLM agents (n8n "AI Agent" node + Gemini or Groq chat model, see PROVIDER):
      Risk Scoring, Manual Review, Rejection Notice. They need a free API key,
      stored as an n8n credential (never inside this file).
    - Rule agents (n8n "Code" node): Intake, Wallet Screening, Travel Rule,
      Ownership Verification, Decision, Blockchain Broadcast. Hard compliance
      rules stay deterministic on purpose.

Usage:
    Edit the agent code below, then run:  python scripts/build_workflow.py
"""

import json  # writes the workflow as JSON
import uuid  # creates unique IDs for every n8n node
from pathlib import Path  # builds file paths independent of the OS

# Small JavaScript helper added to every agent.
# It pauses the agent for some milliseconds to simulate real processing time.
SLEEP = "const sleep = (ms) => new Promise(r => setTimeout(r, ms)); // helper: wait for ms milliseconds\n"

# ---------------------------------------------------------------------------
# JavaScript code of each agent (runs inside an n8n "Code" node).
# $input.first().json = the data that the previous node passed on.
# return [{ json: ... }] = the data that this node passes to the next node.
# throw new Error(...) = the node fails and the run stops with status "error".
# ---------------------------------------------------------------------------

INTAKE = SLEEP + r"""
// Intake Agent: checks that the withdrawal request has all required fields.
const req = $input.first().json.body || {};  // the webhook puts the posted JSON into "body"
await sleep(200 + Math.random() * 300);  // simulate 200-500 ms processing time
const required = ['request_id', 'customer_id', 'asset', 'amount', 'amount_eur',  // fields that must exist
                  'destination_address', 'wallet_type'];  // (continued)
const missing = required.filter(f => req[f] === undefined || req[f] === null || req[f] === '');  // find empty fields
if (missing.length) {  // at least one field is missing
  throw new Error(`Invalid request: missing field(s) ${missing.join(', ')}`);  // stop the run with an error
}
return [{ json: { ...req, received_at: new Date().toISOString() } }];  // pass the request on, plus a timestamp
"""

SCREENING = SLEEP + r"""
// Wallet Screening Agent: checks the target wallet against a small demo risk list.
const tx = $input.first().json;  // the request from the previous step
const RISK_LIST = {  // demo list: wallet address -> risk category
  '0x7f3a000000000000000000000000000000mix001': 'mixer',  // wallet linked to a mixer
  '0x9b1c00000000000000000000000000000sanc001': 'sanctioned',  // sanctioned wallet
};
await sleep(600 + Math.random() * 900);  // simulate 600-1500 ms (e.g. calling a screening service)
const category = RISK_LIST[tx.destination_address] || 'clean';  // unknown wallets count as "clean"
return [{ json: { ...tx, screening: { category, source: 'internal-demo-list' } } }];  // add the result
"""

TRAVEL_RULE = SLEEP + r"""
// Travel Rule Agent: sender and receiver data must be complete (EU Travel Rule).
const tx = $input.first().json;  // the request from the previous step
await sleep(300 + Math.random() * 500);  // simulate 300-800 ms processing time
const tr = tx.travel_rule || {};  // travel rule data (empty object if missing)
const missing = ['originator_name', 'originator_account', 'beneficiary_name'].filter(f => !tr[f]);  // find gaps
if (tx.wallet_type === 'exchange' && !tr.beneficiary_vasp) missing.push('beneficiary_vasp');  // exchange needs provider name
if (missing.length) {  // some data is missing
  throw new Error(`Travel Rule data incomplete: ${missing.join(', ')}`);  // stop the run with an error
}
const needsOwnershipCheck = tx.wallet_type === 'self_hosted' && tx.amount_eur > 1000;  // private wallet over 1000 EUR?
return [{ json: { ...tx, travel_rule_ok: true, needs_ownership_check: needsOwnershipCheck } }];  // pass on result
"""

OWNERSHIP = SLEEP + r"""
// Ownership Verification Agent: the customer proves that the private wallet belongs to them.
// Simulated as a "signed message" check, which takes longer than other steps.
const tx = $input.first().json;  // the request from the previous step
await sleep(2000 + Math.random() * 2000);  // simulate 2-4 seconds (customer has to sign a message)
return [{ json: { ...tx, ownership_verified: true, ownership_method: 'signed_message' } }];  // mark as verified
"""

# ---------------------------------------------------------------------------
# Prompts of the LLM agents (n8n "AI Agent" nodes, model: Gemini or Groq).
# Text starting with "=" is an n8n expression; {{ ... }} is filled in per run.
# ---------------------------------------------------------------------------

RISK_SYSTEM = (  # system prompt: role and scoring rules for the LLM
    "You are a crypto compliance risk analyst at a bank. "  # role of the agent
    "Score the risk of the withdrawal from 0 (no risk) to 100 (must be blocked). "  # task
    "Guidelines: sanctioned destination = 100. Mixer exposure adds about 60. "  # scoring rules (same idea as before)
    "Amount over 10,000 EUR adds about 35, over 1,000 EUR about 10. "  # amount rules
    "Self-hosted wallet adds about 10. Customer younger than 30 days adds about 20. "  # wallet and customer rules
    'Answer ONLY with JSON, no other text: {"risk_score": <integer>, "reasons": ["short reason", ...]}'  # strict output format
)

RISK_PROMPT = (  # user prompt: only the fields the LLM needs (no names, no accounts)
    "=Withdrawal request:\n"  # "=" makes n8n treat the text as an expression
    "{{ JSON.stringify({ asset: $json.asset, amount_eur: $json.amount_eur, "  # coin and amount
    "wallet_type: $json.wallet_type, screening_result: $json.screening.category, "  # wallet type and screening result
    "customer_tenure_days: $json.customer_tenure_days, "  # customer age in days
    "ownership_verified: $json.ownership_verified || false }, null, 2) }}"  # result of the optional ownership step
)

MANUAL_SYSTEM = (  # system prompt of the Manual Review Agent
    "You are an assistant for compliance analysts at a bank. "  # role
    "Write a short case summary (max. 4 sentences) for the analyst who reviews this crypto withdrawal. "  # task
    "Mention the main risk factors and what the analyst should check first."  # content
)

MANUAL_PROMPT = (  # user prompt of the Manual Review Agent
    "=Case data:\n"  # expression text
    "{{ JSON.stringify({ request_id: $json.request_id, asset: $json.asset, amount_eur: $json.amount_eur, "  # case basics
    "wallet_type: $json.wallet_type, screening_result: $json.screening.category, risk: $json.risk }, null, 2) }}"  # risk data
)

REJECT_SYSTEM = (  # system prompt of the Rejection Notice Agent
    "You write short, polite messages from a bank to its customers. "  # role
    "Write max. 3 sentences telling the customer that the crypto withdrawal was rejected for compliance reasons. "  # task
    "Do not mention internal lists, scores or investigations."  # what must not be said
)

REJECT_PROMPT = (  # user prompt of the Rejection Notice Agent
    "=Withdrawal: {{ $json.amount }} {{ $json.asset }} (request {{ $json.request_id }})"  # minimal data
)

DECISION = SLEEP + r"""
// Decision Agent: reads the LLM risk score and decides between approve, review and reject.
// The AI Agent node only returns { output: "<LLM text>" }, so the request data is
// taken again from earlier nodes with $('Node name').
const tx = $('Travel Rule Agent').first().json;  // request data (Travel Rule Agent always runs before)
let ownership = {};  // result of the optional ownership step
try {  // the ownership step only runs for private wallets over 1000 EUR
  ownership = $('Ownership Verification Agent').first().json;  // take its output if it ran
} catch (e) {  // node did not run in this execution
  ownership = {};  // keep it empty
}
const raw = String($input.first().json.output ?? '');  // LLM answer as text
await sleep(150 + Math.random() * 250);  // simulate 150-400 ms processing time
const match = raw.match(/\{[\s\S]*\}/);  // find the JSON part (LLMs sometimes add text around it)
let llm;  // parsed LLM answer
try {
  llm = JSON.parse(match[0]);  // parse the JSON
} catch (e) {  // no JSON or invalid JSON
  throw new Error(`LLM returned invalid JSON: ${raw.slice(0, 120)}`);  // stop the run with an error
}
const score = Math.max(0, Math.min(100, Math.round(Number(llm.risk_score))));  // clean score 0-100
if (Number.isNaN(score)) throw new Error('LLM risk_score is not a number');  // safety check
let decision = 'approve';  // default: approve the withdrawal
if (tx.screening?.category === 'sanctioned') decision = 'reject';  // hard rule: sanctioned is always rejected
else if (score >= 50) decision = 'review';  // high LLM score goes to a human
return [{ json: {  // pass on the request with risk and decision
  ...tx,  // original request data
  ownership_verified: ownership.ownership_verified || false,  // result of the ownership step
  risk: { score, reasons: llm.reasons || [], source: 'llm' },  // LLM result
  decision,  // final decision
} }];
"""

BROADCAST = SLEEP + r"""
// Blockchain Broadcast Agent: sends the transaction to the blockchain (simulated).
const tx = $input.first().json;  // the request from the previous step
const sim = tx.simulation || {};  // test flags from the data generator
const base = { BTC: 1500, ETH: 800, USDC: 600 }[tx.asset] || 1000;  // normal wait time per coin in ms
let delay = base + Math.random() * 700;  // add some random variation
if (sim.network_congestion) delay += 6000 + Math.random() * 3000;  // overloaded network: 6-9 s extra
await sleep(delay);  // wait for the simulated confirmation
if (sim.broadcast_failure) {  // test case: sending fails
  throw new Error('Broadcast failed: node timeout during network congestion');  // stop the run with an error
}
const hex = [...Array(64)].map(() => Math.floor(Math.random() * 16).toString(16)).join('');  // fake tx hash
return [{ json: { ...tx, tx_hash: '0x' + hex, broadcast_status: 'confirmed' } }];  // mark as sent
"""

def code_node(name, js, x, y):
    """
    Creates an n8n "Code" node (one agent).

    Args:
        name: node name, shown in n8n and used as "agent" in the traces
        js:   JavaScript code of the node
        x, y: position on the n8n canvas (only for display)
    """
    return {
        "id": str(uuid.uuid4()),  # unique node ID
        "name": name,  # node name
        "type": "n8n-nodes-base.code",  # n8n node type: Code
        "typeVersion": 2,  # version of the Code node
        "position": [x, y],  # position in the editor
        "parameters": {"jsCode": js.strip() + "\n"},  # the JavaScript code
    }


def if_node(name, kind, value1, value2, x, y):
    """
    Creates an n8n "IF" node (router with a true and a false output).

    Args:
        name:   node name
        kind:   type of comparison ("boolean" or "string")
        value1: left side, usually an n8n expression like {{ $json.decision }}
        value2: right side, the value to compare with
        x, y:   position on the n8n canvas
    """
    return {
        "id": str(uuid.uuid4()),  # unique node ID
        "name": name,  # node name
        "type": "n8n-nodes-base.if",  # n8n node type: IF
        "typeVersion": 1,  # version 1 has the simplest parameter format
        "position": [x, y],  # position in the editor
        "parameters": {"conditions": {kind: [{"value1": value1, "value2": value2}]}},  # condition: value1 == value2
    }


def agent_node(name, system_message, prompt, x, y):
    """
    Creates an n8n "AI Agent" node (LLM agent).

    Args:
        name:           node name, shown in n8n and used as "agent" in the traces
        system_message: instructions for the LLM (role, rules, output format)
        prompt:         the user message; an n8n expression with the run data
        x, y:           position on the n8n canvas
    """
    return {
        "id": str(uuid.uuid4()),  # unique node ID
        "name": name,  # node name
        "type": "@n8n/n8n-nodes-langchain.agent",  # n8n node type: AI Agent
        "typeVersion": 3.1,  # current AI Agent version
        "position": [x, y],  # position in the editor
        "parameters": {
            "promptType": "define",  # we write the prompt ourselves (not taken from a chat)
            "text": prompt,  # the prompt with run data
            "hasOutputParser": False,  # we parse the answer in the Decision Agent ourselves
            "options": {"systemMessage": system_message},  # role and rules for the LLM
        },
        "retryOnFail": True,  # retry once more if the LLM API fails (e.g. rate limit)
        "maxTries": 2,  # at most 2 tries
        "waitBetweenTries": 2000,  # wait 2 s before the retry
    }


# Which LLM provider the workflow uses. Both are free to start with:
#   "gemini" -> Google AI Studio key (aistudio.google.com)
#   "groq"   -> Groq key (console.groq.com)
PROVIDER = "gemini"  # change to "groq" and re-run this script to switch

LLM_NODE_NAME = {"gemini": "Gemini Chat Model", "groq": "Groq Chat Model"}[PROVIDER]  # name of the model node


def llm_model_node(x, y):
    """
    Creates the chat model node that all LLM agents share.

    The API key is NOT stored here. After the import you select your own
    credential in this node (see README).
    """
    if PROVIDER == "gemini":  # Google Gemini
        return {
            "id": str(uuid.uuid4()),  # unique node ID
            "name": LLM_NODE_NAME,  # node name
            "type": "@n8n/n8n-nodes-langchain.lmChatGoogleGemini",  # n8n node type: Google Gemini Chat Model
            "typeVersion": 1.2,  # node version (model list is loaded from Google)
            "position": [x, y],  # position in the editor (below the agents)
            "parameters": {
                "modelName": "models/gemini-3.5-flash-lite",  # free model that answered in our test (2.5 is gone for new users); can be changed in n8n
                "options": {"temperature": 0},  # 0 = most stable, repeatable answers
            },
        }
    return {  # Groq
        "id": str(uuid.uuid4()),  # unique node ID
        "name": LLM_NODE_NAME,  # node name
        "type": "@n8n/n8n-nodes-langchain.lmChatGroq",  # n8n node type: Groq Chat Model
        "typeVersion": 1,  # node version
        "position": [x, y],  # position in the editor (below the agents)
        "parameters": {
            "model": "llama-3.3-70b-versatile",  # free Groq model; can be changed in n8n
            "options": {"temperature": 0},  # 0 = most stable, repeatable answers
        },
    }


def link(*targets_per_output):
    """
    Builds the n8n connection format for one node.

    Each argument is a list of target node names for one output.
    Normal nodes have 1 output, IF nodes have 2 (true, false).
    Example: link(["A"], ["B"]) -> true goes to A, false goes to B.
    """
    return {"main": [[{"node": t, "type": "main", "index": 0} for t in outs]  # targets of one output
                     for outs in targets_per_output]}  # one list per output


# All nodes of the workflow (name, type, code, position).
nodes = [
    {
        "id": str(uuid.uuid4()),  # unique node ID
        "name": "Webhook",  # start of every run
        "type": "n8n-nodes-base.webhook",  # n8n node type: Webhook
        "typeVersion": 2,  # version of the Webhook node
        "position": [0, 300],  # position in the editor
        "webhookId": str(uuid.uuid4()),  # internal webhook ID
        "parameters": {"httpMethod": "POST", "path": "crypto-withdrawal",  # URL: /webhook/crypto-withdrawal
                       "responseMode": "onReceived", "options": {}},  # answer immediately, run continues
    },
    code_node("Intake Agent", INTAKE, 220, 300),  # step 1
    code_node("Wallet Screening Agent", SCREENING, 440, 300),  # step 2
    code_node("Travel Rule Agent", TRAVEL_RULE, 660, 300),  # step 3
    if_node("Ownership Check Needed?", "boolean",  # router: private wallet over 1000 EUR?
            "={{ $json.needs_ownership_check }}", True, 880, 300),  # (continued)
    code_node("Ownership Verification Agent", OWNERSHIP, 1100, 160),  # optional step
    agent_node("Risk Scoring Agent", RISK_SYSTEM, RISK_PROMPT, 1320, 300),  # step 4 (LLM)
    code_node("Decision Agent", DECISION, 1600, 300),  # step 5 (rules on top of the LLM score)
    if_node("Approved?", "string", "={{ $json.decision }}", "approve", 1820, 300),  # router: approved?
    code_node("Blockchain Broadcast Agent", BROADCAST, 2040, 160),  # end: send transaction
    if_node("Rejected?", "string", "={{ $json.decision }}", "reject", 2040, 440),  # router: rejected?
    agent_node("Rejection Notice Agent", REJECT_SYSTEM, REJECT_PROMPT, 2300, 340),  # end: reject (LLM)
    agent_node("Manual Review Agent", MANUAL_SYSTEM, MANUAL_PROMPT, 2300, 560),  # end: human review (LLM)
    llm_model_node(1800, 760),  # shared LLM for all three AI agents
]

# Which node is followed by which (the arrows in n8n).
connections = {
    "Webhook": link(["Intake Agent"]),  # webhook -> intake
    "Intake Agent": link(["Wallet Screening Agent"]),  # intake -> screening
    "Wallet Screening Agent": link(["Travel Rule Agent"]),  # screening -> travel rule
    "Travel Rule Agent": link(["Ownership Check Needed?"]),  # travel rule -> router
    "Ownership Check Needed?": link(["Ownership Verification Agent"], ["Risk Scoring Agent"]),  # true / false
    "Ownership Verification Agent": link(["Risk Scoring Agent"]),  # ownership -> risk
    "Risk Scoring Agent": link(["Decision Agent"]),  # risk -> decision
    "Decision Agent": link(["Approved?"]),  # decision -> router
    "Approved?": link(["Blockchain Broadcast Agent"], ["Rejected?"]),  # true: send / false: next router
    "Rejected?": link(["Rejection Notice Agent"], ["Manual Review Agent"]),  # true: reject / false: review
    LLM_NODE_NAME: {"ai_languageModel": [[  # the model is plugged into every AI agent
        {"node": "Risk Scoring Agent", "type": "ai_languageModel", "index": 0},  # model for risk scoring
        {"node": "Manual Review Agent", "type": "ai_languageModel", "index": 0},  # model for review summaries
        {"node": "Rejection Notice Agent", "type": "ai_languageModel", "index": 0},  # model for customer notices
    ]]},
}

# The complete workflow object in n8n's import format.
workflow = {
    "name": "Crypto Withdrawal Compliance Check (LLM)",  # workflow name in n8n
    "nodes": nodes,  # all nodes
    "connections": connections,  # all arrows
    "settings": {
        "executionOrder": "v1",  # current n8n execution order
        "saveManualExecutions": True,  # also save runs started by hand in the editor
        "saveDataSuccessExecution": "all",  # save data of successful runs (needed for traces)
        "saveDataErrorExecution": "all",  # save data of failed runs (needed for traces)
        "saveExecutionProgress": True,  # save data after each node
    },
    "pinData": {},  # no fixed test data
}

if __name__ == "__main__":  # only runs when the file is started directly
    out = Path(__file__).resolve().parent.parent / "n8n" / "crypto_withdrawal_workflow.json"  # output path
    out.write_text(json.dumps(workflow, indent=2))  # write the workflow as readable JSON
    print(f"Wrote {out}")  # short confirmation in the terminal
