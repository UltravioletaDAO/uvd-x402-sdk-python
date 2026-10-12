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

6. **El tag NO se crea a mano:** el job `tag` de `publish.yml` crea `vX.X.X` (anotado) sobre el commit publicado,
   solo después de que la subida a PyPI salió bien (`scripts/release_tag.py`). Si el tag ya existe en ese commit no
   hace nada; si existe en OTRO commit, el job falla y no lo mueve (no lo borres para "arreglarlo": compara los dos
   commits). El tag es el registro de la release y no dispara nada.

7. **Disparar la publicación (trusted publishing, OIDC, sin token):**
   - `gh workflow run publish.yml --ref main -f version=X.X.X` (misma versión que pyproject.toml, desde `main`)
   - Fuera de `main`, `check` se salta y con él todo lo demás. En `main`, falla si la versión no es la de pyproject.toml, y corre `tests/interop` y la suite offline; `build` arma `dist/`
   - `gh run list --workflow publish.yml --limit 1` para seguirlo

8. **Publica sin aprobación humana:** el job `publish` corre en el environment `pypi`, que solo admite `main` y no tiene revisor requerido (se quitó el 2026-09-30), y sube `dist/` por OIDC (`pypa/gh-action-pypi-publish`) sin esperar a nadie. Los controles son: solo `main`, solo `workflow_dispatch`, OIDC sin token, y el camino a `main` (revisión, CI verde).

9. **Verificar en PyPI:** `curl -s https://pypi.org/pypi/uvd-x402-sdk/X.X.X/json` devuelve la versión, y
   `git ls-remote --tags origin refs/tags/vX.X.X 'refs/tags/vX.X.X^{}'` el commit del run (la línea `^{}`, o la
   única línea si el tag ya existía y era liviano).

10. **GitHub Release (opcional, no dispara nada):** `gh release create vX.X.X`, title `vX.X.X`, notas con los últimos 5 commits + comando de instalación.

11. **Resumen final** con links al run de Actions y a PyPI

## Reglas

- **NUNCA** dejar co-author de Claude en commits
- **SIEMPRE** usar la versión de `pyproject.toml` para el tag
- **NUNCA** force push a main
- Argumento opcional: $ARGUMENTS (se usa como mensaje de commit)
