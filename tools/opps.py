import sys, json, random
mode = sys.argv[1] if len(sys.argv) > 1 else "station"
for line in sys.stdin:
    m = json.loads(line); t = m.get("type")
    if t == "match_end": break
    if t == "action_request":
        L = m["legal_actions"]
        if mode == "station": a = {"action": "check" if "check" in L else "call"}
        elif mode == "maniac":
            a = {"action": "raise", "amount": m["max_raise"]} if "raise" in L else {"action": "call" if "call" in L else "check"}
        else:
            c = random.choice(L)
            a = {"action": c}
            if c == "raise": a["amount"] = random.randint(m["min_raise"], m["max_raise"])
        a["type"] = "action"; print(json.dumps(a), flush=True)
    elif t == "discard_request":
        print(json.dumps({"type": "discard", "card": m["hole_cards"][0]}), flush=True)
