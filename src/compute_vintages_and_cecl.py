import os, numpy as np, pandas as pd
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
DATA = os.path.join(ROOT, "data")
FIGS = os.path.join(ROOT, "figures")
RES  = os.path.join(ROOT, "results")
os.makedirs(FIGS, exist_ok=True); os.makedirs(RES, exist_ok=True)

# Load the small sample
sample_path = os.path.join(DATA, "loans_sample.csv")
if not os.path.exists(sample_path):
    raise FileNotFoundError(f"Missing {sample_path}. Create it first.")

df = pd.read_csv(sample_path)

def first_exist(cols):
    for c in cols:
        if c in df.columns: return c
    return None

col_iss = first_exist(["issue_d","issue_date","Issue Date","origination_date","orig_date"]) or "issue_d"
col_term= first_exist(["term","Term"])
col_ls  = first_exist(["loan_status","Loan Status","status","current_status","Status"])
col_rec = first_exist(["recoveries","Recoveries"])
col_amt = first_exist(["loan_amnt","original_balance","orig_bal","LoanAmount"])
col_lp  = first_exist(["last_pymnt_d","last_payment_date","Last Payment Date"])
col_grade = first_exist(["grade","Grade"])
col_ficoh = first_exist(["fico_range_high","fico_high","FICO"])

# Parse essentials
df["term_m"] = pd.to_numeric(df[col_term].astype(str).str.extract(r"(\d+)")[0], errors="coerce") if col_term else 36
df["issue_dt"] = pd.to_datetime(df[col_iss], errors="coerce")
df["last_pymnt_dt"] = pd.to_datetime(df[col_lp], errors="coerce") if col_lp else pd.NaT
df["orig_amt"] = pd.to_numeric(df[col_amt], errors="coerce") if col_amt else 10000.0
df["recoveries"] = pd.to_numeric(df[col_rec], errors="coerce").fillna(0.0) if col_rec else 0.0
df["grade"] = (df[col_grade] if col_grade else "C").fillna("C")
df["fico_high"] = pd.to_numeric(df[col_ficoh], errors="coerce") if col_ficoh else 680

charged_off = df[col_ls].str.contains("Charged Off", case=False, na=False) if col_ls else (pd.Series(False, index=df.index))

def mob(a, b):
    if pd.isna(a) or pd.isna(b): return np.nan
    return (b.year - a.year)*12 + (b.month - a.month)

df["chargeoff_flag"] = charged_off.astype(int)
df["vintage"] = df["issue_dt"].dt.to_period("Q").astype(str)
df["co_mob"] = [mob(a,b) if f==1 else np.nan for a,b,f in zip(df["issue_dt"], df["last_pymnt_dt"], df["chargeoff_flag"])]
df["nco_amt"] = np.where(df["chargeoff_flag"]==1, df["orig_amt"] - df["recoveries"], 0.0)

# Vintage curves
rows=[]
for v, g in df.dropna(subset=["issue_dt"]).groupby("vintage"):
    total_orig = g["orig_amt"].sum()
    if not np.isfinite(total_orig) or total_orig<=0: 
        continue
    max_mob = int(np.nanmax(g["co_mob"])) if np.isfinite(g["co_mob"]).any() else 0
    max_mob = min(max_mob, 60)
    for m in range(max_mob+1):
        nco_cum = g.loc[g["co_mob"].le(m), "nco_amt"].sum()
        rows.append({"vintage": v, "mob": m, "cum_loss_pct": nco_cum / total_orig})

vint = pd.DataFrame(rows)
vint.to_csv(os.path.join(RES,"vintage_curves.csv"), index=False)

# Plot top 6 vintages by orig balance
order = (df.groupby("vintage")["orig_amt"].sum().sort_values(ascending=False).head(6).index.tolist())
vplot = vint[vint["vintage"].isin(order)].copy()
plt.figure(figsize=(10,5))
for v in order:
    seg = vplot[vplot["vintage"]==v]
    if not seg.empty:
        plt.plot(seg["mob"], seg["cum_loss_pct"], label=v)
