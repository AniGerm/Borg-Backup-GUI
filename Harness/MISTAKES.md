# MISTAKES.md
Rohdaten-Log. Wird NIE automatisch in den Prompt-Kontext geladen, dient nur als
Nachschlagewerk bei der periodischen Pflege von .clinerules (siehe Sektion 7).

Format pro Eintrag:
## <Datum>
- Vorfall: <was konkret schieflief>
- Auslöser: <A = KI hat es selbst erkannt / B = User hat "doof"/"schlechter" gesagt>
- Regel ergänzt: <welche Regel in .clinerules daraus entstand, oder "keine, da Einzelfall">

---

## 24.06.2026
- Vorfall: Inline-CSS in index.html eingefügt
- Auslöser: A
- Regel ergänzt: "Kein Inline-CSS, kein Inline-JS" (Sektion 2)

## 29.06.2026
- Vorfall: mypy.ini eigenmächtig relaxiert (strict → ignore_missing_imports) und type: ignore gesetzt, ohne vorher nach OK zu fragen
- Auslöser: B (User: "Du hast ... ohne nach OK zu fragen")
- Regel ergänzt: "Konfigurationsänderungen an .pre-commit-config.yaml, mypy.ini o. Ä. NIE ohne vorherigen Plan und OK" (Sektion 9)
