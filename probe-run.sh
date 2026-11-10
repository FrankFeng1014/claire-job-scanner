set -x
SECONDS=0
python -m scanner.main scan --no-channels
echo "scan took $SECONDS s"
mkdir -p probe-out/run
cp data/latest.json reports/latest.md probe-out/run/
python - <<'P'
import json
S=json.load(open('data/seen.json'))
d=[r for r in S.values() if r.get('details')]
json.dump([{k:r.get(k) for k in ('employer','title','url','candidate_status','filter_reason','details')} for r in d], open('probe-out/run/details.json','w'), indent=1, ensure_ascii=False)
P
git checkout -- data reports 2>/dev/null; git clean -fdq data reports
