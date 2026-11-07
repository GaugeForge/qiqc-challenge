"""Validate the copied QIQCBench 49-task runtime bundle without executing agents."""
from pathlib import Path
import hashlib, json, os, sys, tomllib
ROOT=Path(__file__).resolve().parents[3]; bundle=ROOT/'benchmark-assets'/'qiqcbench-runtime'
tasks=sorted(bundle.joinpath('harbor_tasks').iterdir())
errors=[]; rows=[]
for d in tasks:
    try: spec=tomllib.loads((d/'task.toml').read_text())
    except Exception as exc: errors.append(f'{d.name}: task.toml: {exc}'); continue
    for req in ['task.toml','instruction.md','environment','tests']:
        if not (d/req).exists(): errors.append(f'{d.name}: missing {req}')
    env=spec.get('environment',{})
    rows.append({'id':d.name,'agent_timeout_sec':spec.get('agent',{}).get('timeout_sec'),'cpus':env.get('cpus'),'memory_mb':env.get('memory_mb'),'network':env.get('network_mode', 'unspecified'),'compose':(d/'environment'/'docker-compose.yaml').exists(),'public_material':(bundle/'configs'/'task_materials'/d.name/'public').exists(),'hidden_material':(bundle/'configs'/'task_materials'/d.name/'hidden').exists()})
config_ids={p.stem for p in bundle.joinpath('configs/tasks').glob('*.yaml')}
task_ids={r['id'] for r in rows}
if task_ids != config_ids: errors.append(f'task/config mismatch: tasks_only={sorted(task_ids-config_ids)} configs_only={sorted(config_ids-task_ids)}')
network={}
for r in rows: network[r['network']]=network.get(r['network'],0)+1
result={'task_count':len(rows),'config_count':len(config_ids),'errors':errors,'ready_for_static_catalog':not errors,'network_modes':network,'compose_tasks':sum(r['compose'] for r in rows),'public_material_tasks':sum(r['public_material'] for r in rows),'hidden_material_tasks':sum(r['hidden_material'] for r in rows),'max_agent_timeout_sec':max((r['agent_timeout_sec'] or 0 for r in rows),default=0),'total_declared_cpus':sum(r['cpus'] or 0 for r in rows),'sha256_manifest':hashlib.sha256((bundle/'MANIFEST.sha256').read_bytes()).hexdigest()}
out=ROOT/'competition-platform'/'platform'/'test-results'/'benchmark-audit.json';out.write_text(json.dumps(result,ensure_ascii=False,indent=2)); print(json.dumps(result,ensure_ascii=False))
if errors: raise SystemExit(1)