plt.title("Cumulative Net Loss by Vintage (top 6)")
plt.xlabel("Months on Book")
plt.ylabel("Cumulative Net Loss (%)")
plt.gca().yaxis.set_major_formatter(plt.FuncFormatter(lambda y,_: f"{y*100:.1f}%"))
plt.legend(title="Vintage")
plt.tight_layout()
plt.savefig(os.path.join(FIGS,"vintage_curves.png"), dpi=200, bbox_inches="tight")
plt.close()

# CECL-lite allowance sensitivity
pd_map = {'A':0.02,'B':0.03,'C':0.05,'D':0.08,'E':0.12,'F':0.18,'G':0.25}
df["pd_base"] = df["grade"].map(pd_map).fillna(0.05)
df["lgd"] = 0.85
df["ead"] = df["orig_amt"] * 0.65

def allowance_pct(mult):
    ecl = (df["pd_base"]*mult*df["lgd"]*df["ead"]).sum()
    amort_cost = df["ead"].sum()
    return ecl / amort_cost

scenarios = {"Baseline":1.00, "Adverse":1.25, "Severe":1.50}
allow = pd.DataFrame({"scenario": list(scenarios.keys()),
                      "allowance_pct": [allowance_pct(m) for m in scenarios.values()]})
allow.to_csv(os.path.join(RES,"cecl_allowance_sensitivity.csv"), index=False)

plt.figure(figsize=(8,5))
plt.bar(allow["scenario"], allow["allowance_pct"])
for i,v in enumerate(allow["allowance_pct"]):
    plt.text(i, v+0.002, f"{v*100:.1f}%", ha="center", va="bottom", fontsize=11, fontweight="bold")
plt.title("CECL Allowance Sensitivity\n(Allowance as % of Amortized Cost)")
plt.ylabel("Allowance (%)")
plt.gca().yaxis.set_major_formatter(plt.FuncFormatter(lambda y,_: f"{y*100:.1f}%"))
plt.tight_layout()
plt.savefig(os.path.join(FIGS,"cecl_sensitivity.png"), dpi=200, bbox_inches="tight")
plt.close()

print("Done. Wrote figures/vintage_curves.png and figures/cecl_sensitivity.png")

# --- vintage summary table (12/24/36/60M + final) ---
summary_rows = []
for v, g in vint.groupby("vintage"):
    orig_bal = df.loc[df["vintage"] == v, "orig_amt"].sum()
    loans = df.loc[df["vintage"] == v].shape[0]
    s = g.set_index("mob")["cum_loss_pct"].sort_index()
    s = s.reindex(range(0, 61)).ffill()  # fill forward if younger than 60M
    row = {
        "vintage": v,
        "orig_balance": float(orig_bal),
        "loans": int(loans),
        "loss_12m_pct": float(s.loc[12]) if 12 in s.index else float(s.iloc[-1]),
        "loss_24m_pct": float(s.loc[24]) if 24 in s.index else float(s.iloc[-1]),
        "loss_36m_pct": float(s.loc[36]) if 36 in s.index else float(s.iloc[-1]),
        "loss_60m_pct": float(s.loc[60]) if 60 in s.index else float(s.iloc[-1]),
        "final_loss_pct": float(s.dropna().iloc[-1])
    }
    summary_rows.append(row)

vsum = pd.DataFrame(summary_rows).sort_values("vintage")
vsum["orig_balance"] = vsum["orig_balance"].round(0)
for c in ["loss_12m_pct","loss_24m_pct","loss_36m_pct","loss_60m_pct","final_loss_pct"]:
    vsum[c] = vsum[c].round(4)

vsum.to_csv(os.path.join(RES, "vintage_summary.csv"), index=False)
print("Wrote results/vintage_summary.csv")
