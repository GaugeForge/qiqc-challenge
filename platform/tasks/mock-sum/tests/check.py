import json
from pathlib import Path
reward=0
try:
    p=Path('/workspace/answer.json')
    if p.stat().st_size<=4096:
        answer=json.loads(p.read_text())
        reward=int(type(answer.get('sum')) in [int,float] and answer['sum']==81)
except (OSError,ValueError,AttributeError):
    pass
Path('/logs/verifier').mkdir(parents=True,exist_ok=True)
Path('/logs/verifier/reward.txt').write_text(str(reward))
