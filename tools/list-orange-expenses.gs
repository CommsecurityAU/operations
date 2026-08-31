/**
 * List the ORANGE-flagged cells in the Project Expenses matrix.
 *
 * Cell colour survives no export, so the sheet has to tell us.
 *
 * This version checks the FONT colour and the FILL, and if it finds
 * neither it reports every colour it did see -- because a script that says
 * `0 found` has told you nothing about why.
 *
 * TO USE
 *   Extensions -> Apps Script, paste over what is there, Save.
 *   Run `listOrangeExpenses`. Read the log at the bottom of the editor.
 *   A tab called `PE Orange` appears when it finds something.
 *
 * NOTE ON CONDITIONAL FORMATTING. If the orange comes from a rule rather
 * than from someone picking a colour, neither `getFontColors` nor
 * `getBackgrounds` can see it -- Apps Script reports what was SET, not what
 * is displayed. The log will then show everything as black on white, which
 * is itself the answer: say so and we read the rule instead.
 */

/**
 * The flag is #FF9900 -- Google's standard orange from the text-colour
 * palette, NOT the #F26722 in the legend cell. The legend records the
 * brand colour; whoever flags a cell picks the nearest swatch. They are 97
 * apart in summed channel distance, which is why a tolerance of 90 found
 * nothing at all.
 *
 * Both are listed because either may turn up, and a third orange from the
 * palette would land within the tolerance of one of them.
 */
var TARGETS = [[0xff, 0x99, 0x00],    // #FF9900, what the sheet uses
               [0xf2, 0x67, 0x22]];   // #F26722, the brand colour
var TOLERANCE = 60;                   // summed channel distance

function near(hex) {
  if (!hex || String(hex).length < 7) return false;
  hex = String(hex).toLowerCase();
  var got = [parseInt(hex.substr(1, 2), 16),
             parseInt(hex.substr(3, 2), 16),
             parseInt(hex.substr(5, 2), 16)];
  if (got.some(isNaN)) return false;
  for (var t = 0; t < TARGETS.length; t++) {
    var distance = 0;
    for (var i = 0; i < 3; i++) distance += Math.abs(TARGETS[t][i] - got[i]);
    if (distance < TOLERANCE) return true;
  }
  return false;
}

/** The header row, hunted for rather than hard-coded: the title block
 *  above it grows. */
function findLayout(values) {
  for (var r = 0; r < Math.min(values.length, 40); r++) {
    var row = values[r].map(function (c) { return String(c).trim(); });
    if (row.indexOf('Project') > -1 && row.indexOf('Job Code') > -1) {
      return { row: r, header: row };
    }
  }
  throw new Error('no header row carrying Project and Job Code');
}

function listOrangeExpenses() {
  var book = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = book.getSheetByName('PE');
  if (!sheet) throw new Error('no tab called PE');

  var range = sheet.getDataRange();
  var values = range.getValues();
  var fonts = range.getFontColors();
  var fills = range.getBackgrounds();

  var layout = findLayout(values);
  var header = layout.header;
  var projectCol = header.indexOf('Project');
  var jobCol = header.indexOf('Job Code');
  var typeCol = header.indexOf('Type');
  var monthPattern = /^[A-Z][a-z]{2}-\d\d$/;

  var out = [['Project', 'Job Code', 'Type', 'EOM', 'Amount', 'Matched']];
  var seenFonts = {}, seenFills = {}, cellsWithValues = 0, flaggedZeros = 0;

  for (var r = layout.row + 1; r < values.length; r++) {
    var project = String(values[r][projectCol] || '').trim();
    if (!project) continue;
    for (var c = 0; c < header.length; c++) {
      if (!monthPattern.test(header[c])) continue;
      var amount = values[r][c];
      var font = fonts[r][c], fill = fills[r][c];
      // An EMPTY cell with a flag is a flag someone forgot to clear. A
      // cell holding $0.00 is different: `116 Cremorne St` Nov-26 is
      // flagged at zero, which is a deliberate nil rather than an
      // oversight. It is reported separately rather than silently dropped.
      if (amount === '' || amount === null) continue;
      cellsWithValues++;
      if (!amount && near(font)) flaggedZeros++;
      seenFonts[font] = (seenFonts[font] || 0) + 1;
      seenFills[fill] = (seenFills[fill] || 0) + 1;
      var how = near(font) ? 'font' : (near(fill) ? 'fill' : '');
      if (!how) continue;
      out.push([project,
                String(values[r][jobCol] || '').trim(),
                typeCol > -1 ? String(values[r][typeCol] || '').trim() : '',
                header[c], amount, how]);
    }
  }

  console.log(cellsWithValues + ' month cells carry a value');
  if (flaggedZeros) {
    console.log(flaggedZeros + ' flagged cell(s) hold $0.00 and are '
                + 'INCLUDED -- a deliberate nil, not an oversight.');
  }
  console.log((out.length - 1) + ' matched #F26722 on font or fill');
  if (out.length === 1) {
    // Nothing matched: say what IS there, so the next step is obvious
    // rather than another guess.
    console.log('font colours seen: ' + JSON.stringify(seenFonts));
    console.log('fill colours seen: ' + JSON.stringify(seenFills));
    console.log('If both are only black and white, the orange comes from a '
                + 'CONDITIONAL FORMAT rule and neither call can see it.');
    return;
  }

  var target = book.getSheetByName('PE Orange');
  if (target) book.deleteSheet(target);
  target = book.insertSheet('PE Orange');
  target.getRange(1, 1, out.length, out[0].length).setValues(out);
  target.setFrozenRows(1);
  console.log('written to the "PE Orange" tab');
}

/** Run this on its own if the above finds nothing: it dumps the colours of
 *  the first 25 populated month cells, so we can see what we are actually
 *  looking for. */
function reportColours() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('PE');
  var range = sheet.getDataRange();
  var values = range.getValues();
  var fonts = range.getFontColors();
  var fills = range.getBackgrounds();
  var layout = findLayout(values);
  var projectCol = layout.header.indexOf('Project');
  var monthPattern = /^[A-Z][a-z]{2}-\d\d$/;
  var shown = 0;
  for (var r = layout.row + 1; r < values.length && shown < 25; r++) {
    for (var c = 0; c < layout.header.length; c++) {
      if (!monthPattern.test(layout.header[c])) continue;
      if (!values[r][c]) continue;
      console.log(String(values[r][projectCol]) + ' | ' + layout.header[c]
                  + ' | ' + values[r][c]
                  + ' | font ' + fonts[r][c] + ' | fill ' + fills[r][c]);
      shown++;
    }
  }
}
