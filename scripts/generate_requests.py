"""
File: generate_requests.py

Purpose:
    Generates synthetic (fake) crypto withdrawal requests for the n8n workflow.
    Every request is one future workflow run.

Details:
    - All data is fake: customer IDs, names and wallet addresses are random.
    - Each request has a `scenario` label (e.g. "mixer_wallet") so that every
      run can later be explained in the visualization and in the report.
    - Some scenarios are built on purpose to cause errors, delays or other
      branches in the workflow (required by the task: at least one failing
      or deviating run).

Usage:
    python scripts/generate_requests.py      # writes data/requests.json
"""

import json  # used to write the requests as a JSON file
import random  # used for random amounts, names and scenario details
import secrets  # used to create random hex strings for fake wallet addresses
from pathlib import Path  # used to build file paths independent of the OS

random.seed(42)  # fixed seed so amounts and names are the same on every run

# Fixed demo exchange rates (NOT live prices), used to convert coin amounts to EUR.
EUR_RATE = {"BTC": 80000.0, "ETH": 3000.0, "USDC": 0.9}  # 1 coin = X EUR

# Fake risky wallet addresses.
# They must match RISK_LIST in the "Wallet Screening Agent" node of the workflow.
MIXER_WALLET = "0x7f3a000000000000000000000000000000mix001"  # wallet linked to a mixer
SANCTIONED_WALLET = "0x9b1c00000000000000000000000000000sanc001"  # wallet on a sanctions list


def eth_address():
    """Returns a random fake Ethereum address (format: 0x + 40 hex characters)."""
    return "0x" + secrets.token_hex(20)  # 20 random bytes = 40 hex characters


def btc_address():
    """Returns a random fake Bitcoin address in bech32 style (starts with 'bc1q')."""
    return "bc1q" + secrets.token_hex(19)  # 19 random bytes = 38 hex characters


def address_for(asset):
    """Returns a fake wallet address that fits the given coin (BTC or ETH-based)."""
    return btc_address() if asset == "BTC" else eth_address()  # USDC runs on Ethereum


def amount_for(asset, eur):
    """Converts a EUR value into a coin amount using the fixed demo rate."""
    return round(eur / EUR_RATE[asset], 6)  # 6 decimals is enough for the demo


def base_request(i, asset, eur, wallet_type, scenario):
    """
    Builds one normal, valid withdrawal request.

    Args:
        i:           running number, used for the request ID (WD-001, WD-002, ...)
        asset:       coin to send ("BTC", "ETH" or "USDC")
        eur:         value of the withdrawal in EUR
        wallet_type: "exchange" (wallet at another provider) or "self_hosted" (private wallet)
        scenario:    label that describes what this test case should show

    Returns:
        dict with all fields the workflow expects.
    """
    first = random.choice(["Alex", "Sam", "Chris", "Robin", "Kim", "Jo"])  # fake first name
    last = random.choice(["Muster", "Beispiel", "Test", "Demo"])  # fake last name
    req = {  # the request object sent to the n8n webhook
        "request_id": f"WD-{i:03d}",  # unique request ID, e.g. WD-007
        "scenario": scenario,  # test scenario label
        "customer_id": f"CUST-{random.randint(1000, 9999)}",  # fake customer ID
        "customer_tenure_days": random.randint(40, 2000),  # how long the customer has existed
        "asset": asset,  # which coin is sent
        "amount": amount_for(asset, eur),  # amount in coins
        "amount_eur": round(eur, 2),  # amount in EUR (rounded to cents)
        "destination_address": address_for(asset),  # target wallet address
        "wallet_type": wallet_type,  # "exchange" or "self_hosted"
        "travel_rule": {  # sender/receiver data required by the EU Travel Rule
            "originator_name": f"{first} {last}",  # sender name (fake)
            "originator_account": f"ACC-{random.randint(100000, 999999)}",  # sender account (fake)
            "beneficiary_name": f"{random.choice(['Lee', 'Max', 'Noa'])} Sample",  # receiver name (fake)
        },
        "simulation": {},  # flags that force special behavior (delay, failure)
    }
    if wallet_type == "exchange":  # transfers to another provider need its name
        req["travel_rule"]["beneficiary_vasp"] = random.choice(  # VASP = crypto service provider
            ["DemoExchange AG", "SampleCoin GmbH", "TestTrade BV"]  # fake provider names
        )
    return req  # return the finished request


