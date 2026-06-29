# SETUP.md – Was wird WO installiert

Vier verschiedene Orte, vier verschiedene Befehle. Nicht mischen.

## 1. Python-Tools → IMMER in die Projekt-venv, NIE systemweit

Pro Projekt (z. B. KeyManager) einmalig:
```bash
cd /pfad/zu/KeyManager
python3 -m venv venv
source venv/bin/activate
pip install ruff mypy pre-commit
```

In VS Code danach den Interpreter umstellen, damit Roo Code dieselbe venv nutzt:
- Befehlspalette (Strg+Shift+P) → "Python: Select Interpreter" → `./venv/bin/python`

Falls pip wegen "externally-managed-environment" meckert: das passiert NUR, wenn man
versucht systemweit zu installieren. Innerhalb einer aktivierten venv tritt der Fehler
nicht auf. Nur falls du wirklich mal ein CLI-Tool systemweit brauchst:
```bash
pip install <tool> --break-system-packages
```

## 2. JavaScript/CSS/HTML-Tools → npm, projektlokal (nicht global)

Im Projektordner (gleiche Ebene wie .pre-commit-config.yaml):
```bash
npm init -y          # nur falls noch keine package.json existiert
npm install --save-dev stylelint htmlhint prettier
```
`npx stylelint` / `npx htmlhint` (wie im .pre-commit-config.yaml hinterlegt) finden die
projektlokal installierten Pakete automatisch – kein globales `npm install -g` nötig.

## 3. pre-commit Hook aktivieren (einmalig pro Projekt, innerhalb der venv)
```bash
source venv/bin/activate
pre-commit install
```
Ab jetzt blockiert jeder `git commit` automatisch, bis Sektion 5 aus .clinerules
grün ist. Testen kannst du es manuell mit:
```bash
pre-commit run --all-files
```

## 4. VS Code Extensions (optional, für Echtzeit-Unterstreichung im Editor)
Im Extensions-Tab (Strg+Shift+X) installieren:
- **Mypy Type Checker** (Microsoft)
- **Ruff** (Astral Software)
- **stylelint** (Stylelint)
- Diese Extensions ersetzen NICHT den pre-commit Hook, sondern zeigen Fehler schon
  beim Schreiben an – der Hook bleibt trotzdem die verbindliche Prüfung vor jedem Commit.

## Dateien-Übersicht – wohin damit
| Datei                      | Ort                                  |
|-----------------------------|---------------------------------------|
| .clinerules                 | Projekt-Root                          |
| MISTAKES.md                 | Projekt-Root                          |
| .pre-commit-config.yaml     | Projekt-Root                          |
| mypy.ini                    | Projekt-Root                          |
| package.json (von npm init) | Projekt-Root                          |
| venv/                       | Projekt-Root (in .gitignore!)         |
