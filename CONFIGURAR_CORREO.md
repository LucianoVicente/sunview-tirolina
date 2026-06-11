# Configurar el envío por correo

El asistente de envío **sube el vídeo a Google Drive automáticamente**, genera
el enlace de descarga y abre Gmail ya redactado. Para eso (y para que rellene
el **email del cliente** a partir del **número ORDEN** del Sheets) hay que
configurar una "cuenta de servicio" una vez. La hoja sigue **privada**.

## Paso 1 — Crear la "cuenta de servicio" (el robot)

1. Entra en https://console.cloud.google.com con la cuenta de Google.
2. Arriba, crea un proyecto nuevo (ej. "Sunview Videos") o usa uno existente.
3. Menú ☰ → **APIs y servicios → Biblioteca**. Busca **Google Sheets API** y
   pulsa **Habilitar**. Repite con **Google Drive API** (necesaria para la
   subida automática del vídeo).
4. Menú ☰ → **APIs y servicios → Credenciales** → **Crear credenciales** →
   **Cuenta de servicio**.
   - Nombre: `lector-videos` (el que quieras) → **Crear y continuar** → **Listo**.
5. En la lista de cuentas de servicio, pulsa la que creaste → pestaña **Claves**
   → **Agregar clave → Crear clave nueva → JSON**. Se descarga un archivo `.json`.
   Guárdalo en el PC (ej. `C:\sunview\service_account.json`).
   **No lo subas a internet ni al repositorio.**

## Paso 2 — Compartir la hoja con el robot

1. Abre el archivo `.json` con el Bloc de notas y copia el valor de
   `"client_email"` (algo como `lector-videos@...iam.gserviceaccount.com`).
2. En el Google Sheets del mes (ej. "VIDEOS SUNVIEW PARK JUNIO2026") → botón
   **Compartir** → pega ese email → permiso **Lector** → **Enviar**.

> **Cada mes se crea un Sheets nuevo**: lo único que hay que hacer es repetir
> este paso (Compartir → email del robot → Lector). La app encuentra sola la
> hoja del mes por su nombre (busca el mes y el año en el título, p. ej.
> "JUNIO" y "2026"). No hace falta tocar ninguna configuración.

## Paso 3 — Crear el archivo de configuración

Copia `config_sheets.example.json` a un archivo nuevo llamado
`config_sheets.json` y rellénalo:

   ```json
   {
       "service_account_json": "C:/sunview/service_account.json",
       "columnas": { "orden": 1, "videos": 2, "email": 3, "nombre": 3 }
   }
   ```

   (los números de columna son base 0: A=0, B=1, C=2, D=3… B=ORDEN, D=E-MAIL)

La clave `"spreadsheet_id"` (el ID que aparece en la URL de la hoja, en
`https://docs.google.com/spreadsheets/d/AQUI_VA_EL_ID/edit`) es **opcional**:
solo sirve de respaldo si la búsqueda automática del mes no encuentra nada
(por ejemplo, si la Drive API no está habilitada en el proyecto).

## Paso 4 — La cuenta desde la que se envía

**Por defecto no hay que configurar nada**: el botón "Abrir correo" abre
Gmail en el navegador por defecto del PC, con la sesión de Google que ya esté
iniciada (en el PC de recepción, la cuenta Gmail de la tirolina). Funciona
igual en Chrome y en Edge.

Solo si hiciera falta, en `config_sheets.json` la clave **`remitente_correo`**
permite afinar:

* una `@gmail.com` concreta → fuerza esa sesión de Google (útil si en el
  navegador hay varias cuentas abiertas a la vez).
* `@hotmail.com` / `@outlook.com` / `@live.com` / `@msn.com` → abre **Outlook
  web** en vez de Gmail (las cuentas de Google creadas con un Hotmail no
  tienen buzón de Gmail: saldría el formulario "Agrega Gmail a tu cuenta").

(La clave antigua `remitente_gmail` se sigue aceptando.)

**Varios perfiles de Edge** (típico en un PC personal de pruebas): si la
sesión del correo vive en otro perfil de Edge, añade
`"edge_perfil": "Profile 1"` al config y la app abrirá el correo en ese
perfil. Ojo: el nombre visible no coincide con la carpeta interna — el
"Perfil 2" de Edge suele ser la carpeta `Profile 1` (el primero es
`Default`). En el PC de recepción esta clave no debe estar.

## Listo

Al terminar el render, por cada vídeo:

1. **"📤 Subir vídeo"** lo sube automáticamente a **Gofile.io** (gratis, sin
   cuenta, sin configuración) y copia el enlace de descarga (verás el
   progreso al lado del botón).
2. **"✉️ Abrir correo"** busca el email por el Nº de cliente y abre Gmail con
   destinatario, asunto, texto y enlace ya puestos. Solo revisar y enviar.

### Cambiar el método de subida (opcional)

En `config_sheets.json`, clave `"entrega_metodo"`:

* `"gofile"` (defecto) — automático, no necesita nada.
* `"drive"` — sube al Google Drive de la cuenta de servicio (requiere
  habilitar la **Google Drive API** en el proyecto y que el robot tenga cuota
  libre). Los vídeos se borran a los 7 días (`"drive_retencion_dias"`).
* `"manual"` — flujo antiguo: abre WeTransfer y tú arrastras el archivo.

> Nota: WeTransfer **no** se puede automatizar — cerró su API pública y la
> subida anónima de su web (verificado 06/2026). Por eso el automático usa
> Gofile, que tiene API oficial.

Mientras no exista `config_sheets.json`, la subida automática funciona igual;
solo el email del cliente habrá que escribirlo a mano en Gmail.
