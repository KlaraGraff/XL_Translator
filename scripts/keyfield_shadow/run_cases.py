import keyfield_check as k
from cases import CASES, MEMORY

fp = fn = tp = tn = 0
for label, sl, tl, src, tgt, bad in CASES:
    f = k.check_all(src, tgt, sl, tl, MEMORY.get(label))
    flagged = bool(f)
    if flagged and bad: tp += 1; mark = "✓ 报出"
    elif flagged and not bad: fp += 1; mark = "✗ 误报"
    elif not flagged and bad: fn += 1; mark = "✗ 漏报"
    else: tn += 1; mark = "✓ 不报"
    print(f"{mark}  {label}: {'; '.join(x.rule + '：' + x.message for x in f)}")
print(f"\n该报 {tp+fn} 条，报出 {tp}；不该报 {tn+fp} 条，误报 {fp}")
