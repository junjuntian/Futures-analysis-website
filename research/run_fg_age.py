"""玻璃「成本进场 + 轮龄≥2」—— 按 PLAN_FG_AGE_v1 预注册执行。

五道闸门复用 run_cost_gates.gates(同一份实现),外加第 6 关:轮龄 2/3/5
三格夏普全部高于基线且单调或平稳。规格与判据一个字不改。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hog_money as H  # noqa: E402
import run_cost_entry as R  # noqa: E402
import run_cost_gates as G  # noqa: E402

MAIN = {"need_adding": True, "min_age": 2}

print("#" * 96)
print("# 玻璃 成本 + 卸仓≤30% + 还在加仓 + 轮龄≥2(PLAN_FG_AGE_v1,事后假设复验)")
print("#" * 96)
n_pass, info = G.gates("FG", MAIN)

# —— 第 6 关:参数面 ——
print("第6关 参数面(轮龄 2/3/5 + 不要求加仓的轮龄≥2):")
sig, mkt, rdf, op, st, groups, unload = R.load("FG")
_, _, day_b = H.replay(sig, mkt, rdf, op, st)
base_sh = G.sharpe(day_b)
surf = {}
for label, kw in (("轮龄≥2", {"need_adding": True, "min_age": 2}),
                  ("轮龄≥3", {"need_adding": True, "min_age": 3}),
                  ("轮龄≥5", {"need_adding": True, "min_age": 5}),
                  ("轮龄≥2 不要求加仓", {"need_adding": False, "min_age": 2})):
    tr, d = G.run_candidate(sig, mkt, rdf, op, st, groups, unload, kw)
    sh = G.sharpe(d)
    surf[label] = sh
    cum = float((1 + d.fillna(0)).prod() - 1) * 100
    print(f"    {label:<14} {len(tr):>4} 笔  累计 {cum:>+7.1f}%  回撤 {H._perf(d)['max_dd_pct']:>+6.1f}%"
          f"  夏普 {sh:.2f}  (基线 {base_sh:.2f})")
three = [surf["轮龄≥2"], surf["轮龄≥3"], surf["轮龄≥5"]]
all_beat = all(x > base_sh for x in three)
# 单调或平稳:相邻格不许翻脸(任意相邻格差不超过最大值的一半),且都赢基线
spread_ok = (max(three) - min(three)) <= 0.5 * max(three)
g6 = all_beat and spread_ok
print(f"  → 三格全赢基线:{all_beat};相邻平稳:{spread_ok}  [{'过' if g6 else '不过'}]")
print(f"\n★ 六关通过 {n_pass + int(g6)}/6(前五关 {n_pass}/5)")
