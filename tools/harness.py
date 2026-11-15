"""Minimal local engine for Discard Hold'em (per the PDFs). Usage: harness.py BOT OPP LEGHANDS SEED [BANK_MS]"""
import sys, json, subprocess, threading, queue, time, random, itertools

RANKS = "23456789TJQKA"; SUITS = "shdc"
DECK = [r + s for r in RANKS for s in SUITS]

def score5(cs):
    rk = sorted((RANKS.index(c[0]) for c in cs), reverse=True)
    flush = len({c[1] for c in cs}) == 1
    cnt = {}
    for r in rk: cnt[r] = cnt.get(r, 0) + 1
    groups = sorted(cnt.items(), key=lambda x: (-x[1], -x[0]))
    uniq = sorted(set(rk), reverse=True)
    st = None
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4: st = uniq[0]
        elif uniq == [12, 3, 2, 1, 0]: st = 3
    shape = [g[1] for g in groups]; order = [g[0] for g in groups]
    if st is not None and flush: return (8, [st])
    if shape == [4, 1]: return (7, order)
    if shape == [3, 2]: return (6, order)
    if flush: return (5, order)
    if st is not None: return (4, [st])
    if shape == [3, 1, 1]: return (3, order)
    if shape == [2, 2, 1]: return (2, order)
    if shape == [2, 1, 1, 1]: return (1, order)
    return (0, order)

def best7(cs):
    return max(score5(c) for c in itertools.combinations(cs, 5))

class Proc:
    def __init__(self, cmd, bank):
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, bufsize=1, encoding="utf-8")
        self.launch = time.perf_counter()
        self.q = queue.Queue(); self.bank = bank; self.used = 0.0; self.err = []; self.maxreply = 0.0
        threading.Thread(target=self._r, daemon=True).start()
        threading.Thread(target=self._e, daemon=True).start()
        self.notices = 0; self.sent = None
    def _r(self):
        for line in self.p.stdout: self.q.put((time.perf_counter(), line))
        self.q.put((time.perf_counter(), None))
    def _e(self):
        for line in self.p.stderr: self.err.append(line)
    def send(self, m, req=False):
        try:
            self.p.stdin.write(json.dumps(m) + "\n"); self.p.stdin.flush()
        except Exception:
            pass
        if req: self.sent = time.perf_counter()
    def recv(self):
        """returns parsed dict, or 'FORFEIT:<why>'"""
        start = max(self.sent, self.launch + 5)
        try:
            t, line = self.q.get(timeout=max(0.01, 5 + (start - time.perf_counter()) + 0.5))
        except queue.Empty:
            return "FORFEIT:timeout5s"
        if line is None: return "FORFEIT:eof/crash"
        dt = t - start
        self.maxreply = max(self.maxreply, dt)
        self.used += max(0, dt)
        if dt > 5: return "FORFEIT:reply>5s"
        if self.used * 1000 > self.bank: return "FORFEIT:bank"
        try: return json.loads(line)
        except Exception: return {}

