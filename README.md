# velox-engine

MVP terminal-only pour home-trainer FTMS BLE (focus Elite Direto XRT), structuré pour évoluer en projet open-source et orchestration OpenClaw.

## MVP scope
- Scan BLE
- Connexion au Direto
- Subscribe Indoor Bike Data
- Affichage puissance/cadence en temps réel
- Option ERG fixe (`set target power`)
- Interface Linux (`tkinter`) pour piloter un workout ERG (CSV/JSON)
- Entraînements préconstruits (Réveil / FTP / Power) avec scaling FTP
- Session VO2max prédéfinie (`VO2max 5x3`)
- Courbe de puissance + timers step/séance pendant l'exécution
- Dashboard avec jauges vitesse/puissance/RPM/odomètre
- Couleurs de jauges selon zone attendue de l'exercice
- Objectifs RPM par step (workouts préconstruits + fichiers custom)
- Sauvegarde locale des sessions (`~/.velox-engine/sessions.jsonl`)
- Filtre de séances par durée (`<=30`, `30-45`, `45-60`, `>=60`)
- Nouvelles séances adaptées 30/45/60 min (`Tempo 30`, `Sweet Spot 45`, `Endurance 60`)

## Project structure
```text
velox-engine/
├── backend/
│   ├── ble/
│   │   ├── ftms_client.py
│   │   └── constants.py
│   ├── core/
│   │   ├── engine.py
│   │   └── state.py
│   ├── cli/
│   │   └── main.py
│   └── utils/
│       └── logger.py
├── tests/
│   └── test_ftms_parsing.py
├── requirements.txt
├── pyproject.toml
├── README.md
└── openclaw/
    ├── agents.md
    └── workflow.md
```

## Installation (Linux)
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## BLE permissions (Linux)
### Option A: run with sudo
```bash
sudo -E .venv/bin/python -m backend.cli.main --scan
```

### Option B: allow capabilities on Python binary
```bash
sudo setcap 'cap_net_raw,cap_net_admin+eip' "$(readlink -f "$(which python3)")"
```

## Usage
### Scan BLE
```bash
python -m backend.cli.main --scan
```

### Connect to first FTMS trainer
```bash
python -m backend.cli.main --connect
```

### Connect and set ERG target to 200W
```bash
python -m backend.cli.main --erg 200
```

### Connect with longer startup wait before ERG (time to start pedaling)
```bash
python -m backend.cli.main --connect EC:3A:81:4A:F9:3D --erg 200 --startup-wait 90
```

### Connect to specific trainer (address or exact name)
```bash
python -m backend.cli.main --connect AA:BB:CC:DD:EE:FF
```

### Connect with FTMS debug output (raw payload + flags)
```bash
python -m backend.cli.main --connect EC:3A:81:4A:F9:3D --debug-ftms
```

### Launch Linux UI (desktop app)
```bash
python -m backend.cli.main --ui
```

### Launch responsive web UI (NiceGUI)
```bash
python -m backend.cli.main --ui-web --debug-sim-ht --web-host 0.0.0.0 --web-port 8088
```

### Debug mode: simulate home trainer (no BLE)
```bash
# Simulated scan/connect/metrics in terminal
python -m backend.cli.main --debug-sim-ht --connect --erg 180 --debug-ftms

# Simulated Linux UI
python -m backend.cli.main --ui --debug-sim-ht
```

Dans l'UI:
- scanner/connecter le home trainer
- charger un workout depuis fichier (`.json`/`.csv`) ou un preset
- définir le FTP (watts) pour les presets
- choisir le mode de pilotage (`erg`, `resistance`, `slope`)
- lancer/arrêter la séance ERG
- suivre la courbe cible, le step actif et les timers
- analyser les jauges live:
  - vert: dans la zone attendue
  - orange: proche de la zone
  - rouge: hors zone
- consulter l'historique des sessions sauvegardées

Notes de mode:
- `erg`: supporté sur HT réel et mode simulé.
- `resistance` / `slope`: supportés en mode simulé (`--debug-sim-ht`) pour analyse UI.
  Sur HT réel, l'application indique explicitement que ces commandes ne sont pas encore implémentées.

## Workout file format

### JSON
```json
{
  "name": "Sweet Spot",
  "steps": [
    {
      "duration_sec": 300,
      "target_watts": 120,
      "label": "Warmup",
      "cadence_min_rpm": 85,
      "cadence_max_rpm": 95
    },
    {
      "duration_sec": 600,
      "target_watts": 180,
      "label": "Block 1",
      "cadence_min_rpm": 88,
      "cadence_max_rpm": 96
    },
    {
      "duration_sec": 180,
      "target_watts": 100,
      "label": "Recovery"
    }
  ]
}
```

### CSV
```csv
duration_sec,target_watts,label,cadence_min_rpm,cadence_max_rpm
300,120,Warmup,85,95
600,180,Block 1,88,96
180,100,Recovery,,
```

## Expected output
```text
Connected to Elite Direto XRT (AA:BB:CC:DD:EE:FF)
Power: 182 W | Cadence: 88.0 rpm
Power: 185 W | Cadence: 89.0 rpm
Power: 190 W | Cadence: 90.0 rpm
```

## Development checks
```bash
flake8 backend tests
mypy backend
pytest
```

## OpenClaw orchestration
Run the full MVP workflow (BLE -> tests -> engine -> CLI -> docs):
```bash
./openclaw/run_mvp_workflow.sh
```

Single-turn local command example:
```bash
openclaw agent --local --message "Review backend/ble/ftms_client.py and improve FTMS robustness"
```

## Troubleshooting
- `No FTMS device found`: vérifier que le home trainer est réveillé, non connecté à une autre app (Zwift, TrainerRoad, etc.).
- `bleak is not installed`: installer les dépendances via `pip install -r requirements.txt`.
- `Operation not permitted`: appliquer les permissions BLE Linux ou lancer en `sudo`.
- Aucune donnée reçue: certains appareils n’envoient Indoor Bike Data qu’après début de pédalage.
