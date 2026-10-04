/**
 * Añade al Google Sheets de los vídeos la columna "Country / País" con un
 * desplegable de países, justo a la izquierda del teléfono.
 *
 * El cliente elige su país y escribe el número sin prefijo; la app de la
 * tirolina junta los dos al pulsar «WhatsApp» (ver telefonos.py).
 *
 * Cómo usarlo (una vez por cada Sheets mensual; si el del mes siguiente se
 * crea copiando este, ya lleva la columna):
 *   1. En el Sheets: Extensiones > Apps Script.
 *   2. Borra lo que haya, pega este archivo entero y guarda (icono disco).
 *   3. Arriba elige la función "configurarColumnaPais" y pulsa Ejecutar.
 *      La primera vez Google pide permiso: acéptalo con tu cuenta.
 * Se puede ejecutar varias veces: si la columna ya existe, solo refresca el
 * desplegable.
 */

const CABECERA_PAIS = 'Country / País';
const CABECERA_TELEFONO = 'Phone Number (without prefix)';
const AYUDA_PAIS = 'Choose your country, then write your phone number in the next column.';

// Primero los países que más vienen; luego el resto por orden alfabético.
// La app solo lee el "(+NN)": el texto se puede cambiar libremente.
const PAISES = [
  '🇪🇸 Spain / España (+34)',
  '🇬🇧 United Kingdom (+44)',
  '🇩🇪 Germany / Deutschland (+49)',
  '🇫🇷 France (+33)',
  '🇮🇹 Italy / Italia (+39)',
  '🇳🇱 Netherlands / Nederland (+31)',
  '🇧🇪 Belgium / België (+32)',
  '🇮🇪 Ireland (+353)',
  '🇵🇹 Portugal (+351)',
  '🇺🇸 USA / Canada (+1)',
  '🇦🇷 Argentina (+54)',
  '🇦🇺 Australia (+61)',
  '🇦🇹 Austria / Österreich (+43)',
  '🇧🇷 Brazil / Brasil (+55)',
  '🇧🇬 Bulgaria (+359)',
  '🇨🇱 Chile (+56)',
  '🇨🇳 China (+86)',
  '🇨🇴 Colombia (+57)',
  '🇭🇷 Croatia / Hrvatska (+385)',
  '🇨🇺 Cuba (+53)',
  '🇨🇿 Czechia / Česko (+420)',
  '🇩🇰 Denmark / Danmark (+45)',
  '🇪🇨 Ecuador (+593)',
  '🇪🇪 Estonia / Eesti (+372)',
  '🇫🇮 Finland / Suomi (+358)',
  '🇬🇷 Greece (+30)',
  '🇭🇺 Hungary / Magyarország (+36)',
  '🇮🇸 Iceland (+354)',
  '🇮🇳 India (+91)',
  '🇮🇱 Israel (+972)',
  '🇯🇵 Japan (+81)',
  '🇱🇻 Latvia / Latvija (+371)',
  '🇱🇹 Lithuania / Lietuva (+370)',
  '🇱🇺 Luxembourg (+352)',
  '🇲🇽 Mexico / México (+52)',
  '🇲🇦 Morocco / Maroc (+212)',
  '🇳🇴 Norway / Norge (+47)',
  '🇵🇾 Paraguay (+595)',
  '🇵🇪 Peru / Perú (+51)',
  '🇵🇱 Poland / Polska (+48)',
  '🇷🇴 Romania / România (+40)',
  '🇷🇺 Russia (+7)',
  '🇸🇰 Slovakia / Slovensko (+421)',
  '🇸🇮 Slovenia / Slovenija (+386)',
  '🇸🇪 Sweden / Sverige (+46)',
  '🇨🇭 Switzerland / Schweiz (+41)',
  '🇹🇷 Turkey / Türkiye (+90)',
  '🇺🇦 Ukraine (+380)',
  '🇺🇾 Uruguay (+598)',
  '🇻🇪 Venezuela (+58)',
  '🌍 Other country (write +prefix in the number)',
];

function configurarColumnaPais() {
  const resumen = SpreadsheetApp.getActiveSpreadsheet().getSheets()
    .map(function (hoja) { return hoja.getName() + ': ' + configurarHoja_(hoja); });
  SpreadsheetApp.getUi().alert('Columna de país\n\n' + resumen.join('\n'));
}

function configurarHoja_(hoja) {
  const ultimaCol = hoja.getLastColumn();
  if (ultimaCol === 0) return 'vacía, no se toca';

  const cabecera = hoja.getRange(1, 1, 1, ultimaCol).getDisplayValues()[0].map(normalizar_);
  let colTel = cabecera.findIndex(function (t) { return t.includes('PHONE') || t.includes('TELEF'); }) + 1;
  let colPais = cabecera.findIndex(function (t) { return t.includes('COUNTRY') || t.includes('PAIS'); }) + 1;
  if (colTel === 0) return 'sin columna de teléfono, no se toca';

  let estado = 'desplegable actualizado';
  if (colPais === 0) {
    hoja.insertColumnBefore(colTel);
    colPais = colTel;
    colTel += 1;
    hoja.getRange(1, colTel).copyTo(hoja.getRange(1, colPais),
      SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
    hoja.getRange(1, colPais).setValue(CABECERA_PAIS);
    estado = 'columna de país añadida';
  }
  hoja.getRange(1, colTel).setValue(CABECERA_TELEFONO);

  const filas = hoja.getMaxRows() - 1;
  if (filas > 0) {
    const regla = SpreadsheetApp.newDataValidation()
      .requireValueInList(PAISES, true)
      .setAllowInvalid(false)
      .setHelpText(AYUDA_PAIS)
      .build();
    hoja.getRange(2, colPais, filas, 1).setDataValidation(regla);
    // Como texto: si no, Sheets se come el "+" y los ceros iniciales.
    hoja.getRange(2, colTel, filas, 1).setNumberFormat('@');
  }
  return estado;
}

function normalizar_(texto) {
  return String(texto).normalize('NFD').replace(/[̀-ͯ]/g, '').toUpperCase();
}
