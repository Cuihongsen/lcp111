
import csv
import re

def parse_time(value):
    if not value or value == 'N/A':
        return None
    value = value.strip()
    if value.endswith('ms'):
        return float(value[:-2]) / 1000.0
    elif value.endswith('s'):
        return float(value[:-1])
    try:
        return float(value)
    except ValueError:
        return None

def parse_score(value):
    try:
        return float(value)
    except ValueError:
        return 0.0

def read_csv(filepath):
    data = {}
    with open(filepath, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = row['URL']
            data[url] = {
                'score': parse_score(row['性能分数']),
                'lcp': parse_time(row['LCP']),
                'tbt': parse_time(row['TBT']),
                'fcp': parse_time(row['FCP']),
                'cls': parse_score(row['CLS']),
                'ttfb': parse_time(row['TTFB'])
            }
    return data

old_data = read_csv('/Users/cui/code/ths/lcp/report-old.csv')
new_data = read_csv('/Users/cui/code/ths/lcp/report-new.csv')

common_urls = set(old_data.keys()) & set(new_data.keys())

metrics = ['score', 'lcp', 'tbt', 'fcp', 'cls', 'ttfb']
diffs = {m: [] for m in metrics}
old_totals = {m: [] for m in metrics}
new_totals = {m: [] for m in metrics}

print(f"Comparing {len(common_urls)} URLs found in both reports.")

for url in common_urls:
    old = old_data[url]
    new = new_data[url]
    
    for m in metrics:
        if old[m] is not None and new[m] is not None:
            diffs[m].append(new[m] - old[m])
            old_totals[m].append(old[m])
            new_totals[m].append(new[m])

print("\nAverage Performance Metrics (Old vs New):")
print("-" * 60)
print(f"{'Metric':<10} | {'Old Avg':<10} | {'New Avg':<10} | {'Diff':<10} | {'Improvement %':<15}")
print("-" * 60)

metric_names = {
    'score': 'Score',
    'lcp': 'LCP (s)',
    'tbt': 'TBT (s)',
    'fcp': 'FCP (s)',
    'cls': 'CLS',
    'ttfb': 'TTFB (s)'
}

for m in metrics:
    if old_totals[m]:
        avg_old = sum(old_totals[m]) / len(old_totals[m])
        avg_new = sum(new_totals[m]) / len(new_totals[m])
        avg_diff = avg_new - avg_old
        
        # For Score, higher is better. For others (time), lower is better.
        if m == 'score':
            improvement = ((avg_new - avg_old) / avg_old * 100) if avg_old != 0 else 0
        else:
            improvement = ((avg_old - avg_new) / avg_old * 100) if avg_old != 0 else 0
            
        print(f"{metric_names[m]:<10} | {avg_old:<10.4f} | {avg_new:<10.4f} | {avg_diff:<10.4f} | {improvement:<15.2f}%")

print("-" * 60)
