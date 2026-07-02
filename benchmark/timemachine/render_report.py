import json, os
HERE=os.path.dirname(os.path.abspath(__file__))
inj=json.load(open('data/injection_rates.json'))
nz=json.load(open('data/noise_results.json'))
LAB={"A":"floor=off spread=off","B":"floor=on  spread=off","C":"floor=off spread=on","D":"floor=on  spread=on"}
print("### 注入率")
print(f"{'構成':<26}{'総T':>5}{'注入率1hop':>11}{'注入率spread込':>13}{'1hop発火数':>10}{'spread発火数':>11}")
for r in inj:
    print(f"{r['cfg']+' '+LAB[r['cfg']]:<26}{r['turns']:>5}{r['inj_1hop']:>11.3f}{r['inj_all']:>13.3f}{r['n_flashback']:>10}{r['n_spread']:>11}")
print()
print("### ノイズ率（distinct発火プールからサンプリング）")
print(f"{'発火種別':<12}{'全発火':>7}{'判定数':>7}{'noise':>7}{'ノイズ率':>9}")
for t in ['1hop','spread']:
    d=nz[t]
    print(f"{t:<12}{d['total_fires']:>7}{d['n_judged']:>7}{d['n_noise']:>7}{d['noise_rate']:>9.3f}")
print()
print("### 構成別ノイズ率（1hop=A/B共通, spread=C/D加算）")
n1=nz['1hop']['noise_rate']; ns=nz['spread']['noise_rate']
f1_n=nz['1hop']['n_noise']; f1_j=nz['1hop']['n_judged']
fs_n=nz['spread']['n_noise']; fs_j=nz['spread']['n_judged']
for cfg in ['A','B','C','D']:
    if cfg in ('A','B'):
        print(f"{cfg} {LAB[cfg]}: ノイズ率={n1:.3f} (1hopのみ)")
    else:
        comb_n=f1_n+fs_n; comb_j=f1_j+fs_j
        print(f"{cfg} {LAB[cfg]}: ノイズ率(1hop)={n1:.3f} / ノイズ率(spread発火)={ns:.3f} / 合算={comb_n/comb_j:.3f}")
