// 国コード(ISO 3166-1 alpha-2)の単一の表。ブラウザ（window）と Worker（src/seo.js が import して SSR に使う）の両方から読めるよう globalThis に置く。
// ・MISE_ISO: ISO 3166-1 の全 249 コード。旗 SVG（public/flags/<小文字コード>.svg）も同じ 249 個を最初から持つ（flag-icons の 4x3、MIT。docs/design/ui.md「国旗」）。国が増えても何も足さない。
// ・国名（日英）は表を持たず Intl.DisplayNames から取る（MISE_COUNTRIES[code].ja / .en）。香港・マカオだけ長い正式名なので短い名前（style: short）にする。
// ・MISE_COUNTRIES の lat/lon は地球の向き移動（globe.js）だけが使う代表座標（首都または国の中心付近）。座標が無い国は向き移動しない。座標を足すときは下の MISE_COUNTRY_POS に 1 行。
// ・旗が無いコード（独自・暫定・古いコード）は MiseCountry が小さな国コードのバッジで代替する（空の枠・壊れた画像を出さない）。旗を出す全箇所がここを通る。
globalThis.MISE_ISO = [
  'AD','AE','AF','AG','AI','AL','AM','AO','AQ','AR','AS','AT','AU','AW','AX','AZ','BA','BB','BD','BE','BF','BG','BH',
  'BI','BJ','BL','BM','BN','BO','BQ','BR','BS','BT','BV','BW','BY','BZ','CA','CC','CD','CF','CG','CH','CI','CK','CL',
  'CM','CN','CO','CR','CU','CV','CW','CX','CY','CZ','DE','DJ','DK','DM','DO','DZ','EC','EE','EG','EH','ER','ES','ET',
  'FI','FJ','FK','FM','FO','FR','GA','GB','GD','GE','GF','GG','GH','GI','GL','GM','GN','GP','GQ','GR','GS','GT','GU',
  'GW','GY','HK','HM','HN','HR','HT','HU','ID','IE','IL','IM','IN','IO','IQ','IR','IS','IT','JE','JM','JO','JP','KE',
  'KG','KH','KI','KM','KN','KP','KR','KW','KY','KZ','LA','LB','LC','LI','LK','LR','LS','LT','LU','LV','LY','MA','MC',
  'MD','ME','MF','MG','MH','MK','ML','MM','MN','MO','MP','MQ','MR','MS','MT','MU','MV','MW','MX','MY','MZ','NA','NC',
  'NE','NF','NG','NI','NL','NO','NP','NR','NU','NZ','OM','PA','PE','PF','PG','PH','PK','PL','PM','PN','PR','PS','PT',
  'PW','PY','QA','RE','RO','RS','RU','RW','SA','SB','SC','SD','SE','SG','SH','SI','SJ','SK','SL','SM','SN','SO','SR',
  'SS','ST','SV','SX','SY','SZ','TC','TD','TF','TG','TH','TJ','TK','TL','TM','TN','TO','TR','TT','TV','TW','TZ','UA',
  'UG','UM','US','UY','UZ','VA','VC','VE','VG','VI','VN','VU','WF','WS','YE','YT','ZA','ZM','ZW',
];
const MISE_COUNTRY_POS = { JP:[36,138], US:[39,-98], GB:[54,-2], DE:[51,10], FR:[46.5,2.5], KR:[36.5,127.8], CN:[35,103], AU:[-25,134], CA:[56,-96], BR:[-10,-52], IN:[22,79], IT:[42.8,12.5], ES:[40,-3.7], RU:[60,90], TW:[23.7,121], HK:[22.3,114.2], SG:[1.35,103.8], TH:[15,101], VN:[16,108], ID:[-2,118], PH:[12.5,122], MY:[4.2,102], NZ:[-41,173], MX:[23,-102], AR:[-35,-65], CL:[-33,-71], NL:[52.2,5.5], BE:[50.6,4.7], CH:[46.8,8.2], AT:[47.5,14.5], SE:[62,15], NO:[62,10], FI:[64,26], DK:[56,10], IE:[53.2,-8], PT:[39.6,-8], PL:[52,19.5], CZ:[49.8,15.5], UA:[49,32], TR:[39,35], IL:[31.5,35], EG:[26.5,30], ZA:[-29,24], NG:[9.5,8], KE:[0.5,38] };
// 範囲ナビの国ドロップダウン（#291）の文言と、国が 2 つ以上あるときだけドロップダウンにする閾値。SSR（src/seo.js の scopeNavHtml）と app.js の renderScopeNav が共有する。
// SSR は英語固定（日本語表示でも JS 起動前の一瞬だけ英語が出る既存仕様）。
globalThis.MISE_SCOPE_DD_MIN = 2;
globalThis.MISE_SCOPE_DD_WORDS = {
  ja: { pick: '国を選ぶ', search: '国を検索', none: '該当なし', note: '所在国が未設定のサイトを含む', btn: (l, n) => `${l} ${n}件、国を選ぶ`, row: (l, n) => `${l} ${n}件` },
  en: { pick: 'Choose a country', search: 'Search countries', none: 'No match', note: 'Includes sites with no country set', btn: (l, n) => `${l}, ${n} ${Number(n) === 1 ? 'site' : 'sites'}. Choose a country`, row: (l, n) => `${l}, ${n} ${Number(n) === 1 ? 'site' : 'sites'}` },
};
globalThis.MISE_COUNTRIES = (() => {
  const names = lang => {
    let long = null, short = null;
    try { long = new Intl.DisplayNames([lang], { type: 'region' }); short = new Intl.DisplayNames([lang], { type: 'region', style: 'short' }); } catch (_) {}
    return code => {
      let n = null;
      try { n = long && long.of(code); if (n && /SAR China|特別行政区/.test(n)) n = short.of(code); } catch (_) {}
      return n && n !== code ? n : code;
    };
  };
  const ja = names('ja'), en = names('en'), out = {};
  for (const code of globalThis.MISE_ISO) { const p = MISE_COUNTRY_POS[code]; out[code] = { ja: ja(code), en: en(code), ...(p ? { lat: p[0], lon: p[1] } : {}) }; }
  return out;
})();
// 旗の表示を 1 か所に集約する（トップの範囲ナビ・国ドロップダウン・サイト一覧・縦モードのカード・SSR が使う）。
// 旗 SVG があるコードは <img>、無いコード・読み込みに失敗したコードは国コードを小さな文字にしたバッジ（.scope-code）。
globalThis.MiseCountry = (() => {
  const ISO = new Set(globalThis.MISE_ISO), esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const has = code => ISO.has(code);
  const src = code => (has(code) ? `/flags/${code.toLowerCase()}.svg` : null);
  const name = (code, lang) => globalThis.MISE_COUNTRIES[code]?.[lang] ?? String(code);
  const html = (code, cls = 'scope-flag', w = 16, h = 12) => (src(code) ? `<img class="${cls}" src="${src(code)}" alt="" width="${w}" height="${h}">` : `<span class="scope-code" aria-hidden="true">${esc(code)}</span>`);
  const badge = code => { const el = document.createElement('span'); el.className = 'scope-code'; el.setAttribute('aria-hidden', 'true'); el.textContent = code; return el; };
  // 旗要素。opts: className, width, height, lazy。読み込みに失敗したらバッジに差し替える（onFallback があれば差し替え後に呼ぶ）。
  const el = (code, opts = {}) => {
    if (!src(code)) return badge(code);
    const img = document.createElement('img'); img.className = opts.className ?? 'scope-flag'; img.alt = ''; img.width = opts.width ?? 16; img.height = opts.height ?? 12; img.src = src(code);
    if (opts.lazy) { img.loading = 'lazy'; img.decoding = 'async'; }
    img.addEventListener('error', () => { const b = badge(code); img.replaceWith(b); opts.onFallback?.(b); });
    return img;
  };
  return { has, src, name, html, el, badge };
})();
