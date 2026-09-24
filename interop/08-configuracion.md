# Capa 8 · Configuración y secretos

Especificación de interop del stack, **v1**.

Cada app del stack configura a las demás a su manera: la URL del facilitador vive en varios nombres
de variable y en constantes, hay URLs guardadas dentro de secretos y secretos HMAC duplicados entre
dos cuentas. Esta capa separa lo que es público de lo que es secreto y deja un solo secreto por app.

## R8.1 · La URL de otra app no es un secreto

Las URLs de las apps del stack las da el **directorio del SDK** ([R1.8](01-descubrimiento.md)): una
tabla por omisión en un solo lugar, que se puede cambiar por app con **una** variable,
`UVD_<APP>_URL` (el id de la app en mayúsculas y con `_` en lugar de `-`:
`UVD_EXECUTION_MARKET_URL`, `UVD_DESCRIBE_NET_URL`). Ninguna URL de otra app va dentro de un secreto
ni en una constante suelta en el código.

- **Por qué:** una URL dentro de un secreto no se ve en una revisión ni en un diff, y la misma URL
  copiada en siete lugares se cambia en seis.

## R8.2 · Cada app tiene un solo secreto de interop: `<app>/service-signer`

La llave de la wallet de servicio ([R2.1](02-identidad.md)) vive en el gestor de secretos de la app
con el nombre `<app>/service-signer` y se carga con `ServiceSigner.from_secret("<app>/service-signer")`
del SDK. El nombre que ya usan las apps partner de describe-net, `<app>/describenet-partner-signer`,
queda como alias: es la misma llave. El valor nunca se imprime ni se registra.

- **Por qué:** cuatro partners resolvieron la misma carga de cuatro formas, con cuatro nombres de
  variable; un nombre fijo y un cargador del SDK la vuelven una línea.
- **Sale de:** describe-net (`<proyecto>/describenet-partner-signer`, el mismo patrón en los cuatro
  partners).

## R8.3 · Ningún secreto se comparte entre dos apps de la casa

Entre apps de la casa no hay secretos compartidos: la autenticación es ERC-8128 contra una allowlist
de direcciones públicas ([R3.2](03-autenticacion.md), [R3.13](03-autenticacion.md)). Una app no lee
secretos de la cuenta de otra.

- **Por qué:** un secreto compartido tiene dos custodios, se rota en dos lugares a la vez, y si se
  filtra de un lado compromete al otro.

## R8.4 · Ninguna variable que exporta la infraestructura queda sin que el código la lea

Si la infraestructura (Terraform) le pasa una variable a una app, el código la lee; si el código no
la lee, la variable se borra.

- **Por qué:** una variable que no gobierna nada miente: quien la cambia cree que movió el facilitador
  o la tesorería, y no movió nada.
