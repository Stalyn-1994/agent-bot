from pathlib import Path
import importlib.util
import json
import sys
import traceback
home = Path('C:\\Users\\stalyn\\AppData\\Local\\hermes')
root = home / 'plugins/corelab-crm'
spec = importlib.util.spec_from_file_location('corelab_crm_job', root/'__init__.py', submodule_search_locations=[str(root)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
try:
    from corelab_crm_job.runner import run
    print(json.dumps(run(home, send=True), ensure_ascii=False))
except Exception as exc:
    print('ERROR CRM: ' + type(exc).__name__ + '. Ejecuta revisar-envios.cmd y verificar.cmd en el paquete CRM.', file=sys.stderr)
    sys.exit(1)
