# Capa 2 · Identidad

Especificación de interop del stack, **v1**. Se declara en `identity` del manifiesto
([`schemas/manifest.schema.json`](schemas/manifest.schema.json)).

En todas las apps del stack la wallet ya es la identidad. Lo que falta es declararla **en un solo
lugar**, con la misma forma, para que las allowlists y los indexadores la contrasten en vez de
copiarla.

## R2.1 · Cada app tiene una wallet de servicio: dedicada, sin fondos, que solo firma

La wallet de servicio firma las peticiones S2S de la app ([R3.2](03-autenticacion.md)) y nada más:
no paga, no recibe, no es la del tesoro. Si la app ya es partner de describe-net, **es esa misma
wallet**: la dirección no cambia y la allowlist de describe-net tampoco.

- **Por qué:** una llave sin fondos que se filtra no drena nada; y una sola llave sirve para todos
  los receptores porque ERC-8128 ata cada firma a la authority de su receptor.
- **Sale de:** describe-net (wallet de partner dedicada, sin fondos, por proyecto).

## R2.2 · La identidad se declara en `identity` del manifiesto

| Campo | Qué declara |
|---|---|
| `service_signer` | La dirección pública de la wallet de servicio, o `null` (R2.5). |
| `erc8004` | El `agentId` ERC-8004 de la app, por red CAIP-2 (`"eip155:8453": 1234`). |
| `reputation_hosts` | Los hosts desde los que la app emite documentos de reputación (el host del `feedbackURI`). |

- **Por qué:** hoy el emisor de un feedback se reconoce por el host de su URI contra una lista escrita
  a mano, y una app que emite sin estar en esa lista pasa desapercibida. Declararlo en un lugar
  permite contrastar la lista con lo declarado.

## R2.3 · Las direcciones EVM van en minúsculas

`service_signer` (y toda dirección EVM del contrato) es `0x` + 40 hex en minúsculas.

- **Por qué:** es la forma del keyid de ERC-8128 canónico y la que comparan las allowlists; un
  checksum mixto obliga a normalizar en cada comparación, y quien se olvida compara mal.

## R2.4 · A lo sumo un `agentId` por red

`erc8004` es un mapa por red CAIP-2, así que una app no puede declarar dos identidades en la misma
red. El valor es un entero en EVM y la clave base58 en Solana.

- **Por qué:** dos identidades de la misma app en la misma red parten su reputación en dos, y nadie
  sabe cuál es la buena.

## R2.5 · `service_signer: null` declara que la app no firma como sí misma

Una app que todavía no firma nada en nombre propio lo dice explícitamente con `null`. La clave es
obligatoria, el valor puede ser `null`.

- **Por qué:** «no lo declaró» y «no firma» son distintos; el primero es un manifiesto incompleto y el
  segundo, una decisión. Es el caso de Emporium mientras no tenga que firmar ante una plataforma.

## R2.6 · ERC-8004 se emite a través del facilitador

El registro de identidad y el feedback ERC-8004 se hacen por el facilitador, que paga el gas
(`uvd_x402_sdk.erc8004`). El esquema de emisión (el rol en `tag1`, el host del endpoint y el `schema`
del documento de feedback) se publica como documento propio en una versión menor de esta
especificación.

- **Por qué:** una sola ruta para escribir reputación deja a todas las apps con la misma forma de
  documento, que es lo que un indexador necesita para atribuirlas.

## R2.7 · Las allowlists siguen siendo un cambio revisado

El manifiesto **declara**; no da de alta a nadie. Una allowlist de un receptor (partners, hosts de
reputación, suscriptores) se modifica con un commit revisado, como hasta ahora. Lo que cambia es que
se la puede contrastar contra los manifiestos: una entrada de la allowlist que ningún manifiesto
declara es un error; un host declarado que todavía no aparece en los datos del receptor es un aviso.

- **Por qué:** si el manifiesto diera de alta, cualquiera que publique un manifiesto se daría de alta
  solo.

## R2.8 · La wallet de servicio no se carga con `EnvKeyAdapter()` sin argumentos

`EnvKeyAdapter()` sin argumentos lee `WALLET_PRIVATE_KEY` y después `PRIVATE_KEY`: es el valor por
omisión del **pagador**. La wallet de servicio se carga desde su propio secreto
([R8.2](08-configuracion.md)) o con `EnvKeyAdapter(private_key=...)` y una variable propia.

- **Por qué:** en una app que paga, `WALLET_PRIVATE_KEY` suele ser la llave con fondos; usarla como
  wallet de servicio pone la llave del tesoro en un camino que no mueve plata y publica su dirección
  en las allowlists de los receptores.
