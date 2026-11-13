// 国コード(ISO 3166-1 alpha-2) -> 名前(日英)と代表座標(緯度経度。首都または国の中心付近)。
// 地球の向き移動（globe.js）と旗の alt/aria-label（app.js）が共有する単一の表。
// ブラウザ（window）と Worker（src/seo.js が import して SSR・範囲の国名に使う）の両方から読めるよう globalThis に置く。
// 国を足すときはここに1行と、旗SVG public/flags/<小文字コード>.svg（flag-icons の 4x3、MIT）を足す。
// 旗SVGが無い国は旗を出さない。旗を足したら下の MISE_FLAGS にも国コードを足す。
// 旗SVG（public/flags/）がある国。ここに無い国は旗要素を作らない（404を出さない）。
globalThis.MISE_FLAGS = ['AU','BR','CA','CN','DE','FR','GB','IN','IT','JP','KR','RU','US'];
globalThis.MISE_COUNTRIES = {
  JP:{ja:'日本',en:'Japan',lat:36,lon:138}, US:{ja:'アメリカ合衆国',en:'United States',lat:39,lon:-98},
  GB:{ja:'イギリス',en:'United Kingdom',lat:54,lon:-2}, DE:{ja:'ドイツ',en:'Germany',lat:51,lon:10},
  FR:{ja:'フランス',en:'France',lat:46.5,lon:2.5}, KR:{ja:'韓国',en:'South Korea',lat:36.5,lon:127.8},
  CN:{ja:'中国',en:'China',lat:35,lon:103}, AU:{ja:'オーストラリア',en:'Australia',lat:-25,lon:134},
  CA:{ja:'カナダ',en:'Canada',lat:56,lon:-96}, BR:{ja:'ブラジル',en:'Brazil',lat:-10,lon:-52},
  IN:{ja:'インド',en:'India',lat:22,lon:79}, IT:{ja:'イタリア',en:'Italy',lat:42.8,lon:12.5},
  ES:{ja:'スペイン',en:'Spain',lat:40,lon:-3.7}, RU:{ja:'ロシア',en:'Russia',lat:60,lon:90},
  TW:{ja:'台湾',en:'Taiwan',lat:23.7,lon:121}, HK:{ja:'香港',en:'Hong Kong',lat:22.3,lon:114.2},
  SG:{ja:'シンガポール',en:'Singapore',lat:1.35,lon:103.8}, TH:{ja:'タイ',en:'Thailand',lat:15,lon:101},
  VN:{ja:'ベトナム',en:'Vietnam',lat:16,lon:108}, ID:{ja:'インドネシア',en:'Indonesia',lat:-2,lon:118},
  PH:{ja:'フィリピン',en:'Philippines',lat:12.5,lon:122}, MY:{ja:'マレーシア',en:'Malaysia',lat:4.2,lon:102},
  NZ:{ja:'ニュージーランド',en:'New Zealand',lat:-41,lon:173}, MX:{ja:'メキシコ',en:'Mexico',lat:23,lon:-102},
  AR:{ja:'アルゼンチン',en:'Argentina',lat:-35,lon:-65}, CL:{ja:'チリ',en:'Chile',lat:-33,lon:-71},
  NL:{ja:'オランダ',en:'Netherlands',lat:52.2,lon:5.5}, BE:{ja:'ベルギー',en:'Belgium',lat:50.6,lon:4.7},
  CH:{ja:'スイス',en:'Switzerland',lat:46.8,lon:8.2}, AT:{ja:'オーストリア',en:'Austria',lat:47.5,lon:14.5},
  SE:{ja:'スウェーデン',en:'Sweden',lat:62,lon:15}, NO:{ja:'ノルウェー',en:'Norway',lat:62,lon:10},
  FI:{ja:'フィンランド',en:'Finland',lat:64,lon:26}, DK:{ja:'デンマーク',en:'Denmark',lat:56,lon:10},
  IE:{ja:'アイルランド',en:'Ireland',lat:53.2,lon:-8}, PT:{ja:'ポルトガル',en:'Portugal',lat:39.6,lon:-8},
  PL:{ja:'ポーランド',en:'Poland',lat:52,lon:19.5}, CZ:{ja:'チェコ',en:'Czechia',lat:49.8,lon:15.5},
  UA:{ja:'ウクライナ',en:'Ukraine',lat:49,lon:32}, TR:{ja:'トルコ',en:'Turkey',lat:39,lon:35},
  IL:{ja:'イスラエル',en:'Israel',lat:31.5,lon:35}, EG:{ja:'エジプト',en:'Egypt',lat:26.5,lon:30},
  ZA:{ja:'南アフリカ',en:'South Africa',lat:-29,lon:24}, NG:{ja:'ナイジェリア',en:'Nigeria',lat:9.5,lon:8},
  KE:{ja:'ケニア',en:'Kenya',lat:0.5,lon:38}
};
