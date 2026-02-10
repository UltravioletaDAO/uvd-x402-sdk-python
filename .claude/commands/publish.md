Publica el SDK a PyPI. Ejecuta el workflow completo de publicación:

1. **Verificar estado del repo:**
   - Ejecuta `git status` para ver cambios pendientes
   - Ejecuta `git log origin/main..HEAD --oneline` para ver commits sin push
   - Si no hay cambios ni commits pendientes, aborta con mensaje claro

2. **Extraer versión actual:**
   - Lee `pyproject.toml` y extrae la línea `version = "X.X.X"`

3. **Manejar commits:**
   - Si hay cambios sin commit: stage todos los archivos modificados y commitea
   - Usa el argumento del usuario como mensaje de commit, o "chore: release vX.X.X" si no hay argumento
   - Si hay commits locales sin push: continuar con ellos

4. **Limpiar co-author (CRÍTICO):**
   - Ejecuta `git log -1 --format="%B" | grep -i "co-authored-by.*claude"`
   - Si encuentra co-author:
     - Guarda mensaje sin la línea co-author
     - `git reset --soft HEAD~1`
     - `git commit -m "mensaje limpio"`
   - Verifica con `git log -1 --format="%B" | grep -i claude` (debe retornar vacío)

5. **Push a GitHub:**
   - `git push origin main`

6. **Crear y pushear tag:**
   - Tag: `vX.X.X` (usa versión de pyproject.toml)
   - Si el tag ya existe localmente, bórralo primero: `git tag -d vX.X.X`
   - `git tag vX.X.X -m "vX.X.X"`
   - `git push origin vX.X.X`

7. **Crear GitHub Release (dispara el workflow de PyPI):**
   - Usa `gh release create vX.X.X`
   - Title: `vX.X.X`
   - Notes: Lista los últimos 5 commits como bullet points + comando de instalación

8. **Resumen final** con links a release y actions

## Reglas

- **NUNCA** dejar co-author de Claude en commits
- **SIEMPRE** usar la versión de `pyproject.toml` para el tag
- **NUNCA** force push a main
- Argumento opcional: $ARGUMENTS (se usa como mensaje de commit)
