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

6. **Crear y pushear tag (NO dispara nada):**
   - Tag: `vX.X.X` (usa versión de pyproject.toml)
   - Si el tag ya existe localmente, bórralo primero: `git tag -d vX.X.X`
   - `git tag vX.X.X -m "vX.X.X"`
   - `git push origin vX.X.X`
   - El tag es el registro de la release. `publish.yml` ya no escucha tags ni releases.

7. **Disparar la publicación (trusted publishing, OIDC, sin token):**
   - `gh workflow run publish.yml --ref main -f version=X.X.X` (misma versión que pyproject.toml, desde `main`)
   - Fuera de `main`, `check` se salta y con él todo lo demás. En `main`, falla si la versión no es la de pyproject.toml, y corre `tests/interop` y la suite offline; `build` arma `dist/`
   - `gh run list --workflow publish.yml --limit 1` para seguirlo

8. **El dueño aprueba:** el job `publish` espera en el environment `pypi` hasta que el dueño lo aprueba en **Review deployments**. Sin esa aprobación no se publica nada. Con ella, sube `dist/` por OIDC (`pypa/gh-action-pypi-publish`).

9. **Verificar en PyPI:** `curl -s https://pypi.org/pypi/uvd-x402-sdk/X.X.X/json` devuelve la versión.

10. **GitHub Release (opcional, no dispara nada):** `gh release create vX.X.X`, title `vX.X.X`, notas con los últimos 5 commits + comando de instalación.

11. **Resumen final** con links al run de Actions y a PyPI

## Reglas

- **NUNCA** dejar co-author de Claude en commits
- **SIEMPRE** usar la versión de `pyproject.toml` para el tag
- **NUNCA** force push a main
- Argumento opcional: $ARGUMENTS (se usa como mensaje de commit)