def build():
    """
    Builds the full list of test requests (18 in total).

    Returns:
        list of request dicts in random order.
    """
    reqs = []  # collects all requests
    i = 1  # running number for the request IDs

    def add(r):
        """Adds a request to the list and increases the running number."""
        nonlocal i  # allows changing the outer variable i
        reqs.append(r)  # store the request
        i += 1  # next request gets the next ID

    # 1) Normal withdrawals to an exchange -> expected result: approve
    for _ in range(6):  # create 6 normal cases
        asset = random.choice(["BTC", "ETH", "USDC"])  # random coin
        add(base_request(i, asset, random.uniform(50, 900), "exchange", "normal_exchange"))  # small amount

    # 2) Large withdrawal to a private wallet -> extra ownership check step
    for _ in range(3):  # create 3 cases
        asset = random.choice(["BTC", "ETH"])  # random coin
        add(base_request(i, asset, random.uniform(1500, 8000), "self_hosted", "self_hosted_large"))  # > 1000 EUR

    # 3) Wallet linked to a mixer -> manual review
    for _ in range(2):  # create 2 cases
        r = base_request(i, "ETH", random.uniform(300, 2500), "exchange", "mixer_wallet")  # normal request
        r["destination_address"] = MIXER_WALLET  # replace target with the risky mixer wallet
        add(r)  # store it

    # 4) Sanctioned wallet -> reject
    r = base_request(i, "USDC", random.uniform(2000, 5000), "exchange", "sanctioned_wallet")  # normal request
    r["destination_address"] = SANCTIONED_WALLET  # replace target with the sanctioned wallet
    add(r)  # store it

    # 5) Travel Rule data missing -> error at the Travel Rule Agent
    for _ in range(2):  # create 2 cases
        r = base_request(i, "BTC", random.uniform(200, 3000), "exchange", "missing_travel_rule")  # normal request
        r["travel_rule"].pop("beneficiary_name")  # remove the receiver name to cause the error
        add(r)  # store it

    # 6) Network congestion -> slow but successful broadcast
    r = base_request(i, "ETH", random.uniform(100, 800), "exchange", "network_congestion")  # normal request
    r["simulation"]["network_congestion"] = True  # tells the Broadcast Agent to wait longer
    add(r)  # store it

    # 7) Broadcast fails -> error at the last step
    r = base_request(i, "BTC", random.uniform(100, 800), "exchange", "broadcast_failure")  # normal request
    r["simulation"]["network_congestion"] = True  # long wait first
    r["simulation"]["broadcast_failure"] = True  # then the Broadcast Agent throws an error
    add(r)  # store it

    # 8) Required field missing -> error at the first step (Intake Agent)
    r = base_request(i, "ETH", random.uniform(100, 800), "exchange", "missing_field")  # normal request
    r.pop("destination_address")  # remove a required field to cause the error
    add(r)  # store it

    # 9) Very new customer with a high amount -> risk score leads to manual review
    r = base_request(i, "BTC", random.uniform(12000, 20000), "exchange", "new_customer_high_amount")  # > 10k EUR
    r["customer_tenure_days"] = 5  # customer is only 5 days old
    add(r)  # store it

    random.shuffle(reqs)  # mix the order so runs are not grouped by scenario
    return reqs  # return all requests


if __name__ == "__main__":  # only runs when the file is started directly
    out = Path(__file__).resolve().parent.parent / "data" / "requests.json"  # output file path
    out.parent.mkdir(exist_ok=True)  # create the data folder if it does not exist
    data = build()  # build all requests
    out.write_text(json.dumps(data, indent=2))  # write them as readable JSON
    print(f"Wrote {len(data)} requests to {out}")  # short confirmation in the terminal
