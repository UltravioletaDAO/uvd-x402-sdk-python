# Capa 9 · La cara MCP de cada app

Especificación de interop del stack, **v1**.

El transporte de hecho entre apps y agentes es MCP Streamable HTTP sin estado. Las fachadas escritas
a mano derivan en silencio del REST que envuelven, y una tool sin esquema de salida no se puede
combinar con otra. Esta capa fija cómo se expone una app por MCP para que se pueda usar, clasificar y
combinar igual que su REST.

## R9.1 · El MCP de cada app es una fachada sin estado sobre su REST

Cada tool arma una petición contra **la misma ruta** del REST y devuelve lo que esa ruta devuelve. No
tiene lógica propia, ni estado de sesión.

- **Por qué:** la tool hereda el control de acceso, el límite de tasa y los privilegios de la ruta (la
  misma paridad de privilegio por las dos puertas), y no puede derivar de ella.
- **Sale de:** el facilitador (su `/mcp` despacha cada tool como una petición sintética contra el
  router REST).

## R9.2 · El MCP acepta el mismo auth S2S que el REST

Si una ruta REST acepta una firma ERC-8128 de partner, la tool que la envuelve también: la firma se
propaga hasta la ruta. El manifiesto declara los modos de auth de la puerta `mcp` como los de cualquier
otra ([R1.6](01-descubrimiento.md)).

- **Por qué:** si la puerta MCP no propaga la identidad, la misma app de la casa paga o no según la
  puerta por la que entra.

## R9.3 · Cada tool publica sus anotaciones MCP

`readOnlyHint`, `destructiveHint`, `idempotentHint` y `openWorldHint`, con el valor que corresponde a
la ruta que envuelve. Agregar o cambiar una anotación cambia la huella de la tool en el mostrador
(R9.4): esos cambios se juntan y se vuelven a declarar en una sola tanda.

- **Por qué:** son lo que un cliente MCP genérico lee para decidir si una tool se puede llamar sin
  preguntarle a una persona.

## R9.4 · La clase de la casa va en `_meta["uvd/clase"]`, no en `annotations`

Cada tool declara su clase en el `_meta` de la definición de la tool:
`"_meta": {"uvd/clase": "lectura"}`. El vocabulario es cerrado, el de las siete clases del mostrador
de Emporium:

| Clase | Qué hace la tool |
|---|---|
| `lectura` | Lee y devuelve; no escribe ni cobra |
| `escribe` | Cambia estado |
| `mueve_dinero` | Mueve plata |
| `riel_de_pago` | Es parte de un riel de pago (verify, settle, autorizaciones) |
| `cobra_por_llamada` | Cobra por cada llamada |
| `pide_credencial` | Exige una credencial que el llamador tiene que traer |
| `sin_clasificar` | Nadie la clasificó todavía (no se reenvía) |

La declara la superficie; el mostrador la lee al medir el contrato de la tool, no en cada llamada, y
la decisión de reenviarla sigue siendo de una persona.

- **Por qué:** la huella con la que el mostrador detecta que una tool cambió es el SHA-256 de
  `{annotations, description, inputSchema, name}`; una clave nueva dentro de `annotations` cambia la
  huella y saca la tool del mostrador hasta que una persona la vuelve a declarar. `_meta` no entra en
  la huella.
- **Sale de:** Emporium (las siete clases y la huella del mostrador).

## R9.5 · Las tools de lectura publican `outputSchema`

Toda tool de clase `lectura` publica `outputSchema` y devuelve su resultado también en
`structuredContent`.

- **Por qué:** combinar dos tools es cruzar campos de la salida de una con la entrada de otra; sin
  esquema de salida no hay tipos que cruzar.

## R9.6 · El texto de terceros va marcado con `untrustedContentHint`

Una tool que devuelve texto escrito por terceros (mensajes, descripciones, contenido indexado) lo
marca con `annotations.untrustedContentHint: true` y lo avisa en su descripción. Como toca
`annotations`, cambia la huella de la tool en el mostrador (R9.4): se agrega junto con los demás
cambios de anotaciones, no suelto.

- **Por qué:** un modelo que lee ese texto tiene que tratarlo como dato, no como instrucción.
- **Sale de:** el MCP de la DAO (la marca ya va en `annotations` de sus tools de texto ajeno).

## R9.7 · Si la app tiene datos públicos, ofrece un nivel de lectura anónimo

Las tools de lectura de datos públicos se pueden listar y llamar sin credencial; las de escritura
siguen pidiendo la suya.

- **Por qué:** una superficie que pide credencial hasta para listar sus tools queda fuera de toda
  federación de lectura y de todo combo.

## R9.8 · Los errores de una tool llevan `uvd_error`

Ver [R6.9](06-errores.md): `isError: true` y el sobre en `structuredContent.uvd_error`.