def play_leg(cmds, deals, bank, swap, log):
    procs = [Proc(c, bank) for c in cmds]  # index 0 = A (bot under test)
    for p in procs:
        p.send({"type": "init", "protocol_version": "1.0", "total_hands": len(deals), "starting_stack": 200,
                "small_blind": 1, "big_blind": 2, "time_bank_ms": bank, "per_response_ms": 5000})
    net = [0, 0]; forfeit = None; illegal = [0, 0]
    for hid, (h0, h1, board) in enumerate(deals, 1):
        holes = [list(h1), list(h0)] if swap else [list(h0), list(h1)]
        btn = (hid % 2 == 0) if not swap else (hid % 2 == 1)   # A button on even hands in leg 1
        btn = 0 if btn else 1  # player index that is button
        seats = {btn: "button", 1 - btn: "big_blind"}
        for i in (0, 1):
            procs[i].send({"type": "new_hand", "hand_id": hid, "seat": seats[i], "hole_cards": holes[i], "stack": 200, "opp_stack": 200})
        commit = [0, 0]; hist = []; folded = None
        def req_for(i, street, bets, raises_left, last_s, allin_total):
            o = 1 - i
            to_call = bets[o] - bets[i]
            legal = []
            if to_call > 0: legal += ["fold", "call"]
            else: legal += ["check"]
            r = {"type": "action_request", "hand_id": hid, "street": street, "board": board[:{"preflop": 0, "flop": 3, "turn": 4, "river": 5}[street]],
                 "pot": commit[0] + commit[1], "my_bet": bets[i], "opp_bet": bets[o], "to_call": to_call,
                 "my_stack": 200 - commit[i], "opp_stack": 200 - commit[o]}
            if raises_left > 0 and allin_total > bets[o] and bets[i] < allin_total:
                legal.append("raise")
                r["min_raise"] = min(allin_total, bets[o] + max(last_s, 2)); r["max_raise"] = allin_total
            r["legal_actions"] = legal; r["raises_left"] = raises_left; r["history"] = list(hist)
            r["time_left_ms"] = int(bank - procs[i].used * 1000)
            return r
        def betting(street, first):
            nonlocal folded
            base = list(commit)  # equal committed at start of street (except preflop blinds handled)
            if street == "preflop":
                bets = [0, 0]; bets[btn] = 1; bets[1 - btn] = 2; commit[btn] += 1; commit[1 - btn] += 2
                raises_left = 3; last_s = 2; prior = 0
            else:
                bets = [0, 0]; raises_left = 4; last_s = 2; prior = commit[0]
            allin_total = 200 - prior
            acted = set(); cur = first
            while True:
                if bets[0] == bets[1] and (len(acted) == 2 or bets[0] == allin_total):
                    if not (street == "preflop" and len(acted) < 2 and bets[0] < allin_total): return
                if street != "preflop" and bets[0] == allin_total and bets[1] == allin_total: return
                if bets[0] == bets[1] == allin_total: return
                rq = req_for(cur, street, bets, raises_left, last_s, allin_total)
                procs[cur].send(rq, True)
                rep = procs[cur].recv()
                if isinstance(rep, str): raise RuntimeError((cur, rep))
                a = rep.get("action") if rep.get("type") == "action" else None
                legal = rq["legal_actions"]; ok = a in legal
                if ok and a == "raise":
                    amt = rep.get("amount"); ok = isinstance(amt, int) and rq["min_raise"] <= amt <= rq["max_raise"]
                if not ok:
                    illegal[cur] += 1; procs[cur].notices += 1
                    a = "check" if "check" in legal else "fold"
                acted.add(cur)
                if a == "fold":
                    hist.append({"street": street, "actor": "x", "action": "fold"}); folded = cur; return
                if a == "check": hist.append({"street": street, "actor": cur, "action": "check"})
                elif a == "call":
                    commit[cur] += bets[1 - cur] - bets[cur]; bets[cur] = bets[1 - cur]
                    hist.append({"street": street, "actor": cur, "action": "call", "amount": bets[cur]})
                else:
                    amt = rep["amount"]; commit[cur] += amt - bets[cur]; last_s = amt - bets[1 - cur]; bets[cur] = amt
                    raises_left -= 1
                    hist.append({"street": street, "actor": cur, "action": "raise", "amount": amt})
                cur = 1 - cur
        def rel(h):
            # per-player view
            return h
        # history actor translation done per request: wrap send
        orig_req = req_for
        def req_for(i, *a, **k):
            r = orig_req(i, *a, **k)
            r["history"] = [dict(x, actor=("me" if x["actor"] == i else "opp")) for x in r["history"] if x["actor"] != "x"]
            return r
        try:
            betting("preflop", btn)
            disc = [None, None]
            if folded is None:
                betting("flop", 1 - btn)
            if folded is None:
                for i in (0, 1):
                    procs[i].send({"type": "discard_request", "hand_id": hid, "hole_cards": holes[i], "board": board[:3],
                                   "pot": commit[0] + commit[1], "my_stack": 200 - commit[i], "opp_stack": 200 - commit[1 - i],
                                   "time_left_ms": int(bank - procs[i].used * 1000)}, True)
                for i in (0, 1):
                    rep = procs[i].recv()
                    if isinstance(rep, str): raise RuntimeError((i, rep))
                    c = rep.get("card") if rep.get("type") == "discard" else None
                    if c not in holes[i]:
                        illegal[i] += 1
                        c = min(holes[i], key=lambda x: (RANKS.index(x[0]), SUITS.index(x[1])))
                    disc[i] = c
                for i in (0, 1):
                    holes[i].remove(disc[i])
                for i in (0, 1):
                    procs[i].send({"type": "discard_result", "hand_id": hid, "my_discard": disc[i], "opp_discard": disc[1 - i], "hole_cards": holes[i]})
                betting("turn", 1 - btn)
                if folded is None: betting("river", 1 - btn)
        except RuntimeError as e:
            forfeit = e.args[0]; break
        if folded is not None:
            n = [0, 0]; n[folded] = -commit[folded]; n[1 - folded] = commit[folded]
        else:
            s0 = best7(holes[0] + board); s1 = best7(holes[1] + board)
            n = [0, 0]
            if s0 > s1: n = [commit[1], -commit[1]]
            elif s1 > s0: n = [-commit[0], commit[0]]
        net[0] += n[0]; net[1] += n[1]
        for i in (0, 1):
            procs[i].send({"type": "hand_result", "hand_id": hid, "result": "fold" if folded is not None else "showdown", "net": n[i],
                           "board": board, "my_hole_cards": holes[i], "opp_hole_cards": None if folded is not None else holes[1 - i],
                           "opp_discard": None, "history": []})
    for p in procs:
        p.send({"type": "match_end", "reason": "complete"})
    time.sleep(0.2)
    info = [(round(p.used, 2), round(p.maxreply, 3)) for p in procs]
    errs = ["".join(p.err)[-400:] for p in procs]
    for p in procs:
        try: p.p.kill()
        except Exception: pass
    return net, forfeit, illegal, info, errs

def main():
    bot, opp, n, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    bank = int(sys.argv[5]) if len(sys.argv) > 5 else 30000
    rng = random.Random(seed); deals = []
    for _ in range(n):
        d = DECK[:]; rng.shuffle(d); deals.append((d[0:3], d[3:6], d[6:11]))
    cmds = [[sys.executable, "-I", bot], [sys.executable, "-I"] + opp.split("|")]
    tot = 0
    for leg, swap in enumerate((False, True), 1):
        net, forfeit, illegal, info, errs = play_leg(cmds, deals, bank, swap, None)
        print("leg", leg, "net(A,B)=", net, "forfeit=", forfeit, "illegal=", illegal, "(timeused_s,maxreply_s)=", info)
        for e in errs:
            if e.strip(): print("  stderr:", e.strip()[:300])
        tot += net[0]
        if forfeit is not None: break
    print("MATCH net for bot A:", tot)

if __name__ == "__main__":
    main()
