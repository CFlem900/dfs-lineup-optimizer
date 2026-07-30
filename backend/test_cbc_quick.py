"""Quick CBC solver test with the night pool."""
import json
import time
import pulp

# Load pool
pool = json.load(open("cache/pool_1bb15ab90d00.json", encoding="utf-8"))["players"]
print(f"Pool: {len(pool)} players")

# Build a simple DK lineup ILP
prob = pulp.LpProblem("test", pulp.LpMaximize)
x = {i: pulp.LpVariable(f"x_{i}", cat="Binary") for i in range(len(pool))}

# Objective: maximize projected FP
prob += pulp.lpSum(pool[i]["projected_fp"] * x[i] for i in range(len(pool)))

# Constraint: exactly 8 players
prob += pulp.lpSum(x[i] for i in range(len(pool))) == 8

# Constraint: salary cap $50,000
prob += pulp.lpSum(pool[i]["salary"] * x[i] for i in range(len(pool))) <= 50000

# Position constraints (DK NBA: PG, SG, SF, PF, C, G, F, UTIL)
slot_mins = {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1}
for slot, min_count in slot_mins.items():
    eligible = [i for i in range(len(pool)) if slot in (pool[i].get("eligible_slots") or [])]
    if eligible:
        prob += pulp.lpSum(x[i] for i in eligible) >= min_count

print("Solving...")
t0 = time.time()
solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=10)
status = prob.solve(solver)
elapsed = time.time() - t0
print(f"Status: {pulp.LpStatus[status]}, Time: {elapsed:.1f}s")

if pulp.LpStatus[status] == "Optimal":
    selected = [i for i in range(len(pool)) if x[i].value() > 0.5]
    total_fp = sum(pool[i]["projected_fp"] for i in selected)
    total_sal = sum(pool[i]["salary"] for i in selected)
    print(f"Total FP: {total_fp:.1f}, Salary: ${total_sal:,}")
    for i in selected:
        p = pool[i]
        print(f"  {p['player_name']:30s} {p['position']:8s} fp={p['projected_fp']:5.1f} sal=${p['salary']:,}")
